# WebSocket Subscriptions & Configurable Commitment: Fork Changes

Changes required in our `hyperlane-monorepo` fork and in `hyperlane-stacks` to
support (a) WebSocket-based event detection via Helius LaserStream
subscriptions and (b) configurable Solana commitment level for near
real-time bridging.

**Date:** 2026-05-25
**Status:** Design — implementation spec for upcoming fork work.

---

## 1. Background

The Hyperlane SVM indexer polls Solana state on a timer using
`getProgramAccounts` (relayer) and `getAccountInfo` (validator). Two
properties are hardcoded:

1. **Commitment level is `finalized`** — every RPC read in the Sealevel
   crate uses `CommitmentConfig::finalized()`. There is no configuration
   knob. Finalized commitment takes ~12-13 seconds on Solana.

2. **Detection is poll-based** — the `ContractSync` loop sleeps for a
   fixed duration (default 5 seconds), then calls
   `Indexer::fetch_logs_in_range()`. There is no push/subscription path.

Together, these mean the floor on Solana-origin bridge latency is
~12-13s (finality) + 0-Ns (polling interval, where N = sleep duration),
averaged ~12-13s + N/2. With the canonical 30s polling, average
end-to-end bridge time is ~28s; worst case ~45-75s.

---

## 2. Optimization A: Configurable commitment level

### 2.1 What changes and why

Switching the indexer from `finalized` (~12-13s) to `confirmed` (~1-2s)
eliminates ~11s from every Solana-origin bridge transfer. The Hyperlane
protocol's correctness does not depend on the commitment level — the
validator signs whatever state it observes, and the destination ISM
verifies signatures, not source-chain finality. The risk is that a
`confirmed` block could theoretically be rolled back (extremely rare on
Solana, but nonzero), which would mean a message was delivered on the
destination for a source transaction that was later reverted.

The goal is to make commitment **configurable**, defaulting to
`finalized` (safe) with an opt-in to `confirmed` (faster).

### 2.2 Current state in code

All indexing reads use `finalized`. The relevant call sites:

**Relayer indexer — `getProgramAccounts`:**
- `hyperlane-sealevel/src/account.rs:39` —
  `commitment: Some(CommitmentConfig::finalized())`

**Relayer indexer — per-message account reads:**
- `hyperlane-sealevel/src/mailbox_indexer.rs:101` —
  `get_account_with_finalized_commitment(...)` (dispatched messages)
- `hyperlane-sealevel/src/mailbox_indexer.rs:201` —
  `get_account_with_finalized_commitment(...)` (delivered messages)

**Validator — merkle tree hook:**
- `hyperlane-sealevel/src/merkle_tree_hook.rs:68` —
  `get_account_with_finalized_commitment(self.outbox.0)`

**Validator — slot queries:**
- `hyperlane-sealevel/src/rpc/client.rs:222` —
  `get_slot_with_commitment(CommitmentConfig::finalized())`

**RPC client — block and transaction reads:**
- `hyperlane-sealevel/src/rpc/client.rs:136` —
  `get_block_with_commitment(slot, CommitmentConfig::finalized())`
- `hyperlane-sealevel/src/rpc/client.rs:165` —
  `get_multiple_accounts_with_commitment(pubkeys, CommitmentConfig::finalized())`
- `hyperlane-sealevel/src/rpc/client.rs:251` —
  `get_transaction_with_commitment(signature, CommitmentConfig::finalized())`

**Merkle tree hook — reorg period assertions:**
- `hyperlane-sealevel/src/merkle_tree_hook.rs:22-23` —
  `assert!(reorg_period.is_none(), "Sealevel does not support querying point-in-time")`
- `hyperlane-sealevel/src/merkle_tree_hook.rs:36` — same assertion for
  `latest_checkpoint`

The RPC client layer already has generic `_with_commitment(commitment)`
methods alongside the finalized-specific wrappers:
- `rpc/client.rs:88` — `get_account_option_with_commitment(pubkey, commitment: CommitmentConfig)`
- `rpc/client.rs:116` — `get_block_with_commitment(slot, commitment: CommitmentConfig)`
- `rpc/client.rs:228` — `get_transaction_with_commitment(signature, commitment: CommitmentConfig)`

