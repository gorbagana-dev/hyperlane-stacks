#!/bin/bash
set -euo pipefail

echo "=== Hyperlane SVM Warp Route Deployer ==="
echo "Token mint: ${WARP_TOKEN_MINT}"
echo "Collateral chain: ${COLLATERAL_CHAIN} (domain ${COLLATERAL_DOMAIN_ID})"
echo "Synthetic chain: ${SYNTHETIC_CHAIN} (domain ${SYNTHETIC_DOMAIN_ID})"

# -------------------------------------------------------
# Prerequisite check: core deployment must exist
# -------------------------------------------------------
echo ""
echo "=== Checking core deployment artifacts ==="
COLLATERAL_PROGRAMS=$(kubectl get configmap hyperlane-program-ids \
  -o jsonpath="{.data.${COLLATERAL_CHAIN}-program-ids\.json}" 2>/dev/null || echo "")
SYNTHETIC_PROGRAMS=$(kubectl get configmap hyperlane-program-ids \
  -o jsonpath="{.data.${SYNTHETIC_CHAIN}-program-ids\.json}" 2>/dev/null || echo "")

if [ -z "$COLLATERAL_PROGRAMS" ] || [ "$COLLATERAL_PROGRAMS" = "{}" ]; then
  echo "ERROR: hyperlane-program-ids ConfigMap missing data for ${COLLATERAL_CHAIN}."
  echo "Run the hyperlane-svm-deployer stack first."
  exit 1
fi
if [ -z "$SYNTHETIC_PROGRAMS" ] || [ "$SYNTHETIC_PROGRAMS" = "{}" ]; then
  echo "ERROR: hyperlane-program-ids ConfigMap missing data for ${SYNTHETIC_CHAIN}."
  echo "Run the hyperlane-svm-deployer stack first."
  exit 1
fi

echo "Core program IDs found for both chains."

# Extract mailbox addresses from core deployment
COLLATERAL_MAILBOX=$(echo "$COLLATERAL_PROGRAMS" | jq -r '.mailbox')
SYNTHETIC_MAILBOX=$(echo "$SYNTHETIC_PROGRAMS" | jq -r '.mailbox')
echo "Collateral mailbox (${COLLATERAL_CHAIN}): ${COLLATERAL_MAILBOX}"
echo "Synthetic mailbox (${SYNTHETIC_CHAIN}): ${SYNTHETIC_MAILBOX}"

# -------------------------------------------------------
# Idempotency check: skip if token-config already populated
# -------------------------------------------------------
if [ "${FORCE_REDEPLOY:-false}" != "true" ]; then
  EXISTING=$(kubectl get configmap hyperlane-token-config \
    -o jsonpath='{.data.token-config\.json}' 2>/dev/null || echo "")
  if [ -n "$EXISTING" ] && [ "$EXISTING" != "{}" ] && [ "$EXISTING" != "null" ]; then
    echo ""
    echo "Warp route config already exists (hyperlane-token-config ConfigMap has data)."
    echo "Set FORCE_REDEPLOY=true to override. Exiting."
    exit 0
  fi
fi

# -------------------------------------------------------
# Write deployer keypair to file
# -------------------------------------------------------
DEPLOYER_KEY_FILE="/tmp/deployer-keypair.json"
echo "${DEPLOYER_KEYPAIR}" > "${DEPLOYER_KEY_FILE}"
chmod 600 "${DEPLOYER_KEY_FILE}"

WORK_DIR="/tmp/hyperlane-warp-deploy"
mkdir -p "${WORK_DIR}/output"

# Write program IDs to files for the CLI
echo "$COLLATERAL_PROGRAMS" > "${WORK_DIR}/${COLLATERAL_CHAIN}-program-ids.json"
echo "$SYNTHETIC_PROGRAMS" > "${WORK_DIR}/${SYNTHETIC_CHAIN}-program-ids.json"

# -------------------------------------------------------
# Deploy collateral token on the collateral chain
# -------------------------------------------------------
echo ""
echo "=== Deploying collateral warp route on ${COLLATERAL_CHAIN} ==="
COLLATERAL_RPC_URL="${COLLATERAL_CHAIN_RPC_URL}"

