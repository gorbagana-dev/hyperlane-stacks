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
| `PRIVY_API_URL` | No | `https://api.privy.io/v1` | Privy API base URL (override for mock server in tests) |
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
| `GAS_PRICE` | No | `0.000000001` | Raw gas price in decimal SOL per gas unit (before min cost floor) |
| `GAS_OVERHEAD` | No | `200000` | Destination gas overhead in compute units |
| `EXCHANGE_RATE_MARGIN_PCT` | No | `10` | Exchange rate safety margin percentage |
| `MIN_USD_COST` | No | `0.50` | Minimum USD cost floor per message (0 = disabled) |
| `GORCHAIN_NATIVE_TOKEN_MULTIPLIER` | No | `100` | sGOR → gGOR conversion factor (1 gGOR = 100 sGOR) |
| `MAX_PRICE_DEVIATION` | No | `0.5` | Max allowed price change fraction between updates (50%) |
| `RUN_LOOP` | No | `false` | Enable continuous loop mode |
| `GAS_ORACLE_INTERVAL_MS` | No | `900000` | Loop interval in milliseconds (15 min) |

## Build & Run

```bash
# Build
yarn install && yarn build

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
yarn install

# Fetch live prices from CoinGecko and print the config JSON
yarn compute-config

# Write directly to the deployer config file
yarn compute-config --write

# Override prices manually (skip CoinGecko fetch)
yarn compute-config --sgor-price 0.10 --sol-price 150

# Override parameters
yarn compute-config --margin 10 --gas-price 0.000000001 --overhead 200000

# Disable min USD cost floor (use raw gasPrice)
yarn compute-config --min-usd-cost 0
```

The script uses the same `computeOracleConfigs()` function as the oracle service, including the min USD cost floor, so the initial deployer config will match what the service computes on its first run.

The exchange rate formula (Sealevel 1e19 scale):
```
tokenExchangeRate = (localTokenPrice / remoteTokenPrice) × 1e19 × (1 + margin/100)
gasPrice = adjusted by min USD cost floor (or raw: SOL amount × 1e9 lamports)
```

The oracle service will overwrite these values on its first successful run with live prices.

## Gas Price Calibration for SVM Chains

The on-chain IGP fee quote formula is:

```
quote = (gasAmount + overhead) × gasPrice × tokenExchangeRate / 1e19
```

Where:
- **gasAmount** is set per warp route token program during deployment (hardcoded EVM defaults: 44k native, 64k synthetic, 68k collateral)
- **overhead** is set on the Overhead IGP during `igp configure` (from `gas-oracle-configs.json`)
- **gasPrice** and **tokenExchangeRate** are set on the IGP by this oracle service (or statically by the deployer)

### The EVM gas amount problem

The `gasAmount` values (44–68k) originate from EVM gas accounting where each unit costs `gasPrice` wei. In SVM (Solana/Gorchain), transaction fees are a flat ~5000 lamports regardless of compute — there is no per-unit gas metering. The Hyperlane Rust client has a TODO acknowledging this:

```rust
// TODO: note these are the amounts specific to the EVM.
// We should eventually make this configurable per protocol type
// before we enforce gas amounts to Sealevel chains.
```

### Minimum USD cost floor

To prevent absurdly low or high IGP quotes, the oracle applies a **minimum USD cost floor** (default `$0.50`, matching Hyperlane production). When the natural quote for a typical message falls below this floor, `gasPrice` is automatically increased to hit the target.

This matches the production behavior in `hyperlane-monorepo/typescript/infra/src/config/gas-oracle.ts`. The modifier:

1. Estimates the IGP quote for a typical message: `(gasAmount + overhead) × gasPrice × exchangeRate / 1e19`
2. Converts to USD using the local token price
3. If below `MIN_USD_COST`, back-computes a new `gasPrice` that produces exactly the floor amount

The raw `GAS_PRICE` env var (default `0.000000001` SOL = 1 lamport) serves as a starting point. For most gGOR/SOL price ratios, the min cost floor will override it upward.

### Why the floor matters for gGOR/SOL

Gorchain's native token gGOR is much cheaper than SOL. Without the floor, a raw gasPrice of 1 lamport produces very cheap quotes:

```
With gasPrice=1 (raw, no floor):
  (68000 + 200000) × 1 × 7.67e21 / 1e19 = 0.21 gGOR ≈ $0.04

With min USD cost floor ($0.50):
  gasPrice is auto-increased to ~133,500 so that the quote ≈ $0.50
```

Set `MIN_USD_COST=0` to disable the floor and use the raw gasPrice directly.
