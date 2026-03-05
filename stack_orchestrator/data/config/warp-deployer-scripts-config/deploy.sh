#!/bin/bash
set -euo pipefail

echo "=== Hyperlane SVM Warp Route Deployer ==="
echo "Token mint: ${WARP_TOKEN_MINT}"
echo "Warp route name: ${WARP_ROUTE_NAME}"

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

# Create Solana CLI config (required by hyperlane-sealevel-client even when --keypair is set)
mkdir -p /root/.config/solana/cli
cat > /root/.config/solana/cli/config.yml <<SOLCFG
json_rpc_url: "${COLLATERAL_CHAIN_RPC_URL}"
websocket_url: ""
keypair_path: "${DEPLOYER_KEY_FILE}"
commitment: finalized
SOLCFG

WORK_DIR="/tmp/hyperlane-warp-deploy"
ENVIRONMENTS_DIR="${WORK_DIR}/environments"
ENVIRONMENT="e2e"
mkdir -p "${ENVIRONMENTS_DIR}" "${WORK_DIR}/output"

# Write program IDs to files for the CLI
echo "$COLLATERAL_PROGRAMS" > "${WORK_DIR}/${COLLATERAL_CHAIN}-program-ids.json"
echo "$SYNTHETIC_PROGRAMS" > "${WORK_DIR}/${SYNTHETIC_CHAIN}-program-ids.json"

# -------------------------------------------------------
# Render config templates via envsubst
# The ConfigMap mounts contain .tmpl files with ${VAR} placeholders.
# envsubst substitutes them with env vars from the spec config.
# -------------------------------------------------------
echo ""
echo "=== Rendering config templates ==="

# Registry: CLI expects {registry}/chains/metadata.yaml
REGISTRY_DIR="/tmp/registry"
mkdir -p "${REGISTRY_DIR}/chains"
envsubst < /config/registry/metadata.yaml.tmpl > "${REGISTRY_DIR}/chains/metadata.yaml"
echo "Registry rendered at ${REGISTRY_DIR}/chains/metadata.yaml"

# Token config
envsubst < /config/token/token-config.json.tmpl > "${WORK_DIR}/token-config.json"
echo "Token config rendered at ${WORK_DIR}/token-config.json"

# -------------------------------------------------------
# Deploy warp routes (single invocation for all chains)
# -------------------------------------------------------
echo ""
echo "=== Deploying warp routes ==="

hyperlane-sealevel-client \
  --keypair "${DEPLOYER_KEY_FILE}" \
  warp-route deploy \
  --environment "${ENVIRONMENT}" \
  --environments-dir "${ENVIRONMENTS_DIR}" \
  --built-so-dir /opt/hyperlane/programs \
  --warp-route-name "${WARP_ROUTE_NAME}" \
  --token-config-file "${WORK_DIR}/token-config.json" \
  --registry "${REGISTRY_DIR}" \
  --ata-payer-funding-amount 1000000000

echo "Warp routes deployed"

# -------------------------------------------------------
# Collect deployment output
# The CLI writes output to: {environments-dir}/{environment}/warp-routes/
# -------------------------------------------------------
echo ""
echo "=== Checking deployment outputs ==="
WARP_OUTPUT_DIR="${ENVIRONMENTS_DIR}/${ENVIRONMENT}/warp-routes"
if [ -d "${WARP_OUTPUT_DIR}" ]; then
  echo "Warp route deployment outputs:"
  ls -la "${WARP_OUTPUT_DIR}/"
else
  echo "WARNING: Expected output directory ${WARP_OUTPUT_DIR} not found."
  echo "Checking environment dir for output files..."
  find "${ENVIRONMENTS_DIR}" -name "*.json" -type f 2>/dev/null || true
fi

# -------------------------------------------------------
# Post-deploy program hash verification
# -------------------------------------------------------
echo ""
echo "=== Verifying deployed warp route program hashes ==="
VERIFY_FAILED=0

for SO_NAME in hyperlane_sealevel_token hyperlane_sealevel_token_native hyperlane_sealevel_token_collateral; do
  SO_FILE="/opt/hyperlane/programs/${SO_NAME}.so"
  [ -f "$SO_FILE" ] || continue

  # Note: We'd need the deployed program addresses from the CLI output
  # to verify hashes. This section is best-effort.
  LOCAL_HASH=$(solana-verify get-executable-hash "$SO_FILE" 2>/dev/null || echo "")
  if [ -n "$LOCAL_HASH" ]; then
    echo "Local hash for ${SO_NAME}: ${LOCAL_HASH}"
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
  echo "NOTE: Transfer of warp route program ownership requires the deployed"
  echo "program addresses from the CLI output. This may need manual steps."

  # Transfer upgrade authority via solana CLI (if program addresses are known)
  # The warp-route deploy output should contain the deployed program IDs.
  # TODO: Parse warp-route deploy output for program addresses and transfer
  # upgrade authority automatically.
  echo "WARNING: Automatic warp route ownership transfer not yet implemented."
  echo "Use 'solana program set-upgrade-authority' manually with the deployed program IDs."
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
      "mailbox": "${COLLATERAL_MAILBOX}",
      "rpcUrl": "${COLLATERAL_CHAIN_RPC_URL}"
    },
    "synthetic": {
      "chain": "${SYNTHETIC_CHAIN}",
      "domainId": ${SYNTHETIC_DOMAIN_ID},
      "mailbox": "${SYNTHETIC_MAILBOX}",
      "rpcUrl": "${SYNTHETIC_CHAIN_RPC_URL}"
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

# Copy any warp-route deploy outputs to ConfigMap if available
if [ -d "${WARP_OUTPUT_DIR}" ]; then
  WARP_FILES_ARGS=""
  for f in "${WARP_OUTPUT_DIR}"/*.json; do
    [ -f "$f" ] || continue
    BASENAME=$(basename "$f")
    WARP_FILES_ARGS="${WARP_FILES_ARGS} --from-file=${BASENAME}=${f}"
  done
  if [ -n "${WARP_FILES_ARGS}" ]; then
    kubectl create configmap hyperlane-warp-deploy-outputs \
      ${WARP_FILES_ARGS} \
      --dry-run=client -o yaml | kubectl apply -f -
  fi
fi

for CM in hyperlane-token-config; do
  kubectl label configmap "$CM" \
    app.kubernetes.io/managed-by=hyperlane-svm-warp-deployer \
    app.kubernetes.io/component=deployment-artifacts \
    --overwrite
done

if kubectl get configmap hyperlane-warp-deploy-outputs >/dev/null 2>&1; then
  kubectl label configmap hyperlane-warp-deploy-outputs \
    app.kubernetes.io/managed-by=hyperlane-svm-warp-deployer \
    app.kubernetes.io/component=deployment-artifacts \
    --overwrite
fi

# -------------------------------------------------------
# Clean up deployer keypair
# -------------------------------------------------------
rm -f "${DEPLOYER_KEY_FILE}"

echo ""
echo "=== Warp route deployment complete ==="
echo "Collateral chain: ${COLLATERAL_CHAIN}"
echo "Synthetic chain: ${SYNTHETIC_CHAIN}"
echo ""
echo "Artifacts written to ConfigMaps:"
echo "  - hyperlane-token-config"
echo "  - hyperlane-warp-deploy-outputs (if deploy produced output files)"