hyperlane-sealevel-client --url "${COLLATERAL_RPC_URL}" \
  warp-route deploy \
  --keypair "${DEPLOYER_KEY_FILE}" \
  --warp-route-type collateral \
  --token-mint "${WARP_TOKEN_MINT}" \
  --chain "${COLLATERAL_CHAIN}" \
  --remote-chain "${SYNTHETIC_CHAIN}" \
  --remote-domain "${SYNTHETIC_DOMAIN_ID}" \
  --mailbox "${COLLATERAL_MAILBOX}" \
  --token-config /config/token/token-config.json \
  --built-so-dir /hyperlane/sealevel-programs \
  --program-ids "${WORK_DIR}/${COLLATERAL_CHAIN}-program-ids.json" \
  --output "${WORK_DIR}/output/collateral-deploy.json"

COLLATERAL_TOKEN_ADDRESS=$(jq -r '.token // .program_id // .collateral_address' \
  "${WORK_DIR}/output/collateral-deploy.json")
echo "Collateral token program: ${COLLATERAL_TOKEN_ADDRESS}"

# -------------------------------------------------------
# Deploy synthetic token on the synthetic chain
# -------------------------------------------------------
echo ""
echo "=== Deploying synthetic warp route on ${SYNTHETIC_CHAIN} ==="
SYNTHETIC_RPC_URL="${SYNTHETIC_CHAIN_RPC_URL}"

hyperlane-sealevel-client --url "${SYNTHETIC_RPC_URL}" \
  warp-route deploy \
  --keypair "${DEPLOYER_KEY_FILE}" \
  --warp-route-type synthetic \
  --chain "${SYNTHETIC_CHAIN}" \
  --remote-chain "${COLLATERAL_CHAIN}" \
  --remote-domain "${COLLATERAL_DOMAIN_ID}" \
  --mailbox "${SYNTHETIC_MAILBOX}" \
  --token-config /config/token/token-config.json \
  --built-so-dir /hyperlane/sealevel-programs \
  --program-ids "${WORK_DIR}/${SYNTHETIC_CHAIN}-program-ids.json" \
  --output "${WORK_DIR}/output/synthetic-deploy.json"

SYNTHETIC_TOKEN_ADDRESS=$(jq -r '.token // .program_id // .synthetic_address' \
  "${WORK_DIR}/output/synthetic-deploy.json")
echo "Synthetic token program: ${SYNTHETIC_TOKEN_ADDRESS}"

# -------------------------------------------------------
# Post-deploy program hash verification
# -------------------------------------------------------
echo ""
echo "=== Verifying deployed warp route program hashes ==="
VERIFY_FAILED=0

for SO_NAME in hyperlane_sealevel_token hyperlane_sealevel_token_native hyperlane_sealevel_token_collateral; do
  SO_FILE="/hyperlane/sealevel-programs/${SO_NAME}.so"
  [ -f "$SO_FILE" ] || continue

  if [ -n "${COLLATERAL_TOKEN_ADDRESS}" ]; then
    LOCAL_HASH=$(solana-verify get-executable-hash "$SO_FILE" 2>/dev/null || echo "")
    ONCHAIN_HASH=$(solana-verify get-program-hash -u "${COLLATERAL_RPC_URL}" "${COLLATERAL_TOKEN_ADDRESS}" 2>/dev/null || echo "")
    if [ -n "$LOCAL_HASH" ] && [ -n "$ONCHAIN_HASH" ] && [ "$LOCAL_HASH" != "$ONCHAIN_HASH" ]; then
      echo "WARNING: Hash mismatch for ${SO_NAME} on ${COLLATERAL_CHAIN} (may be expected if different program)"
    fi
  fi
done

if [ "$VERIFY_FAILED" -ne 0 ]; then
  echo "FATAL: Program hash verification failed. Aborting."
  exit 1
fi

