# hyperlane-monitoring

Prometheus, Grafana, pushgateway, and balance monitoring for the Hyperlane deployment. Scrapes metrics from validator and relayer pods, monitors wallet balances, and provides dashboards.

## 1. Create deployment

```bash
laconic-so --stack hyperlane-monitoring deploy init --output monitoring-spec.yml
```

Edit `monitoring-spec.yml` (see `deployment/spec-monitoring.yml` for reference):

```yaml
stack: stack_orchestrator/data/stacks/hyperlane-monitoring
deploy-to: k8s-kind
config:
  GORCHAIN_RPC_URL: "https://gorchain-rpc.example.com"
  SOLANA_RPC_URL: "https://solana-rpc.example.com"
  MONITORED_WALLETS_GORCHAIN: "validator:Abc123,relayer:Def456"
  MONITORED_WALLETS_SOLANA: "validator:Abc123,relayer:Def456"
  BALANCE_THRESHOLD_SOL: "1.0"
  BALANCE_CHECK_INTERVAL: "300"
network:
  ports:
    prometheus:
      - "9090"
    grafana:
      - "3000"
volumes:
  prometheus-data: 10Gi
  grafana-data: 2Gi
configmaps:
  prometheus-config: ./configmaps/prometheus-config
  grafana-datasources-config: ./configmaps/grafana-datasources-config
  grafana-dashboard-config: ./configmaps/grafana-dashboard-config
  grafana-dashboards-config: ./configmaps/grafana-dashboards-config
  balance-monitor-scripts-config: ./configmaps/balance-monitor-scripts-config
secrets:
  hyperlane-monitoring-secrets:
    - GF_SECURITY_ADMIN_PASSWORD
```

```bash
laconic-so --stack hyperlane-monitoring deploy create --spec-file monitoring-spec.yml --deployment-dir monitoring-deployment
```

## 2. Populate config files

Edit the config templates in `monitoring-deployment/configmaps/`:

| ConfigMap directory | Contents |
|---|---|
| `prometheus-config/` | `prometheus.yml`, `alerts.yml` -- scrape targets and alert rules |
| `grafana-datasources-config/` | `datasources.yaml` -- Prometheus datasource definition |
| `grafana-dashboard-config/` | `dashboards.yaml` -- dashboard provisioning config |
| `grafana-dashboards-config/` | JSON dashboard files |
| `balance-monitor-scripts-config/` | `check-balance.py` -- balance monitoring script |

## 3. Create secrets

```bash
kubectl create secret generic hyperlane-monitoring-secrets \
  --from-literal=GF_SECURITY_ADMIN_PASSWORD='<grafana-password>'
```

| Secret key | Description |
|---|---|
| `GF_SECURITY_ADMIN_PASSWORD` | Grafana admin password |

## 4. Start

```bash
laconic-so deployment --dir monitoring-deployment start
```

## 5. Verify

```bash
# Check pods are running
kubectl get pods -l app=hyperlane-monitoring

# Check Prometheus targets
kubectl port-forward svc/prometheus 9090:9090
# Open http://localhost:9090/targets

# Check Grafana
kubectl port-forward svc/grafana 3000:3000
# Open http://localhost:3000 (admin / <password>)
```
