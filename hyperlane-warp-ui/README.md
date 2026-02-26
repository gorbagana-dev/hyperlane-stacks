# hyperlane-warp-ui

Docker image for the Hyperlane Warp Route UI, built from [hyperlane-warp-ui-template](https://github.com/hyperlane-xyz/hyperlane-warp-ui-template) at commit `6227c04`.

## Architecture

Next.js inlines `NEXT_PUBLIC_*` env vars at build time. To avoid slow runtime builds, this image uses a **sentinel placeholder** pattern:

1. **Build time:** The Next.js app is built with placeholder values (e.g., `__GORCHAIN_RPC_URL__`) baked into the JS bundles
2. **Runtime:** `entrypoint.sh` uses `sed` to replace all sentinels with real environment variable values before starting the server

This gives instant container startup with full environment configurability.

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `GORCHAIN_RPC_URL` | Gorchain RPC endpoint | `http://localhost:8899` |
| `SOLANA_RPC_URL` | Solana RPC endpoint | `http://localhost:18899` |
| `GORCHAIN_DOMAIN_ID` | Gorchain Hyperlane domain ID | `0` |
| `SOLANA_DOMAIN_ID` | Solana Hyperlane domain ID | `0` |
| `GORCHAIN_CHAIN_NAME` | Gorchain chain name | `gorchain` |
| `SOLANA_CHAIN_NAME` | Solana chain name | `solanatestnet` |
| `GORCHAIN_CHAIN_ID` | Gorchain chain ID | `0` |
| `SOLANA_CHAIN_ID` | Solana chain ID | `0` |
| `WARP_COLLATERAL_ADDRESS` | Collateral token program address | |
| `WARP_SYNTHETIC_ADDRESS` | Synthetic token program address | |
| `NEXT_PUBLIC_WALLET_CONNECT_ID` | WalletConnect project ID | |
| `GORCHAIN_NATIVE_TOKEN_NAME` | Gorchain native token name | `GOR` |
| `GORCHAIN_NATIVE_TOKEN_SYMBOL` | Gorchain native token symbol | `GOR` |
| `GORCHAIN_NATIVE_TOKEN_DECIMALS` | Gorchain native token decimals | `9` |
| `SOLANA_NATIVE_TOKEN_NAME` | Solana native token name | `SOL` |
| `SOLANA_NATIVE_TOKEN_SYMBOL` | Solana native token symbol | `SOL` |
| `SOLANA_NATIVE_TOKEN_DECIMALS` | Solana native token decimals | `9` |

## Docker

```bash
docker build -t hyperlane-warp-ui .

docker run -p 3000:3000 \
  -e GORCHAIN_RPC_URL=https://gorchain.example.com \
  -e SOLANA_RPC_URL=https://api.mainnet-beta.solana.com \
  -e GORCHAIN_DOMAIN_ID=99999 \
  -e SOLANA_DOMAIN_ID=1399811149 \
  -e GORCHAIN_CHAIN_NAME=gorchain \
  -e SOLANA_CHAIN_NAME=solana \
  -e WARP_COLLATERAL_ADDRESS=<address> \
  -e WARP_SYNTHETIC_ADDRESS=<address> \
  -e NEXT_PUBLIC_WALLET_CONNECT_ID=<id> \
  hyperlane-warp-ui
```

## Files

- `Dockerfile` — Multi-stage build (node builder + node runtime)
- `entrypoint.sh` — Sentinel replacement script
- `configs/chains.yaml` — Chain config template with placeholders
- `configs/warpRoutes.yaml` — Warp route config template with placeholders
- `configs/.env.sentinel` — NEXT_PUBLIC env var placeholders
