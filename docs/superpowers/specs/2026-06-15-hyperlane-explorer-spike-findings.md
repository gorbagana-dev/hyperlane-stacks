# Hyperlane Explorer — Spike Findings & Implementation Reference

**Purpose:** Capture everything verified during the dev-host spike (2026-06-15) so the
implementation plan can be written precisely. This **supersedes/corrects** parts of
`2026-06-15-hyperlane-explorer-stack-design.md` (noted inline). Read this first.

**Status: the full pipeline is verified end-to-end with REAL staging data.** A real
bridged message (colleague's solana→gorchain USDC transfer) was indexed by a
locally-built scraper into Postgres, exposed via the authored Hasura over `message_view`,
and rendered in the explorer UI (via search). Only cosmetic/UX fork work remains.

---

## 1. What the spike proved (end-to-end, with real data)

Stock `postgres:15` + stock `hasura/graphql-engine:v2.36.0` + the Hyperlane **scraper**
(built from source) → `message_view` → Hasura anonymous role → explorer UI.

- Scraper indexed **real gorchain↔solana messages** from the live staging bridge
  (6× solana→gorchain + 1× gorchain→solana, nonces 10–15, all `is_delivered=t`).
- `message_view` returns the **exact 48-column frontend contract** (validated by curling
  every query shape the frontend issues, as the anonymous role).
- The explorer UI rendered a real message detail page (Status: Delivered, destination
  "Solana Devnet", tx hashes, blocks, nonce, sender/recipient, body, IGP payment) — data
  pulled from our self-hosted index, not Hyperlane's.

---

## 2. CRITICAL corrections to the design spec

These change the implementation materially:

### 2.1 `message_view` and `total_gas_payment` are NOT authored by us — the scraper migrator creates them
The earlier spec called authoring `message_view` "the hardest task." **It is not a task at
all.** Both views are created by raw `CREATE VIEW` SQL inside the scraper's sea-orm migrations:
- `message_view`: `hyperlane-monorepo/rust/main/agents/scraper/migration/src/m20230309_000005_create_table_message.rs` (in `up()`)
- `total_gas_payment`: `.../m20230309_000004_create_table_gas_payment.rs` (in `up()`)

So running the migrator (`init-db`) creates **all base tables + both views**. We only need
Hasura to *track* `message_view` + `domain` and set permissions. (A verbatim transcription
lives at `explorer-spike/sql/02-views.sql` for reference, but the migrator is the source of truth.)

### 2.2 `domain` table already matches the frontend contract — no view needed
The base `domain` table has `native_token` AND `is_deprecated` (the earlier verification
mislabeled them as `token`/missing). Source: `m20230309_000001_create_table_domain.rs`
(cols: `id, time_created, time_updated, name, native_token, chain_id, is_test_net, is_deprecated`).
Hasura tracks the `domain` table directly. **No `domain` view/aliasing.**

### 2.3 Domain rows MUST be seeded for gorchain + solana
The scraper does **not** auto-create domain rows. Without them, every block insert fails:
`insert or update on table "block" violates foreign key constraint "block_domain_fkey"`.
The migrator pre-seeds ~65 **mainnet** domains but NOT gorchain/solana. So the deploy must
seed the two gorbagana domain rows (id, name, chain_id, native_token, is_test_net,
is_deprecated) before/independent of the scraper. (Seed values come from the deployer-generated
registry metadata — see §5.)

### 2.4 Scraper version must match the DEPLOYER (program), NOT the agent
**This overrides the spec's "extend the gorbagana agent build" decision.**
- The deployed mailbox **program** is built from the **svm-deployer** pin:
  `hyperlane-svm-deployer/stack.yml` → `hyperlane-monorepo@16c056a09af862b3ce9e14bd3b5b8034750af9d0`.
- The **agent** (validator/relayer) is pinned to a *different* commit:
  `hyperlane-validator/stack.yml` → `@4da9c44…` (agents-v2.2.0, carries the KMS/S3 patches).
- The scraper parses the program's on-chain accounts, so it must match the **program** = `16c056a`.
  The spike's working scraper was built from `16c056a` (local monorepo HEAD).

**Implication:** do **not** simply add `--bin scraper` to the `4da9c44` agent Dockerfile
(version mismatch risk with the `16c056a` program). Build the scraper (+ the `init-db`
migrator binary) from `16c056a` — the scraper needs **none** of the agent's KMS/S3 patches
(it signs nothing, writes no S3), so it can be a clean, separate container-build at the
deployer's commit. (Open question to confirm in impl: whether `4da9c44`'s scraper would in
fact parse `16c056a` program accounts — the relayer@4da9c44 does relay program@16c056a
messages, suggesting wire-compat — but the *verified* path is scraper@16c056a. Default to
`16c056a` unless proven otherwise.)

