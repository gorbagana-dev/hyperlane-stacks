# Helius RPC Call Inventory & Per-Component Math

Supporting material for [`README.md`](./README.md). Per-method credit
costs, per-component call patterns, and detailed scenario math. All
numbers come from reading the running code; calibrate against
measurement post-launch.

The primary model uses **WebSocket-based indexing** with long-interval
fallback polling. For polling-only reference numbers, see
[`polling-baseline-reference.md`](./polling-baseline-reference.md).

---

## 1. Helius pricing reference

Source: <https://www.helius.dev/pricing>,
<https://www.helius.dev/docs/billing/credits>.

### 1.1 Tiers

| Tier | $/month | Credits/month | RPC RPS | GPA RPS | sendTx RPS | DAS RPS |
|---|---|---|---|---|---|---|
| Free | 0 | 1 M | 10 | 5 | 1 | 2 |
| Developer | 49 | 10 M | 50 | **25** | 5 | 10 |
| Business | 499 | 100 M | 200 | 50 | 50 | 50 |
| Professional | 999 | 200 M | 500 | 75 | 100 | 100 |

### 1.2 WebSocket / LaserStream availability

Source: <https://www.helius.dev/docs/faqs/websockets>,
<https://www.helius.dev/blog/laserstream-websockets>.

| Feature | Free | Developer | Business | Professional |
|---|---|---|---|---|
| Standard WSS (`programSubscribe`, `accountSubscribe`, etc.) | all | all | all | all |
| Enhanced WSS (`transactionSubscribe`, enhanced `accountSubscribe`) | — | yes | yes | yes |
| LaserStream gRPC (mainnet) | — | — | yes | yes |
| WebSocket connection limit | 5 | 150 | 250 | 1,000 |

All WebSocket connections go through LaserStream infrastructure at
`wss://mainnet.helius-rpc.com/?api-key=<KEY>`. Helius disconnects idle
connections after **10 minutes**; implementations must send ping frames
every ~60 seconds. Each connection supports up to **1,000
subscriptions** (we need 1-2).

### 1.3 Credit cost per method

| Method | Credits/call | Notes |
|---|---|---|
| `getProgramAccounts` | **10** | Used by the Hyperlane SVM indexer on message fetch. |
| `getProgramAccountsV2` | **1** | 10× cheaper. Hyperlane does not currently use this. |
| `getAccountInfo`, `getMultipleAccounts` | 1 | Used for tip-checks (idle polling). |
| `getSlot`, `getBlock`, `getBlockTime`, `getLatestBlockhash` | 1 | |
| `getSignaturesForAddress` | 1 | |
| `getTransaction`, `getParsedTransaction` | 1 | |
| `getTokenAccountsByOwner`, `getParsedTokenAccountsByOwner` | 1 | |
| `getBalance`, `getSignatureStatuses` | 1 | |
| `sendTransaction` (default endpoint) | 1 | |
| **Sender endpoint** | **0** | Helius-specific priority pathway. |
| `simulateTransaction` | 1 | |
| WebSocket connection open | **1** | Per connection establishment. |
| WebSocket data (LaserStream) | **20 / MB** | Post April 7 2026 rate (reduced from 30/MB). |
| DAS API (any) | 10 | Not used today. |
| Enhanced Transactions API | 100 | Not used. |

WebSocket data metering began **May 1, 2026**. All streaming traffic
(gRPC and WebSocket) is now metered at 20 credits per 1 MB
uncompressed.

---

## 2. Per-component RPC inventory

All citations are in `hyperlane-monorepo` and `hyperlane-stacks` as of
2026-05-25. The primary model assumes WebSocket subscriptions for the
relayer and validator, with configurable fallback polling interval.

### 2.1 Hyperlane relayer (WebSocket + fallback polling)

The relayer runs **two** indexer loops against the Solana mailbox
program: one for dispatched messages, one for processed (delivered)
messages. Each loop uses a `ForwardSequenceAwareSyncCursor` that checks
the chain tip via `latest_sequence_count_and_tip()`, then either sleeps
(no new messages) or queries via `getProgramAccounts` (new messages
found).

**Detection mechanism (WebSocket primary):**

With WebSocket enabled, the relayer subscribes via `programSubscribe` on
the mailbox program ID. Notifications wake the cursor loop immediately,
bypassing the sleep timer. When the WebSocket is down, the loop falls
back to timer-based polling at the configured interval (120s
recommended).

Source: <https://www.helius.dev/docs/api-reference/rpc/websocket/programsubscribe>.
`programSubscribe` supports `confirmed`, `finalized`, and `processed`
commitment levels. Available on all Helius tiers including Free.

**Code (indexer loop):**

- Cursor loop: `hyperlane-base/src/contract_sync/mod.rs:251-318`
  — `cursor_indexer_task`: `next_action()` → `Sleep` | `Query`
