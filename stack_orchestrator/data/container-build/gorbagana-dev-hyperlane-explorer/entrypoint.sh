#!/bin/sh
set -eu

# Required env for the injected chain-metadata file and the in-cluster Hasura URL.
# HASURA_GRAPHQL_URL is read by the Next server at runtime (server-side only);
# the browser uses the relative /api/graphql proxy and needs no GraphQL env.
missing=""
for var in GORCHAIN_DOMAIN_ID SOLANA_DOMAIN_ID GORCHAIN_CHAIN_ID SOLANA_CHAIN_ID \
           GORCHAIN_RPC_URL SOLANA_RPC_URL HASURA_GRAPHQL_URL; do
  eval "val=\${$var:-}"
  [ -z "$val" ] && missing="$missing $var"
done
if [ -n "$missing" ]; then
  echo "ERROR: Required environment variables not set:$missing" >&2
  exit 1
fi

PUBLIC_DIR="/app/public"

# Self-hosted chain metadata (gorchain + solana), merged over the public registry
# by loadChainMetadata at runtime. Keyed by chain name (must match agent-config /
# domain.name, i.e. gorchain / solana).
cat > "$PUBLIC_DIR/gorbagana-chains.json" <<EOF
{
  "${GORCHAIN_CHAIN_NAME:-gorchain}": {
    "protocol": "sealevel",
    "chainId": ${GORCHAIN_CHAIN_ID},
    "domainId": ${GORCHAIN_DOMAIN_ID},
    "name": "${GORCHAIN_CHAIN_NAME:-gorchain}",
    "displayName": "${GORCHAIN_DISPLAY_NAME:-Gorbagana}",
    "rpcUrls": [{ "http": "${GORCHAIN_RPC_URL}" }],
    "nativeToken": { "name": "${GORCHAIN_NATIVE_TOKEN_NAME:-GOR}", "symbol": "${GORCHAIN_NATIVE_TOKEN_SYMBOL:-GOR}", "decimals": ${GORCHAIN_NATIVE_TOKEN_DECIMALS:-9} },
    "blocks": { "confirmations": 1, "estimateBlockTime": 1, "reorgPeriod": 0 }
  },
  "${SOLANA_CHAIN_NAME:-solana}": {
    "protocol": "sealevel",
    "chainId": ${SOLANA_CHAIN_ID},
    "domainId": ${SOLANA_DOMAIN_ID},
    "name": "${SOLANA_CHAIN_NAME:-solana}",
    "displayName": "${SOLANA_DISPLAY_NAME:-Solana}",
    "rpcUrls": [{ "http": "${SOLANA_RPC_URL}" }],
    "nativeToken": { "name": "${SOLANA_NATIVE_TOKEN_NAME:-SOL}", "symbol": "${SOLANA_NATIVE_TOKEN_SYMBOL:-SOL}", "decimals": ${SOLANA_NATIVE_TOKEN_DECIMALS:-9} },
    "blocks": { "confirmations": 1, "estimateBlockTime": 1, "reorgPeriod": 0 }
  }
}
EOF
echo "Rendered gorbagana-chains.json"

echo "Starting Next.js standalone server..."
exec node server.js