NOTE: the agent Dockerfile is currently **unchanged** (no `--bin scraper` added); earlier
edits did not persist. Tree is clean.

### 2.5 `index.from` is a message NONCE, not a slot
For Sealevel sequence-aware indexing, `index.from` is a **message nonce/sequence**.
Source: `hyperlane-monorepo/rust/main/chains/hyperlane-sealevel/src/mailbox_indexer.rs`
— `Indexer<HyperlaneMessage>::fetch_logs_in_range(range)` does `for nonce in range { get_dispatched_message_with_nonce(nonce) }`.
Setting it to a block number (e.g. `1673000`) means "start at nonce 1.6M" → past all real
messages → indexes nothing. The deployer-generated `agent-config.json` uses `index.from: 0`,
which is correct for a **fresh** chain (indexes from nonce 0). The 502 storms seen in the
spike were a *staging artifact*: the existing staging chain has pruned historical
accounts/blocks, so backfilling old nonces 502'd. A fresh deploy with a co-located reliable
RPC does not have this problem.

### 2.6 `@cached` works on Hasura CE — no fork change needed
The frontend's `query @cached(ttl: 5)` is accepted (no-op) by Hasura CE v2.36.0. The spec
listed "strip @cached" as a fork change; **drop that** — not needed.

### 2.7 Aggregates: enable for the anonymous role (free win)
The hosted Hyperlane Hasura does NOT expose `message_view_aggregate` to anonymous; ours can.
Enabling `allow_aggregations: true` on the anonymous select permission makes the frontend's
message-count feature work. Verified.

---

## 3. Backend recipe (verified, concrete)

1. **Postgres 15** (the scraper migration tooling targets `postgres:15`). Persistent volume.
2. **Schema init** = run the scraper's `init-db` migrator (creates base tables + `message_view`
   + `total_gas_payment`). Then **seed gorchain + solana `domain` rows**.
3. **Hasura** (cli-migrations variant for auto-apply): track `message_view` + `domain`;
   grant anonymous **select** (columns `*`, filter `{}`, `allow_aggregations: true`) on both.
   For browser access set `HASURA_GRAPHQL_CORS_DOMAIN` (or use the proxy approach, §6).
4. **Scraper** (built from `16c056a`): runs continuously, indexing into Postgres.

### Scraper runtime config (this version: `HYP_` prefix, `_` separator)
- `CONFIG_FILES=/config/agent-config.json` — the deployer-generated agent-config (has
  mailboxes, IGP, domainIds, protocol=sealevel; rpcUrls are placeholders).
- `HYP_DB=postgresql://…` — Postgres DSN.
- `HYP_CHAINSTOSCRAPE=gorchain,solana` — comma list (config key `chainsToScrape`).
- `HYP_CHAINS_GORCHAIN_CUSTOMRPCURLS` / `HYP_CHAINS_SOLANA_CUSTOMRPCURLS` — real RPC URLs
  (the canonical override mechanism, per hyperlane-stacks CLAUDE.md; solana URL is the
  Helius secret → inject via secret env, never the committed config).
- `HYP_METRICSPORT=<port>` — **must be set to a free port**; the scraper binds `metrics_port`
  for its HTTP server and panics (`AddrInUse`) if it collides. (`base_server.rs:46`.)