- Cursor decision: `cursors/sequence_aware/forward.rs:472-481`
  — `get_next_range()` (`:113`) calls `latest_sequence_count_and_tip()`
- Dispatch tip-check: `hyperlane-sealevel/src/mailbox_indexer.rs:299-304`
  — `get_slot()` (1 credit) + `Mailbox::count()` → `get_tree()` →
  `getAccountInfo(outbox)` (1 credit)
- Processed tip-check: `mailbox_indexer.rs:336-346`
  — `get_inbox()` → `getAccountInfo(inbox)` (1 credit) + `get_slot()`
  (1 credit)
- Message fetch (GPA): `mailbox_indexer.rs:82-90`
  — `search_accounts_by_discriminator()` → `getProgramAccounts` (10 credits)
  + `getAccountInfo` (1 credit) per nonce
- Tx submission: `hyperlane-sealevel/src/rpc/client.rs:270,288`
  — `sendTransaction`; or via `JitoTransactionSubmitter` at
  `tx_submitter.rs:99-146`

**Cost per fallback poll cycle (no new messages):** 2 × (`getSlot` +
`getAccountInfo`) = **4 credits**.

GPA does **not** run on idle cycles. The cursor calls
`latest_sequence_count_and_tip()` (2 credits per loop), finds
`current_sequence == onchain_count`, returns `Sleep` — no
`getProgramAccounts` call. See
[`polling-baseline-reference.md`](./polling-baseline-reference.md) §1
for the full code-path trace.

**Cost per indexed message:** 1 × `getProgramAccounts` (10) + 1 ×
`getAccountInfo` (1) = **11 credits**.

**Cost per delivered tx to Solana:** `simulateTransaction` (1) +
`getLatestBlockhash` (1) + `sendTransaction` (1, or 0 via Sender) +
state-check `getAccountInfo`s (~2) ≈ **5 credits** (4 with Sender).

**WebSocket overhead:**

Per-notification data sizes (from Sealevel mailbox account structs in
`sealevel/programs/mailbox/src/accounts.rs`):
- `programSubscribe` dispatched message notification: ~350 bytes
  (52-byte header + ~77-byte HyperlaneMessage prefix + body)
- `programSubscribe` processed message notification: ~56 bytes (fixed)
- `accountSubscribe` outbox (merkle tree) notification: ~1,119 bytes
  (includes 32-level merkle tree = 1,032 bytes)

| Item | Credits/day | Notes |
|---|---|---|
| Connections | ~10 | 2 initial + ~5 reconnects/day × 1 credit each |
| Data volume (low, 10 msgs) | <1 | ~15 KB/day → negligible vs 20 credits/MB |
| Data volume (moderate, 100 msgs) | <1 | ~150 KB/day |
| Data volume (heavy, 1k msgs) | ~30 | ~1.5 MB/day |
| Data volume (stress, 10k msgs) | ~300 | ~15 MB/day |
| Pings | 0 | Protocol-level frames, not metered |

Our `getProgramAccounts` queries use a memcmp filter on discriminator +
nonce, which returns 0 or 1 accounts per call (nonces are unique).
Response size is never a concern. Helius does not document explicit
response size limits; `getProgramAccountsV2` adds pagination for large
result sets but is unnecessary for our single-account lookups.

**Daily credits (WebSocket + 120s fallback):**

| Load | Floor (fallback polls) | + Indexing | + Delivery | + WS | Total/day |
|---|---|---|---|---|---|
| Idle | 720 × 4 = 2,880 | 0 | 0 | 10 | **2,890** |
| Low (10 bridge tx) | 2,880 | 110 | 25 | 10 | **3,025** |
| Moderate (100) | 2,880 | 1,100 | 250 | 15 | **4,245** |
| Heavy (1,000) | 2,880 | 11,000 | 2,500 | 40 | **16,420** |
| Stress (10,000) | 2,880 | 110,000 | 25,000 | 310 | **138,190** |

Bridge-tx scenarios assume 50/50 split: half Solana-origin dispatches,
half delivered to Solana. "Indexing" covers both dispatch and processed
loops. "Delivery" covers `sendTransaction` + state reads for
Solana-bound messages only.

### 2.2 Hyperlane validator (WebSocket + fallback polling)

The validator runs a `ContractSync` for merkle tree insertions (same
cursor mechanism as the relayer) plus a checkpoint submitter loop.

**Detection mechanism (WebSocket primary):**

The validator can use either `programSubscribe` (same as relayer, shared
subscription) or `accountSubscribe` on the merkle tree hook PDA
(`self.outbox.0`). `accountSubscribe` is more targeted — it fires only
when the outbox account changes (new merkle root insertion), not on all
program activity. Available on all Helius tiers.

**Code:**

