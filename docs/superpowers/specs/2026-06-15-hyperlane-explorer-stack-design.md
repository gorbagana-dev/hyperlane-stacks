# Hyperlane Explorer Stack — Design Spec

**Goal:** Add a self-hosted Hyperlane **message explorer** to the gorbagana
deployment as a single bundled laconic-so stack (`hyperlane-explorer`), so
operators and users can search, view, and debug interchain messages flowing
across the gorchain ↔ solana bridge — the same way the public Hyperlane
explorer works, but indexing **our** chains.

**Tech stack:** Next.js 16 frontend (forked), the Hyperlane Rust **scraper**
agent, **PostgreSQL 15**, and **Hasura** GraphQL — four pods in one
laconic-so stack, deployed through the existing ansible ops layer.

---

## 1. Background — why this is not just "package a frontend"

The Hyperlane explorer (`hyperlane-explorer`) is a Next.js app that renders
interchain **messages**: search by message id / tx hash / address, delivery
status, latency, gas payments, warp-route transfers. It is a *read-only
frontend over an indexed message database*.

The data pipeline behind it is:

```
Mailbox contracts        Scraper agent          Postgres            Hasura            Explorer frontend
(gorchain, solana)  →    reads Dispatch/   →    stores indexed  →   GraphQL over  →   queries + renders
emit Dispatch/Process    Process events         message rows        those tables
```

The upstream frontend hardcodes its GraphQL endpoint to Hyperlane's **hosted**
backend (`https://explorer4.hasura.app/v1/graphql`), which is backed by
Hyperlane's mainnet scraper. **It has no gorbagana data.** Pointing our
explorer at it would render the UI with zero messages.

Therefore a useful gorbagana explorer requires running the **whole pipeline**
ourselves: the scraper (pointed at our mailboxes), Postgres, Hasura, and the
frontend. This spec covers all four.

### Verification basis (so the reader can trust the contract)

Every non-obvious claim below was verified against source, not assumed:

- **Frontend GraphQL contract** — read directly from the fork's query code
  (`src/features/messages/queries/`, `src/features/chains/queries/`,
  `src/consts/config.ts`).
- **Scraper behaviour, config, migrations, SVM support** — read from
  `hyperlane-monorepo/rust/main/agents/scraper/` (settings, migration crate,
  Sealevel indexers) and `rust/Dockerfile`.
- **The exact `message_view` / `domain` schema** — captured by querying the
  **live hosted Hasura** with the anonymous role (introspection is disabled
  there, but direct selects work), confirming all 48 `message_view` columns
  and 6 `domain` columns with real values and types. This is the
  source-of-truth contract our views must reproduce.
- **Existing agent image** — read from
  `stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-agent/Dockerfile`
  (it is already built+published by our CI for the validator/relayer, but with
  only `--bin validator --bin relayer`, so the scraper binary is absent — we
  extend that same build to add it, §5.2).

## 2. Scope and non-goals

**In scope:**
- One bundled `hyperlane-explorer` stack (frontend + scraper + Postgres + Hasura).
- Authoring the `message_view` and `domain` SQL views (not open-source).
- Extending our existing gorbagana agent build to include the scraper + migration binary.
- A forked frontend that (a) proxies GraphQL through itself and (b) loads
  gorbagana chain metadata at runtime.
- Specs for **all three** environments (local, staging, prod), ops wiring,
  CI image publishing, and e2e + local tests.

**Non-goals:**
- Indexing chains other than the deployment's gorchain + solana.
- Reproducing Hyperlane's hosted feature set that depends on their private
  infra beyond the verified `message_view`/`domain` contract (e.g. block
  explorer API-key enrichment is optional and left unset).
- High-availability Postgres / replication. Single-node Postgres with a
  persistent volume is sufficient for this deployment; the indexed data is
  rebuildable by re-scraping from chain.

## 3. Architecture & topology

**One laconic-so stack, four pods, on the bridge-ops host** (same host as
warp-ui and the relayer), in all three environments.

| Pod | Exposure | Role |
|---|---|---|
| **frontend** (Next.js fork) | **public** ingress `explorer.<domain>` → :3000 | serves the UI **and** a same-origin `/api/graphql` proxy |
| **hasura** | **internal only** (ClusterIP :8080) | read-only GraphQL over the two views |
| **postgres** | **internal only** (:5432) + persistent volume | indexed-message store |
| **scraper** | internal, no ingress (metrics :9090) | indexes gorchain + solana mailboxes → Postgres |