- The scraper requires a `./config` directory to exist in its CWD (can be empty); it merges
  `./config/*.json` + `CONFIG_FILES`. In a container this is `/app/config` (already present
  in the agent image layout).

---

## 4. Frontend fork — remaining work (the only real implementation left for display)

The data path is proven; these make it usable. Fork `hyperlane-explorer` →
`gorbagana-dev/hyperlane-explorer`, branch, pin by tag (warp-ui pattern).

### 4.1 GraphQL endpoint (done in spike, choose final form)
Spike change: `src/consts/config.ts:20` → `apiUrl: process.env.NEXT_PUBLIC_API_URL || '<hosted>'`.
For production prefer the **proxy** approach (spec §3): a same-origin `/api/graphql` Next route
→ in-cluster Hasura, so the browser needs no separate Hasura host and there's no CORS.

### 4.2 Inject gorchain (+ solana) chain metadata  ← the "Unknown chain" fix
Without it the UI shows `Unknown (Unknown / 1198486095)` for gorchain. **Solana Devnet
(1399811151) already resolves** from the public registry; **gorchain (1198486095) does not.**
For a self-contained UI, inject **both**.
- Injection point: `src/features/chains/loadChainMetadata.ts` — it takes
  `overrideChainMetadata` and merges via `mergeChainMetadataMap`. Load the generated
  gorbagana chain metadata at runtime (warp-ui `/public` pattern) and pass it as the override.
- Source of the metadata: deployer-generated
  `deployment/staging/bridges/default/generated/registry/metadata.yaml` (contains gorchain +
  solana: name, chainId, domainId, protocol=sealevel, isTestnet, nativeToken). Same file
  shape will exist per-env from the deployer.

### 4.3 Relax the mainnet-only "Latest Messages" feed filter  ← messages don't list otherwise
The feed (no search) filters `origin_domain_id _in [mainnetDomainIds] AND destination_domain_id _in [mainnetDomainIds]`.
- `src/features/messages/queries/build.ts` → `buildDomainIdWhereClause` (uses `mainnetDomainIds`).
- `src/features/messages/queries/useMessageQuery.ts:55` →
  `mainnetDomainIds = Object.values(chains).filter(c => !c.isTestnet).map(c => c.domainId)`.