So the underlying plumbing supports arbitrary commitment; only the call
sites are hardcoded.

### 2.3 Changes required in `hyperlane-monorepo` fork

**File 1: `hyperlane-sealevel/src/trait_builder.rs`** — Add a commitment
field to `ConnectionConf`:

```rust
pub struct ConnectionConf {
    pub urls: Vec<Url>,
    pub indexing_commitment: CommitmentConfig,  // NEW — default: finalized
    // ... existing fields unchanged
}
```

**File 2: `hyperlane-sealevel/src/account.rs`** — Replace the hardcoded
commitment in `search_accounts_by_discriminator` (line 39) with a
parameter passed from the caller:

```
-   commitment: Some(CommitmentConfig::finalized()),
+   commitment: Some(commitment),
```

**File 3: `hyperlane-sealevel/src/mailbox_indexer.rs`** — Thread
`self.commitment` into the account reads at lines 101 and 201, replacing
`get_account_with_finalized_commitment(...)` with
`get_account_option_with_commitment(..., self.commitment)`.

**File 4: `hyperlane-sealevel/src/merkle_tree_hook.rs`** — Replace the
hardcoded finalized call at line 68 with the configurable commitment.
Relax or remove the `reorg_period.is_none()` assertions at lines 22 and
36 (these assert that Sealevel cannot query historical state, which
remains true — but the assertion message is misleading when commitment
is configurable).

**File 5: `hyperlane-sealevel/src/rpc/client.rs`** — The slot query at
line 222 should use the configurable commitment. The `get_block` default
at line 136 and `get_transaction` default at line 251 should also use it.

**File 6: Agent config parsing** — Accept a `commitment` key in the
Sealevel chain configuration (e.g. under `chains.solana.connection`),
mapping string values `"finalized"` / `"confirmed"` to
`CommitmentConfig`. Default to `finalized` when absent.

**Leave unchanged:** Transaction submission in `provider.rs:176-180`
(`get_latest_blockhash_with_commitment(CommitmentConfig::finalized())`)
should stay `finalized` regardless — the comment there explains that
finalized blockhash prevents expiry.

**Estimated scope:** ~30-50 lines changed across 6 files. Mechanical
find-and-replace plus one new config field.

### 2.4 Changes required in `hyperlane-stacks`

**Chain metadata templates** — Add `commitment` to the Sealevel chain
config if the Hyperlane agent config schema supports it. Relevant files:
- `stack_orchestrator/data/config/deployer-registry-config/metadata.yaml.tmpl`
- `stack_orchestrator/data/config/warp-deployer-registry-config/metadata.yaml.tmpl`

**Compose files** — Add an optional `SOLANA_COMMITMENT` env var
(default: `finalized`) to the relayer and validator compose files:
- `stack_orchestrator/data/compose/docker-compose-hyperlane-relayer.yml`
- `stack_orchestrator/data/compose/docker-compose-hyperlane-validator.yml`

**Deployment specs** — Add the env var (commented out, showing the
default) to:
- `deployment/spec-relayer.yml`
- `deployment/spec-validator-solana.yml`

### 2.5 Latency impact

| Commitment | Solana wait | Avg detection (30s poll) | Avg end-to-end |
|---|---|---|---|
| `finalized` (current) | ~12-13s | ~15s | ~28s |
| `confirmed` | ~1-2s | ~15s | ~17s |

The commitment change saves ~11s on average but does not eliminate
polling delay. For maximum benefit, combine with Optimization B.

### 2.6 Risk

`confirmed` commitment means 66%+ of validators voted on the block, but
~31 confirmation slots have not yet built on top. Rollback at this level
has no widely documented precedent on Solana mainnet, but is
theoretically possible. If a rollback occurs, a bridge message could be
delivered on the destination chain for a source transaction that no
longer exists. The destination-side action cannot be undone.

Recommendation: default to `finalized`. Offer `confirmed` as an opt-in
for deployments where speed is prioritized over maximum safety, and
where bridge values are small enough that the rollback risk is
acceptable.

