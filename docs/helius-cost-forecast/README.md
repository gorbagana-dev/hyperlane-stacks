# Helius API Cost Forecast

Projection of monthly Helius spend for the Gorbagana ↔ Solana bridge and
related Solana-mainnet consumers in the gorbagana ecosystem.

This is the executive summary. Detailed RPC inventory and per-component
credit math is in [`rpc-inventory.md`](./rpc-inventory.md). Empirical
calibration setup and provider-comparison data is in
[`calibration-and-comparison.md`](./calibration-and-comparison.md).

**Date:** 2026-05-21
**Status:** Pre-launch estimate. Calibrate against the Helius dashboard
once the bridge is live.

---

## 1. TL;DR

The bridge fits comfortably on Helius's **Developer tier ($49/month)** at
launch under realistic load, provided we set the Hyperlane agent polling
intervals (validator + relayer indexer) to a sane value (30s, or 15s if
we want tighter latency). The default 5s would push us into the
**Business tier ($499/month)** before any user traffic.

| Scenario | Bridge tx/day | **30s polling** (canonical) | **15s polling** (tighter, see §4) | 5s default (no change) |
|---|---|---|---|---|
| **Canonical** | low (~10) | **~2.5 M → Developer** | ~4.6 M → Developer | ~13.3 M → Business |
| Moderate | 100 | ~2.6 M → Developer | ~4.7 M → Developer | ~13.4 M → Business |
| Heavy | 1,000 | ~3.5 M → Developer | ~5.7 M → Developer | ~14.3 M → Business |
| Stress | 10,000 | ~12.8 M → Business | ~15.0 M → Business | ~23.6 M → Business |

Developer rows = **$49/month**; Business rows = **$499/month**. The 5s
column is shown for contrast — it's wasteful (Solana finality is ~12s,
so faster polling re-reads settled state with no latency benefit) and is
not a deployment target.

Pick **30s** for the most cost margin, or **15s** for ~10-20s less
worst-case bridge latency at ~2× the credit cost (still Developer in all
realistic load scenarios).

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

Sorted by credits/day under the canonical (30s polling, low traffic)
scenario:

| # | Component | Credits/day | Share | Driver |
|---|---|---|---|---|
| 1 | Relayer indexer | 57,600 | 70 % | 2 × `getProgramAccounts` (10c) every 30s |
| 2 | Validator polling | 14,400 | 17 % | 5 standard calls every 30s |
| 3 | Explorer bridge-tracker | 10,000 | 12 % | 120s cron, ~2 tracked wallets |
| 4 | Warp-UI | 210 – 21,000 | 0.3 – 20 % | Scales with user traffic |
| 5 | Gas oracle | 290 | 0.4 % | 15-minute cadence |
| 6 | Deployer | <100 | <0.1 % | One-time |

The two Hyperlane agents account for ~87 % of the pre-traffic load. Any
further cost-engineering should start there. See
[`rpc-inventory.md`](./rpc-inventory.md) for the per-method math.

---

## 4. Optimization levers (in priority order)

Helius feature / credit-cost claims below sourced from
<https://www.helius.dev/docs/billing/credits> and
<https://www.helius.dev/pricing>.

1. **Set agent polling intervals.** The single largest cost lever — and
   the one the canonical scenario already assumes.

   *What it controls.* The relayer and validator are pull-based: they
   poll Solana on a timer for new mailbox state (dispatched messages,
   processed messages, merkle root insertions). The interval governs how
   often that polling happens. The indexer uses `finalized` commitment
   (`hyperlane-sealevel/src/account.rs:47`), so it only ever sees state
   Solana has already settled.

   *Why 5s is wasteful.* Solana's `finalized` commitment takes ~32 slots
   ≈ 12-13 seconds. Polling more often than that re-reads the same
   finalized state — strictly wasted RPC budget, no latency benefit.

   *Trade-off.* Raising the interval adds bridge-transfer latency on
   Solana-origin messages, because the validator and relayer both gate
   the critical path and each adds up to one interval of detection
   delay. It does **not** weaken security, change correctness, drop
   messages, or expand reorg surface — the trust model is unaffected.

   *Sane settings:*
   - **30s (canonical):** ~6× cost reduction vs default. Worst-case
     adds ~25-50s detection latency on Solana-origin transfers. The
     comfortable default.
   - **15-20s (tighter alternative):** ~3-4× cost reduction vs default.
     Worst-case adds ~10-30s. Just above Solana finality, so still no
     wasted polls. Still fits Developer tier; pick this if user-perceived
     bridge time matters more than the extra cost margin.
   - **Below ~15s:** over-polling, no latency benefit, higher bill.

   Configured via `HYP_VALIDATOR_INTERVAL` and the relayer's
   `SLEEP_DURATION` override in the agent config we generate.

2. **Adopt `getProgramAccountsV2` in the Sealevel indexer** (1 credit vs 10).
   Our patch to make. Requires modifying the Hyperlane SVM indexer in our
   fork (`hyperlane-monorepo`) to call `getProgramAccountsV2` and confirming
   Helius supports the memcmp filter shapes we use. Potential ~10× drop on
   the relayer line — would push even the stress scenario into Developer.

3. **Use Helius Sender for relayer/validator tx submission** (0 credits vs 1).
   Configuration change at the agent level. Effect is small compared to GPA
   polling but free. Sender is also faster than plain `sendTransaction`.

