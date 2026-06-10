# WebSocket-Primary Fast Bridging: Design

**Date:** 2026-06-10
**Status:** Validated design (supersedes `2026-05-25-websocket-and-commitment-fork-changes.md`)
**Priority:** #1 — must land before bridge deployment (it changes how the bridge indexes chains)

Make end-to-end bridge latency **3-5 seconds** in fast mode by replacing
poll-based event detection with WebSocket subscriptions, making the Solana
commitment level configurable, and removing every multi-second sleep from the
message critical path. Also fixes a latent secret leak in the generated bridge
state (a hard pre-deployment blocker regardless of this work).

The 2026-05-25 spec survives adversarial review on mechanics (all line refs
verified against the fork source) but optimized only one of three polling
stages, omitted the gorchain direction entirely, and routed a secret into a
committed artifact. This revision corrects those and adds the warp-UI UX.

---

## 1. Latency budget and goal

End-to-end, Solana-origin, fast mode (`confirmed` + WebSocket), all fixes in
this spec applied:

| Stage | Time | Reducible? |
|---|---|---|
| Origin tx → `confirmed` | ~1-2s | No — optimistic confirmation needs supermajority votes |
| Validator: WS wake → re-read → Privy sign → MinIO put | ~0.3-0.8s | Partly (Privy HTTPS call dominates) |
| Relayer (woken in parallel): index msg → fetch checkpoint → metadata → submit | ~0.5-1.5s | Partly (100-500ms internal ticks + RPC roundtrips) |
| Destination tx lands + observed | ~0.5s (gorchain) / ~1-2s (solana) | No — inclusion physics |

**Target: ~2.5-4s to gorchain, ~3-5s to solana.** Design rule: **no sleep
longer than 500ms anywhere in the happy path.** Any component that can't meet
that must be event-driven.

Context: a native Solana transfer shows `confirmed` in ~1-2s; a lock-and-mint
bridge pays that twice (origin confirmation + destination inclusion) plus one
attestation hop, so ~3s is the structural floor. Wormhole on Solana waits full
finality (~14s+).

**Rejected alternatives for going faster:**
- `processed` commitment — a single RPC node's unvoted view; unsafe for value.
- Liquidity-fronting / intents (deBridge-style sub-second) — a market maker
  pays out instantly from its own funds and carries the reorg risk. Different
  product, not a faster version of this one.

---

## 2. Critical path analysis — three polling stages, not one

The prior spec wired a wake-up signal into the relayer's message indexer only.
The full pipeline has **three** independent poll loops, and the slowest one
gates delivery:

1. **Relayer message indexing** — `ContractSync` loop over the mailbox
   (`hyperlane-base/src/contract_sync/mod.rs:251-318`); cursor sleeps are
   hardcoded 5s (`cursors/sequence_aware/mod.rs:154`, `forward.rs:479`,
   `backward.rs:511`, `rate_limited.rs:230`). Covered by the old spec.
2. **Validator merkle-tree indexing** — the validator's own `ContractSync`
   over merkle-tree insertions. Same loop, same sleeps. Not covered before.
3. **Validator checkpoint submitter** —
   `agents/validator/src/submit.rs:121-166`: a plain `sleep(self.interval)`
   RPC-poll loop calling `latest_checkpoint()`; `interval` defaults to 5s
   (`settings.rs:140-144`). **Not a `ContractSync`** — a `Notify` wired into
   the sync machinery never reaches it. Not covered before.

**The race hazard.** The relayer cannot deliver until the validator's signed
checkpoint is on MinIO. If the relayer (WS-woken in ~1s) prepares before the
checkpoint exists, the message enters exponential backoff: **+5s, +10s, +30s,
+60s** (`pending_message.rs:922-927` `calculate_msg_backoff`). A pipeline that
consistently loses this race averages *worse* than coarse polling. The
validator must therefore be at least as event-driven as the relayer; an
occasional lost race (+5s) is acceptable, a systematic one is not.

---