- Our chains are **testnet** (gorchain isTestnet=true, solana-devnet isTestnet=true) → excluded.
  Fix: include all **scraped** chains (drop the `!isTestnet` filter), so the feed lists
  gorbagana messages. (Search already bypasses this filter — that's how we verified in the UI.)

### 4.4 No `@cached` change (works on CE — see §2.6).

### Container build (mirror warp-ui)
`container-build/gorbagana-dev-hyperlane-explorer/{Dockerfile, build.sh, entrypoint.sh}`:
pnpm 11 / Node 24, Next.js standalone, entrypoint renders the generated chain-metadata file
into `/app/public`. (Dev ran fine on Node 22 too.)

---

## 5. Key source-of-truth references (file:line)

| What | Location |
|---|---|
| `message_view` SQL | `hyperlane-monorepo/.../scraper/migration/src/m20230309_000005_create_table_message.rs` (CREATE VIEW in `up()`) |
| `total_gas_payment` view | `.../m20230309_000004_create_table_gas_payment.rs` |
| `domain` table (native_token, is_deprecated) | `.../m20230309_000001_create_table_domain.rs` |
| scraper migrator binaries (`init-db`) | `.../scraper/migration/` (`bin/init_db.rs`, Cargo `[[bin]] name="init-db"`) |
| `index.from` = nonce | `hyperlane-monorepo/.../chains/hyperlane-sealevel/src/mailbox_indexer.rs` (`fetch_logs_in_range`) |
| scraper env prefix `HYP_`/`_` | `hyperlane-monorepo/.../hyperlane-base/src/settings/loader/mod.rs:98` |
| metrics-port bind/panic | `hyperlane-monorepo/.../hyperlane-base/src/server/base_server.rs:46` |
| deployer (program) pin = 16c056a | `hyperlane-stacks/stack_orchestrator/data/stacks/hyperlane-svm-deployer/stack.yml` |
| agent pin = 4da9c44 (KMS/S3 patches) | `hyperlane-stacks/.../stacks/hyperlane-validator/stack.yml`; Dockerfile `container-build/gorbagana-dev-hyperlane-agent/Dockerfile` |
| frontend apiUrl | `hyperlane-explorer/src/consts/config.ts:20` |
| frontend chain injection | `hyperlane-explorer/src/features/chains/loadChainMetadata.ts` (`overrideChainMetadata`) |
| frontend feed filter | `hyperlane-explorer/src/features/messages/queries/build.ts` (`buildDomainIdWhereClause`) + `useMessageQuery.ts:55` |
| frontend PI path (EVM-only; not for us) | `hyperlane-explorer/src/features/messages/pi-queries/` |
| live message contract (48 cols) | captured from `https://explorer4.hasura.app/v1/graphql` (introspection disabled; selected by column) |

### Deployer-generated artifacts (on `origin/staging-test`, already in local git)
`deployment/staging/bridges/default/generated/`:
`agent-config.json` (mailboxes/IGP/domainIds), `registry/metadata.yaml` (chain metadata),
`program-ids.json`, `warp-routes/warpRoutes.yaml`. Mailbox addrs from this deploy:
gorchain `6JXGfMKbSRWGsMjKnR5H5tJv3Ghid6srXATKnAJNUpdb`, solana `Hkb3B495ELGuxDX59cZJ2ShGk8ukiVofn8YzCmqrBDzc`.
domainIds: gorchain `1198486095`, solana `1399811151`.

---

## 6. Spike scratch (dev host — disposable, for reference/teardown)

Dir `/home/dev/workspace/pranav/explorer-spike/`:
- `sql/{01-schema.sql,02-views.sql,03-seed.sql}` — transcribed schema + verbatim view + seed.
- `agent-config.json` — scraper config (gorchain staging RPC; solana RPC was filled by the user).
- `target/release/scraper` — scraper built from local monorepo `16c056a` via `cargo +1.86`.
- Containers: `explorer-spike-pg` (:15432), `explorer-spike-hasura` (:18080), net `explorer-spike-net`.
- Explorer dev server: `localhost:13000` (`NEXT_PUBLIC_API_URL=http://localhost:18080/v1/graphql`).
- Teardown: `docker rm -f explorer-spike-pg explorer-spike-hasura && docker network rm explorer-spike-net`;
  kill scraper/dev procs; `rm -rf /home/dev/workspace/pranav/explorer-spike`. The
  `hyperlane-explorer` checkout has one uncommitted edit (`config.ts` apiUrl) + installed
  `node_modules`. Building the scraper added to the shared `~/.cargo` cache (~GBs, additive).

Shared-host caution: this is a multi-dev box. Ports 9090/3001 are used by other services
(hence the scraper metrics-port note). Keep anything new local-only + disposable.

---

## 7. Decisions log (net, post-spike)

| Item | Decision |
|---|---|
| Stack shape | One bundled `hyperlane-explorer` stack: frontend + scraper + Postgres + Hasura |
| Environments | local + staging + prod specs (parity) |
| Browser→GraphQL | Proxy via Next `/api/graphql` (no public Hasura, no CORS) |
| `message_view`/`total_gas_payment` | **Created by the scraper migrator** — not authored |
| `domain` | Track the base table — no view |
| Domain rows | **Seed gorchain+solana** (scraper does not upsert; FK-required) |
| Scraper binary | Build from **deployer pin `16c056a`** (matches program), not agent `4da9c44`; no KMS/S3 patches needed |
| `index.from` | Nonce (not slot); `0` for fresh chains |
| Hasura | cli-migrations; anon select on view+domain; **aggregations on** |
| `@cached` | No change (works on CE) |
| Frontend fork | (a) endpoint via proxy; (b) inject gorchain+solana chain metadata; (c) relax mainnet-only feed filter |
| Metrics port | Set to a free port (avoid `AddrInUse`) |
