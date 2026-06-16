# Balance Monitoring + Alerting & Configurable Intervals — Design

**Date:** 2026-06-16
**Status:** Approved (pending user review of this spec)

## Goal

Two prod-readiness features in the `hyperlane-monitoring`, `hyperlane-relayer`, and
`hyperlane-gas-oracle` stacks:

1. **Generic balance monitoring + Slack alerting.** Watch native and SPL token
   balances for a configurable set of accounts across both chains, and deliver a
   low-balance alert to Slack. Auto-include the bridge's own gas-consuming signers;
   let an operator add arbitrary extra watches by editing the generated config.
2. **Configurable intervals.** Make the IGP fee-claim interval and the gas-oracle
   loop interval operator-settable from the deployment config (today one is a
   commented-out spec line, the other a literal).

## Context — what exists today

- The `hyperlane-monitoring` stack already runs a long-running `balance-monitor`
  loop container (`check-balance.py`). It checks **native SOL only** (`getBalance`)
  for accounts listed in two per-chain env vars
  (`MONITORED_WALLETS_GORCHAIN/SOLANA`, format `label:address:threshold`), and
  **pushes a gauge to Pushgateway**. The prod spec ships literal `ADDR` placeholders
  that ops never populates, so balance monitoring is effectively dead in prod.
- Prometheus evaluates `alerts.yml` (incl. `WalletBalanceLow`) but there is **no
  Alertmanager and no `alerting:` target** — alerts fire internally and are never
  delivered. "Monitoring" exists; "alerting" does not.
- Pushgateway exists **solely** for balance-monitor metrics (it is the only
  producer; `prometheus.yml` scrapes it only for this).
- `GAS_ORACLE_INTERVAL_MS` is already a real spec `config:` key in all three
  gas-oracle specs (default `900000` = 15 min) but is not surfaced through the ops
  deployment-config layer. `CLAIM_INTERVAL_SECONDS` exists in the relayer compose
  (`igp-fee-claim` sidecar, default `21600` = 6 h) but the relayer specs only carry
  it as a **commented-out** line, so an operator can't set it.
- `spec_token_renders` (in each `group_vars/all.yml`) is the established mechanism
  for rendering deployment-config values into specs, prod included (e.g.
  `REPLACE_WITH_WALLETCONNECT_PROJECT_ID`, `__S3_ENDPOINT__`).
- SO maps stack services to `V1Deployment` (pods) or one-shot `V1Job` (`jobs:`)
  only — there is **no CronJob kind for stacks**. The existing loop container is the
  de-facto cron; no SO change is needed.

## Feature 1 — Balance monitoring + Slack alerting

### Stack placement

Stays in `hyperlane-monitoring`, where `balance-monitor` already lives. No new
stack, no new k8s primitive.

### Watch config — single generated file

The monitor is driven by one generated JSON, mounted via a new runtime-populated
ConfigMap `balance-monitor-config` at `/config/watches.json`. "Runtime-populated"
means the same pattern as `agent-config`: **no `data/config/balance-monitor-config/`
source dir** — ops fills `{deploy_dir}/configmaps/balance-monitor-config/` at deploy
time, e2e fills it via conftest.

```json
{
  "watches": [
    { "chain": "gorchain", "label": "relayer", "address": "Gor…",
      "tokens": [ { "symbol": "GOR", "mint": "native", "threshold": 5.0 } ] },
    { "chain": "solana", "label": "relayer", "address": "So1…",
      "tokens": [ { "symbol": "SOL",  "mint": "native",  "threshold": 5.0 },
                  { "symbol": "USDC", "mint": "EPjF…",   "threshold": 250.0 } ] }
  ]
}
```

