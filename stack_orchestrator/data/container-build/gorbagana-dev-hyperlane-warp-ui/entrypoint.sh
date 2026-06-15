#!/bin/sh
set -eu

# Required env (chain config, secret solana RPC, WalletConnect id). Routes arrive as
# a mounted file. NEXT_PUBLIC_WALLET_CONNECT_ID must be non-empty (RainbowKit fatals
# on an empty id).
missing=""
for var in GORCHAIN_RPC_URL SOLANA_RPC_URL GORCHAIN_MAILBOX SOLANA_MAILBOX \
           GORCHAIN_DOMAIN_ID SOLANA_DOMAIN_ID GORCHAIN_CHAIN_ID SOLANA_CHAIN_ID \
           NEXT_PUBLIC_WALLET_CONNECT_ID; do
  eval "val=\${$var:-}"
  [ -z "$val" ] && missing="$missing $var"
done
if [ -n "$missing" ]; then
  echo "ERROR: Required environment variables not set:$missing" >&2
  exit 1
fi

WARP_CFG_SRC="/config/warp-ui-config/warpRoutes.yaml"
PUBLIC_DIR="/app/public"

# 1) Route config: copy the deploy-time-generated file the deployment layer mounted.
if [ -f "$WARP_CFG_SRC" ]; then
  cp "$WARP_CFG_SRC" "$PUBLIC_DIR/warpRoutes.yaml"
  echo "Installed warpRoutes.yaml from $WARP_CFG_SRC"
else
  echo "ERROR: $WARP_CFG_SRC not found (warp-ui-config configmap not mounted?)" >&2
  exit 1
fi

# 2) Chain config: render from env so the secret solana RPC never lands in git.
# When WARP_UI_PUBLIC_URL is set (staging/prod), browsers get the app's own
# /api/rpc/solana proxy instead of the keyed SOLANA_RPC_URL — the key stays
# server-side. Unset (e2e/local) keeps the direct keyless URL.
if [ -n "${WARP_UI_PUBLIC_URL:-}" ]; then
  SOLANA_BROWSER_RPC_URL="${WARP_UI_PUBLIC_URL%/}/api/rpc/solana"
else
  SOLANA_BROWSER_RPC_URL="${SOLANA_RPC_URL}"
fi
cat > "$PUBLIC_DIR/chains.yaml" <<EOF
gorchain:
  protocol: sealevel
  chainId: ${GORCHAIN_CHAIN_ID}
  domainId: ${GORCHAIN_DOMAIN_ID}
  name: ${GORCHAIN_CHAIN_NAME:-gorchain}
  displayName: ${GORCHAIN_DISPLAY_NAME:-Gorbagana}
  logoURI: ${GORCHAIN_LOGO_URI:-/gorbagana-logo.jpg}
  mailbox: ${GORCHAIN_MAILBOX}
  rpcUrls:
    - http: ${GORCHAIN_RPC_URL}
  nativeToken:
    name: ${GORCHAIN_NATIVE_TOKEN_NAME:-GOR}
    symbol: ${GORCHAIN_NATIVE_TOKEN_SYMBOL:-GOR}
    decimals: ${GORCHAIN_NATIVE_TOKEN_DECIMALS:-9}
  blocks:
    confirmations: 1
    estimateBlockTime: 0.4
    reorgPeriod: 0
solana:
  protocol: sealevel
  chainId: ${SOLANA_CHAIN_ID}
  domainId: ${SOLANA_DOMAIN_ID}
  name: ${SOLANA_CHAIN_NAME:-solana}
  displayName: ${SOLANA_DISPLAY_NAME:-Solana}
  logoURI: ${SOLANA_LOGO_URI:-/solana-logo.png}
  mailbox: ${SOLANA_MAILBOX}
  rpcUrls:
    - http: ${SOLANA_BROWSER_RPC_URL}
  nativeToken:
    name: ${SOLANA_NATIVE_TOKEN_NAME:-SOL}
    symbol: ${SOLANA_NATIVE_TOKEN_SYMBOL:-SOL}
    decimals: ${SOLANA_NATIVE_TOKEN_DECIMALS:-9}
  blocks:
    confirmations: 1
    estimateBlockTime: 0.4
    reorgPeriod: 0
EOF
echo "Rendered chains.yaml"

# 3) NEXT_PUBLIC_* values: inlined at build, so the bundle ships with sentinel
# placeholders; replace them here with the real values from pod env. EXPLORER_URL
# is optional — an empty value renders no "View in Explorer" link in the UI.
find /app/.next -name '*.js' -exec sed -i \
  -e "s|__NEXT_PUBLIC_WALLET_CONNECT_ID__|${NEXT_PUBLIC_WALLET_CONNECT_ID}|g" \
  -e "s|__NEXT_PUBLIC_EXPLORER_URL__|${EXPLORER_URL:-}|g" {} +
echo "Substituted NEXT_PUBLIC_* runtime values"

echo "Starting Next.js standalone server..."
exec node server.js