## 3. Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | Single fork branch (`gorbagana`), advanced to upstream `4da9c4419a` (agents v2.2.0) as new base | Agents (`rust/main`) and programs (`rust/sealevel`) are separate workspaces in one repo; one branch keeps every image build on one checkout. Advance now, pre-deployment: prod programs deploy *from* the new base, so contract provenance starts clean. Only 2 fork commits to rebase; upstream didn't touch `rust/sealevel/client` in range, so the Ledger commit rebases clean. |
| D2 | Fold the agent image's docker-build-time patches (`kms-endpoint.patch`, `s3-path-style.patch`) into commits on the branch | The WS change (~200+ lines, two crates) can't live as a patch file; while converting, check whether v2.2.0 made either patch obsolete. Dockerfile stops patching entirely. |
| D3 | Wake-up signal exposed via a new `Indexer<T>` trait method `fn wake_signal(&self) -> Option<Arc<Notify>>` (default `None`) | The indexer reaches `ContractSync` as a boxed trait object; a trait method with a default avoids both downcasting and an all-chains `ContractSync::new()` signature change. EVM/Cosmos wiring untouched. |
| D4 | Generated bridge-state artifacts are **secret-free**: placeholder URLs only; agents receive real URLs via `customRpcUrls`/`customWsUrls` env overrides injected from spec `secrets:` | `publish-bridge-state` commits `generated/` to `deploy_branch` (= `main` for staging/prod). Today `agent-config.json` and `registry/metadata.yaml` embed `${SOLANA_RPC_URL}` (the Helius key). Latent, not yet realized — verified no pushed ref carries it. See §6. |
| D5 | One commitment knob per chain, honored consistently by indexing reads, WS subscriptions, and the validator's checkpoint observation; tx submission stays `finalized` | A commitment mismatch between subscription and reads wastes the wake-up (poll sees nothing). The security-relevant hop is the **validator signing over a `confirmed` slot** — see §4.1. |
| D6 | Gorchain gets the same treatment: `GORCHAIN_WS_URL`, `GORCHAIN_COMMITMENT` | Fast bridging is bidirectional. Gorchain polling has no credit cost but identical latency cost; 32-slot finality (~13s) applies even on a single-node cluster. |
| D7 | Warp-UI shows delivery and origin-finality as two independent signals; a runtime-injected mode flag collapses the display in safe mode | In fast mode delivery (~3-5s) precedes origin finality (~13-15s) — the gap *is* the rollback window and the UI must not claim irreversibility inside it. Flag is sourced from the same deployment var that sets agent commitment, so UI and agents cannot disagree. |
| D8 | Implementation lands as commits on `gorbagana`; retag (`sealevel-vX.Y.Z-gorbagana.N`) after the rebase and after the WS work | Tags, not branch tips, are the provenance pins for on-chain programs. Nothing is deployed yet (staging included), so the new base is the first provenance pin for every environment. |

---

## 4. Fork changes (`hyperlane-monorepo`, branch `gorbagana`)

Upstream has no WS support for sealevel to reuse (verified against
`hyperlane-xyz/main`), and only 4 upstream commits have touched
`contract_sync`/`hyperlane-sealevel` since our old base — maintenance burden
is low.

### 4.0 Base advance (first, separate from feature work)

1. Rebase `gorbagana` (2 commits: CI removal, Ledger signing) onto
   `4da9c4419a` (agents v2.2.0).
2. Convert `kms-endpoint.patch` and `s3-path-style.patch` into commits;
   verify each is still needed against v2.2.0 (`s3_storage.rs` and
   `signers.rs` saw only chore/tron churn in range — expect trivial conflicts
   at most). Remove the `patch` steps from
   `container-build/gorbagana-dev-hyperlane-agent/Dockerfile`.
3. Tag the result; rebuild agent + deployer images. No environment is
   deployed yet, so there is nothing to migrate — staging's first deploy
   happens from the new base.

### 4.1 Configurable commitment

Add `indexing_commitment: CommitmentConfig` (default `finalized`) to
`ConnectionConf` (`chains/hyperlane-sealevel/src/trait_builder.rs:15-30`) and
thread it through every hardcoded `finalized` read:

- `account.rs:39` (`search_accounts_by_discriminator` — 3 callers:
  `mailbox_indexer.rs:82, 179`, `interchain_gas.rs:139`)
- `mailbox_indexer.rs:101, 201`; `interchain_gas.rs:159`
- `merkle_tree_hook.rs:68` (and relax the misleading
  `reorg_period.is_none()` assertion messages at lines 22, 36)
- `rpc/client.rs:136, 165, 222, 251`

**Leave `finalized`:** blockhash fetch for tx submission
(`provider.rs:176-180`) — prevents blockhash expiry.

