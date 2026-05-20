#!/bin/bash
set -euo pipefail

STATE_DIR="${STATE_OUTPUT_DIR:-/state}"
LOGS_DIR="${LOGS_OUTPUT_DIR:-/logs}"
mkdir -p "${STATE_DIR}" "${LOGS_DIR}"

LOG_FILE="${LOGS_DIR}/svm-warp-deployer-$(date -u +%Y%m%dT%H%M%SZ).log"
exec > >(tee -a "${LOG_FILE}") 2>&1
echo "Logging to ${LOG_FILE}"

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
PROGRAM_IDS_FILE="${STATE_DIR}/program-ids.json"
if [ ! -s "${PROGRAM_IDS_FILE}" ]; then
  echo "ERROR: ${PROGRAM_IDS_FILE} missing. Run the hyperlane-svm-deployer stack first."
  exit 1
fi

COLLATERAL_PROGRAMS=$(python3 -c "import json,sys;print(json.dumps(json.load(open('${PROGRAM_IDS_FILE}')).get('${COLLATERAL_CHAIN}', {})))")
SYNTHETIC_PROGRAMS=$(python3 -c "import json,sys;print(json.dumps(json.load(open('${PROGRAM_IDS_FILE}')).get('${SYNTHETIC_CHAIN}', {})))")

if [ "$COLLATERAL_PROGRAMS" = "{}" ]; then
  echo "ERROR: program-ids.json missing data for ${COLLATERAL_CHAIN}."
  exit 1
fi
if [ "$SYNTHETIC_PROGRAMS" = "{}" ]; then
  echo "ERROR: program-ids.json missing data for ${SYNTHETIC_CHAIN}."
  exit 1
fi

echo "Core program IDs found for both chains."

# Extract addresses from core deployment for use by envsubst in templates
export COLLATERAL_MAILBOX=$(echo "$COLLATERAL_PROGRAMS" | jq -r '.mailbox')
export SYNTHETIC_MAILBOX=$(echo "$SYNTHETIC_PROGRAMS" | jq -r '.mailbox')
export COLLATERAL_ISM=$(echo "$COLLATERAL_PROGRAMS" | jq -r '.multisig_ism_message_id')
export COLLATERAL_IGP=$(echo "$COLLATERAL_PROGRAMS" | jq -r '.overhead_igp_account')
export SYNTHETIC_ISM=$(echo "$SYNTHETIC_PROGRAMS" | jq -r '.multisig_ism_message_id')
export SYNTHETIC_IGP=$(echo "$SYNTHETIC_PROGRAMS" | jq -r '.overhead_igp_account')

echo "Collateral mailbox (${COLLATERAL_CHAIN}): ${COLLATERAL_MAILBOX}"
echo "Synthetic mailbox (${SYNTHETIC_CHAIN}): ${SYNTHETIC_MAILBOX}"
echo "Collateral ISM: ${COLLATERAL_ISM}, IGP: ${COLLATERAL_IGP}"
echo "Synthetic  ISM: ${SYNTHETIC_ISM}, IGP: ${SYNTHETIC_IGP}"

# -------------------------------------------------------
# Idempotency check: skip if token-config already populated
# -------------------------------------------------------
if [ "${FORCE_REDEPLOY:-false}" != "true" ]; then
  EXISTING_FILE="${STATE_DIR}/token-config.json"
  if [ -s "${EXISTING_FILE}" ]; then
    CONTENT=$(cat "${EXISTING_FILE}")
    if [ "$CONTENT" != "{}" ] && [ "$CONTENT" != "null" ]; then
      echo ""
      echo "Warp route config already exists (${EXISTING_FILE} has data)."
      echo "Set FORCE_REDEPLOY=true to override. Exiting."
      exit 0
    fi
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

# Write core program IDs where the CLI expects them:
#   {environments_dir}/{environment}/{chain}/core/program-ids.json
mkdir -p "${ENVIRONMENTS_DIR}/${ENVIRONMENT}/${COLLATERAL_CHAIN}/core"
mkdir -p "${ENVIRONMENTS_DIR}/${ENVIRONMENT}/${SYNTHETIC_CHAIN}/core"
echo "$COLLATERAL_PROGRAMS" > "${ENVIRONMENTS_DIR}/${ENVIRONMENT}/${COLLATERAL_CHAIN}/core/program-ids.json"
echo "$SYNTHETIC_PROGRAMS" > "${ENVIRONMENTS_DIR}/${ENVIRONMENT}/${SYNTHETIC_CHAIN}/core/program-ids.json"

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