**Data flow:**
- Browser → `explorer.<domain>` (frontend). Browser-side GraphQL →
  `/api/graphql` (same origin) → the frontend's Next.js server proxies →
  `hasura:8080` → Postgres. Server-side fetches (`serverFetch.ts`) hit
  `hasura:8080` directly in-cluster.
- Scraper reads gorchain + solana RPC + mailbox addresses (from
  `agent-config.json`) and writes `message / transaction / block /
  delivered_message / gas_payment` rows to Postgres.
- Frontend resolves `domain_id → chain` from **generated gorbagana chain
  metadata** loaded at runtime (the same pattern warp-ui uses for `chains.yaml`).

**Cross-stack dependency:** the scraper and frontend need deployer output
(mailbox addresses, chain config), so the stack deploys **after the deployer
Job**, wired through the existing `state_distribute` role — exactly like the
relayer/validator. No new state-distribution mechanism is introduced.

### Reasoning — single bundled stack

The pieces could be split into a frontend stack + a backend stack. We chose
**one bundled stack** because the user wants the explorer managed as a single
unit; the four pods share one lifecycle (they are useless individually), one
host, and one deploy/reset cadence. Splitting would add a second stack
definition, a second deploy play, and inter-stack ordering for no operational
benefit at this scale. The trade-off accepted is that the stack is
**stateful** (the Postgres volume) and **ordered** (schema before serving) —
both handled explicitly in §6 and §7.

### Reasoning — proxy GraphQL through the frontend (not a public Hasura)

The upstream frontend's urql client runs **in the browser** and calls the
GraphQL endpoint directly, so the endpoint must be reachable from the user's
browser. Two ways to satisfy that:

1. Expose Hasura on its own public hostname (anonymous read-only), like
   upstream's `explorer4.hasura.app`.
2. **Proxy GraphQL through the Next.js app**: a same-origin `/api/graphql`
   route forwards to in-cluster Hasura.

