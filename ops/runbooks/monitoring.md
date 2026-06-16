# Monitoring & balance alerts

The `hyperlane-monitoring` stack runs Prometheus + Grafana (agent metrics) and a
balance monitor that posts low-balance alerts to Slack.

## Slack alerts

Set `slack_webhook_url` in `deployment-config.yml` to a Slack incoming-webhook URL.
Leaving it empty disables alerting (the monitor still runs and logs). Tune
thresholds and cadence under `balance_monitor:` (see `deployment-config.example.yml`).

## What is watched automatically

`deploy-all` renders `watches.json` from the bridge's own signers, with native
thresholds from `balance_monitor.thresholds` (fallback `default_threshold`):

- relayer (gorchain + solana)
- fee-claim (gorchain + solana)
- igp-oracle (gorchain + solana)
- each validator (its chain)

The IGP beneficiary is a fee sink and is intentionally not watched.

## Adding extra or SPL token watches

Edit the generated file on the monitoring host and restart the stack:

```bash
# on the monitoring host
$EDITOR ~/deployments/hyperlane-monitoring/configmaps/balance-monitor-config/watches.json
laconic-so deployment ~/deployments/hyperlane-monitoring restart
```

Add entries like:

```json
{ "chain": "solana", "label": "treasury", "address": "<pubkey>",
  "tokens": [ { "symbol": "USDC", "mint": "EPjF...", "threshold": 1000 },
              { "symbol": "SOL",  "mint": "native", "threshold": 10 } ] }
```

`mint: "native"` watches the gas token; any other `mint` watches that SPL token's
balance for the account (summed across its token accounts).

**Caveat:** a full `deploy-all` (or any ops redeploy of monitoring) regenerates
`watches.json` from the auto-signers only — re-apply manual entries afterwards.
Plain `laconic-so … restart` preserves your edits.
