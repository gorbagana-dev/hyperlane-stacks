# Helius RPC Call Inventory & Per-Component Math

Supporting material for [`README.md`](./README.md). Per-method credit costs,
per-component call patterns, and detailed scenario math. All numbers come
from reading the running code; calibrate against measurement post-launch.

---

## 1. Helius pricing reference

Source: <https://www.helius.dev/pricing>, <https://www.helius.dev/docs/billing/credits>.

### 1.1 Tiers

| Tier | $/month | Credits/month | RPC RPS | GPA RPS | sendTx RPS | DAS RPS | LaserStream gRPC mainnet |
|---|---|---|---|---|---|---|---|
| Free | 0 | 1 M | 10 | 5 | 1 | 2 | devnet only |
| Developer | 49 | 10 M | 50 | **25** | 5 | 10 | devnet only |
| Business | 499 | 100 M | 200 | 50 | 50 | 50 | included |
| Professional | 999 | 200 M | 500 | 75 | 100 | 100 | included |

`getProgramAccounts` and `sendTransaction` have their own RPS ceilings,
separate from (and stricter than) the general RPC RPS. Business also
includes Sender at 50 TPS and `sendBundle` at 5/sec. See
[README §5](./README.md#5-rate-limit-headroom) for how these limits
interact with our workload.

### 1.2 Credit cost per method (the ones relevant to us)

| Method | Credits/call | Notes |
|---|---|---|
| `getProgramAccounts` | **10** | The hotspot. Used by the Hyperlane SVM indexer. |
| `getProgramAccountsV2` | **1** | 10× cheaper. Hyperlane does not currently use this. |
| `getAccountInfo`, `getMultipleAccounts` | 1 | |
| `getSlot`, `getBlock`, `getBlockTime`, `getLatestBlockhash` | 1 | |
| `getSignaturesForAddress` | 1 | |
| `getTransaction`, `getParsedTransaction` | 1 | |
| `getTokenAccountsByOwner`, `getParsedTokenAccountsByOwner` | 1 | |
| `getBalance`, `getSignatureStatuses` | 1 | |
| `sendTransaction` (default endpoint) | 1 | |
| **Sender endpoint** | **0** | Helius-specific. |
| `simulateTransaction` | 1 | |
| `getTransactionsForAddress` | 10+ | 10 credits / 100 returned, 10-credit min. |
| `getTransfersByAddress` | 10 | |
| DAS API (any) | 10 | Not used in any of our components today. |
| Enhanced Transactions API | 100 | Not used. |
| Wallet API | 100 | Not used. |
| LaserStream WebSocket / gRPC | 2 / 0.1 MB | |
| Webhooks | 1 / event | |

Overage rate is not disclosed publicly. Our planned tier (Developer) has
~7-8 M credits of headroom under canonical load; overage is irrelevant
unless we mis-size.

---

## 2. Per-component RPC inventory

All citations are in `hyperlane-monorepo` and `hyperlane-stacks` as of
2026-05-21.

### 2.1 Hyperlane relayer (Solana indexing + delivery)

The relayer runs **two** `getProgramAccounts`-polling indexer loops against
the Solana mailbox program: one for dispatched messages, one for processed
(delivered) messages. Each walks per-nonce, probing via `getProgramAccounts`
with a memcmp filter on `(discriminator, nonce)` at offset 1.

**Code:**

- Indexer loop: `rust/main/chains/hyperlane-sealevel/src/mailbox_indexer.rs:272-287`
  (dispatch) and `:308-331` (processed).
- The `getProgramAccounts` call:
  `rust/main/chains/hyperlane-sealevel/src/account.rs:47`
  → `provider.rpc_client().get_program_accounts_with_config(*program_id, config)`.
- Polling cadence: `rust/main/hyperlane-base/src/contract_sync/mod.rs:36`
  — `SLEEP_DURATION` (5s default).
- Tx submission: `rust/main/chains/hyperlane-sealevel/src/rpc/client.rs:270,288`
  — plain `sendTransaction`; or via `JitoTransactionSubmitter` at
  `rust/main/chains/hyperlane-sealevel/src/tx_submitter.rs:99-146`.

**Confirmation that warp routes are not separately indexed:** the relayer's
origin factory at
`rust/main/agents/relayer/src/relayer/origin.rs:220-323` wires up exactly
`message_sync`, `merkle_tree_hook_sync` (reads a known PDA, not GPA), and
an optional `interchain_gas_payment_sync`. No warp-route or token-router
indexer is registered.

**Cost per poll (no new messages):** 2 × `getProgramAccounts` = 20 credits.

**Cost per indexed message (above the floor):** 1 × `getProgramAccounts`
+ 1 × `getAccountInfo` ≈ 11 credits.

**Cost per delivered tx to Solana:** `simulateTransaction` (1) +
`getLatestBlockhash` (1) + `sendTransaction` (1, or 0 via Sender) + a few
state-check `getAccountInfo`s ≈ 5 credits without Sender, ~4 with Sender.

**Daily credits (canonical 30s polling):**

| Load | Floor (polls) | + Indexing | + Delivery | Total/day |
|---|---|---|---|---|
| Idle | 2,880 × 20 = 57,600 | 0 | 0 | **57,600** |
| 100 in / 50 out | 57,600 | 1,100 | 250 | **58,950** |
| 1000 in / 500 out | 57,600 | 11,000 | 2,500 | **71,100** |
| 10k in / 5k out | 57,600 | 110,000 | 25,000 | **192,600** |

**Daily credits (5s default polling — not target):** floor = 17,280 × 20 =
**345,600**, plus the same per-message deltas.

### 2.2 Hyperlane validator (Solana origin)

**Code:**

- Loop and default interval:
  `rust/main/agents/validator/src/validator.rs:298-548`,
  `rust/main/agents/validator/src/settings.rs:145` (`Duration::from_secs(5)`).
- `Mailbox::count` → `MerkleTreeHook::count` → `get_tree` →
  `get_account_with_finalized_commitment(self.outbox.0)` at
  `rust/main/chains/hyperlane-sealevel/src/merkle_tree_hook.rs:68`. This
  resolves to **`getAccountInfo` (1 credit)**, not `getProgramAccounts`.
- Tx submission: same code paths as the relayer (`sendTransaction` or
  Jito/Sender).

**Cost per iteration:** `getSlot` (1) + `Mailbox::count` via
`getAccountInfo` (1) + optional `getBlock` (1, if advanced_log_meta) +
incidental state reads ≈ **5 credits**.

**Cost per checkpoint submitted:** ~3-4 credits including `sendTransaction`
(0 with Sender). Checkpoints are submitted at most ~once per finalized
merkle root insertion, which tracks message dispatch — far below the
polling iteration count.

**Daily credits:**

| Cadence | Iterations/day | Per iter | Floor/day |
|---|---|---|---|
| **30s (canonical)** | 2,880 | 5 | **14,400** |
| 60s | 1,440 | 5 | 7,200 |
| 5s (default) | 17,280 | 5 | 86,400 |

### 2.3 Gas oracle

**Code:** `stack_orchestrator/data/compose/docker-compose-hyperlane-gas-oracle.yml:9`
— `GAS_ORACLE_INTERVAL_MS:-900000` (15 minutes).

**Cost per cycle:** `getAccountInfo` + `getBalance` + `sendTransaction`
≈ 3 credits.

**Daily credits:** 96 cycles × 3 = **~290 credits/day**. Rounding error
in the model.

### 2.4 Warp-UI

User-facing React app; cost scales with users.

**Code:** `stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-warp-ui/configs/chains.yaml:33`
(`__SOLANA_RPC_URL__`).

**Cost per page visit** (wallet detection + balance display):
~3 standard calls = 3 credits.

**Cost per bridge tx attempt** (balance + token accounts + simulate +
blockhash + send + status check): ~6 standard calls = 6 credits.

**Scenarios:**

| Scenario | Page visits/day | Bridge tx/day | Credits/day |
|---|---|---|---|
| Low | 50 | 10 | 50×3 + 10×6 = **210** |
| Moderate | 500 | 100 | 1,500 + 600 = **2,100** |
| Heavy | 5,000 | 1,000 | 15,000 + 6,000 = **21,000** |
| Stress | 50,000 | 10,000 | 150,000 + 60,000 = **210,000** |

### 2.5 Deployer jobs (one-time)

**Code:**

- `stack_orchestrator/data/compose-jobs/docker-compose-hyperlane-svm-deployer.yml`
- `stack_orchestrator/data/compose-jobs/docker-compose-hyperlane-svm-warp-deployer.yml`

Collectively ~50-100 RPC calls during initial bootstrap, including some
`getProgramAccounts` lookups. Amortized over a month: < 100 credits/day.

### 2.6 TrashScan-Explorer `solana-bridge-tracker`

Backend cron, every 120s, tracks Solana-side bridge wallet activity.

**Code:** `TrashScan-Explorer/server/services/solana-bridge-tracker.ts`
(endpoint at line 16, interval at line 50, calls at 80/134/154/190).

**Per cycle, per tracked wallet:** `getParsedTokenAccountsByOwner` (1) +
`getSignaturesForAddress` (1) + N × `getParsedTransaction` (1 each, where
N = new signatures since last cycle). Assume N ≈ 5 average.

**Daily credits:** 720 cycles/day × ~7 credits per wallet ≈ **5,000
credits/day per wallet**. Expected tracked wallets at launch: 1-3.

---

## 3. Scenario math

### 3.1 Canonical: 30s polling

| Component | Low (10 tx/d) | Moderate (100) | Heavy (1k) | Stress (10k) |
|---|---|---|---|---|
| Relayer | 57,600 | 58,950 | 71,100 | 192,600 |
| Validator | 14,400 | 14,400 | 14,500 | 15,000 |
| Gas oracle | 290 | 290 | 290 | 290 |
| Warp-UI | 210 | 2,100 | 21,000 | 210,000 |
| Explorer (2 wallets) | 10,000 | 10,000 | 10,000 | 10,000 |
| Deployer (amortized) | 100 | 100 | 100 | 100 |
| **Total / day** | **~82,600** | **~85,800** | **~117,000** | **~428,000** |
| **Total / month** (×30) | **~2.5 M** | **~2.6 M** | **~3.5 M** | **~12.8 M** |
| **Tier** | Developer | Developer | Developer | Business |
| **Cost** | $49 | $49 | $49 | $499 |

### 3.2 Tighter alternative: 15s polling

Polling cadence doubles from 30s, so the relayer and validator polling
floors double (every other line item is independent of polling cadence).

| Component | Low (10 tx/d) | Moderate (100) | Heavy (1k) | Stress (10k) |
|---|---|---|---|---|
| Relayer | 115,360 | 116,550 | 128,700 | 250,200 |
| Validator | 28,800 | 29,000 | 29,500 | 30,000 |
| Gas oracle | 290 | 290 | 290 | 290 |
| Warp-UI | 210 | 2,100 | 21,000 | 210,000 |
| Explorer (2 wallets) | 10,000 | 10,000 | 10,000 | 10,000 |
| Deployer (amortized) | 100 | 100 | 100 | 100 |
| **Total / day** | **~154,800** | **~158,000** | **~189,600** | **~500,600** |
| **Total / month** (×30) | **~4.6 M** | **~4.7 M** | **~5.7 M** | **~15.0 M** |
| **Tier** | Developer | Developer | Developer | Business |
| **Cost** | $49 | $49 | $49 | $499 |

Low / Moderate / Heavy all stay on Developer with ~4-5 M headroom under
the 10 M ceiling. Stress moves to Business at both 30s and 15s polling —
the polling cadence isn't the differentiator at extreme load; warp-UI
traffic is.

### 3.3 Without the polling change: 5s default

Shown for contrast — 5s polling is below Solana's ~12s finality, so it
re-reads settled state without latency benefit. Not a deployment target.

| Component | Low (10 tx/d) | Moderate (100) | Heavy (1k) | Stress (10k) |
|---|---|---|---|---|
| Relayer | 345,735 | 346,950 | 359,100 | 480,600 |
| Validator | 86,400 | 86,500 | 86,500 | 87,000 |
| Gas oracle | 290 | 290 | 290 | 290 |
| Warp-UI | 210 | 2,100 | 21,000 | 210,000 |
| Explorer (2 wallets) | 10,000 | 10,000 | 10,000 | 10,000 |
| Deployer (amortized) | 100 | 100 | 100 | 100 |
| **Total / day** | **~442,700** | **~445,900** | **~477,000** | **~788,000** |
| **Total / month** (×30) | **~13.3 M** | **~13.4 M** | **~14.3 M** | **~23.6 M** |
| **Tier** | Business | Business | Business | Business |
| **Cost** | $499 | $499 | $499 | $499 |

At low traffic, 30s polling is **~5.3× cheaper** than 5s; 15s is
**~2.9× cheaper**. The gap closes only at extreme user traffic where
warp-UI dominates; at sub-1000 tx/day the polling floor is the line item
that matters.

---

## 4. Assumptions that would shift these numbers

Listed so a future calibration pass can check each against measurement:

1. **No agent restart storms.** Each agent restart re-bootstraps its
   cursor and may issue burst `getProgramAccounts` calls. Our scenarios
   assume steady-state operation.
2. **No reorg backfills.** Solana finality is rapid; reorg events
   trigger the indexer to re-fetch. Frequency is empirically small.
3. **N=5 new signatures per explorer cron cycle.** If bridge volume
   spikes, the explorer line scales linearly.
4. **No external scrapers, monitoring exporters, or health-check probes
   hitting Helius.** Our monitoring stack hits the agents directly, not
   Solana RPC.
5. **No Helius LaserStream / Webhook / DAS adoption** in the current
   estimate. If we adopt any, model them separately.
6. **Validator submits checkpoints at low frequency** (per merkle root,
   not per poll). If that assumption is wrong, validator delivery cost
   could grow but stays small in absolute terms.
