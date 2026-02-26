# hyperlane-gas-oracle

Automated gas oracle service for Hyperlane SVM IGP. Fetches token prices, computes exchange rates, and submits `SetGasOracleConfigs` transactions via Privy server wallet (Ed25519).

Designed to run as a Kubernetes CronJob in the `hyperlane-svm-agents` stack.

## How it works

1. Fetches current USD prices for both chain native tokens (CoinGecko API)
2. Computes token exchange rates scaled by 1e10 (IGP format)
3. Builds `SetGasOracleConfigs` instructions for both chains' IGP programs
4. Signs and submits transactions using Privy's Solana wallet API

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `PRIVY_APP_ID` | Yes | Privy application ID |
| `PRIVY_APP_SECRET` | Yes | Privy application secret |
| `PRIVY_ORACLE_WALLET_ID` | Yes | Privy server wallet ID (IGP owner) |
| `GORCHAIN_RPC_URL` | Yes | Gorchain RPC endpoint |
| `SOLANA_RPC_URL` | Yes | Solana RPC endpoint |
| `GORCHAIN_IGP_PROGRAM_ID` | Yes | IGP program address on Gorchain (base58) |
| `SOLANA_IGP_PROGRAM_ID` | Yes | IGP program address on Solana (base58) |
| `GORCHAIN_DOMAIN_ID` | Yes | Gorchain Hyperlane domain ID |
| `SOLANA_DOMAIN_ID` | Yes | Solana Hyperlane domain ID |
| `PRICE_FEED_URL` | No | Price API base URL (default: CoinGecko) |
| `GORCHAIN_TOKEN_ID` | No | CoinGecko token ID for Gorchain (default: `solana`) |
| `SOLANA_TOKEN_ID` | No | CoinGecko token ID for Solana (default: `solana`) |
| `MAX_PRICE_DEVIATION` | No | Max allowed price change fraction (default: `0.5`) |
| `FALLBACK_GORCHAIN_PRICE_USD` | No | Static fallback price if feed unavailable |
| `FALLBACK_SOLANA_PRICE_USD` | No | Static fallback price if feed unavailable |
| `GORCHAIN_GAS_PRICE` | No | Gas price for Gorchain in destination units (default: `1`) |
| `SOLANA_GAS_PRICE` | No | Gas price for Solana in destination units (default: `1`) |

## Docker

```bash
docker build -t hyperlane-gas-oracle .
docker run --env-file .env hyperlane-gas-oracle
```

## K8s CronJob Example

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: gas-oracle
spec:
  schedule: "*/15 * * * *"
  jobTemplate:
    spec:
      template:
        spec:
          containers:
          - name: gas-oracle
            image: git.vdb.to/laconicnetwork/hyperlane-gas-oracle:local
            envFrom:
            - secretRef:
                name: privy-oracle-credentials
            - configMapRef:
                name: hyperlane-chain-config
          restartPolicy: OnFailure
```
