# Helius API Cost Forecast

Projection of monthly Helius spend for the Gorbagana ↔ Solana bridge and
related Solana-mainnet consumers in the gorbagana ecosystem.

This is the executive summary. Detailed RPC inventory and per-component
credit math is in [`rpc-inventory.md`](./rpc-inventory.md). Polling-only
reference numbers are in
[`polling-baseline-reference.md`](./polling-baseline-reference.md).
Empirical calibration setup and provider-comparison data is in
[`calibration-and-comparison.md`](./calibration-and-comparison.md).

**Date:** 2026-05-25 (revised from 2026-05-21 original)
**Status:** Pre-launch estimate. Calibrate against the Helius dashboard
once the bridge is live.

---

## 1. TL;DR

The bridge fits comfortably on Helius's **Developer tier ($49/month)** at
launch under all realistic load scenarios, with either WebSocket-based
indexing (canonical) or polling-only (fallback).

### WebSocket indexing (canonical)

WebSocket `programSubscribe` replaces sleep-based polling as the primary
detection mechanism in the relayer and validator. Fallback polling at
120s provides resilience when the WebSocket is down. Detection latency
drops from seconds-to-minutes (polling) to sub-second (WebSocket
notification). Standard Solana WebSocket methods are available on all
Helius tiers including Free.

| Scenario | Bridge tx/day | Credits/month | Tier | Cost |
|---|---|---|---|---|
| **Low (canonical)** | ~10 | **~477k** | Developer | **$49** |
| Moderate | 100 | ~600k | Developer | $49 |
| Heavy | 1,000 | ~1.8M | Developer | $49 |
| Stress | 10,000 | ~14.0M | Business | $499 |

### Polling only at 30s (for comparison)

If WebSocket adoption is deferred, polling at 30s is the fallback.
Higher idle cost due to continuous tip-checking, but still Developer
tier through Heavy traffic.

| Scenario | Bridge tx/day | Credits/month | Tier | Cost |
|---|---|---|---|---|
| **Low** | ~10 | **~930k** | Developer | **$49** |
| Moderate | 100 | ~1.05M | Developer | $49 |
| Heavy | 1,000 | ~2.3M | Developer | $49 |
| Stress | 10,000 | ~14.4M | Business | $499 |

WebSocket saves ~49% of credits at low traffic (eliminating the polling
floor) and ~22% at heavy traffic. At stress, warp-UI traffic dominates
both models.

### Bridge latency

| Configuration | Avg bridge time | Worst case |
|---|---|---|
| Polling 30s + finalized | ~28-30s | ~75s |
| WebSocket + finalized | ~15-18s | ~20s |
| WebSocket + confirmed | ~5-7s | ~10s |

`confirmed` commitment (~2-3s on Solana) enables fast user feedback
in the bridge UI. `finalized` (~12-15s) provides maximum safety. The
warp-UI should surface both: "confirmed" for immediate user feedback,
"finalized" for security assurance.

---

## 2. Scope

In scope (Solana-mainnet Helius consumers):

| Component | Role | Repo |
|---|---|---|
| Hyperlane relayer | Indexes Solana mailbox, submits deliveries | hyperlane-stacks |
| Hyperlane validator (Solana origin) | Signs Solana mailbox checkpoints | hyperlane-stacks |
| Hyperlane gas-oracle | Posts gas pricing to Solana IGP | hyperlane-stacks |
| Warp-UI | Browser bridge front-end | hyperlane-stacks |
| Deployer jobs | One-shot Hyperlane + warp route setup | hyperlane-stacks |
| TrashScan-Explorer `solana-bridge-tracker` | Cron tracking Solana bridge wallets | TrashScan-Explorer |

Out of scope:

- `dumpster-backend` / `dumpster-frontend` — gorbagana-only, no Helius
- Internal gorbagana RPC infrastructure (not Helius)

---

## 3. Hotspot ranking

Sorted by credits/day under the canonical WebSocket scenario at low
traffic:

| # | Component | Credits/day | Share | Driver |
|---|---|---|---|---|
| 1 | Explorer bridge-tracker | 10,000 | 63% | 120s cron, ~2 tracked wallets |
| 2 | Relayer (WS + 120s FB) | 3,025 | 19% | Fallback tip-checks + per-message GPA |
| 3 | Validator (WS + 120s FB) | 2,220 | 14% | Fallback tip-checks + per-dispatch GPA |
| 4 | Gas oracle | 290 | 2% | 15-minute cadence |
| 5 | Warp-UI | 260 | 2% | Scales with user traffic |
| 6 | Deployer | <100 | <1% | One-time |

