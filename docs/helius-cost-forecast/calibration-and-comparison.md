# Empirical Calibration & Provider-Comparison Readiness

Supporting material for [`README.md`](./README.md). How to validate the
forecast against real Helius dashboard data, and what to bring to a
future provider comparison if we ever evaluate alternatives.

---

## 1. Empirical calibration

The model in [`rpc-inventory.md`](./rpc-inventory.md) is derived from
code, not from measurement. This section is the procedure to confirm
those numbers against live Helius usage and to update the model when
they diverge.

### 1.1 Pre-launch setup

- **Provision API keys per component.** One Helius project, but separate
  API keys for relayer, validator, gas-oracle, warp-UI, and explorer.
  Helius dashboards usage per key, which is the only practical way to
  attribute credits to a component without log scraping. There is no
  per-key surcharge on Developer+.
- **Wire each component's API key independently.** For agents and the
  gas oracle, set `SOLANA_RPC_URL` in the respective deployment spec.
  For warp-UI, set the relevant `__SOLANA_RPC_URL__` in its chain config.
  For the explorer, set `HELIUS_API_KEY` in its server env.
- **Tag the keys** in the Helius dashboard with the component name so
  reports are readable.

### 1.2 Measurement window

Run at least **72 hours** of steady-state operation before drawing
conclusions. 72h smooths over:

- Agent restart bursts (the indexer re-bootstraps its cursor on restart
  and can issue a burst of `getProgramAccounts` calls)
- Reorg backfills
- Solana epoch boundaries
- Diurnal usage cycles in the warp-UI

If 72h is too long for a pre-launch dry run, **48h is the floor**;
shorter than that, results are dominated by setup noise.

### 1.3 Data to export

From the Helius dashboard, per API key, export credit counts for the
window split by method. Methods to track explicitly (everything else
rolled into a catch-all):

- `getProgramAccounts` (and `getProgramAccountsV2` if we adopt it)
- `getAccountInfo`, `getMultipleAccounts`
- `getSignaturesForAddress`
- `getTransaction`, `getParsedTransaction`
- `getTokenAccountsByOwner`, `getParsedTokenAccountsByOwner`
- `getSlot`, `getBlock`, `getLatestBlockhash`
- `sendTransaction`
- `simulateTransaction`
- WebSocket connection opens
- WebSocket data volume (MB)
- Catch-all (all others)

### 1.3.1 WebSocket-specific metrics

When WebSocket indexing is active, also track:

- **Connection count / day** — how many WebSocket connections were
  opened (initial + reconnects). Target: <10/day with healthy
  keep-alive. >50/day indicates reconnection churn.
- **Data volume / day** — metered WebSocket data in MB. At low bridge
  traffic (<100 tx/day), expect <1 MB/day. Significantly higher
  indicates noisy `programSubscribe` notifications (other activity on
  the mailbox program).
- **Fallback poll hit rate** — percentage of cursor cycles that fire
  via the timer (not the WebSocket signal). Target: <5% when WebSocket
  is healthy. >50% indicates persistent WebSocket connectivity issues.
- **Wake-up-to-fetch latency** — time between WebSocket notification
  and the corresponding `fetch_logs_in_range()` call completing. Proxy
  for real-world detection latency.

### 1.4 Reconciliation

For each component:

1. Pull predicted credits/day from
   [`rpc-inventory.md`](./rpc-inventory.md) §2 at the cadence and
   indexing mode (WebSocket or polling-only) actually running. If
   polling-only, use
   [`polling-baseline-reference.md`](./polling-baseline-reference.md).
2. Pull measured credits/day from the Helius dashboard.
3. Compute delta and percentage.
4. If delta > ±25 %, dig into the per-method breakdown and identify the
   cause (restart bursts, undisclosed RPC calls in the indexer, scrape
   overhead, WebSocket reconnection churn, etc.).
5. Update the model's per-cycle credit numbers and re-run §3 of
   `rpc-inventory.md`.

### 1.5 Re-calibration cadence