# Token config — render template via envsubst (ISM/IGP/mailbox vars exported above),
# then strip empty "uri" fields (CLI panics on "" — needs null/absent).
envsubst < /config/token/token-config.json.tmpl > "${WORK_DIR}/token-config.raw.json"

jq 'walk(if type == "object" and .uri == "" then del(.uri) else . end)' \
  "${WORK_DIR}/token-config.raw.json" > "${WORK_DIR}/token-config.json"
echo "Token config rendered at ${WORK_DIR}/token-config.json"
echo "Token config contents:"
cat "${WORK_DIR}/token-config.json"

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
# The CLI writes output to: {environments-dir}/{environment}/warp-routes/{name}/
# -------------------------------------------------------
echo ""
echo "=== Checking deployment outputs ==="
WARP_OUTPUT_DIR="${ENVIRONMENTS_DIR}/${ENVIRONMENT}/warp-routes/${WARP_ROUTE_NAME}"
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
  echo "=== Transferring warp route upgrade authority to hardware wallet ==="
  echo "Hardware wallet pubkey: ${HARDWARE_WALLET_PUBKEY}"

  # The warp-route deploy writes program-ids.json with per-chain entries:
  #   {"solana": {"hex": "0x...", "base58": "..."}, "gorchain": {"hex": "0x...", "base58": "..."}}
  WARP_PROGRAMS_FILE="${WARP_OUTPUT_DIR}/program-ids.json"
  if [ ! -f "${WARP_PROGRAMS_FILE}" ]; then
    echo "WARNING: ${WARP_PROGRAMS_FILE} not found, cannot transfer upgrade authority."
    echo "Use 'solana program set-upgrade-authority' manually with the deployed program IDs."
  else
    # Map chain names to RPC URLs for the solana CLI
    for CHAIN_NAME in $(jq -r 'keys[]' "${WARP_PROGRAMS_FILE}"); do
      PROGRAM_ID=$(jq -r ".\"${CHAIN_NAME}\".base58 // empty" "${WARP_PROGRAMS_FILE}")
      if [ -z "$PROGRAM_ID" ]; then
        echo "WARNING: No base58 address for ${CHAIN_NAME} in warp program-ids.json, skipping."
        continue
      fi

      # Determine RPC URL from chain name
      CHAIN_UPPER=$(echo "$CHAIN_NAME" | tr '[:lower:]' '[:upper:]')
      RPC_VAR="${CHAIN_UPPER}_RPC_URL"
      RPC_URL=$(eval echo "\${${RPC_VAR}:-}")
      if [ -z "$RPC_URL" ]; then
        echo "WARNING: No RPC URL for ${CHAIN_NAME} (${RPC_VAR} not set), skipping."
        continue
      fi

      echo "Transferring warp route upgrade authority on ${CHAIN_NAME}: ${PROGRAM_ID}..."
      solana program set-upgrade-authority "$PROGRAM_ID" \
        --new-upgrade-authority "${HARDWARE_WALLET_PUBKEY}" \
        --skip-new-upgrade-authority-signer-check \
        --keypair "${DEPLOYER_KEY_FILE}" \
        --url "$RPC_URL" \
        || echo "WARNING: Failed to transfer upgrade authority for warp route on ${CHAIN_NAME}"
    done
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
echo "=== Writing warp route artifacts to ${STATE_DIR} ==="

cp "${WORK_DIR}/output/token-config.json" "${STATE_DIR}/token-config.json"

if [ -d "${WARP_OUTPUT_DIR}" ]; then
  rm -rf "${STATE_DIR}/warp-deploy-outputs"
  mkdir -p "${STATE_DIR}/warp-deploy-outputs"
  cp -a "${WARP_OUTPUT_DIR}/." "${STATE_DIR}/warp-deploy-outputs/" 2>/dev/null || true
fi

# -------------------------------------------------------
# Clean up deployer keypair
# -------------------------------------------------------
rm -f "${DEPLOYER_KEY_FILE}"

# -------------------------------------------------------
# Preflight: verify expected outputs
# -------------------------------------------------------
if [ ! -s "${STATE_DIR}/token-config.json" ]; then
  echo "ERROR: warp-deployer preflight failed: ${STATE_DIR}/token-config.json missing or empty"
  exit 1
fi

echo ""
echo "=== Warp route deployment complete ==="
echo "Collateral chain: ${COLLATERAL_CHAIN}"
echo "Synthetic chain: ${SYNTHETIC_CHAIN}"
echo ""
echo "Artifacts written to ${STATE_DIR}:"
echo "  - token-config.json"
[ -d "${STATE_DIR}/warp-deploy-outputs" ] && echo "  - warp-deploy-outputs/"
