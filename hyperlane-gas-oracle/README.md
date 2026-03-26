# hyperlane-gas-oracle

Automated gas oracle service for Hyperlane SVM IGP. Fetches token prices, computes exchange rates using `@hyperlane-xyz/sdk`, and submits `SetGasOracleConfigs` transactions.

Supports two signing modes:
- **Privy** (production): Signs via Privy server wallet (Ed25519)
- **Keypair** (testing): Signs with a local Solana keypair

## How it works

1. Fetches current USD prices for both chain native tokens (CoinGecko API or configurable endpoint)
2. Computes token exchange rates using `@hyperlane-xyz/sdk` `getLocalStorageGasOracleConfig()` (1e19 Sealevel scale, with configurable margin)
3. Builds `SetGasOracleConfigs` instructions using SDK Borsh serialization
4. Signs and submits transactions via Privy or local keypair
5. Optionally runs in a loop (`RUN_LOOP=true`) for continuous updates

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SIGNER_MODE` | No | `privy` | Signing mode: `privy` or `keypair` |
| `PRIVY_APP_ID` | Privy mode | — | Privy application ID |
| `PRIVY_APP_SECRET` | Privy mode | — | Privy application secret |
| `PRIVY_ORACLE_WALLET_ID` | Privy mode | — | Privy server wallet ID (IGP owner) |
| `ORACLE_KEYPAIR` | Keypair mode | — | JSON keypair array (e.g. `[1,2,3,...,64]`) |
| `GORCHAIN_RPC_URL` | Yes | — | Gorchain RPC endpoint |
| `SOLANA_RPC_URL` | Yes | — | Solana RPC endpoint |
| `GORCHAIN_IGP_PROGRAM_ID` | Yes | — | IGP program address on Gorchain (base58) |
| `SOLANA_IGP_PROGRAM_ID` | Yes | — | IGP program address on Solana (base58) |
| `GORCHAIN_DOMAIN_ID` | Yes | — | Gorchain Hyperlane domain ID |
| `SOLANA_DOMAIN_ID` | Yes | — | Solana Hyperlane domain ID |
| `PRICE_FEED_URL` | No | CoinGecko | Price API base URL |
| `GORCHAIN_TOKEN_ID` | No | `gorbagana` | CoinGecko token ID for Gorchain (sGOR) |
| `SOLANA_TOKEN_ID` | No | `solana` | CoinGecko token ID for Solana |
| `GAS_PRICE` | No | `0.000005` | Gas price in decimal SOL (= 5000 lamports) |
| `GAS_OVERHEAD` | No | `200000` | Destination gas overhead in compute units |
| `EXCHANGE_RATE_MARGIN_PCT` | No | `10` | Exchange rate safety margin percentage |
| `GORCHAIN_NATIVE_TOKEN_MULTIPLIER` | No | `100` | sGOR → gGOR conversion factor |
| `MAX_PRICE_DEVIATION` | No | `0.5` | Max allowed price change fraction (50%) |
| `FALLBACK_GORCHAIN_PRICE_USD` | No | `0` | Static fallback price if feed unavailable |
| `FALLBACK_SOLANA_PRICE_USD` | No | `0` | Static fallback price if feed unavailable |
| `RUN_LOOP` | No | `false` | Enable continuous loop mode |
| `GAS_ORACLE_INTERVAL_MS` | No | `900000` | Loop interval in milliseconds (15 min) |

## Build & Run

```bash
# Build
npm install && npm run build

# Run (one-shot)
node dist/index.js

# Run (loop mode)
RUN_LOOP=true node dist/index.js

# Docker
docker build -t gorbagana-dev/hyperlane-gas-oracle:local .
docker run --env-file .env gorbagana-dev/hyperlane-gas-oracle:local
```

## Deployment via laconic-so

```bash
laconic-so --stack hyperlane-gas-oracle build-containers
laconic-so --stack hyperlane-gas-oracle deploy init --output spec-gas-oracle.yml
# Edit spec-gas-oracle.yml with chain config and secrets
laconic-so --stack hyperlane-gas-oracle deploy create --spec-file spec-gas-oracle.yml
laconic-so --stack hyperlane-gas-oracle deploy start
```
