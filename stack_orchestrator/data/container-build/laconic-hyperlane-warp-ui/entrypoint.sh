#!/bin/sh
set -e

# Validate required environment variables
missing=""
for var in GORCHAIN_RPC_URL SOLANA_RPC_URL GORCHAIN_MAILBOX SOLANA_MAILBOX \
           WARP_COLLATERAL_ADDRESS WARP_SYNTHETIC_ADDRESS; do
  eval val=\$$var
  if [ -z "$val" ]; then
    missing="$missing $var"
  fi
done
if [ -n "$missing" ]; then
  echo "ERROR: Required environment variables not set:$missing" >&2
  exit 1
fi

echo "Substituting sentinel placeholders with environment values..."

# Replace sentinels in all JS/JSON bundles under .next/
find /app/.next -type f \( -name '*.js' -o -name '*.json' \) -exec sed -i \
  -e "s|__GORCHAIN_RPC_URL__|${GORCHAIN_RPC_URL:-http://localhost:8899}|g" \
  -e "s|__SOLANA_RPC_URL__|${SOLANA_RPC_URL:-http://localhost:18899}|g" \
  -e "s|__GORCHAIN_DOMAIN_ID__|${GORCHAIN_DOMAIN_ID:-0}|g" \
  -e "s|__SOLANA_DOMAIN_ID__|${SOLANA_DOMAIN_ID:-0}|g" \
  -e "s|__GORCHAIN_CHAIN_NAME__|${GORCHAIN_CHAIN_NAME:-gorchain}|g" \
  -e "s|__SOLANA_CHAIN_NAME__|${SOLANA_CHAIN_NAME:-solana}|g" \
  -e "s|__GORCHAIN_CHAIN_ID__|${GORCHAIN_CHAIN_ID:-0}|g" \
  -e "s|__SOLANA_CHAIN_ID__|${SOLANA_CHAIN_ID:-0}|g" \
  -e "s|__WARP_COLLATERAL_ADDRESS__|${WARP_COLLATERAL_ADDRESS:-}|g" \
  -e "s|__WARP_SYNTHETIC_ADDRESS__|${WARP_SYNTHETIC_ADDRESS:-}|g" \
  -e "s|__WARP_TOKEN_MINT__|${WARP_TOKEN_MINT:-}|g" \
  -e "s|__WARP_SYNTHETIC_MINT__|${WARP_SYNTHETIC_MINT:-}|g" \
  -e "s|__GORCHAIN_MAILBOX__|${GORCHAIN_MAILBOX:-}|g" \
  -e "s|__SOLANA_MAILBOX__|${SOLANA_MAILBOX:-}|g" \
  -e "s|__WARP_TOKEN_NAME__|${WARP_TOKEN_NAME:-USD Coin}|g" \
  -e "s|__WARP_TOKEN_SYMBOL__|${WARP_TOKEN_SYMBOL:-USDC}|g" \
  -e "s|__WARP_TOKEN_DECIMALS__|${WARP_TOKEN_DECIMALS:-6}|g" \
  -e "s|__WARP_SYNTHETIC_NAME__|${WARP_SYNTHETIC_NAME:-Synthetic USD Coin}|g" \
  -e "s|__WARP_SYNTHETIC_SYMBOL__|${WARP_SYNTHETIC_SYMBOL:-gUSDC}|g" \
  -e "s|__NEXT_PUBLIC_WALLET_CONNECT_ID__|${NEXT_PUBLIC_WALLET_CONNECT_ID:-}|g" \
  -e "s|__GORCHAIN_NATIVE_TOKEN_NAME__|${GORCHAIN_NATIVE_TOKEN_NAME:-GOR}|g" \
  -e "s|__GORCHAIN_NATIVE_TOKEN_SYMBOL__|${GORCHAIN_NATIVE_TOKEN_SYMBOL:-GOR}|g" \
  -e "s|__GORCHAIN_NATIVE_TOKEN_DECIMALS__|${GORCHAIN_NATIVE_TOKEN_DECIMALS:-9}|g" \
  -e "s|__SOLANA_NATIVE_TOKEN_NAME__|${SOLANA_NATIVE_TOKEN_NAME:-SOL}|g" \
  -e "s|__SOLANA_NATIVE_TOKEN_SYMBOL__|${SOLANA_NATIVE_TOKEN_SYMBOL:-SOL}|g" \
  -e "s|__SOLANA_NATIVE_TOKEN_DECIMALS__|${SOLANA_NATIVE_TOKEN_DECIMALS:-9}|g" \
  {} +

# Also replace in static assets if configs are served from public/
find /app/public -type f \( -name '*.yaml' -o -name '*.yml' -o -name '*.json' \) -exec sed -i \
  -e "s|__GORCHAIN_RPC_URL__|${GORCHAIN_RPC_URL:-http://localhost:8899}|g" \
  -e "s|__SOLANA_RPC_URL__|${SOLANA_RPC_URL:-http://localhost:18899}|g" \
  -e "s|__GORCHAIN_DOMAIN_ID__|${GORCHAIN_DOMAIN_ID:-0}|g" \
  -e "s|__SOLANA_DOMAIN_ID__|${SOLANA_DOMAIN_ID:-0}|g" \
  -e "s|__GORCHAIN_MAILBOX__|${GORCHAIN_MAILBOX:-}|g" \
  -e "s|__SOLANA_MAILBOX__|${SOLANA_MAILBOX:-}|g" \
  -e "s|__WARP_COLLATERAL_ADDRESS__|${WARP_COLLATERAL_ADDRESS:-}|g" \
  -e "s|__WARP_SYNTHETIC_ADDRESS__|${WARP_SYNTHETIC_ADDRESS:-}|g" \
  -e "s|__WARP_TOKEN_MINT__|${WARP_TOKEN_MINT:-}|g" \
  -e "s|__WARP_SYNTHETIC_MINT__|${WARP_SYNTHETIC_MINT:-}|g" \
  {} + 2>/dev/null || true

# Fix numeric types: sentinel-based values compile as strings ("99999")
# but the SDK expects numbers (99999). This script converts them back,
# only in chunks containing our chain config (not SDK registry entries).
echo "Fixing numeric types in compiled bundles..."
node /app/fix-numeric-types.js \
  --chain-names "${GORCHAIN_CHAIN_NAME:-gorchain},${SOLANA_CHAIN_NAME:-solana}"

echo "Starting Next.js standalone server..."
exec node server.js
