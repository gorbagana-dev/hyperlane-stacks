# Polling-Only Baseline Reference

Historical reference for the polling-only cost model. Superseded by the
WebSocket-based estimates in [`rpc-inventory.md`](./rpc-inventory.md) and
[`README.md`](./README.md). Retained for comparison and calibration.

**Date:** 2026-05-21 (original), corrected 2026-05-25.
**Status:** Reference only — not the deployment target.

---

## 1. Per-cycle costs (corrected)

The original version of this model (committed 2026-05-21) assumed every
idle poll cycle runs `getProgramAccounts` (10 credits × 2 loops = 20
credits/cycle for the relayer). Code analysis shows this is incorrect:
GPA only fires when the cursor finds new messages to fetch.

On an idle poll cycle, the cursor calls
`latest_sequence_count_and_tip()` — a `getSlot` (1 credit) plus a
`getAccountInfo` on a known PDA (1 credit) — finds no new nonces, and
returns `Sleep`. No GPA.

**Code path (relayer):**
`ForwardSequenceAwareSyncCursor::next_action()`
(`hyperlane-base/src/contract_sync/cursors/sequence_aware/forward.rs:472`)
→ `get_next_range()` (`:113`) → `latest_sequence_count_and_tip()`:
- Dispatch loop: `SealevelMailboxIndexer` (`:299`) → `get_slot()` (1) +
  `Mailbox::count()` → `get_tree()` → `getAccountInfo(outbox)` (1) = **2 credits**
- Processed loop: `SealevelMailboxIndexer` (`:336`) → `get_inbox()` →
  `getAccountInfo(inbox)` (1) + `get_slot()` (1) = **2 credits**

If `current_sequence == onchain_count` → `None` → `Sleep(5s)` → no GPA.
If `current_sequence < onchain_count` → `Query(range)` → GPA per nonce.

**Corrected per-cycle costs:**

| Component | Per idle cycle | Method breakdown |
|---|---|---|
| Relayer (2 loops) | **4 credits** | 2 × (getSlot + getAccountInfo) |
| Validator (sync + checkpoint) | **3 credits** | sync: getSlot (1) + getAccountInfo (1); checkpoint: getAccountInfo (1) |

**Per-message costs (unchanged — these only fire on actual messages):**

| Action | Credits | Methods |
|---|---|---|
| Index one message (relayer or validator) | **11** | getProgramAccounts (10) + getAccountInfo (1) |
| Deliver one tx to Solana | **~5** | simulateTransaction (1) + getLatestBlockhash (1) + sendTransaction (1) + state checks (~2) |
| Deliver one tx (with Sender endpoint) | **~4** | Same minus sendTransaction (0 via Sender) |

---

## 2. Polling-only scenario tables

### 2.1 Relayer — 30s polling

| Load | Floor (polls) | + Indexing | + Delivery | Total/day |
|---|---|---|---|---|
| Idle | 2,880 × 4 = 11,520 | 0 | 0 | **11,520** |
| Low (10 bridge tx) | 11,520 | 110 | 25 | **11,655** |
| Moderate (100) | 11,520 | 1,100 | 250 | **12,870** |
| Heavy (1,000) | 11,520 | 11,000 | 2,500 | **25,020** |
| Stress (10,000) | 11,520 | 110,000 | 25,000 | **146,520** |

Bridge-tx scenarios assume a 50/50 split between Solana-origin dispatches
and deliveries to Solana. "Indexing" covers both dispatch and processed
loops. "Delivery" covers `sendTransaction` + state reads for
delivered-to-Solana messages only.

### 2.2 Relayer — 15s polling

| Load | Floor | + Indexing | + Delivery | Total/day |
|---|---|---|---|---|
| Idle | 5,760 × 4 = 23,040 | 0 | 0 | **23,040** |
| Low | 23,040 | 110 | 25 | **23,175** |
| Moderate | 23,040 | 1,100 | 250 | **24,390** |
| Heavy | 23,040 | 11,000 | 2,500 | **36,540** |
| Stress | 23,040 | 110,000 | 25,000 | **158,040** |