- Merkle tree sync: `merkle_tree_hook.rs:115-141` — delegates to
  `SealevelMailboxIndexer::fetch_logs_in_range()` for the actual GPA
  fetch; uses the same `latest_sequence_count_and_tip()` (`:299-304`)
  for tip-checks
- Checkpoint submitter: `validator/src/submit.rs:103-162` — calls
  `merkle_tree_hook.latest_checkpoint()` → `get_tree()` →
  `getAccountInfo(outbox)` (1 credit) per iteration
- Checkpoint signing and storage: local (MinIO/S3), no Helius cost

**Cost per fallback poll cycle:** sync cursor (2) + checkpoint submitter
(1) = **3 credits**.

**Cost per Solana-origin dispatch:** 11 credits (same GPA path as
relayer dispatch indexer — the validator independently fetches each
dispatched message for its merkle tree sync).

**Daily credits (WebSocket + 120s fallback):**

| Load | Floor (fallback polls) | + Indexing | + WS | Total/day |
|---|---|---|---|---|
| Idle | 720 × 3 = 2,160 | 0 | 5 | **2,165** |
| Low (10 bridge tx → 5 dispatches) | 2,160 | 55 | 5 | **2,220** |
| Moderate (100 → 50 dispatches) | 2,160 | 550 | 5 | **2,715** |
| Heavy (1k → 500 dispatches) | 2,160 | 5,500 | 10 | **7,670** |
| Stress (10k → 5k dispatches) | 2,160 | 55,000 | 50 | **57,210** |

### 2.3 Gas oracle

**Code:** `stack_orchestrator/data/compose/docker-compose-hyperlane-gas-oracle.yml:9`
— `GAS_ORACLE_INTERVAL_MS:-900000` (15 minutes).

**Cost per cycle:** `getAccountInfo` + `getBalance` + `sendTransaction`
≈ 3 credits.

**Daily credits:** 96 cycles × 3 = **~290 credits/day**. Unchanged by
the indexing model.

### 2.4 Warp-UI (with dual-commitment UX)

User-facing React app; cost scales with users. The UX should surface
both fast confirmation (~2-3s at `confirmed` commitment) and secure
finalization (~12-15s at `finalized`) so users see progress immediately
without waiting for finality.

**Code:** `stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-warp-ui/configs/chains.yaml:33`
(`__SOLANA_RPC_URL__`).

**Dual-commitment tracking:** After a bridge transaction is submitted,
the UI tracks its confirmation progression via `signatureSubscribe`
(WebSocket). Marginal credit cost is near zero (data volume per
subscription is bytes). Estimates include ~5 credits/tx as conservative
headroom. The implementation should make dual-commitment tracking
configurable (operators can disable it).

**Cost per page visit:** ~3 standard calls = 3 credits (unchanged).

**Cost per bridge tx attempt:** balance + token accounts + simulate +
blockhash + send + status + confirmation tracking ≈ **11 credits**.

**Scenarios:**

| Scenario | Page visits/day | Bridge tx/day | Credits/day |
|---|---|---|---|
| Low | 50 | 10 | 50×3 + 10×11 = **260** |
| Moderate | 500 | 100 | 1,500 + 1,100 = **2,600** |
| Heavy | 5,000 | 1,000 | 15,000 + 11,000 = **26,000** |
| Stress | 50,000 | 10,000 | 150,000 + 110,000 = **260,000** |

### 2.5 Deployer jobs (one-time)

**Code:**

- `stack_orchestrator/data/compose-jobs/docker-compose-hyperlane-svm-deployer.yml`
- `stack_orchestrator/data/compose-jobs/docker-compose-hyperlane-svm-warp-deployer.yml`

~50-100 RPC calls during initial bootstrap. Amortized: < 100 credits/day.

### 2.6 TrashScan-Explorer `solana-bridge-tracker`

Backend cron, every 120s, tracks Solana-side bridge wallet activity.

**Code:** `TrashScan-Explorer/server/services/solana-bridge-tracker.ts`
(endpoint at line 16, interval at line 50, calls at 80/134/154/190).

**Per cycle, per tracked wallet:** `getParsedTokenAccountsByOwner` (1) +
`getSignaturesForAddress` (1) + N × `getParsedTransaction` (1 each,
where N = new signatures since last cycle). Assume N ≈ 5 average.

**Daily credits:** 720 cycles/day × ~7 credits per wallet ≈ **5,000
credits/day per wallet**. Expected tracked wallets at launch: 1-3.

---

## 3. Scenario math

### 3.1 WebSocket + 120s fallback (canonical)

