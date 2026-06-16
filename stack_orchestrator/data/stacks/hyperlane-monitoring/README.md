# hyperlane-monitoring

Monitoring stack for the Hyperlane SVM bridge. Deploys as a single pod with three containers:

- **Prometheus** — scrapes validator and relayer `/metrics` over their public Caddy hostnames (targets configured in the spec)
- **Grafana** — dashboards for bridge operations, validator checkpoints, and relayer throughput
- **Balance monitor** — Python script that checks signer balances via JSON-RPC and posts low-balance alerts to Slack

## How it works

```
                      ┌─────────────────────────────────────────────┐
                      │            monitoring pod                   │
                      │                                             │
  validator pods ────►│  Prometheus      balance monitor ──► Slack  │
  relayer pod    ────►│  (scrapes)       (checks RPC, alerts)       │
                      │      │                                      │
                      │      ▼                                      │
                      │  Grafana (dashboards)                       │
                      └─────────────────────────────────────────────┘
```

**Metrics flow:**

1. **Validator/relayer metrics**: Prometheus scrapes each validator/relayer `/metrics` endpoint over its public Caddy hostname. Targets are declared in the spec (`PROMETHEUS_VALIDATOR_TARGETS` / `PROMETHEUS_RELAYER_TARGETS`); the prometheus container's entrypoint (`run.sh`) renders them to `validators.yml` / `relayer.yml` on each start, and the `validators` / `relayers` jobs in `prometheus.yml` consume them via `file_sd_configs`. Each target carries a `hyperlane_instance` label so multiple validators (including two on the same chain) appear as distinct series. Because rendering happens at container start, adding a validator is an env change plus a restart — no deploy hook or `laconic-so` update step. The scrape scheme is rendered from `PROMETHEUS_SCRAPE_SCHEME` (`https` in prod; the e2e harness sets `http` to scrape pods in-cluster).
2. **Balance monitoring + Slack alerts**: The balance monitor reads
   `/config/watches.json` (the `balance-monitor-config` ConfigMap) and, every
   `BALANCE_CHECK_INTERVAL` seconds, checks each account's native and/or SPL token
   balances against per-token thresholds. When a balance is below threshold it
   POSTs a batched alert to `SLACK_WEBHOOK_URL` (empty disables alerting); it
   re-alerts every `ALERT_REPEAT_SECONDS` while still low and posts a recovery
   message when it climbs back. RPC URLs come from `GORCHAIN_RPC_URL` /
   `SOLANA_RPC_URL` (kept out of the watch file). No Prometheus/Pushgateway metric
   is emitted for balances.
3. **Grafana**: Queries Prometheus as its datasource. Three dashboards are provisioned automatically: overview, validator detail, and relayer detail

**Prerequisites:**
- Validator and relayer specs must expose `/metrics` via a `network.http-proxy` route (see `deployment/spec-validator-*.yml` and `deployment/spec-relayer.yml`), and their hostnames must be listed in `PROMETHEUS_VALIDATOR_TARGETS` / `PROMETHEUS_RELAYER_TARGETS` in this stack's spec

## Configuration

### Watch file (`balance-monitor-config`)

The balance monitor reads `/config/watches.json` from the `balance-monitor-config`
ConfigMap. Schema:

```json
{ "watches": [
  { "chain": "solana", "label": "relayer", "address": "<pubkey>",
    "tokens": [ { "symbol": "SOL", "mint": "native", "threshold": 5 } ] }
] }
```

- `mint: "native"` (or omitted) → native gas balance via `getBalance`
- any other `mint` → that SPL token's balance via `getTokenAccountsByOwner`
  (summed across the account's token accounts; decimals read from RPC)
- `threshold` is the low-balance floor for that token

In ops deployments the file is generated from the bridge's own signers; operators
add extra/SPL watches by editing it on the monitoring host and restarting the stack
(see `ops/runbooks/monitoring.md`).

### Environment variables

| Variable | Description | Default |
|----------|-------------|---------|
| `GORCHAIN_RPC_URL` | Gorchain Solana-compatible RPC endpoint | — |
| `SOLANA_RPC_URL` | Solana RPC endpoint (secret) | — |
| `BALANCE_CHECK_INTERVAL` | Seconds between balance checks | `300` |
| `ALERT_REPEAT_SECONDS` | Re-alert cadence while a balance stays low | `21600` |
| `PROMETHEUS_VALIDATOR_TARGETS` | Validator scrape targets, `instance=host:port` comma-separated | — |
| `PROMETHEUS_RELAYER_TARGETS` | Relayer scrape targets, `instance=host:port` comma-separated | — |

### Secrets

| Secret key | Description |
|------------|-------------|
| `GF_SECURITY_ADMIN_PASSWORD` | Grafana admin password |
| `SLACK_WEBHOOK_URL` | Slack incoming-webhook URL for balance alerts (empty disables alerting) |
| `SOLANA_RPC_URL` | Solana RPC endpoint (Helius URL embeds an API key) |

## Deployment

### 1. Initialize spec

```bash
laconic-so --stack hyperlane-monitoring deploy init --output monitoring-spec.yml
```

The generated spec includes `http-proxy` defaults for Grafana (`grafana.example.com`) and Prometheus (`prometheus.example.com`). Edit the hostnames.

### 2. Create secrets

```bash
kubectl create secret generic hyperlane-monitoring-secrets \
  --from-literal=GF_SECURITY_ADMIN_PASSWORD='<grafana-password>' \
  --from-literal=SLACK_WEBHOOK_URL='<slack-webhook-url>' \
  --from-literal=SOLANA_RPC_URL='<solana-rpc-url>'
```

### 3. Deploy

```bash
laconic-so --stack hyperlane-monitoring deploy create \
  --spec-file monitoring-spec.yml \
  --deployment-dir monitoring-deployment

laconic-so deployment --dir monitoring-deployment start
```

### 4. Verify

```bash
# Check pod is running
kubectl get pods -l app=hyperlane-monitoring

# Prometheus targets (should show validator + relayer as "up")
curl https://prometheus.example.com/api/v1/targets

# Grafana
# Open https://grafana.example.com (admin / <password>)
```

## Dashboards

Three dashboards are provisioned automatically:

- **Hyperlane Overview** — latest checkpoints, message throughput
- **Hyperlane Validator** — per-chain validator checkpoint progress, block height, signing activity
- **Hyperlane Relayer** — message processing rates, gas usage, delivery status
