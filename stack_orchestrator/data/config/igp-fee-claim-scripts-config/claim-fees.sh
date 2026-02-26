#!/bin/bash
# IGP fee claim sidecar — claims accumulated IGP fees on both chains
# Runs in a loop every 6 hours. Uses the relayer key for tx fees (permissionless operation).

set -euo pipefail

echo "IGP fee claim sidecar starting (interval: 6h)..."
while true; do
  echo "[$(date -u)] Claiming IGP fees on Gorchain..."
  hyperlane-sealevel-client \
    --url "$GORCHAIN_RPC_URL" \
    igp claim \
    --program-id "$GORCHAIN_IGP_PROGRAM_ID" \
    --keypair /dev/stdin <<< "$RELAYER_KEYPAIR_JSON" || \
    echo "Warning: Gorchain fee claim failed"

  echo "[$(date -u)] Claiming IGP fees on Solana..."
  hyperlane-sealevel-client \
    --url "$SOLANA_RPC_URL" \
    igp claim \
    --program-id "$SOLANA_IGP_PROGRAM_ID" \
    --keypair /dev/stdin <<< "$RELAYER_KEYPAIR_JSON" || \
    echo "Warning: Solana fee claim failed"

  echo "[$(date -u)] Fee claim cycle complete. Sleeping 6h..."
  sleep 21600
done