Config: a chain-level `commitment` key (`"finalized"` | `"confirmed"`),
parsed in the settings layer alongside `rpcUrls` — env-overridable as
`HYP_CHAINS_<CHAIN>_COMMITMENT`.

**Security framing (corrects the old §2.6):** the risk is not "indexing sees
rolled-back state" in the abstract — it is the **validator signing a
checkpoint over a `confirmed` (rollback-able) slot**. That signature is what
the destination ISM trusts; if the origin slot rolls back, the minted tokens
on the destination cannot be clawed back. There is no safe hybrid: keeping
the validator at `finalized` while the relayer indexes at `confirmed`
recovers none of the latency, because the validator gates delivery. Choosing
fast mode = accepting that an optimistically-confirmed Solana slot does not
roll back (no documented mainnet precedent, but nonzero). Default stays
`finalized`; fast mode is an explicit opt-in per deployment.

### 4.2 WebSocket wake-up

**New module `chains/hyperlane-sealevel/src/pubsub.rs`** (~100-150 lines)
using `solana_pubsub_client::nonblocking::PubsubClient` (workspace dep
`solana-pubsub-client = "3.0"`, already declared, currently unused):

- Connect to `ws_url`; subscribe with the **same commitment** as
  `indexing_commitment` (D5).
- On notification → `Notify::notify_waiters()`.
- Ping every ~60s (Helius drops idle connections at 10min).
- On disconnect: warn, reconnect with exponential backoff; while down, stop
  signaling (consumers fall back to timer polling — identical to today).

**`ConnectionConf` gains `ws_url: Option<Url>`.** Config side: add
`wsUrls`/`customWsUrls` parsing mirroring `rpcUrls`/`customRpcUrls`
(`hyperlane-base/src/settings/parser/mod.rs:189` —
`parse_base_and_override_urls` is generic), so
`HYP_CHAINS_<CHAIN>_CUSTOMWSURLS` works as a secret-bearing env override with
zero new plumbing.

**Wiring — relayer (message indexing):** `SealevelMailboxIndexer::new()`
spawns the pubsub task (`programSubscribe` on the mailbox program) when
`ws_url` is set and holds the `Notify`. Expose it via the new
`Indexer::wake_signal()` trait method (D3). `ContractSync` consults
`indexer.wake_signal()` and replaces both sleep paths
(`mod.rs:265-273` Sleep action; error-path sleeps at 259/282 can stay):

```rust
match wake {
    Some(signal) => tokio::select! {
        _ = sleep(duration) => {},
        _ = signal.notified() => {},
    },
    None => sleep(duration).await,
}
```

`wake_signal` is fetched once before the loop in `cursor_indexer_task` (a
static method — the signal arrives as a parameter, threaded from `sync()`).

**Wiring — validator (both loops, the part the old spec missed):**

1. *Merkle-tree insertion sync:* `SealevelMerkleTreeHookIndexer` spawns the
   same pubsub machinery (`accountSubscribe` on the merkle-tree/outbox
   account is sufficient and quieter than `programSubscribe`) and exposes it
   via `wake_signal()` — the shared `ContractSync` change covers it for free.
2. *Checkpoint submitter:* `checkpoint_submitter` (`submit.rs:121-166`) is
   not a `ContractSync`. Pass the same `Notify` handle into the validator's
   submitter and replace both `sleep(self.interval)` calls (lines 152, 165)
   with the same `select!` pattern. The validator settings plumbing already
   carries the merkle tree hook; the `Notify` rides along.

Spurious wake-ups (`programSubscribe` fires for any program-account change)
are accepted: the poll finds nothing and re-sleeps; for our low-volume bridge
this is noise, not cost.

**Naming correction:** this is Helius **standard WebSocket**
(`programSubscribe`/`accountSubscribe`, available on all tiers including
Free); "LaserStream" is the gRPC product. Re-verify tier limits and the
20-credits/MB data metering at implementation time.

### 4.3 Corrected cost table (validator row was wrong)

The old spec claimed validator ~50-200 credits/day under WS, but
`checkpoint_submitter` polls `latest_checkpoint()` via RPC every `interval`
regardless of the sync loop. With §4.2's submitter wiring, the validator
*does* become event-driven and the estimate holds; without it, the validator
either burns ~14k credits/day (5s interval) or adds 0-30s latency (30s
interval). This is why the submitter wiring is in scope, not optional.

