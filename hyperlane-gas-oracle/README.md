# hyperlane-gas-oracle

Automated gas oracle service for Hyperlane SVM IGP. Fetches token prices, computes exchange rates using `@hyperlane-xyz/sdk`, and submits `SetGasOracleConfigs` transactions.

Supports two signing modes:
- **Privy** (production): Signs via Privy server wallet using `signTransaction` (sign-only), then submits to the configured RPC. This gives the oracle full control over which RPC receives the transaction.
- **Keypair** (testing): Signs with a local Solana keypair.

## How it works

1. Fetches current USD prices for both chain native tokens (CoinGecko API or configurable endpoint)
2. Converts sGOR price to gGOR (Gorchain native token) via configurable multiplier (default ×100, since 1 gGOR = 100 sGOR)
3. Computes token exchange rates using `@hyperlane-xyz/sdk` `getLocalStorageGasOracleConfig()` — handles 1e19 Sealevel exchange rate scaling, configurable margin, and gas price conversion
4. Builds `SetGasOracleConfigs` instructions using SDK Borsh serialization (correct discriminator, account ordering, and Option/enum wrappers)
5. Signs via Privy `signTransaction` or local keypair, then submits to the chain's RPC
6. Optionally runs in a loop (`RUN_LOOP=true`) for continuous updates
7. Skips updates when `PRICE_FEED_URL` is not set (empty string)

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SIGNER_MODE` | No | `privy` | Signing mode: `privy` or `keypair` |
| `PRIVY_APP_ID` | Privy mode | — | Privy application ID |
| `PRIVY_APP_SECRET` | Privy mode | — | Privy application secret |
| `PRIVY_ORACLE_WALLET_ID` | Privy mode | — | Privy server wallet ID (Ed25519, IGP account owner) |
| `PRIVY_API_URL` | No | `https://auth.privy.io/api/v1` | Privy API base URL (override for mock server in tests) |
| `ORACLE_KEYPAIR` | Keypair mode | — | JSON keypair array (e.g. `[1,2,3,...,64]`) |
| `GORCHAIN_RPC_URL` | Yes | — | Gorchain RPC endpoint |
| `SOLANA_RPC_URL` | Yes | — | Solana RPC endpoint |
| `GORCHAIN_IGP_PROGRAM_ID` | Yes | — | IGP program address on Gorchain (base58) |
| `SOLANA_IGP_PROGRAM_ID` | Yes | — | IGP program address on Solana (base58) |
| `GORCHAIN_DOMAIN_ID` | Yes | — | Gorchain Hyperlane domain ID |
| `SOLANA_DOMAIN_ID` | Yes | — | Solana Hyperlane domain ID |
| `PRICE_FEED_URL` | No | *(empty)* | CoinGecko-compatible API base URL. Empty = skip oracle updates. |
| `GORCHAIN_TOKEN_ID` | No | `gorbagana` | CoinGecko token ID for sGOR |
| `SOLANA_TOKEN_ID` | No | `solana` | CoinGecko token ID for SOL |
| `GAS_PRICE` | No | `0.000005` | Gas price in decimal SOL (= 5000 lamports) |
| `GAS_OVERHEAD` | No | `200000` | Destination gas overhead in compute units |
| `EXCHANGE_RATE_MARGIN_PCT` | No | `10` | Exchange rate safety margin percentage |
| `GORCHAIN_NATIVE_TOKEN_MULTIPLIER` | No | `100` | sGOR → gGOR conversion factor (1 gGOR = 100 sGOR) |
| `MAX_PRICE_DEVIATION` | No | `0.5` | Max allowed price change fraction between updates (50%) |
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

## Computing Initial Gas Oracle Values

The deployer sets initial gas oracle values from `gas-oracle-configs.json`. Use the included script to compute values from current market prices:

```bash
cd hyperlane-gas-oracle
npm install

# Fetch live prices from CoinGecko and print the config JSON
npm run compute-config

# Write directly to the deployer config file
npm run compute-config -- --write

# Override prices manually (skip CoinGecko fetch)
npm run compute-config -- --sgor-price 0.10 --sol-price 150

# Override parameters
npm run compute-config -- --margin 10 --gas-price 0.000005 --overhead 200000
```

The exchange rate formula (Sealevel 1e19 scale):
```
tokenExchangeRate = (localTokenPrice / remoteTokenPrice) × 1e19 × (1 + margin/100)
gasPrice = SOL amount × 1e9 (lamports)
```

The oracle service will overwrite these values on its first successful run with live prices.
