# hyperlane-monitoring

Monitoring stack for the Hyperlane SVM bridge. Deploys as a single pod with four containers:

- **Prometheus** — scrapes metrics from validator and relayer pods via `kubernetes_sd_configs`, and from Pushgateway for balance metrics
- **Grafana** — dashboards for bridge operations, validator checkpoints, relayer throughput, and wallet balances
- **Pushgateway** — receives balance metrics pushed by the balance monitor
- **Balance monitor** — Python script that polls wallet balances via Solana JSON-RPC and pushes to Pushgateway

## How it works

```
                      ┌─────────────────────────────────────────────┐
                      │            monitoring pod                   │
                      │                                             │
  validator pods ────►│  Prometheus ◄── Pushgateway ◄── balance    │
  relayer pod   ────►│  (scrapes)      (receives)      monitor    │
                      │      │                          (polls RPC)│
                      │      ▼                                     │
                      │  Grafana (dashboards)                      │
                      └─────────────────────────────────────────────┘
```

**Metrics flow:**

1. **Validator/relayer metrics**: Prometheus discovers pods with `prometheus.io/scrape: "true"` annotation via `kubernetes_sd_configs` and scrapes their `/metrics` endpoints directly
2. **Wallet balances**: The balance monitor queries Solana JSON-RPC (`getBalance`) for each configured wallet, then pushes `hyperlane_wallet_balance_sol` gauge metrics to Pushgateway. Prometheus scrapes Pushgateway on `localhost:9091`
3. **Grafana**: Queries Prometheus as its datasource. Three dashboards are provisioned automatically: overview, validator detail, and relayer detail

**Prerequisites:**
- Validator and relayer specs must include `prometheus.io/scrape` and `prometheus.io/port` annotations (see `deployment/spec-validator-*.yml` and `deployment/spec-relayer.yml`)
- The `deploy/commands.py` create hook applies RBAC (ClusterRole + ClusterRoleBinding) granting the namespace's default ServiceAccount permission to list/watch pods for Prometheus service discovery

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