We chose **(2)** because it yields **one public hostname** (matching how
warp-ui is deployed — single host behind Caddy), keeps **Hasura and its admin
secret entirely off the public internet**, and removes the need for a second
ingress + ACME cert. It costs a small amount of fork work (one API route).
A side benefit: the browser no longer needs the GraphQL URL baked into its
bundle, so **no build-time sentinel is required** for the endpoint (contrast
warp-ui's WalletConnect sentinel) — the browser always calls the relative
`/api/graphql`, and only the *server* needs the in-cluster Hasura URL (a
plain runtime env var).

## 4. The data contract — `message_view` and `domain` views

The base tables are created by the scraper's sea-orm migrations and are left
**unchanged**: `message`, `transaction`, `block`, `delivered_message`,
`gas_payment`, `domain`, `cursor`, `raw_message_dispatch`. On top of them we
add **two read-only views** that reproduce exactly the contract the frontend
queries.

### Reasoning — why we author these views ourselves

`message_view` is the only object the frontend's message queries touch, and
**it does not exist in any open-source Hyperlane repo** (confirmed: not in the
scraper migrations, and there is no public explorer-backend/infra repo in the
`hyperlane-xyz` org; GitHub code search for it requires login). Hyperlane
maintains it on their hosted Postgres/Hasura. So we reconstruct it from the
**live-introspected column contract** — which we captured directly, so this is
a copy of the source of truth, not a guess. The same applies to the small
`domain` shape difference.

### `message_view` (48 columns) — join structure

| Output group | Source |
|---|---|
| `id, msg_id, nonce, sender, recipient, message_body, origin_mailbox` | `message` ( `message_body` ← `msg_body` ) |
| `origin_domain_id`, `destination_domain_id` | `message.origin`, `message.destination` |
| `origin_chain_id`, `destination_chain_id` | `domain.chain_id` joined on each domain id |
| `origin_tx_*` (hash, sender ← `from`, recipient ← `to`, nonce, `*gas*` fields) | `transaction` via `message.origin_tx_id` |
| `origin_block_*` (hash, height, id), `send_occurred_at` | `block` via the origin tx's `block_id` (timestamp → `send_occurred_at`) |
| `is_delivered`, `destination_tx_*`, `destination_block_*`, `destination_mailbox`, `delivery_occurred_at` | `delivered_message` → `transaction` → `block`, joined by `msg_id`; `is_delivered` = a delivery row exists |
| `delivery_latency` | `delivery_occurred_at − send_occurred_at` |
| `total_payment`, `total_gas_amount`, `num_payments` | aggregate over `gas_payment` rows for the message |

The delivery / destination-tx / destination-block / gas-payment sides are
**LEFT-joined**, so undelivered messages still appear (destination columns
`null`, `is_delivered = false`) — matching the live behavior we observed
(a `where: {is_delivered:{_eq:false}}` row returned with null destination
fields).

Column **names and types are fixed by the contract** (verified live):
- bytea columns render as `\x…` hex (`msg_id`, `sender`, `recipient`,
  `message_body`, `origin_mailbox`, `destination_mailbox`, all `*_tx_hash`,
  `*_tx_sender`, `*_tx_recipient`, `*_block_hash`),
- `send_occurred_at` / `delivery_occurred_at` are `timestamp`,
- `delivery_latency` is `interval`,
- ids / heights / nonces are integers; gas + payment fields are numeric/bigint.

`id` must be **monotonic** (the frontend orders by `{id: desc}` and paginates
on it); `message.id` (the serial PK) satisfies this.

**The exact `CREATE VIEW message_view AS …` SQL is the authoritative artifact
and is committed with the implementation** (Hasura migration file). It is
validated against every query the frontend issues (§8 tests).

### `domain` view

The base `domain` table has `token` and no `is_deprecated`; the frontend
contract wants `native_token` and `is_deprecated`. The view aliases
`token → native_token` and adds `is_deprecated` as constant `false`, exposing
exactly `id, name, chain_id, native_token, is_test_net, is_deprecated`.

### Schema init & ownership (deterministic order)

1. **Postgres** starts empty (persistent volume).
2. **Schema-init step** runs the scraper's sea-orm **`init-db`** migration
   (creates the base tables) **and seeds the gorchain + solana `domain` rows**
   (domain id, chain_id, name, native_token) derived from the deployer's
   generated chain config.
3. **Hasura** (after base tables exist) creates/tracks the two views and
   applies the **anonymous read-only** permissions, auto-applied on boot from
   its mounted migrations + metadata directories.
4. **Scraper** and **frontend** start serving.

**Ownership split:** base tables + domain seed are owned by the migration
step; the two views + Hasura tracking + permissions are owned by the Hasura
migrations/metadata. This keeps the scraper's expected schema entirely
sea-orm-managed (so a future scraper upgrade's migrations apply cleanly) and
keeps everything Hasura-specific in Hasura's own migration dir.

### Reasoning — why seed `domain` rows explicitly

The frontend's "which chains have data" dropdown queries the `domain` table,
and `message_view.origin_chain_id`/`destination_chain_id` are resolved by
joining `domain`. The open-source `recreate-db` seeds ~40 *mainnet* domains —
**not** gorchain/solana. Whether the running scraper upserts its own domain
rows is version-dependent and we will not rely on it. Seeding explicitly from
the deployer's chain config makes chain resolution and the filter work
**deterministically**; if the scraper also upserts, the seed is idempotent
(`ON CONFLICT DO NOTHING`).

## 5. Backend pods

### 5.1 Postgres

- Image: **`postgres:15`** (the version the scraper's migration tooling
  targets — `generate_entities` references `postgres:15`).
- Internal-only service on :5432.
- **Persistent volume** for `/var/lib/postgresql/data`.
- Credentials (`POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`) come from
  the deployment secrets; the password is generated by the `credentials` role
  (see §6) so it is not hand-set.
- A healthcheck (`pg_isready`) gates the schema-init step and Hasura.

### 5.2 Scraper (built into our existing agent image)

The scraper is the Hyperlane Rust agent `scraper`. Verified facts:

- **SVM/Sealevel is fully supported** — the scraper builds Sealevel providers
  and implements the message-dispatch, delivery, and gas-payment indexers for
  Sealevel chains (`rust/main/.../sealevel` indexers). gorchain + solana both
  index correctly. This was the single biggest risk and it is cleared.
- **Config** (verified from `ScraperSettings`): `HYP_DB` (Postgres DSN),
  `HYP_CHAINS_TO_SCRAPE` (comma-separated chain names matching agent-config
  keys), `CONFIG_FILES=/config/agent-config.json`, optional
  `HYP_METRICS_PORT`. It reads the **same `agent-config.json`** the relayer
  and validator already consume (mailbox addresses, IGP, RPC URLs, domain ids,
  `index.from`).
- **Migrations do NOT auto-run on scraper startup** — they are a separate
  sea-orm migrator binary in the scraper's `migration` crate. The scraper
  assumes the schema already exists.

**The existing gorbagana agent image does not yet contain the scraper.** It is
built with `cargo build --release --bin validator --bin relayer`
(`gorbagana-dev-hyperlane-agent/Dockerfile`). We **add two binaries to that
same build** — the scraper and the sea-orm migration init binary:

```
cargo build --release --bin validator --bin relayer --bin scraper
# plus the sea-orm migration init binary from the scraper migration crate
```

#### Reasoning — extend our agent build (not a second image)

This image is **already built and published by our CI** for the validator and
relayer (it carries `kms-endpoint.patch` + `s3-path-style.patch` and a pinned
monorepo commit, which upstream doesn't have). So the choice is not "build an
agent image vs not" — we build it regardless. The real choice is:

- **extend our build** with `--bin scraper` (+ the migrator) — one line in a
  job we already run, yielding **one** agent image with the scraper pinned at
  the **same commit** as the validator/relayer it shares `agent-config.json`
  with; or
- **pull the upstream image** for the scraper — which leaves our agent build
  untouched but adds a **second** agent image (and a new supply-chain
  dependency) to the deployment.

Extending our build is the smaller, cleaner change. The scraper needs neither
patch (it signs nothing and writes no S3), but carrying them is harmless, and
including the **migrator** binary lets us run `init-db` at deploy time
(§4) instead of shipping a captured schema dump. Cost: a marginally longer
agent build and a slightly larger image — both negligible and one-time per bump.

The scraper pod selects its binary via the compose `command:` (`./scraper …`),
exactly as the relayer/validator select theirs.

### 5.3 Hasura

- Image: a **pinned `hasura/graphql-engine:vX.Y.Z.cli-migrations-v3`** (a
  current stable v2.x, e.g. `v2.40.0.cli-migrations-v3`, recorded in the
  repo's version-pinning file alongside the other pinned images). The
  CLI-migrations variant **auto-applies** the mounted migrations + metadata on
  boot, so view creation, table tracking, and permissions are declarative and
  version-controlled (no manual `metadata apply`). The exact patch version is
  fixed during implementation per the repo's supply-chain pinning convention.
- Internal-only on :8080 (reached only via the frontend proxy and in-cluster
  server fetches).
- Env: `HASURA_GRAPHQL_DATABASE_URL` (the Postgres DSN),
  `HASURA_GRAPHQL_ADMIN_SECRET` (generated secret),
  `HASURA_GRAPHQL_UNAUTHORIZED_ROLE=anonymous`,
  `HASURA_GRAPHQL_ENABLE_CONSOLE=false`,
  `HASURA_GRAPHQL_ENABLE_TELEMETRY=false`.
- **Anonymous role** = select-only on `message_view` and `domain`, mirroring
  what the live endpoint exposes. **Aggregates are enabled** for the anonymous
  role on `message_view` (see reasoning), so `message_view_aggregate`
  resolves.

#### Reasoning — enable aggregates (a deliberate deviation from hosted)

The live hosted Hasura does **not** expose `message_view_aggregate` to the
anonymous role (the frontend's `useTransactionMessageCount` query fails there
and the UI tolerates it). Because we own our metadata, enabling aggregate on
the anonymous select permission is a one-line win that makes the message-count
feature actually work, with no downside (it is still read-only). We
deliberately diverge from hosted here.

## 6. Frontend fork

Fork `hyperlane-explorer` to `gorbagana-dev/hyperlane-explorer`, on a
dedicated branch, pinned by tag in the stack (mirroring the warp-ui fork). The
fork makes three changes:

1. **GraphQL proxy + endpoint config.** Add a Next.js `/api/graphql` API
   route that forwards POSTs to the in-cluster Hasura (`HASURA_GRAPHQL_URL`
   server env). Change the endpoint selection so the **browser** uses the
   relative `/api/graphql` and **server-side** code (`serverFetch.ts`, the
   urql SSR client) uses `HASURA_GRAPHQL_URL` directly. This replaces the
   hardcoded `config.ts:20` `apiUrl`.
2. **Gorbagana chain metadata at runtime.** Mirror warp-ui: load a generated
   gorbagana chain-metadata file from `/app/public` at startup and seed the
   metadata store, so domain-id ↔ chain resolution and logos work for gorchain
   + solana without depending on the public Hyperlane registry / jsDelivr /
   `proxy.hyperlane.xyz`. (Those upstream defaults remain as a fallback but
   are not relied on.)
3. **Strip `@cached` directives.** The frontend's queries use
   `query @cached`, which is a Hasura **Cloud/EE** feature; self-hosted Hasura
   **CE** rejects unknown directives. Remove `@cached` from the fork's query
   definitions so every query is valid on CE. (Removed in the query source so
   both browser and server paths are clean; query caching is not needed at our
   traffic level.)

#### Reasoning — why a fork rather than env-only config

`apiUrl` is hardcoded, the chain source is the public registry, and `@cached`
is baked into the queries — none are env-overridable upstream. These are code
changes, so a fork is unavoidable. We keep the fork **minimal and mirrored on
the warp-ui fork's conventions** (same org, same tag-pinning, same
`/public`-config runtime pattern) so the team has one mental model for both
UIs.

### Container build

A `container-build/gorbagana-dev-hyperlane-explorer/{Dockerfile, build.sh,
entrypoint.sh}` mirroring warp-ui:
- Builder: pnpm 11 / Node 24, `pnpm build` producing Next.js **standalone**
  output.
- Runtime: minimal image running `node server.js` on :3000.
- `entrypoint.sh`: render the generated gorbagana chain-metadata file into
  `/app/public`, then exec the server. No GraphQL sentinel needed (browser
  uses the relative proxy path).

## 7. Stack assembly, specs, ops, CI

### 7.1 Stack files (mirroring warp-ui's anatomy)

- `stack_orchestrator/data/stacks/hyperlane-explorer/{stack.yml, README.md}` —
  `repos:` pins the explorer fork tag; `pods:` lists the four pods.
- `stack_orchestrator/data/compose/docker-compose-hyperlane-explorer.yml` —
  the four services, internal wiring (frontend→Hasura, Hasura→Postgres,
  scraper→Postgres + agent-config), healthchecks, the Postgres volume, and the
  `command:`/entrypoints that encode the **startup ordering** (wait-for gates):
  Postgres healthy → schema-init (migration + domain seed) → Hasura
  (views/metadata) + scraper + frontend.

### 7.2 Deployment specs — all three environments

`deployment/spec-explorer.yml` (prod) and `deployment/{staging,local}/spec-explorer.yml`:
- `network:` ingress `explorer.<domain>` → `frontend:3000` (one public host).
- `config:` chain RPCs, mailbox addresses (from deployer output), chain ids /
  domain ids, `HYP_CHAINS_TO_SCRAPE`, `HASURA_GRAPHQL_URL` (internal).
- `secrets:` `SOLANA_RPC_URL` (Helius, as elsewhere), the generated Postgres
  password, the generated Hasura admin secret.
- `configmaps:` the `agent-config` (for the scraper, via state distribution)
  and the generated chain-metadata config (for the frontend).
- `volumes:` the Postgres persistent volume.
- `image-pull-secret:` ghcr auth (private fork + agent image).
- `image-overrides:` pin the explorer + agent image tags.
- `resources:` per-pod reservations consistent with the existing resource
  pass — Postgres gets the largest share (it is the DB); frontend/Hasura/
  scraper modest. Prod values larger than staging; staging larger than local.

### 7.3 Ops layer

- `ops/inventories/{local,staging,prod}/group_vars/all.yml`: add
  `hyperlane-explorer` to the `stacks` map (spec filename, stack path,
  configmaps) and to `stack_env_vars` (`SOLANA_RPC_URL`, `GHCR_PAT`).
- `ops/playbooks/deploy-all.yml`: an **Explorer** play after the deployer,
  using `fetch_stack` + `stack_deploy` with the `state_distribute` pre-start
  task to deliver `agent-config.json` to the bridge-ops host (identical
  pattern to the relayer play).
- The `credentials` role generates the Postgres password + Hasura admin secret
  into the env's `deployment-config.yml` on first run (same mechanism as
  minio/grafana creds), so operators fill in nothing extra.

### 7.4 CI / image publishing

In `.github/workflows/publish-images.yml`:
- **`build-explorer`** job: build the frontend image from the fork (mirrors
  `build-warp-ui`), tagged by timestamp+SHA and the fork's release tag.
- **Agent image**: extend the existing agent build to include `--bin scraper`
  (+ migration binary) and republish, so the scraper pod has a binary to run.
- Trigger files (`.github/trigger-publish-explorer.txt`) and `changes`
  outputs, matching the existing convention.
- Postgres and Hasura are **stock public images** — no build step.

## 8. Testing

All deployment runs happen on the user's **separate test machine** (the dev
host has no deployment), per the established workflow: the user runs the
playbooks / laconic-so commands and pastes output.

**Bring-up order the user validates manually first** (per the user's plan):
build the frontend + agent images, then `laconic-so` deploy the stack against
a running bridge, confirm all four pods healthy and the UI loads.

**e2e** (`tests/e2e/`, mirroring `test_11_warp_ui` / `test_13_warp_ui_bridge`):
- A `test-spec-explorer.yml` fixture with placeholder image + mailbox tokens
  patched at runtime.
- Fixtures to build/fetch the explorer image and deploy the stack after the
  deployer.
- Tests: (a) the stack comes up and `/api/graphql` answers; (b) the two views
  resolve **every query shape the frontend issues** (the `message_view`
  contract test — the guard that our authored SQL matches the frontend); (c)
  end-to-end — send a bridge message, then assert it appears in `message_view`
  with correct origin/destination chain ids and, once relayed, `is_delivered`.

**local** — extend the `local-single-host` runbook with the explorer stack
bring-up and a "find your test transfer in the explorer" step.

## 9. Security considerations

- **Hasura is never publicly exposed.** Only the frontend's `/api/graphql`
  proxy (anonymous, read-only, the two views) is reachable from outside, and
  Postgres/scraper are cluster-internal. The admin secret and the GraphQL
  engine's mutation/metadata surface stay off the public internet.
- **No write path from the browser.** The anonymous role is select-only; the
  proxy forwards queries to that role.
- **Generated secrets** (Postgres password, Hasura admin secret) follow the
  existing `credentials` role pattern — not hand-set, not committed.

## 10. Error handling & edge cases

- **Hasura starting before base tables exist** — view-creation migrations
  would fail; the startup ordering (Postgres healthy → schema-init →) gates
  Hasura behind the migration step.
- **Undelivered messages** — surfaced via LEFT joins (null destination,
  `is_delivered=false`); verified against live data.
- **Scraper re-scan cost** — `index.from` comes from agent-config so the
  scraper starts at the mailbox deploy slot, not genesis.
- **Postgres data is rebuildable** — wiping the volume re-scrapes from
  `index.from`; the reset runbook treats explorer data as disposable
  (no chain state lives here).
- **`@cached` on CE** — stripped in the fork (§6).
- **Aggregate availability** — enabled in our metadata (§5.3), unlike hosted.

## 11. Decisions log (summary)

| Decision | Choice | Why |
|---|---|---|
| Stack granularity | **One bundled stack** (4 pods) | Single lifecycle/host; the pods are useless apart; avoids a second stack+play for no benefit |
| Environments | **local + staging + prod** | Parity with every other stack; validate locally first |
| Browser → GraphQL | **Proxy via `/api/graphql`** | One public host, Hasura+admin-secret stay private, no GraphQL sentinel needed |
| `message_view`/`domain` | **Author views from the live contract** | Not open-source; reconstructed from source-of-truth introspection |
| Domain rows | **Seed gorchain+solana explicitly** | Deterministic chain resolution; doesn't depend on scraper upsert behavior |
| Agent image | **Extend our existing agent build** (`--bin scraper` + migrator) | The image is already built/published by our CI; one extra `--bin` keeps one agent image at one commit vs adding a second upstream image |
| Migrations vs views | **sea-orm owns base tables+seed; Hasura owns views+perms** | Keeps scraper schema cleanly upgradable; Hasura concerns stay in Hasura |
| Hasura image | **`cli-migrations-v3` variant** | Declarative auto-apply of views/metadata on boot |
| Aggregates | **Enabled for anonymous** (diverges from hosted) | Free win; makes the count feature work; still read-only |
| Frontend | **Minimal fork mirroring warp-ui** | Hardcoded apiUrl/registry/`@cached` aren't env-overridable; one mental model with warp-ui |
