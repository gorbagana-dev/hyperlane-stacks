#!/bin/sh
set -eu

# Render /app/public/gorbagana-chains.json (see render-chains.js for the why) and
# start the Next.js server. The render needs the real gorchain RPC + Hasura URL.
missing=""
for var in GORCHAIN_RPC_URL HASURA_GRAPHQL_URL; do
  eval "val=\${$var:-}"
  [ -z "$val" ] && missing="$missing $var"
done
if [ -n "$missing" ]; then
  echo "ERROR: Required environment variables not set:$missing" >&2
  exit 1
fi

AGENT_CONFIG="${CONFIG_FILES:-/config/agent-config.json}"
PUBLIC_DIR="/app/public"

AGENT_CONFIG="$AGENT_CONFIG" PUBLIC_DIR="$PUBLIC_DIR" node /app/render-chains.js

echo "Starting Next.js standalone server..."
exec node server.js
