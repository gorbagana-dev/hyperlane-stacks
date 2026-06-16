#!/bin/sh
set -eu

# Required env for the injected chain-metadata file and the in-cluster Hasura URL.
# HASURA_GRAPHQL_URL is read by the Next server at runtime (server-side only);
# the browser uses the relative /api/graphql proxy and needs no GraphQL env.
# Only gorchain is injected here — Solana is provided by the public Hyperlane
# registry the UI loads, so it needs no override.
missing=""
for var in GORCHAIN_DOMAIN_ID GORCHAIN_CHAIN_ID GORCHAIN_RPC_URL HASURA_GRAPHQL_URL; do
  eval "val=\${$var:-}"
  [ -z "$val" ] && missing="$missing $var"
done
if [ -n "$missing" ]; then
  echo "ERROR: Required environment variables not set:$missing" >&2
  exit 1
fi

PUBLIC_DIR="/app/public"

# gorchain metadata, merged over the registry by loadChainMetadata at runtime.
# The JSON object name must match the scraper's domain.name (gorchain).
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
  }
}
EOF
echo "Rendered gorbagana-chains.json"

echo "Starting Next.js standalone server..."
exec node server.js