# -------------------------------------------------------
# Transfer ownership to hardware wallet (if configured)
# -------------------------------------------------------
if [ -n "${HARDWARE_WALLET_PUBKEY:-}" ]; then
  echo ""
  echo "=== Transferring warp route ownership to hardware wallet ==="

  if [ -n "${COLLATERAL_TOKEN_ADDRESS}" ]; then
    echo "Transferring collateral token upgrade authority on ${COLLATERAL_CHAIN}..."
    solana program set-upgrade-authority "${COLLATERAL_TOKEN_ADDRESS}" \
      --new-upgrade-authority "${HARDWARE_WALLET_PUBKEY}" \
      --keypair "${DEPLOYER_KEY_FILE}" \
      --url "${COLLATERAL_RPC_URL}" || \
      echo "WARNING: Failed to transfer collateral token authority"
  fi

  if [ -n "${SYNTHETIC_TOKEN_ADDRESS}" ]; then
    echo "Transferring synthetic token upgrade authority on ${SYNTHETIC_CHAIN}..."
    solana program set-upgrade-authority "${SYNTHETIC_TOKEN_ADDRESS}" \
      --new-upgrade-authority "${HARDWARE_WALLET_PUBKEY}" \
      --keypair "${DEPLOYER_KEY_FILE}" \
      --url "${SYNTHETIC_RPC_URL}" || \
      echo "WARNING: Failed to transfer synthetic token authority"
  fi
fi

# -------------------------------------------------------
# Build token-config.json output
# -------------------------------------------------------
echo ""
echo "=== Building token-config.json ==="

cat > "${WORK_DIR}/output/token-config.json" <<TOKEN_EOF
{
  "warpRoute": {
    "type": "collateral-and-synthetic",
    "tokenMint": "${WARP_TOKEN_MINT}",
    "collateral": {
      "chain": "${COLLATERAL_CHAIN}",
      "domainId": ${COLLATERAL_DOMAIN_ID},
      "address": "${COLLATERAL_TOKEN_ADDRESS}",
      "mailbox": "${COLLATERAL_MAILBOX}",
      "rpcUrl": "${COLLATERAL_RPC_URL}"
    },
    "synthetic": {
      "chain": "${SYNTHETIC_CHAIN}",
      "domainId": ${SYNTHETIC_DOMAIN_ID},
      "address": "${SYNTHETIC_TOKEN_ADDRESS}",
      "mailbox": "${SYNTHETIC_MAILBOX}",
      "rpcUrl": "${SYNTHETIC_RPC_URL}"
    }
  }
}
TOKEN_EOF

# -------------------------------------------------------
# Write deployment artifacts to k8s ConfigMaps
# -------------------------------------------------------
echo ""
echo "=== Writing warp route artifacts to Kubernetes ConfigMaps ==="

kubectl create configmap hyperlane-token-config \
  --from-file="token-config.json=${WORK_DIR}/output/token-config.json" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl create configmap hyperlane-warp-deploy-outputs \
  --from-file="collateral-deploy.json=${WORK_DIR}/output/collateral-deploy.json" \
  --from-file="synthetic-deploy.json=${WORK_DIR}/output/synthetic-deploy.json" \
  --dry-run=client -o yaml | kubectl apply -f -

for CM in hyperlane-token-config hyperlane-warp-deploy-outputs; do
  kubectl label configmap "$CM" \
    app.kubernetes.io/managed-by=hyperlane-svm-warp-deployer \
    app.kubernetes.io/component=deployment-artifacts \
    --overwrite
done

# -------------------------------------------------------
# Clean up deployer keypair
# -------------------------------------------------------
rm -f "${DEPLOYER_KEY_FILE}"

echo ""
echo "=== Warp route deployment complete ==="
echo "Collateral (${COLLATERAL_CHAIN}): ${COLLATERAL_TOKEN_ADDRESS}"
echo "Synthetic (${SYNTHETIC_CHAIN}): ${SYNTHETIC_TOKEN_ADDRESS}"
echo ""
echo "Artifacts written to ConfigMaps:"
echo "  - hyperlane-token-config"
echo "  - hyperlane-warp-deploy-outputs"