4. **LaserStream or programSubscribe for mailbox state.** 2 credits per
   0.1 MB. For PDAs that change rarely, dramatically cheaper than 5s/30s
   GPA polling. Requires a Sealevel indexer refactor in our fork; deferred
   until 2 + 3 are exhausted. Note: **LaserStream gRPC on mainnet is
   Business-tier and up** ([source](https://www.helius.dev/pricing)) —
   adopting this lever moves us off Developer regardless of credit usage.

5. **Helius account split per component.** Five API keys, one each for
   relayer / validator / gas-oracle / warp-UI / explorer. Enables per-
   component dashboards in Helius (no per-key surcharge on Developer+).
   Worth setting up before launch even before we calibrate, so the
   [calibration procedure](./calibration-and-comparison.md) works.

---

## 5. Rate-limit headroom

Source: <https://www.helius.dev/pricing>.

Credit budget is one half of tier sizing; RPS limits are the other. Helius
publishes a stricter per-method RPS for `getProgramAccounts` and
`sendTransaction` than for general RPC, so the recommended tier needs to
be checked against the actual peak RPS we expect, not just steady-state.

**Per-tier limits relevant to this workload:**

| Limit | Developer | Business |
|---|---|---|
| General RPC RPS | 50 | 200 |
| `getProgramAccounts` RPS | **25** | **50** |
| `sendTransaction` RPS | 5 | 50 |
| Sender TPS | — | 50 |
| LaserStream gRPC (mainnet) | not available (devnet only) | included |

**Our peak RPS by source:**

- **Relayer GPA polling (steady-state):** 2 calls per 30s = 0.07 RPS.
  ~350× under Developer's 25-RPS ceiling. The only realistic burst is
  catchup after a multi-hour outage, where the indexer walks queued
  nonces at 1 GPA per message. With 200 backlogged messages on Developer,
  catchup saturates the 25-RPS limit for ~8 seconds; Helius returns 429s
  and the indexer backs off. Acceptable.
- **Warp-UI concurrent user clicks:** ~6 calls/click over ~2 seconds
  ≈ 3 RPS per simultaneous user. Developer's 50-RPS general limit gives
  ~16 simultaneous bridge clicks before rate-limiting. Sufficient for
  low-to-moderate traffic; insufficient for a viral surge.
- **`sendTransaction`:** under 0.05 RPS average even at the Heavy
  (1k tx/day) scenario; relayer submits serially with confirmations,
  so bursts stay below Developer's 5-RPS ceiling.

**Verdict:** **Developer satisfies our RPS needs** in the canonical, moderate,
and heavy scenarios. Business is needed if any of: we adopt LaserStream;
sustained concurrent warp-UI users exceed ~15; or we want headroom for an
unannounced traffic spike. Professional adds nothing for our workload —
its differentiator over Business is throughput we won't use.

---

## 6. Tier recommendation

**Provision Helius Developer ($49/month).** Apply a 30s polling interval
to validator + relayer in the agent generation script before launch — or
15-20s if we want tighter latency at a modest cost margin (see §4
lever 1). Both fit Developer comfortably.

**Triggers to upgrade to Business ($499):**

- Sustained measured usage > 8 M credits/month (80 % of Developer ceiling)
  over two consecutive weeks.
- Sustained bridge volume > 5,000 tx/day, or
- Sustained concurrent warp-UI users > ~15 (RPS pressure), or
- Adoption of LaserStream / programSubscribe-based indexing (mainnet
  requires Business), or
- Adding a second high-volume Helius consumer (expanded explorer features,
  new product line).

**Do not provision Professional ($999)** unless Business stops covering
sustained load. Our projected RPS is well inside Business limits even
under the Stress scenario.

---

## 7. Key assumptions

These were investigated and resolved while building this estimate
(citations in [`rpc-inventory.md`](./rpc-inventory.md)):

- The Solana validator's `Mailbox::count` resolves to `getAccountInfo`
  on a known PDA, not `getProgramAccounts`. Confirmed at
  `hyperlane-monorepo/rust/main/chains/hyperlane-sealevel/src/merkle_tree_hook.rs:68`.
- The relayer runs exactly **two** `getProgramAccounts`-polling loops per
  Solana origin (dispatched messages + processed messages), not four —
  warp routes are not indexed by the relayer. Confirmed at
  `hyperlane-monorepo/rust/main/agents/relayer/src/relayer/origin.rs:220-323`.
- Default agent polling cadence is 5s in both the validator
  (`validator/src/settings.rs:145`) and relayer indexer
  (`hyperlane-base/src/contract_sync/mod.rs:36` — `SLEEP_DURATION`).
- Helius's `getProgramAccountsV2` charges 1 credit vs the 10 of v1.
  Source: <https://www.helius.dev/docs/billing/credits>.

Items that still need a product/business input rather than code:

- Expected bridge-tx volume curve at launch and steady state. Estimate
  uses placeholders of 10 / 100 / 1000 / 10,000 tx/day across scenarios.
- Number of bridge wallets the explorer cron tracks. Estimate assumes 1-3.
- Helius's per-credit overage rate (not in public docs). Confirm with
  Helius sales before signing; our scenarios stay under tier ceilings so
  overage is irrelevant unless we mis-size.

---

## 8. Next steps

- **Before launch:** set agent polling intervals to 30s; provision a
  Helius Developer account with per-component API keys.
- **During first 72h of mainnet operation:** run the
  [empirical calibration](./calibration-and-comparison.md) procedure to
  reconcile measured vs predicted credits/day.
- **One month in:** revisit the model and the tier choice.
- **Deferred:** evaluate `getProgramAccountsV2` and Sender adoption in
  our `hyperlane-monorepo` fork.