### 2.3 Relayer — 5s default (not a deployment target)

| Load | Floor | + Indexing | + Delivery | Total/day |
|---|---|---|---|---|
| Idle | 17,280 × 4 = 69,120 | 0 | 0 | **69,120** |
| Low | 69,120 | 110 | 25 | **69,255** |
| Moderate | 69,120 | 1,100 | 250 | **70,470** |
| Heavy | 69,120 | 11,000 | 2,500 | **82,620** |
| Stress | 69,120 | 110,000 | 25,000 | **204,120** |

### 2.4 Validator — all cadences

| Cadence | Cycles/day | Per cycle | Floor/day |
|---|---|---|---|
| **30s** | 2,880 | 3 | **8,640** |
| 15s | 5,760 | 3 | **17,280** |
| 5s (default) | 17,280 | 3 | **51,840** |

Per Solana-origin dispatch (merkle tree hook sync): +11 credits. The
validator's sync runs the same `SealevelMailboxIndexer` GPA path as the
relayer's dispatch loop.

### 2.5 Other components (independent of polling cadence)

| Component | Credits/day | Notes |
|---|---|---|
| Gas oracle | ~290 | 96 cycles × 3 credits |
| Warp-UI (low) | 210 – 21,000 | 3/visit + 6/tx |
| Explorer (2 wallets) | ~10,000 | 720 cycles × ~7 credits/wallet |
| Deployer (amortized) | <100 | One-time |

---

## 3. Aggregate scenario tables (polling only)

### 3.1 Canonical: 30s polling

| Component | Low (10 tx/d) | Moderate (100) | Heavy (1k) | Stress (10k) |
|---|---|---|---|---|
| Relayer | 11,655 | 12,870 | 25,020 | 146,520 |
| Validator | 8,695 | 9,190 | 14,140 | 63,640 |
| Gas oracle | 290 | 290 | 290 | 290 |
| Warp-UI | 210 | 2,100 | 21,000 | 210,000 |
| Explorer (2 wallets) | 10,000 | 10,000 | 10,000 | 10,000 |
| Deployer (amortized) | 100 | 100 | 100 | 100 |
| **Total / day** | **~31,000** | **~34,600** | **~70,600** | **~430,600** |
| **Total / month** (×30) | **~930k** | **~1.04M** | **~2.1M** | **~12.9M** |
| **Tier** | Developer | Developer | Developer | Business |
| **Cost** | $49 | $49 | $49 | $499 |

### 3.2 5s default (for comparison only)

| Component | Low (10 tx/d) | Moderate (100) | Heavy (1k) | Stress (10k) |
|---|---|---|---|---|
| Relayer | 69,255 | 70,470 | 82,620 | 204,120 |
| Validator | 51,895 | 52,440 | 57,340 | 106,840 |
| Gas oracle | 290 | 290 | 290 | 290 |
| Warp-UI | 210 | 2,100 | 21,000 | 210,000 |
| Explorer (2 wallets) | 10,000 | 10,000 | 10,000 | 10,000 |
| Deployer (amortized) | 100 | 100 | 100 | 100 |
| **Total / day** | **~131,800** | **~135,400** | **~171,400** | **~531,400** |
| **Total / month** (×30) | **~3.95M** | **~4.06M** | **~5.1M** | **~15.9M** |
| **Tier** | Developer | Developer | Developer | Business |
| **Cost** | $49 | $49 | $49 | $499 |

---

## 4. Correction note

The original model (committed 2026-05-21) reported a 30s idle relayer
floor of **57,600 credits/day** and a validator floor of **14,400
credits/day**. These assumed `getProgramAccounts` runs on every poll
cycle. Code analysis (cursor → `latest_sequence_count_and_tip()` → tip
check only → `Sleep` when no new nonces) shows idle cycles cost **4
credits** (relayer) and **3 credits** (validator), not 20 and 5
respectively.

The corrected 30s relayer floor is **11,520/day** (5× lower). The
corrected validator floor is **8,640/day** (1.7× lower). Per-message
costs are unchanged — `getProgramAccounts` still runs at 10 credits per
nonce when messages are found.
