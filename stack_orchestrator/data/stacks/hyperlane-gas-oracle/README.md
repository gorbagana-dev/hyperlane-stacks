# hyperlane-gas-oracle

Periodically updates IGP (Interchain Gas Paymaster) gas oracle configurations on both Gorchain and Solana. Uses Privy wallet signing to submit on-chain transactions.

## Prerequisites

- A running `k8s-kind` cluster
- `laconic-so` (stack-orchestrator) installed
- `hyperlane-svm-deployer` stack deployed (needs IGP program IDs)

## 1. Build container

```bash
laconic-so --stack hyperlane-gas-oracle build-containers
```

Builds `gorbagana-dev/hyperlane-gas-oracle:local`.

## 2. Create deployment

```bash
laconic-so --stack hyperlane-gas-oracle deploy init --output gas-oracle-spec.yml
```

Edit `gas-oracle-spec.yml` (see `deployment/spec-gas-oracle.yml` for reference):

```yaml
stack: stack_orchestrator/data/stacks/hyperlane-gas-oracle
deploy-to: k8s-kind
config:
  GORCHAIN_RPC_URL: "https://gorchain-rpc.example.com"
  SOLANA_RPC_URL: "https://solana-rpc.example.com"
  GORCHAIN_IGP_PROGRAM_ID: "<program-id>"
  SOLANA_IGP_PROGRAM_ID: "<program-id>"
  GORCHAIN_DOMAIN_ID: "99999"
  SOLANA_DOMAIN_ID: "99998"
  GAS_ORACLE_INTERVAL_MS: "900000"
secrets:
  hyperlane-gas-oracle-secrets:
    - PRIVY_APP_ID
    - PRIVY_APP_SECRET
    - PRIVY_ORACLE_WALLET_ID
```

```bash
laconic-so --stack hyperlane-gas-oracle deploy create --spec-file gas-oracle-spec.yml --deployment-dir gas-oracle-deployment
```

## 3. Create secrets

```bash
kubectl create secret generic hyperlane-gas-oracle-secrets \
  --from-literal=PRIVY_APP_ID='<app-id>' \
  --from-literal=PRIVY_APP_SECRET='<app-secret>' \
  --from-literal=PRIVY_ORACLE_WALLET_ID='<wallet-id>'
```

| Secret key | Description |
|---|---|
| `PRIVY_APP_ID` | Privy application ID |
| `PRIVY_APP_SECRET` | Privy application secret |
| `PRIVY_ORACLE_WALLET_ID` | Privy wallet ID for oracle transaction signing |

## 4. Start

```bash
laconic-so deployment --dir gas-oracle-deployment start
```

## 5. Verify

```bash
# Check pod is running
kubectl get pods -l app=hyperlane-gas-oracle

# Check oracle is updating gas prices
kubectl logs -l app=hyperlane-gas-oracle --tail=50
```