---

## 3. Optimization B: WebSocket subscriptions (LaserStream)

### 3.1 What changes and why

Replace the sleep-based polling loop with a WebSocket subscription
(`programSubscribe`) that pushes notifications when mailbox state
changes. This eliminates the polling delay (0-30s with 30s cadence)
and reduces Helius credit consumption by ~100-300x on the polling
component.

### 3.2 Helius LaserStream WebSocket

Helius's LaserStream WebSocket supports standard Solana subscription
methods (`programSubscribe`, `accountSubscribe`) on **all tiers
including Free and Developer**. Enhanced WebSocket methods (e.g.
`transactionSubscribe`) require Developer+. LaserStream gRPC on
mainnet requires Business ($499/mo).

For our use case, standard `programSubscribe` via LaserStream WebSocket
is sufficient. It is available on Developer ($49/mo) — no tier upgrade
needed.

Credit cost: 2 credits per 0.1 MB of streamed data (data-volume-based,
not per-notification). For our low-volume bridge, daily WebSocket data
transfer would be negligible compared to polling.

Source: <https://www.helius.dev/pricing>,
<https://www.helius.dev/docs/billing/credits>.

### 3.3 Approach: wake-up signal (not full rearchitecture)

Rather than replacing the polling architecture entirely, the recommended
approach is to use WebSocket notifications as a **wake-up signal** for
the existing polling loop. When a notification arrives, the loop skips
its sleep and immediately polls. When the WebSocket disconnects, it
falls back to timer-based polling (identical to today).

This preserves the existing `Indexer<T>` trait, `SequenceAwareIndexer`,
`fetch_logs_in_range()`, cursor logic, and log storage — completely
unchanged. It also maintains architectural consistency with EVM/Cosmos
chains that are also poll-based.

### 3.4 Current state in code

**The indexer trait is poll-only:**
- `hyperlane-core/src/traits/indexer.rs:30-47` — `Indexer<T>` trait:
  `fetch_logs_in_range()`, `get_finalized_block_number()`. No
  subscription method.
- `hyperlane-core/src/traits/indexer.rs:55-58` —
  `SequenceAwareIndexer<T>`: `latest_sequence_count_and_tip()`. No
  push-based method.

**The sync loop is sleep → poll → store → repeat:**
- `hyperlane-base/src/contract_sync/mod.rs:36` —
  `const SLEEP_DURATION: Duration = Duration::from_secs(5);`
- `mod.rs:251-318` — `cursor_indexer_task` loop:
  - Line 255: `cursor.next_action()` → returns `Sleep(duration)` or
    `Query(range)`
  - Lines 265-273: `CursorAction::Sleep(duration)` → `sleep(duration)`
  - Line 278: `CursorAction::Query(range)` →
    `indexer.fetch_logs_in_range(range)`
  - Line 315: update cursor, loop back

**The Sealevel indexer polls via `getProgramAccounts`:**
- `hyperlane-sealevel/src/mailbox_indexer.rs:271-287` — `Indexer<HyperlaneMessage>`
  impl: iterates nonce range, calls `get_dispatched_message_with_nonce`
- `hyperlane-sealevel/src/account.rs:45-48` —
  `provider.rpc_client().get_program_accounts_with_config()`

**WebSocket client dependency exists but is unused:**
- `hyperlane-sealevel/Cargo.toml:30` — `solana-pubsub-client.workspace = true`
- Zero references to `solana_pubsub_client`, `PubsubClient`,
  `programSubscribe`, `accountSubscribe`, or `ws://` / `wss://` in any
  Sealevel source file.

**Connection config has no WebSocket field:**
- `hyperlane-sealevel/src/trait_builder.rs:15-30` — `ConnectionConf`
  has `urls: Vec<Url>` (HTTP only), no WebSocket URL.

### 3.5 Changes required in `hyperlane-monorepo` fork

**File 1: `hyperlane-sealevel/src/trait_builder.rs`** — Add an optional
WebSocket URL to `ConnectionConf`:

