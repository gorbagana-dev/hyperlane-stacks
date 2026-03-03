#!/bin/bash
# IGP fee claim sidecar — claims accumulated IGP fees on both chains
# Runs in a loop every 6 hours. Uses the relayer key for tx fees (permissionless operation).

set -euo pipefail

# Write relayer keypair to a temp file (required by --keypair global flag)
RELAYER_KEY_FILE="/tmp/relayer-keypair.json"
echo "${RELAYER_KEYPAIR_JSON}" > "${RELAYER_KEY_FILE}"
chmod 600 "${RELAYER_KEY_FILE}"

# Create Solana CLI config (required by hyperlane-sealevel-client even when --keypair is set)
mkdir -p /root/.config/solana/cli
cat > /root/.config/solana/cli/config.yml <<SOLCFG
json_rpc_url: "${GORCHAIN_RPC_URL}"
websocket_url: ""
keypair_path: "${RELAYER_KEY_FILE}"
commitment: finalized
SOLCFG

echo "IGP fee claim sidecar starting (interval: 6h)..."
while true; do
  # TODO: UNVERIFIED — `igp claim` args have NOT been confirmed against
  # `hyperlane-sealevel-client igp claim --help`. The --program-id flag is
  # likely correct, but verify before production use.

  echo "[$(date -u)] Claiming IGP fees on Gorchain..."
  hyperlane-sealevel-client \
    --url "$GORCHAIN_RPC_URL" \
    --keypair "${RELAYER_KEY_FILE}" \
    igp claim \
    --program-id "$GORCHAIN_IGP_PROGRAM_ID" || \
    echo "Warning: Gorchain fee claim failed"

  echo "[$(date -u)] Claiming IGP fees on Solana..."
  hyperlane-sealevel-client \
    --url "$SOLANA_RPC_URL" \
    --keypair "${RELAYER_KEY_FILE}" \
    igp claim \
    --program-id "$SOLANA_IGP_PROGRAM_ID" || \
    echo "Warning: Solana fee claim failed"

  echo "[$(date -u)] Fee claim cycle complete. Sleeping 6h..."
  sleep 21600
done