| Configuration | Relayer credits/day | Validator credits/day | Total/month |
|---|---|---|---|
| 30s polling | 57,600 | 14,400 | ~2.2M |
| WS (this spec, both agents event-driven) | ~100-500 | ~100-300 | ~6k-25k |

---

## 5. Stacks changes (`hyperlane-stacks`)

### 5.1 Env vars and config flow

New vars, following the chain-specific canonical pattern:

| Var | Secret? | Consumers |
|---|---|---|
| `SOLANA_WS_URL` | **Yes** (Helius key in URL) | relayer, solana validator |
| `GORCHAIN_WS_URL` | No (self-hosted) | relayer, gorchain validator |
| `SOLANA_COMMITMENT` / `GORCHAIN_COMMITMENT` | No (`finalized` default, `confirmed` opt-in) | relayer, validators, warp-UI mode flag (§7) |

Delivery to agents is **env-only** (D4): compose sets
`HYP_CHAINS_SOLANA_CUSTOMRPCURLS` / `HYP_CHAINS_SOLANA_CUSTOMWSURLS` (from
`secrets:`) and the gorchain equivalents (from `config:`), plus the
commitment keys. `agent-config.json` and the registry templates carry
**placeholder URLs only** (syntactically valid, e.g.
`http://rpc-placeholder.invalid`) — see §6.

Per the keep-in-sync rules: each new compose env var ripples to
`deployment/spec-*.yml`, `tests/e2e/fixtures/test-spec-*.yml` (where they
exist), and `stack_env_vars` in `ops/inventories/*/group_vars/all.yml`.

### 5.2 Gorchain WS reachability

The gorchain node serves standard Solana pubsub on the RPC port + 1 (8900).
Plumbing required wherever the RPC is reachable today:

- e2e/kind: `external-services` `ip:` entries gain the WS port; in-cluster
  DNS `gorchain-rpc:8900` (and solana-test-validator's `:18900` WS for the
  solana side).
- prod/staging: the public endpoint exists in intent but is **broken today**
  (verified 2026-06-10): `wss://rpc.gorbagana.wtf/` upgrade → 502 while HTTP
  RPC on the same host is healthy. The `gorbagana-rpc` deployment's
  `spec.yml` declares `websocket: true` routes, but **SO's http-proxy has no
  websocket support — the key is silently ignored** (verified against SO
  source: no handling anywhere in `stack_orchestrator/deploy/`). The
  workaround (`patch-caddy-websocket.sh`) patches Caddy's admin API
  in-memory — lost on Caddy restart — and dials a `{deployment-id}-service`
  FQDN that goes stale on redeploy; both failure modes match the observed
  502. **Fix: native `websocket: true` support in SO's ingress generation**
  (`deploy/k8s/cluster_info.py`), making the spec key real and deleting the
  patch script. Fallback if needed: expose 8900 directly on the gorchain
  host. The bridge agents depend on this endpoint whenever they run off the
  RPC host (multi-machine prod), so it gates the prod/staging WS rollout.

### 5.3 The latency knobs that already exist

Set in specs (no fork change): validator `interval` (`HYP_INTERVAL`) — with
the submitter event-driven it is only the fallback cadence; keep 5s. Relayer
whitelist etc. unchanged.

---

## 6. Secret-free generated state (latent leak — pre-deployment blocker)

**Finding (verified 2026-06-10):** `deploy.sh` embeds `${SOLANA_RPC_URL}`
(Helius URL with API key) into two artifacts that land in the deployer's
`/state` and are then committed and pushed by `publish-bridge-state.yml` to
`deploy_branch` — which is **`main`** in all three inventories:

- `agent-config.json` (`deployer-scripts-config/deploy.sh:393`; gorchain at `:369`)
- `registry/metadata.yaml` (rendered from `deployer-registry-config/metadata.yaml.tmpl:8,20`, copied to state at `deploy.sh:447-449`)

Clean: `gas-oracle-config.json` (no URLs), warp-route `deploy.log` (already
`sed`-redacted, `warp-deployer-scripts-config/deploy.sh:113`).

**Not yet realized:** every local and remote ref was checked; the only
committed instance is on a local-only branch with in-cluster test URLs. No
key rotation needed. But the first staging/prod publish would commit the key
to `main`.

**Fix (correct-by-construction, two layers):**