With WebSocket indexing, the **explorer cron** is the largest single
consumer at low traffic — not the relayer. The agent polling floor
drops from ~87% of load (polling model) to ~33%.

At heavy-to-stress traffic, the warp-UI takes over as the dominant
consumer (56-78% of total), independent of the indexing model.

---

## 4. Implementation gaps

The canonical estimates assume three capabilities that **do not exist in
upstream Hyperlane or in our fork today**. Until these are implemented,
the bridge falls back to the polling-only model (§1 comparison table).

See
[`docs/superpowers/specs/2026-05-25-websocket-and-commitment-fork-changes.md`](../superpowers/specs/2026-05-25-websocket-and-commitment-fork-changes.md)
for the tentative implementation spec.

1. **WebSocket wake-up signal in the Sealevel indexer.** The
   `ContractSync` cursor loop (`hyperlane-base/src/contract_sync/mod.rs`)
   is purely timer-based: sleep → poll → store → repeat. There is no
   push-notification path. The fork must add a `tokio::select!` branch
   that wakes the loop on a `programSubscribe` / `accountSubscribe`
   notification via `solana_pubsub_client::nonblocking::PubsubClient`
   (dependency already in `hyperlane-sealevel/Cargo.toml:30`, unused).
   The `ConnectionConf` struct (`trait_builder.rs:15-30`) has no
   WebSocket URL field.

   Standard Solana WebSocket methods are available on **all Helius tiers
   including Free**. Credit cost: 20 credits/MB of streamed data +
   1 credit per connection open — negligible at bridge volumes. Each
   connection supports 1,000 subscriptions (we need 1-2); Developer
   tier allows 150 concurrent connections.

2. **Configurable commitment level.** Every RPC read in the Sealevel
   crate is hardcoded to `CommitmentConfig::finalized()` (8 call sites
   across `account.rs`, `mailbox_indexer.rs`, `merkle_tree_hook.rs`,
   `rpc/client.rs`). The fork must thread a configurable commitment
   through `ConnectionConf` and all call sites, defaulting to
   `finalized`. The underlying RPC client already supports arbitrary
   commitment via `_with_commitment()` methods — only the call sites
   are hardcoded.

   Switching to `confirmed` (~2-3s vs ~12-15s) saves ~10-12s per
   Solana-origin transfer. No credit impact. Small rollback risk (66%+
   vote weight, not yet finalized).

3. **Dual-commitment UX in the warp-UI.** The bridge UI should surface
   both `confirmed` (fast feedback, ~2-3s) and `finalized` (secure,
   ~12-15s) status for Solana-side transactions. This uses
   `signatureSubscribe` (WebSocket) to track confirmation progression.
   The implementation should make this **configurable** — operators
   should be able to disable dual-commitment tracking. Estimates
   include ~5 credits/tx as conservative headroom; actual WebSocket
   cost is negligible.

---

## 5. Further optimization levers

Additional cost-reduction levers beyond the canonical model, in
priority order. Helius claims sourced from
<https://www.helius.dev/docs/billing/credits> and
<https://www.helius.dev/pricing>.

1. **`getProgramAccountsV2` adoption** in the Sealevel indexer (1 credit
   vs 10). Requires modifying the fork to call `getProgramAccountsV2`
   and confirming Helius supports the memcmp filter shapes we use.
   Potential ~10× drop on per-message indexing cost for both relayer and
   validator. Would push even the stress scenario comfortably into
   Developer.

2. **Helius Sender for tx submission** (0 credits vs 1). Configuration
   change at the agent level. Small absolute savings but free to adopt.
   Sender is also faster than plain `sendTransaction`.

3. **Explorer cron optimization.** The explorer is now the largest idle
   consumer. Reducing its cadence from 120s to 300s, or switching it to
   WebSocket-based tracking (`signatureSubscribe`), would cut its
   credits by 60-90%.

4. **Helius account split per component.** Separate API keys for
   relayer / validator / gas-oracle / warp-UI / explorer. Enables
   per-component dashboards in Helius. Worth setting up before launch
   for the [calibration procedure](./calibration-and-comparison.md).

---

## 6. Rate-limit headroom

Source: <https://www.helius.dev/pricing>.

### 6.1 RPC rate limits

| Limit | Developer | Business |
|---|---|---|
| General RPC RPS | 50 | 200 |
| `getProgramAccounts` RPS | **25** | **50** |
| `sendTransaction` RPS | 5 | 50 |
| Sender TPS | — | 50 |

**Our peak RPS by source:**

