# hyperlane-monitoring

Monitoring stack for the Hyperlane SVM bridge. Deploys as a single pod with four containers:

- **Prometheus** — scrapes validator and relayer `/metrics` over their public Caddy hostnames (targets configured in the spec), and Pushgateway for balance metrics
- **Grafana** — dashboards for bridge operations, validator checkpoints, relayer throughput, and wallet balances
- **Pushgateway** — receives balance metrics pushed by the balance monitor
- **Balance monitor** — Python script that polls wallet balances via Solana JSON-RPC and pushes to Pushgateway

## How it works

```
                      ┌─────────────────────────────────────────────┐
                      │            monitoring pod                   │
                      │                                             │
  validator pods ────►│  Prometheus ◄── Pushgateway ◄── balance     │
  relayer pod    ────►│  (scrapes)      (receives)      monitor     │
                      │      │                          (polls RPC) │
                      │      ▼                                      │
                      │  Grafana (dashboards)                       │
                      └─────────────────────────────────────────────┘
```

**Metrics flow:**

1. **Validator/relayer metrics**: Prometheus scrapes each validator/relayer `/metrics` endpoint over its public Caddy hostname. Targets are declared in the spec (`PROMETHEUS_VALIDATOR_TARGETS` / `PROMETHEUS_RELAYER_TARGETS`) and rendered to `validators.yml` / `relayer.yml` by the stack's `deploy/commands.py` hook; the `validators` / `relayer` jobs in `prometheus.yml` consume them via `file_sd_configs`. Each target carries a `hyperlane_instance` label so multiple validators (including two on the same chain) appear as distinct series. Prometheus watches the rendered files and reloads targets without a restart, so a validator can be added to a live deployment by appending one entry and re-running deploy.
2. **Wallet balances**: The balance monitor queries Solana JSON-RPC (`getBalance`) for each configured wallet, then pushes `hyperlane_wallet_balance_sol` gauge metrics to Pushgateway. Prometheus scrapes Pushgateway on `localhost:9091`
3. **Grafana**: Queries Prometheus as its datasource. Three dashboards are provisioned automatically: overview, validator detail, and relayer detail

**Prerequisites:**
- Validator and relayer specs must expose `/metrics` via a `network.http-proxy` route (see `deployment/spec-validator-*.yml` and `deployment/spec-relayer.yml`), and their hostnames must be listed in `PROMETHEUS_VALIDATOR_TARGETS` / `PROMETHEUS_RELAYER_TARGETS` in this stack's spec

## Configuration

### Wallet format

Wallets are specified as comma-separated `label:address` or `label:address:threshold` entries:

```
relayer:ABC123:5.0,igp-oracle:DEF456:2.0,deployer:GHI789
```

- Per-wallet threshold (third field) is optional
- Wallets without a threshold use the global `BALANCE_THRESHOLD_SOL` as fallback
- Set higher thresholds for critical wallets (relayer) and lower for less critical ones

### Environment variables

| Variable | Description | Default |
|----------|-------------|---------|
| `GORCHAIN_RPC_URL` | Gorchain Solana-compatible RPC endpoint | — |
| `SOLANA_RPC_URL` | Solana RPC endpoint | — |
| `MONITORED_WALLETS_GORCHAIN` | Wallets to monitor on Gorchain | — |
| `MONITORED_WALLETS_SOLANA` | Wallets to monitor on Solana | — |
| `BALANCE_THRESHOLD_SOL` | Default low-balance warning threshold (SOL) | `1.0` |
| `BALANCE_CHECK_INTERVAL` | Seconds between balance checks | `300` |
| `PROMETHEUS_VALIDATOR_TARGETS` | Validator scrape targets, `instance=host:port` comma-separated | — |
| `PROMETHEUS_RELAYER_TARGETS` | Relayer scrape targets, `instance=host:port` comma-separated | — |

### Secrets

| Secret key | Description |
|------------|-------------|
| `GF_SECURITY_ADMIN_PASSWORD` | Grafana admin password |

## Deployment

### 1. Initialize spec

```bash
laconic-so --stack hyperlane-monitoring deploy init --output monitoring-spec.yml
```

The generated spec includes `http-proxy` defaults for Grafana (`grafana.example.com`) and Prometheus (`prometheus.example.com`). Edit the hostnames and wallet addresses.

### 2. Create secrets

```bash
kubectl create secret generic hyperlane-monitoring-secrets \
  --from-literal=GF_SECURITY_ADMIN_PASSWORD='<grafana-password>'
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

- **Hyperlane Overview** — wallet balances, latest checkpoints, message throughput
- **Hyperlane Validator** — per-chain validator checkpoint progress, block height, signing activity
- **Hyperlane Relayer** — message processing rates, gas usage, delivery status