```rust
pub struct ConnectionConf {
    pub urls: Vec<Url>,
    pub ws_url: Option<Url>,  // NEW — LaserStream WebSocket endpoint
    // ... existing fields unchanged
}
```

**File 2: New module `hyperlane-sealevel/src/pubsub.rs`** — WebSocket
subscription manager. Responsibilities:

- Connect to the WebSocket endpoint using `solana_pubsub_client::PubsubClient`
  (already in Cargo.toml, line 30)
- Call `program_subscribe(mailbox_program_id, commitment)` to watch the
  mailbox program for account changes
- On each notification, signal a `tokio::sync::Notify`
- On disconnect, log a warning and reconnect with exponential backoff
- On permanent failure, stop signaling (loop falls back to timer)

Estimated: ~80-120 lines.

**File 3: `hyperlane-base/src/contract_sync/mod.rs`** — Modify
`ContractSync` and `cursor_indexer_task` to accept an optional wake-up
signal.

The struct (line 50) gains an optional `Arc<tokio::sync::Notify>`:

```rust
pub struct ContractSync<T, S, I> {
    // ... existing fields
    wake_signal: Option<Arc<tokio::sync::Notify>>,  // NEW
}
```

The sleep paths in `cursor_indexer_task` (lines 265-273 and 259) change
from:

```rust
CursorAction::Sleep(duration) => {
    sleep(duration).await;
    continue;
}
```

To:

```rust
CursorAction::Sleep(duration) => {
    match &self.wake_signal {
        Some(signal) => tokio::select! {
            _ = sleep(duration) => {},
            _ = signal.notified() => {},
        },
        None => sleep(duration).await,
    }
    continue;
}
```

When `wake_signal` is `None`, behavior is identical to today.
When `Some`, the loop wakes on whichever comes first: the timer or a
WebSocket notification.

Estimated: ~15-20 lines changed.

**File 4: `hyperlane-sealevel/src/mailbox_indexer.rs`** — In
`SealevelMailboxIndexer::new()` (line 41-50), if `ConnectionConf` has a
`ws_url`, spawn the WebSocket subscription task from File 2 and store
the `Notify` handle. Pass it up to `ContractSync` during wiring.

Estimated: ~15-20 lines.

**File 5: Agent config parsing** — Accept `customWsUrl` (or similar) in
the Sealevel chain configuration, mapping it to `ConnectionConf.ws_url`.

### 3.6 Changes required in `hyperlane-stacks`

**Compose files** — Add `SOLANA_WS_URL` env var to the relayer and
validator compose files (the only two components that run the indexer):

- `stack_orchestrator/data/compose/docker-compose-hyperlane-relayer.yml`:
  ```yaml
  environment:
    SOLANA_WS_URL: ${SOLANA_WS_URL:-}
  ```
- `stack_orchestrator/data/compose/docker-compose-hyperlane-validator.yml`:
  ```yaml
  environment:
    SOLANA_WS_URL: ${SOLANA_WS_URL:-}
  ```

**Chain metadata templates** — Add `wsUrl` to the Solana chain entry in:
- `stack_orchestrator/data/config/deployer-registry-config/metadata.yaml.tmpl`:
  ```yaml
  solana:
    rpcUrls:
      - http: "${SOLANA_RPC_URL}"
    # wsUrl: "${SOLANA_WS_URL}"  # Optional: LaserStream WebSocket
  ```
- Same for `warp-deployer-registry-config/metadata.yaml.tmpl`

**Agent config generation scripts** — If the agent config JSON is
generated by a deploy script (rather than directly from the metadata
template), the script needs to include the `wsUrl` field when
`SOLANA_WS_URL` is set.

**Deployment specs** — Add `SOLANA_WS_URL` (commented out) to:
- `deployment/spec-relayer.yml`
- `deployment/spec-validator-solana.yml`

Example:
```yaml
config:
  SOLANA_RPC_URL: "https://mainnet.helius-rpc.com/?api-key=YOUR_KEY"
  # SOLANA_WS_URL: "wss://mainnet.helius-rpc.com/?api-key=YOUR_KEY"
```