- **Relayer GPA (WebSocket model):** GPA only fires on actual messages,
  not on a timer. At 1,000 tx/day: ~1,000 GPAs spread over 86,400
  seconds ≈ 0.012 RPS. Burst after catchup: similar to polling model —
  walks queued nonces at ~1 GPA/message, limited by Helius 25-RPS
  ceiling.
- **Relayer GPA (30s polling fallback):** 2 tip-check calls per 120s =
  0.017 RPS. Well under all limits.
- **Warp-UI concurrent users:** ~6 calls/click over ~2s ≈ 3 RPS per
  simultaneous user. Developer's 50-RPS limit gives ~16 simultaneous
  bridge clicks.
- **`sendTransaction`:** under 0.05 RPS average at Heavy (1k tx/day).

### 6.2 WebSocket connection limits

| Tier | Max connections | Our usage |
|---|---|---|
| Developer | 150 | 2-4 (relayer + validator) |
| Business | 250 | 2-4 |

We need 2-4 persistent WebSocket connections (relayer: 1-2 for
`programSubscribe`; validator: 1 for `accountSubscribe`; optionally
warp-UI: 1 for `signatureSubscribe`). Developer's 150-connection limit
is ~37× our needs.

**Verdict:** **Developer satisfies all RPS and connection limits** in
canonical through heavy scenarios.

---

## 7. Tier recommendation

**Provision Helius Developer ($49/month).** Adopt WebSocket-based
indexing in the `hyperlane-monorepo` fork with 120s fallback polling.
Both the WebSocket and polling-only models fit Developer comfortably
through Heavy traffic.

**Triggers to upgrade to Business ($499):**

- Sustained measured usage > 8 M credits/month (80% of Developer
  ceiling) over two consecutive weeks.
- Sustained bridge volume > 5,000 tx/day, or
- Sustained concurrent warp-UI users > ~15 (RPS pressure), or
- Adoption of LaserStream gRPC (mainnet requires Business), or
- Adding a second high-volume Helius consumer.

**Do not provision Professional ($999)** unless Business stops covering
sustained load.

---

## 8. Key assumptions

Resolved via code analysis (citations in
[`rpc-inventory.md`](./rpc-inventory.md)):

- **Idle polling does NOT run `getProgramAccounts`.** The cursor calls
  `latest_sequence_count_and_tip()` (getSlot + getAccountInfo = 2
  credits per loop), finds no new nonces, and returns `Sleep`. GPA only
  fires on `Query(range)` when new messages are found.
  Code path: `ForwardSequenceAwareSyncCursor::get_next_range()`
  (`cursors/sequence_aware/forward.rs:113-172`).
- The relayer runs exactly **two** indexer loops per Solana origin
  (dispatched + processed), not four — warp routes are not indexed.
  Confirmed at `relayer/src/relayer/origin.rs:220-323`.
- The validator **independently indexes dispatched messages** via the
  same GPA path as the relayer's dispatch loop. Its merkle tree hook
  sync delegates to `SealevelMailboxIndexer::fetch_logs_in_range()` at
  `merkle_tree_hook.rs:120`.
- Standard Solana WebSocket methods (`programSubscribe`,
  `accountSubscribe`) are available on all Helius tiers including Free.
  Source: <https://www.helius.dev/docs/faqs/websockets>.
- `programSubscribe` supports `confirmed`, `finalized`, and `processed`
  commitment levels. Source:
  <https://www.helius.dev/docs/api-reference/rpc/websocket/programsubscribe>.
- Helius WebSocket connections have a 10-minute inactivity timer;
  implementations must send ping frames every ~60 seconds. Source:
  <https://www.helius.dev/docs/faqs/websockets>.

Items needing product/business input:

- Expected bridge-tx volume curve at launch and steady state.
- Number of bridge wallets the explorer cron tracks (estimate: 1-3).

---

## 9. Next steps

- **Before launch:** implement the three gaps in §4 (WebSocket
  wake-up signal, configurable commitment, dual-commitment UX) in the
  `hyperlane-monorepo` fork — see
  [spec](../superpowers/specs/2026-05-25-websocket-and-commitment-fork-changes.md);
  provision a Helius Developer account with per-component API keys;
  configure 120s fallback polling interval.
- **During first 72h of mainnet operation:** run the
  [empirical calibration](./calibration-and-comparison.md) procedure to
  reconcile measured vs predicted credits/day.
- **One month in:** revisit the model and the tier choice.
- **Deferred:** evaluate `getProgramAccountsV2` (10× GPA cost
  reduction) and Sender adoption (0-credit tx submission).