- Initial validation, pre-launch (above procedure)
- One month into production
- After any of:
  - Sustained traffic regime change (e.g. promotional event)
  - Hyperlane fork upgrade that touches the SVM indexer or validator
  - Switching indexing mode (WebSocket ↔ polling), adopting
    `getProgramAccountsV2`, Sender, or LaserStream gRPC
  - Adding or removing a tracked bridge wallet in the explorer
  - Adding any new Helius consumer

### 1.6 What "calibrated" looks like

We are calibrated when, for two consecutive measurement windows, the
predicted-vs-measured delta is within ±15 % per component (per-component,
not just in aggregate — the aggregate can be right by accident). At that
point the model can be trusted for tier-sizing decisions without fresh
measurement.

---

## 2. Provider-comparison readiness

If we later evaluate QuickNode, Triton, Alchemy, syndica, or self-hosting
as alternatives, this is the data to bring to the comparison.

### 2.1 Per-method daily call rates

Under the canonical (WebSocket + 120s fallback, low traffic) scenario,
quote these volumes at any provider that prices per-call:

| Method | Calls/day | Source |
|---|---|---|
| `getProgramAccounts` | ~10-20 | Per-message only (no idle GPA); relayer + validator |
| `getAccountInfo` (+ `getMultipleAccounts`) | ~3,000 | Tip-checks (720 cycles × 4 credits) + state lookups |
| `getSlot` | ~1,440 | Relayer + validator cursor tip-checks |
| `getSignaturesForAddress` | ~720 | Explorer + warp-UI |
| `getParsedTransaction` / `getTransaction` | ~3,600 | Explorer cron N=5 |
| `getTokenAccountsByOwner` / `getParsedTokenAccountsByOwner` | ~500 | Explorer + warp-UI |
| `sendTransaction` | ~10-100 | Relayer + validator submissions |
| `simulateTransaction` | ~10-100 | Per delivered message |
| WebSocket connections | ~10 | Initial + reconnects |
| WebSocket data | <1 MB | Negligible at low traffic |
| Others (combined) | ~500 | Catch-all |

Recompute and bring the **measured** numbers from §1 once we have them —
those are more credible than these projections.

### 2.2 Pricing dimensions to look up

For each provider, gather:

- Tier prices and included request volumes (or credit budgets)
- Per-method premium pricing — particularly `getProgramAccounts`,
  parsed-transaction calls, archival queries
- Rate limits — RPS, concurrent connections, parallel batch size
- Geographic regions and latency to our deployment region
- WebSocket subscription support (`programSubscribe`,
  `accountSubscribe`) — availability, connection limits, data metering
- gRPC streaming pricing (their LaserStream equivalent)
- Bundle / private-mempool tx submission pricing (their Sender or Jito
  equivalent)
- Support SLA at the $50, $500, $1,000 price points
- Free-tier viability for staging environments
- Egress / bandwidth charges (some providers bill these separately)
- Multi-region failover support

### 2.3 Self-host sketch

A dedicated Solana RPC validator with sufficient hardware to serve the
above workload runs roughly **$300 – $800/month** all-in (bare metal /
high-tier VPS, bandwidth, ops time). The variance is mostly hardware
choice and whether `getProgramAccounts` warm cache lives in RAM.

Self-host starts to compete with managed RPC above the **Business
tier ($499)**, particularly if we also expect to serve other Solana
workloads from the same node (an expanded explorer, future products,
DAS-equivalent indexing). Below that, managed RPC wins on operational
simplicity.

For the bridge in isolation at canonical load (~2.5 M credits/month),
managed Helius Developer at $49 is the clear winner — self-hosting
would be paying $300-800 for capacity we won't use.

### 2.4 Comparison output format

When we run a real comparison, produce a single table with one row per
provider and one column per cost component (base tier + estimated
overage + premium method surcharges + bandwidth + ops). Pick the
canonical scenario as the baseline; show the stress scenario as a
sensitivity.

Hold off on a comparison until at least one calibration cycle has run.
We need measured numbers, not projected ones, before we can compare
fairly.