| Component | Low (10 tx/d) | Moderate (100) | Heavy (1k) | Stress (10k) |
|---|---|---|---|---|
| Relayer | 3,025 | 4,245 | 16,420 | 138,190 |
| Validator | 2,220 | 2,715 | 7,670 | 57,210 |
| Gas oracle | 290 | 290 | 290 | 290 |
| Warp-UI | 260 | 2,600 | 26,000 | 260,000 |
| Explorer (2 wallets) | 10,000 | 10,000 | 10,000 | 10,000 |
| Deployer (amortized) | 100 | 100 | 100 | 100 |
| **Total / day** | **~15,900** | **~20,000** | **~60,500** | **~465,800** |
| **Total / month** (×30) | **~477k** | **~600k** | **~1.8M** | **~14.0M** |
| **Tier** | Developer | Developer | Developer | Business |
| **Cost** | $49 | $49 | $49 | $499 |

### 3.2 Polling only at 30s (for comparison)

See [`polling-baseline-reference.md`](./polling-baseline-reference.md)
for full breakdowns. Summary:

| Component | Low (10 tx/d) | Moderate (100) | Heavy (1k) | Stress (10k) |
|---|---|---|---|---|
| Relayer | 11,655 | 12,870 | 25,020 | 146,520 |
| Validator | 8,695 | 9,190 | 14,140 | 63,640 |
| Gas oracle | 290 | 290 | 290 | 290 |
| Warp-UI | 260 | 2,600 | 26,000 | 260,000 |
| Explorer (2 wallets) | 10,000 | 10,000 | 10,000 | 10,000 |
| Deployer (amortized) | 100 | 100 | 100 | 100 |
| **Total / day** | **~31,000** | **~35,100** | **~75,600** | **~480,600** |
| **Total / month** (×30) | **~930k** | **~1.05M** | **~2.3M** | **~14.4M** |
| **Tier** | Developer | Developer | Developer | Business |
| **Cost** | $49 | $49 | $49 | $499 |

### 3.3 WebSocket vs polling comparison

| Scenario | WebSocket/month | Polling 30s/month | WebSocket savings |
|---|---|---|---|
| Low | ~477k | ~930k | 49% fewer credits |
| Moderate | ~600k | ~1.05M | 43% |
| Heavy | ~1.8M | ~2.3M | 22% |
| Stress | ~14.0M | ~14.4M | 3% |

At stress traffic, warp-UI dominates both models (~56% of total). The
WebSocket advantage is largest at low-to-moderate traffic, where the
polling floor drives total cost.

Both models fit on **Developer ($49/mo)** through Heavy. Only Stress
(10,000 bridge tx/day) requires Business.

---

## 4. Latency comparison

WebSocket eliminates the polling sleep as a detection delay. Combined
with configurable commitment, end-to-end bridge time drops
significantly.

| Configuration | Avg bridge time | Worst case |
|---|---|---|
| Polling 30s + finalized (baseline) | ~28-30s | ~75s |
| WebSocket + finalized | ~15-18s | ~20s |
| WebSocket + confirmed | ~5-7s | ~10s |

`confirmed` commitment (~2-3s on Solana, per Helius docs) carries a
small rollback risk: a confirmed block has 66%+ validator vote weight
but has not yet been finalized. No widely documented mainnet rollback at
this level exists, but it is theoretically possible.

The warp-UI can surface both states: "confirmed on Solana" (fast
feedback, ~2-3s) → "finalized on Solana" (~12-15s) → "relaying" →
"delivered".

---

## 5. Assumptions that would shift these numbers

1. **No agent restart storms.** Each agent restart re-bootstraps its
   cursor and may issue burst `getProgramAccounts` calls. Scenarios
   assume steady-state operation.
2. **WebSocket stays connected.** With proper ping keep-alive (every
   60s), Helius connections persist indefinitely. If the WebSocket drops
   repeatedly, fallback polling at 120s drives cost closer to the
   polling-only model. Helius has a 10-minute inactivity timer; without
   pings, connections drop every 10 minutes (~144 reconnections/day).
3. **N=5 new signatures per explorer cron cycle.** If bridge volume
   spikes, the explorer line scales linearly.
4. **No external scrapers or health-check probes hitting Helius.** Our
   monitoring stack hits the agents directly, not Solana RPC.
5. **50/50 directional split.** Scenarios assume half the bridge
   traffic originates on Solana and half is delivered to Solana. If
   traffic is predominantly one direction, per-message costs shift
   between indexing and delivery but the total is similar.
6. **Dual-commitment UX adds ~5 credits per bridge tx** for
   confirmation tracking (configurable — can be disabled by operators).
   Estimates assume the WebSocket path (`signatureSubscribe`), which
   has ~0 marginal credit cost per tx. The ~5 credits figure is
   conservative headroom; actual WebSocket-based tracking is
   negligible.
7. **Validator independently indexes dispatched messages.** Both the
   relayer and validator fetch the same dispatched messages via
   separate GPA calls. At high volume, a shared RPC cache or
   `getProgramAccountsV2` adoption (1 credit vs 10) would reduce
   this significantly.