**No changes needed for:** gas oracle, warp-UI, monitoring, deployer
jobs, MinIO, or explorer — none of these run the Hyperlane indexer loop.

### 3.7 Latency and cost impact

**Latency (combined with Optimization A):**

| Configuration | Avg bridge time | Worst case |
|---|---|---|
| Current (finalized + 30s polling) | ~28s | ~75s |
| Finalized + WebSocket | ~16-17s | ~18s |
| Confirmed + WebSocket | ~5-6s | ~7s |

**Credit cost (idle/low traffic):**

| Configuration | Relayer credits/day | Validator credits/day | Total/month |
|---|---|---|---|
| 30s polling (current plan) | 57,600 | 14,400 | ~2.2M |
| 15s polling | 115,360 | 28,800 | ~4.3M |
| WebSocket | ~100-500 | ~50-200 | ~5k-21k |

WebSocket reduces the polling component by ~100-300x. The remaining
credits come from the actual `fetch_logs_in_range` calls triggered by
notifications and from other components (gas oracle, warp-UI, explorer).

### 3.8 Failure mode

If the WebSocket connection drops, the `tokio::select!` timer arm fires
after the configured sleep duration, and the loop reverts to
timer-based polling — identical to today's behavior. No messages are
lost; detection latency temporarily increases to the polling-based
level until the WebSocket reconnects.

---

## 4. Implementation order

| Order | Change | Effort | Latency gain | Cost gain |
|---|---|---|---|---|
| 1 | 30s polling interval (config only, no fork) | Env var | Baseline | 5.3x vs default 5s |
| 2 | WebSocket wake-up signal (§3) | ~150-200 lines in fork + compose/spec changes | ~15-60s → <2s detection | ~100-300x on polling credits |
| 3 | Configurable commitment (§2) | ~30-50 lines in fork + compose/spec changes | ~11s off finality wait | None (same credits) |
| 4 | `getProgramAccountsV2` adoption | Separate fork change | None | 10x on GPA credits |

WebSocket (step 2) delivers both latency and cost improvements with no
security trade-off, so it comes first. Configurable commitment (step 3)
adds speed but introduces a small rollback risk — best applied after
operational confidence in the bridge is established.

---

## 5. Total scope summary

### `hyperlane-monorepo` fork

| File | Change | Est. lines |
|---|---|---|
| `hyperlane-sealevel/src/trait_builder.rs` | Add `indexing_commitment` + `ws_url` to `ConnectionConf` | ~5 |
| `hyperlane-sealevel/src/account.rs` | Parameterize commitment in `search_accounts_by_discriminator` | ~5 |
| `hyperlane-sealevel/src/mailbox_indexer.rs` | Thread commitment into account reads | ~10 |
| `hyperlane-sealevel/src/merkle_tree_hook.rs` | Use configurable commitment, relax assertions | ~10 |
| `hyperlane-sealevel/src/rpc/client.rs` | Use configurable commitment in slot/block/tx defaults | ~10 |
| `hyperlane-sealevel/src/pubsub.rs` | **New:** WebSocket subscription manager | ~80-120 |
| `hyperlane-base/src/contract_sync/mod.rs` | Add `wake_signal` to `ContractSync`, `select!` in sleep paths | ~15-20 |
| Agent config parsing | Accept `commitment` + `wsUrl` in chain config | ~15-20 |
| **Total** | | **~150-200** |

### `hyperlane-stacks`

| File | Change |
|---|---|
| `docker-compose-hyperlane-relayer.yml` | Add `SOLANA_WS_URL`, `SOLANA_COMMITMENT` env vars |
| `docker-compose-hyperlane-validator.yml` | Add `SOLANA_WS_URL`, `SOLANA_COMMITMENT` env vars |
| `deployer-registry-config/metadata.yaml.tmpl` | Add `wsUrl` and `commitment` to Solana chain entry |
| `warp-deployer-registry-config/metadata.yaml.tmpl` | Same |
| `deployment/spec-relayer.yml` | Add env vars (commented out with defaults) |
| `deployment/spec-validator-solana.yml` | Add env vars (commented out with defaults) |
| Test fixtures (if applicable) | Mirror any compose changes |
