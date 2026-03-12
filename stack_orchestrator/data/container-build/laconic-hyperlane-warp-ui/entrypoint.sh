#!/bin/sh
set -e

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
  -e "s|__NEXT_PUBLIC_WALLET_CONNECT_ID__|${NEXT_PUBLIC_WALLET_CONNECT_ID:-}|g" \
  -e "s|__GORCHAIN_NATIVE_TOKEN_NAME__|${GORCHAIN_NATIVE_TOKEN_NAME:-GOR}|g" \
  -e "s|__GORCHAIN_NATIVE_TOKEN_SYMBOL__|${GORCHAIN_NATIVE_TOKEN_SYMBOL:-GOR}|g" \
  -e "s|__GORCHAIN_NATIVE_TOKEN_DECIMALS__|${GORCHAIN_NATIVE_TOKEN_DECIMALS:-9}|g" \
  -e "s|__SOLANA_NATIVE_TOKEN_NAME__|${SOLANA_NATIVE_TOKEN_NAME:-SOL}|g" \
  -e "s|__SOLANA_NATIVE_TOKEN_SYMBOL__|${SOLANA_NATIVE_TOKEN_SYMBOL:-SOL}|g" \
  -e "s|__SOLANA_NATIVE_TOKEN_DECIMALS__|${SOLANA_NATIVE_TOKEN_DECIMALS:-9}|g" \
  {} +

# Also replace in config YAMLs if they're served as static
find /app/public -type f \( -name '*.yaml' -o -name '*.yml' -o -name '*.json' \) -exec sed -i \
  -e "s|__GORCHAIN_RPC_URL__|${GORCHAIN_RPC_URL:-http://localhost:8899}|g" \
  -e "s|__SOLANA_RPC_URL__|${SOLANA_RPC_URL:-http://localhost:18899}|g" \
  -e "s|__GORCHAIN_DOMAIN_ID__|${GORCHAIN_DOMAIN_ID:-0}|g" \
  -e "s|__SOLANA_DOMAIN_ID__|${SOLANA_DOMAIN_ID:-0}|g" \
  -e "s|__WARP_COLLATERAL_ADDRESS__|${WARP_COLLATERAL_ADDRESS:-}|g" \
  -e "s|__WARP_SYNTHETIC_ADDRESS__|${WARP_SYNTHETIC_ADDRESS:-}|g" \
  {} + 2>/dev/null || true

echo "Starting Next.js server..."
exec npx next start -p 3000