1. **Don't put secrets in.** `deploy.sh` writes placeholder URLs into
   `agent-config.json` and the rendered registry (the deployer itself keeps
   using the real `${SOLANA_RPC_URL}` env var for its own RPC calls — only
   the *artifacts* change). Agents receive real URLs via the env overrides
   (§5.1), which the settings parser honors generically
   (`parser/mod.rs:189`, verified). The warp-deployer renders its own
   registry from its own env — same placeholder treatment for its published
   outputs.
2. **Prove none got out.** `publish-bridge-state.yml` gains a gate task
   before `git add`: scan the staged `generated/` tree for the literal
   values of the secret env vars (`SOLANA_RPC_URL`, `SOLANA_WS_URL`) and any
   `api-key=` pattern; fail the play on a hit. The gate makes the property
   hold even if a future artifact is added carelessly.

This fix is independent of the WS work and must land before the bridge
deploys, so it is filed as its own pebble and can merge first.

---

## 7. Warp-UI dual-commitment UX

Repo: `hyperlane-warp-ui-template` fork. In fast mode, delivery (~3-5s)
precedes origin finality (~13-15s); the gap is the rollback window and the
UI must represent it honestly (D7).

**Two independent signals on the transfer status view:**

- **Delivered** — destination delivery observed (the UI already tracks
  delivery; no new data source).
- **Final** — the origin tx has reached `finalized` commitment (one
  `getSignatureStatuses` / equivalent check against the origin RPC,
  upgrade-only state).

Fast mode display: *Sent → Delivered (a few seconds, labeled "awaiting
origin finality") → Complete.* The UI never claims irreversibility before
origin finality.

**Safe-mode collapse:** a runtime-injected flag (same mechanism as the
runtime route config — pod env read at container start, not a build-time
`NEXT_PUBLIC_` inline) tells the UI which mode the deployment runs.
Derived from the same spec var that sets the agents' commitment
(`SOLANA_COMMITMENT`), so the UI cannot disagree with the agents. In safe
mode the finality badge is suppressed and the flow is the familiar
single-stage *Sent → Complete*.

---

## 8. Test plan

e2e (kind, host chains gorchain `:8899/:8900`, solana-test-validator
`:18899/:18900`):

1. **WS happy path** — deploy with `*_WS_URL` set; run the standard transfer
   test; assert delivery and that agent logs show subscription establishment
   (not timer wake) for the detection.
2. **Fallback** — kill the WS endpoint (or point it at a dead port) mid-run;
   assert a subsequent transfer still completes via timer polling and that
   the agent logged the reconnect/backoff path.
3. **Commitment opt-in** — run with `confirmed`; assert transfer completes
   and (smoke-level) that end-to-end time beats the finalized baseline.
4. **Secret-free state** — extend the existing e2e state assertions: after
   the deployer Job, assert no fixture RPC URL string appears in
   `agent-config.json` / `registry/metadata.yaml`; unit-test the publish
   gate task pattern in ops-lint or a fixture run.
5. **UI mode flag** — fixture-level check that the warp-UI pod env carries
   the mode flag derived from the commitment var (full UX verification is
   manual/staging).

Fork-side: Rust unit tests for `pubsub.rs` reconnect/backoff state machine
and for `wake_signal()` default behavior (`None` ⇒ timer-only, EVM/Cosmos
untouched).

---

## 9. Implementation order

| Phase | What | Depends on |
|---|---|---|
| 0 | Secret-free generated state + publish gate (§6) | — (merges first; deployment blocker) |
| 1 | Fork base advance + patch fold-in + retag (§4.0) | — |
| 2 | WS wake-up, relayer **and** validator (§4.2) | 1 |
| 3 | Configurable commitment (§4.1) | 1 (parallel with 2) |
| 4 | Stacks plumbing + gorchain WS + e2e (§5, §8) | 2, 3 |
| 5 | Warp-UI dual-commitment UX (§7) | 4 (mode flag exists) |
| 6 | First staging deploy (from the new base); latency measurement vs §1 budget | 1-5 |

WS-before-commitment ordering kept from the old spec: WS has no security
trade-off; commitment fast mode is the explicit opt-in switched on once
operational confidence exists. With finalized + WS the floor is ~15s; the
3-5s target requires fast mode.

---

## 10. Tracking

Epic + children filed as pebbles (`pb`); the leak fix is a separate bug
pebble so it can merge ahead of everything else. See `pb dep tree` of the
epic for current state.