- Per account a `tokens` list → **multiple SPL tokens per account**, and **native is
  opt-in per account** (omit it when that address doesn't need gas).
- `mint: "native"` (or omitted) → `getBalance`. Otherwise SPL via
  `getTokenAccountsByOwner(owner, {mint})`, summed across the owner's token accounts;
  **decimals are read from the RPC response** (not hardcoded).
- `threshold` is per token; `symbol` is display-only.

### RPC resolution stays env-based (file stays secret-free)

The file names a `chain`; the monitor maps `gorchain → GORCHAIN_RPC_URL` (compose)
and `solana → SOLANA_RPC_URL` (secret). This keeps the Helius API key out of any
generated file, consistent with the repo's "generated state is secret-free" rule.
An unknown chain (no RPC env) is skipped with a logged error.

### Delivery — direct Slack webhook

- New secret `SLACK_WEBHOOK_URL` on the monitoring spec. **Empty disables alerting**
  (the monitor still runs and logs), so local/e2e run without a real webhook.
- Each cycle batches all current breaches into **one** Slack message (chain, label,
  symbol, balance vs threshold, address).
- **Anti-spam (in-memory state, keyed by chain+address+mint):** alert once on first
  breach; re-alert every `ALERT_REPEAT_SECONDS` (default `21600` = 6 h) while still
  below; send one "recovered" message when it climbs back above threshold. State
  resets on container restart (re-alerts once — acceptable).

### Metrics removed (Slack-only)

Since Slack now carries delivery and the gauge's only consumers are a self-justifying
Grafana panel + e2e assertions:

- Remove the gauge push from `check-balance.py`.
- Remove the **`pushgateway`** service (compose) and its `prometheus.yml` scrape job
  (balance-monitor is its only producer).
- Remove the `WalletBalanceLow` rule from `alerts.yml`.
- Remove the balance panel from `grafana-dashboards-config/hyperlane-overview.json`.

### Generation — auto signers (ops) + manual extras (operator)

**Auto-included signers (native watches), assembled by ops.** The bridge's
gas-consuming signers, with thresholds from the deployment-config role map:

| Role | Chain(s) | Address source | Notes |
|---|---|---|---|
| relayer | gorchain, solana | `relayer-<chain>.key.pub` | per-chain HexKey address |
| fee-claim | gorchain, solana | `relayer-fee-claim.json` pubkey | sealevel-client signer used on both chains |
| igp-oracle | gorchain, solana | `igp_oracle_pubkey` (config) | Privy oracle wallet |
| validator | its chain | `validator-<chain>.key.pub` | low threshold; gas mainly for one-time announce |

The IGP **beneficiary** is intentionally **excluded** — it is a fee *sink* (balance
grows), so a low-balance floor is meaningless.

**Multi-machine sourcing.** The monitoring host won't hold other hosts' key
material, so the generation step gathers addresses via Ansible — slurp the
non-secret `.pub` files across the relevant hosts (via `delegate_to`/`hostvars`) and
read config vars (`igp_oracle_pubkey`). Signer pubkeys are public, so this stays
secret-free. ops renders the resulting `watches.json` into
`{deploy_dir}/configmaps/balance-monitor-config/` on the monitoring host. (Where a
keyfile is a Solana keypair JSON with no `.pub`, e.g. `relayer-fee-claim.json`, ops
derives the pubkey with `solana-keygen pubkey`.)

**Operator extras — edit the generated file + restart.** To watch additional
addresses or SPL tokens, the operator edits
`{deploy_dir}/configmaps/balance-monitor-config/watches.json` on the host and runs
`laconic-so deployment <dir> restart`. This works by construction:
`deployment_create.py` only re-copies a ConfigMap from its source dir **if that dir
exists** (`deployment_create.py:1296`); `balance-monitor-config` has none, so
`restart`'s `create(update=True)` step **skips** it (preserving the hand-edit), and
the subsequent `up(force_recreate=True)` re-reads the deploy-dir file and patches the
k8s ConfigMap. **Caveat (documented in the runbook):** a full ops re-deploy of the
monitoring stack regenerates `watches.json` (auto-signers only), so manual extras
must be re-applied after an ansible redeploy. Plain SO restarts preserve them.

### deployment-config surface

```yaml
balance_monitor:
  default_threshold: 1.0          # fallback for any auto signer without a role entry
  thresholds:                     # per-role native thresholds for auto signers
    relayer: 5.0
    fee-claim: 2.0
    igp-oracle: 2.0
    validator: 1.0
  check_interval_seconds: 300     # BALANCE_CHECK_INTERVAL
  alert_repeat_seconds: 21600     # ALERT_REPEAT_SECONDS (re-alert cadence while low)
# SLACK_WEBHOOK_URL is a secret, set in the monitoring stack's secret env (not here).
```

## Feature 2 — Configurable intervals

Drive both from the deployment config via the existing `spec_token_renders` path:

- **Fee-claim:** in `spec-relayer.yml` ×3, replace the commented line with a real
  `CLAIM_INTERVAL_SECONDS: "__CLAIM_INTERVAL_SECONDS__"`; render from deployment-config
  `fee_claim_interval_seconds` (default `21600`).
- **Gas-oracle:** in `spec-gas-oracle.yml` ×3, change `GAS_ORACLE_INTERVAL_MS: "900000"`
  to `"__GAS_ORACLE_INTERVAL_MS__"`; render from deployment-config
  `gas_oracle_interval_ms` (default `900000`).
- Add both tokens to `spec_token_renders` in all three `group_vars/all.yml`, with
  defaults so omitting the deployment-config keys preserves today's behavior.
- **e2e fixtures keep literal interval values** (e2e doesn't render via ops).

## Files to change (keep-in-sync)

**Monitor + monitoring stack:**
- `stack_orchestrator/data/config/balance-monitor-scripts-config/check-balance.py` —
  rewrite around `watches.json`, SPL support, Slack delivery + anti-spam; drop gauge push.
- `stack_orchestrator/data/compose/docker-compose-hyperlane-monitoring.yml` — drop
  `MONITORED_WALLETS_*`/`BALANCE_THRESHOLD_SOL` and the `pushgateway` service; add
  `balance-monitor-config` volume, `SLACK_WEBHOOK_URL`, `ALERT_REPEAT_SECONDS`.
- `stack_orchestrator/data/config/prometheus-config/prometheus.yml` — drop pushgateway
  scrape job. `…/alerts.yml` — drop `WalletBalanceLow`.
- `stack_orchestrator/data/config/grafana-dashboards-config/hyperlane-overview.json` —
  drop the balance panel.
- `deployment/spec-monitoring.yml` + `deployment/{staging,local}/spec-monitoring.yml` —
  drop wallet/threshold config, add `ALERT_REPEAT_SECONDS`, add `SLACK_WEBHOOK_URL`
  secret + `balance-monitor-config` configmap.
- `tests/e2e/fixtures/test-spec-monitoring.yml`, `tests/e2e/conftest.py`,
  `tests/e2e/test_08_monitoring.py`, `tests/e2e/test_05_validator.py` — drop metric
  assertions; build `watches.json` (incl. an SPL entry); add mock-Slack assertion
  (one POST when a wallet is underfunded, none when funded).

**Intervals:**
- `deployment/spec-relayer.yml` + `deployment/{staging,local}/spec-relayer.yml` —
  real `CLAIM_INTERVAL_SECONDS` token.
- `deployment/spec-gas-oracle.yml` + `deployment/{staging,local}/spec-gas-oracle.yml` —
  `GAS_ORACLE_INTERVAL_MS` token.

**Ops:**
- New task to assemble + render `watches.json` (slurp `.pub` cross-host, derive
  keypair pubkeys, apply thresholds) into the monitoring configmap, wired into the
  monitoring deploy path.
- `ops/inventories/{prod,staging,local}/group_vars/all.yml` — add the two interval
  tokens to `spec_token_renders` (+ defaults); add `SLACK_WEBHOOK_URL` to
  `hyperlane-monitoring` `stack_env_vars` secrets.
- `ops/.../deployment-config.example.yml` (local/prod/staging) — add `balance_monitor:`
  block + `fee_claim_interval_seconds` / `gas_oracle_interval_ms` + the
  `SLACK_WEBHOOK_URL` secret note.

**Docs:**
- `stack_orchestrator/data/stacks/hyperlane-monitoring/README.md` — rewrite the
  balance section (Slack, watches.json, no metrics).
- `ops/runbooks/*` — add "monitoring & balance alerts": how `watches.json` is
  generated, the edit-file + `laconic-so restart` flow for extras (with the
  redeploy-clobbers caveat + an SPL example), and the Slack webhook secret.
- `docs/stack-specifications.md`, `docs/e2e-test-spec.md` — reflect Slack-only
  monitoring + the new config.

## Testing

- **Monitor unit-ish:** parse `watches.json`; native vs SPL balance read (mockable
  RPC); threshold compare; Slack payload built once per cycle batching breaches;
  anti-spam state transitions (breach → repeat after interval → recovery); empty
  `SLACK_WEBHOOK_URL` → no POST.
- **e2e (`test_08_monitoring.py`):** conftest writes `watches.json` (a funded wallet
  + an underfunded wallet, incl. one SPL entry) and points `SLACK_WEBHOOK_URL` at a
  mock; assert exactly one alert POST for the underfunded wallet and none for the
  funded one; assert the loop starts and reads the config. Remove Pushgateway/metric
  assertions here and in `test_05_validator.py`.
- **Static:** shellcheck (none new), ansible-lint + syntax-check on the new task and
  edited group_vars, `check-spec-parity`, `test_env_contract` / `test_prod_env`.

## Out of scope

- Alertmanager / Grafana unified alerting (Slack webhook is the delivery path).
- Monitoring chains beyond gorchain/solana (the chain→RPC map is extensible but only
  these two are wired).
- Monitoring the IGP beneficiary balance (it is a sink, not a signer).
