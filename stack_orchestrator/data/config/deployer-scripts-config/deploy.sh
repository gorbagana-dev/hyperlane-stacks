#!/bin/bash
set -euo pipefail

echo "=== Hyperlane SVM Core Deployer ==="
echo "Gorchain domain: ${GORCHAIN_DOMAIN_ID}"
echo "Solana domain: ${SOLANA_DOMAIN_ID}"

# -------------------------------------------------------
# Idempotency check: if program-ids ConfigMap already has
# data, a previous deploy succeeded. Skip unless FORCE_REDEPLOY=true.
# -------------------------------------------------------
if [ "${FORCE_REDEPLOY:-false}" != "true" ]; then
  EXISTING=$(kubectl get configmap hyperlane-program-ids \
    -o jsonpath='{.data.gorchain-program-ids\.json}' 2>/dev/null || echo "")
  if [ -n "$EXISTING" ] && [ "$EXISTING" != "{}" ]; then
    echo "Deployment artifacts already exist (hyperlane-program-ids ConfigMap has data)."
    echo "Set FORCE_REDEPLOY=true to override. Exiting."
    exit 0
  fi
fi

# -------------------------------------------------------
# Write deployer keypair to file (required by sealevel-client)
# -------------------------------------------------------
DEPLOYER_KEY_FILE="/tmp/deployer-keypair.json"
echo "${DEPLOYER_KEYPAIR}" > "${DEPLOYER_KEY_FILE}"
chmod 600 "${DEPLOYER_KEY_FILE}"

# Create Solana CLI config (required by hyperlane-sealevel-client even when --keypair is set)
mkdir -p /root/.config/solana/cli
cat > /root/.config/solana/cli/config.yml <<SOLCFG
json_rpc_url: "${GORCHAIN_RPC_URL}"
websocket_url: ""
keypair_path: "${DEPLOYER_KEY_FILE}"
commitment: finalized
SOLCFG

# -------------------------------------------------------
# Prepare working directories
# The CLI writes output to: {environments-dir}/{environment}/{chain}/core/
# -------------------------------------------------------
WORK_DIR="/tmp/hyperlane-deploy"
ENVIRONMENTS_DIR="${WORK_DIR}/environments"
ENVIRONMENT="e2e"
mkdir -p "${ENVIRONMENTS_DIR}"

# -------------------------------------------------------
# Mount config files from ConfigMaps
# -------------------------------------------------------
GAS_ORACLE_CONFIG="/config/gas-oracle/gas-oracle-configs.json"
REGISTRY_DIR="/config/registry"
MULTISIG_CONFIG_DIR="/config/multisig"

# -------------------------------------------------------
# Render config templates via envsubst
# -------------------------------------------------------
echo ""
echo "=== Rendering config templates ==="
RENDERED_REGISTRY_DIR="${WORK_DIR}/registry"
mkdir -p "${RENDERED_REGISTRY_DIR}/chains"
if [ -f "${REGISTRY_DIR}/metadata.yaml.tmpl" ]; then
  envsubst < "${REGISTRY_DIR}/metadata.yaml.tmpl" > "${RENDERED_REGISTRY_DIR}/chains/metadata.yaml"
  echo "Registry rendered at ${RENDERED_REGISTRY_DIR}/chains/metadata.yaml"
elif [ -f "${REGISTRY_DIR}/metadata.yaml" ]; then
  cp "${REGISTRY_DIR}/metadata.yaml" "${RENDERED_REGISTRY_DIR}/chains/metadata.yaml"
  echo "Registry copied (no template) at ${RENDERED_REGISTRY_DIR}/chains/metadata.yaml"
fi

# -------------------------------------------------------
# Deploy core contracts on Gorchain
# -------------------------------------------------------
echo ""
echo "=== Deploying core contracts on Gorchain (domain ${GORCHAIN_DOMAIN_ID}) ==="
hyperlane-sealevel-client \
  --url "${GORCHAIN_RPC_URL}" \
  --keypair "${DEPLOYER_KEY_FILE}" \
  core deploy \
  --local-domain "${GORCHAIN_DOMAIN_ID}" \
  --environment "${ENVIRONMENT}" \
  --environments-dir "${ENVIRONMENTS_DIR}" \
  --chain gorchain \
  --remote-domains "${SOLANA_DOMAIN_ID}" \
  --gas-oracle-config-file "${GAS_ORACLE_CONFIG}" \
  --built-so-dir /opt/hyperlane/programs

echo ""
echo "=== Deploying core contracts on Solana (domain ${SOLANA_DOMAIN_ID}) ==="
hyperlane-sealevel-client \
  --url "${SOLANA_RPC_URL}" \
  --keypair "${DEPLOYER_KEY_FILE}" \
  core deploy \
  --local-domain "${SOLANA_DOMAIN_ID}" \
  --environment "${ENVIRONMENT}" \
  --environments-dir "${ENVIRONMENTS_DIR}" \
  --chain solana \
  --remote-domains "${GORCHAIN_DOMAIN_ID}" \
  --gas-oracle-config-file "${GAS_ORACLE_CONFIG}" \
  --built-so-dir /opt/hyperlane/programs

# -------------------------------------------------------
# Paths to program-ids.json produced by core deploy
# Structure: {environments-dir}/{environment}/{chain}/core/program-ids.json
# -------------------------------------------------------
GORCHAIN_PROGRAMS="${ENVIRONMENTS_DIR}/${ENVIRONMENT}/gorchain/core/program-ids.json"
SOLANA_PROGRAMS="${ENVIRONMENTS_DIR}/${ENVIRONMENT}/solana/core/program-ids.json"

echo ""
echo "=== Checking deployment outputs ==="
echo "Gorchain program-ids: ${GORCHAIN_PROGRAMS}"
ls -la "${GORCHAIN_PROGRAMS}"
echo "Solana program-ids: ${SOLANA_PROGRAMS}"
ls -la "${SOLANA_PROGRAMS}"

# -------------------------------------------------------
# Post-deploy program hash verification
# -------------------------------------------------------
echo ""
echo "=== Verifying deployed program hashes ==="
VERIFY_FAILED=0
for CHAIN_OUTPUT in gorchain solana; do
  if [ "$CHAIN_OUTPUT" = "gorchain" ]; then
    RPC_URL="${GORCHAIN_RPC_URL}"
    PROGRAMS_FILE="${GORCHAIN_PROGRAMS}"
  else
    RPC_URL="${SOLANA_RPC_URL}"
    PROGRAMS_FILE="${SOLANA_PROGRAMS}"
  fi

  for PROGRAM in mailbox validator_announce interchain_gas_paymaster multisig_ism; do
    SO_FILE="/opt/hyperlane/programs/hyperlane_sealevel_${PROGRAM}.so"
    if [ ! -f "$SO_FILE" ]; then
      continue
    fi
    PROGRAM_ID=$(jq -r ".${PROGRAM} // empty" "${PROGRAMS_FILE}" 2>/dev/null || true)
    if [ -z "$PROGRAM_ID" ]; then
      continue
    fi
    LOCAL_HASH=$(solana-verify get-executable-hash "$SO_FILE" 2>/dev/null || echo "unknown")
    ONCHAIN_HASH=$(solana-verify get-program-hash -u "$RPC_URL" "$PROGRAM_ID" 2>/dev/null || echo "unknown")
    if [ "$LOCAL_HASH" != "$ONCHAIN_HASH" ]; then
      echo "ERROR: Hash mismatch for ${PROGRAM} on ${CHAIN_OUTPUT}!"
      echo "  Local:   ${LOCAL_HASH}"
      echo "  On-chain: ${ONCHAIN_HASH}"
      VERIFY_FAILED=1
    else
      echo "OK: ${PROGRAM} on ${CHAIN_OUTPUT} hash verified (${LOCAL_HASH})"
    fi
  done
done
if [ "$VERIFY_FAILED" -ne 0 ]; then
  echo "FATAL: Program hash verification failed. Aborting."
  exit 1
fi

# -------------------------------------------------------
# Configure Multisig ISM validators on each chain
# Each chain's ISM is configured with the remote chain's validator address.
# -------------------------------------------------------
echo ""
echo "=== Configuring Multisig ISM ==="

# Render multisig config templates (validator addresses come from secrets)
RENDERED_MULTISIG_DIR="${WORK_DIR}/multisig"
mkdir -p "${RENDERED_MULTISIG_DIR}"
envsubst < "${MULTISIG_CONFIG_DIR}/gorchain-multisig.json.tmpl" > "${RENDERED_MULTISIG_DIR}/gorchain-multisig.json"
envsubst < "${MULTISIG_CONFIG_DIR}/solana-multisig.json.tmpl" > "${RENDERED_MULTISIG_DIR}/solana-multisig.json"
echo "Multisig configs rendered at ${RENDERED_MULTISIG_DIR}/"

GORCHAIN_ISM_ID=$(jq -r '.multisig_ism_message_id' "${GORCHAIN_PROGRAMS}")
SOLANA_ISM_ID=$(jq -r '.multisig_ism_message_id' "${SOLANA_PROGRAMS}")

echo "Configuring ISM on Gorchain (program: ${GORCHAIN_ISM_ID})..."
hyperlane-sealevel-client \
  --url "${GORCHAIN_RPC_URL}" \
  --keypair "${DEPLOYER_KEY_FILE}" \
  multisig-ism-message-id configure \
  --program-id "${GORCHAIN_ISM_ID}" \
  --multisig-config-file "${RENDERED_MULTISIG_DIR}/gorchain-multisig.json" \
  --registry "${RENDERED_REGISTRY_DIR}"

echo "Configuring ISM on Solana (program: ${SOLANA_ISM_ID})..."
hyperlane-sealevel-client \
  --url "${SOLANA_RPC_URL}" \
  --keypair "${DEPLOYER_KEY_FILE}" \
  multisig-ism-message-id configure \
  --program-id "${SOLANA_ISM_ID}" \
  --multisig-config-file "${RENDERED_MULTISIG_DIR}/solana-multisig.json" \
  --registry "${RENDERED_REGISTRY_DIR}"

echo "Multisig ISM configured on both chains"

# -------------------------------------------------------
# Configure IGP gas oracle on each chain
# Sets token exchange rates, gas prices, and destination gas overheads.
# -------------------------------------------------------
echo ""
echo "=== Configuring IGP gas oracle ==="

GORCHAIN_IGP_ID=$(jq -r '.igp_program_id' "${GORCHAIN_PROGRAMS}")
SOLANA_IGP_ID=$(jq -r '.igp_program_id' "${SOLANA_PROGRAMS}")

echo "Configuring IGP on Gorchain (program: ${GORCHAIN_IGP_ID})..."
hyperlane-sealevel-client \
  --url "${GORCHAIN_RPC_URL}" \
  --keypair "${DEPLOYER_KEY_FILE}" \
  igp configure \
  --program-id "${GORCHAIN_IGP_ID}" \
  --chain gorchain \
  --gas-oracle-config-file "${GAS_ORACLE_CONFIG}" \
  --registry "${RENDERED_REGISTRY_DIR}"

echo "Configuring IGP on Solana (program: ${SOLANA_IGP_ID})..."
hyperlane-sealevel-client \
  --url "${SOLANA_RPC_URL}" \
  --keypair "${DEPLOYER_KEY_FILE}" \
  igp configure \
  --program-id "${SOLANA_IGP_ID}" \
  --chain solana \
  --gas-oracle-config-file "${GAS_ORACLE_CONFIG}" \
  --registry "${RENDERED_REGISTRY_DIR}"

echo "IGP gas oracle configured on both chains"

# -------------------------------------------------------
# Transfer ownership to hardware wallet
# -------------------------------------------------------
if [ -n "${HARDWARE_WALLET_PUBKEY:-}" ]; then
  echo ""
  echo "=== Transferring program ownership to hardware wallet ==="
  echo "Hardware wallet pubkey: ${HARDWARE_WALLET_PUBKEY}"

  for CHAIN_OUTPUT in gorchain solana; do
    if [ "$CHAIN_OUTPUT" = "gorchain" ]; then
      RPC_URL="${GORCHAIN_RPC_URL}"
      PROGRAMS_FILE="${GORCHAIN_PROGRAMS}"
    else
      RPC_URL="${SOLANA_RPC_URL}"
      PROGRAMS_FILE="${SOLANA_PROGRAMS}"
    fi

    # Transfer upgrade authority for all programs (uses solana CLI, not hyperlane)
    for PROGRAM in mailbox validator_announce interchain_gas_paymaster multisig_ism; do
      PROGRAM_ID=$(jq -r ".${PROGRAM} // empty" "${PROGRAMS_FILE}" 2>/dev/null || true)
      if [ -n "$PROGRAM_ID" ]; then
        echo "Transferring upgrade authority for ${PROGRAM} (${PROGRAM_ID}) on ${CHAIN_OUTPUT}..."
        solana program set-upgrade-authority "$PROGRAM_ID" \
          --new-upgrade-authority "${HARDWARE_WALLET_PUBKEY}" \
          --skip-new-upgrade-authority-signer-check \
          --keypair "${DEPLOYER_KEY_FILE}" \
          --url "$RPC_URL" || echo "WARNING: Failed to transfer upgrade authority for ${PROGRAM} on ${CHAIN_OUTPUT}"
      fi
    done

    # Transfer mailbox account ownership (the only transfer-ownership we know works)
    MAILBOX_ID=$(jq -r '.mailbox // empty' "${PROGRAMS_FILE}" 2>/dev/null || true)
    if [ -n "$MAILBOX_ID" ]; then
      echo "Transferring mailbox account ownership on ${CHAIN_OUTPUT}..."
      hyperlane-sealevel-client \
        --url "$RPC_URL" \
        --keypair "${DEPLOYER_KEY_FILE}" \
        mailbox transfer-ownership \
        --program-id "$MAILBOX_ID" \
        "${HARDWARE_WALLET_PUBKEY}" \
        || echo "WARNING: mailbox transfer-ownership on ${CHAIN_OUTPUT} failed or not supported"
    fi

    # Note: core transfer-ownership does not exist in the CLI.
    # validator_announce and multisig_ism account ownership transfer commands
    # are not yet known. Skipping with a warning.
    for PROGRAM in multisig_ism validator_announce; do
      PROGRAM_ID=$(jq -r ".${PROGRAM} // empty" "${PROGRAMS_FILE}" 2>/dev/null || true)
      if [ -n "$PROGRAM_ID" ]; then
        echo "WARNING: No known account ownership transfer command for ${PROGRAM} on ${CHAIN_OUTPUT} (program-id: ${PROGRAM_ID}). Skipping."
      fi
    done

    # Transfer IGP ownership to oracle wallet (if configured)
    if [ -n "${IGP_ORACLE_WALLET_PUBKEY:-}" ]; then
      IGP_ID=$(jq -r '.interchain_gas_paymaster // empty' "${PROGRAMS_FILE}" 2>/dev/null || true)
      IGP_ACCOUNT=$(jq -r '.interchain_gas_paymaster_account // empty' "${PROGRAMS_FILE}" 2>/dev/null || true)
      if [ -n "$IGP_ID" ]; then
        echo "Transferring IGP account ownership to oracle wallet on ${CHAIN_OUTPUT}..."
        hyperlane-sealevel-client \
          --url "$RPC_URL" \
          --keypair "${DEPLOYER_KEY_FILE}" \
          igp transfer-igp-ownership \
          --program-id "$IGP_ID" \
          ${IGP_ACCOUNT:+--igp-account "$IGP_ACCOUNT"} \
          "${IGP_ORACLE_WALLET_PUBKEY}" \
          || echo "WARNING: IGP ownership transfer on ${CHAIN_OUTPUT} failed or not supported"
      fi
    fi
  done
fi

# -------------------------------------------------------
# Build agent-config.json from deployed program IDs
# -------------------------------------------------------
echo ""
echo "=== Building agent-config.json ==="

cat > "${WORK_DIR}/agent-config.json" <<AGENT_EOF
{
  "chains": {
    "gorchain": {
      "name": "gorchain",
      "chainId": ${GORCHAIN_CHAIN_ID},
      "domainId": ${GORCHAIN_DOMAIN_ID},
      "protocol": "sealevel",
      "mailbox": "$(jq -r '.mailbox' "$GORCHAIN_PROGRAMS")",
      "interchainGasPaymaster": "$(jq -r '.overhead_igp_account' "$GORCHAIN_PROGRAMS")",
      "interchainSecurityModule": "$(jq -r '.multisig_ism_message_id' "$GORCHAIN_PROGRAMS")",
      "validatorAnnounce": "$(jq -r '.validator_announce' "$GORCHAIN_PROGRAMS")",
      "merkleTreeHook": "$(jq -r '.mailbox' "$GORCHAIN_PROGRAMS")",
      "rpcUrls": [{"http": "${GORCHAIN_RPC_URL}"}],
      "blocks": {
        "estimateBlockTime": 0.4,
        "reorgPeriod": 0
      },
      "index": {
        "from": 0,
        "chunk": 10000,
        "mode": "sequence"
      },
      "nativeToken": {
        "decimals": 9
      }
    },
    "solana": {
      "name": "solana",
      "chainId": ${SOLANA_CHAIN_ID},
      "domainId": ${SOLANA_DOMAIN_ID},
      "protocol": "sealevel",
      "mailbox": "$(jq -r '.mailbox' "$SOLANA_PROGRAMS")",
      "interchainGasPaymaster": "$(jq -r '.overhead_igp_account' "$SOLANA_PROGRAMS")",
      "interchainSecurityModule": "$(jq -r '.multisig_ism_message_id' "$SOLANA_PROGRAMS")",
      "validatorAnnounce": "$(jq -r '.validator_announce' "$SOLANA_PROGRAMS")",
      "merkleTreeHook": "$(jq -r '.mailbox' "$SOLANA_PROGRAMS")",
      "rpcUrls": [{"http": "${SOLANA_RPC_URL}"}],
      "blocks": {
        "estimateBlockTime": 0.4,
        "reorgPeriod": 0
      },
      "index": {
        "from": 0,
        "chunk": 10000,
        "mode": "sequence"
      },
      "nativeToken": {
        "decimals": 9
      }
    }
  }
}
AGENT_EOF

# -------------------------------------------------------
# Write deployment artifacts to k8s ConfigMaps
# -------------------------------------------------------
echo ""
echo "=== Writing deployment artifacts to Kubernetes ConfigMaps ==="

# Program IDs ConfigMap
kubectl create configmap hyperlane-program-ids \
  --from-file="gorchain-program-ids.json=${GORCHAIN_PROGRAMS}" \
  --from-file="solana-program-ids.json=${SOLANA_PROGRAMS}" \
  --dry-run=client -o yaml | kubectl apply -f -

# Agent config ConfigMap
kubectl create configmap hyperlane-agent-config \
  --from-file="agent-config.json=${WORK_DIR}/agent-config.json" \
  --dry-run=client -o yaml | kubectl apply -f -

# Gas oracle config (copy from input, skip if not mounted)
if [ -f "${GAS_ORACLE_CONFIG}" ]; then
  kubectl create configmap hyperlane-gas-oracle-config \
    --from-file="gas-oracle-configs.json=${GAS_ORACLE_CONFIG}" \
    --dry-run=client -o yaml | kubectl apply -f -
else
  echo "WARNING: Gas oracle config not found at ${GAS_ORACLE_CONFIG}, skipping ConfigMap"
fi

# Multisig config (already rendered during ISM configure step above)
kubectl create configmap hyperlane-multisig-config \
  --from-file="gorchain-multisig.json=${RENDERED_MULTISIG_DIR}/gorchain-multisig.json" \
  --from-file="solana-multisig.json=${RENDERED_MULTISIG_DIR}/solana-multisig.json" \
  --dry-run=client -o yaml | kubectl apply -f -

# Registry metadata (already rendered during template rendering step above)
if [ -f "${RENDERED_REGISTRY_DIR}/chains/metadata.yaml" ]; then
  kubectl create configmap hyperlane-registry \
    --from-file="${RENDERED_REGISTRY_DIR}/chains/" \
    --dry-run=client -o yaml | kubectl apply -f -
else
  echo "WARNING: Registry config not found, skipping ConfigMap"
fi

# Label all output ConfigMaps
for CM in hyperlane-program-ids hyperlane-agent-config hyperlane-gas-oracle-config hyperlane-multisig-config hyperlane-registry; do
  kubectl label configmap "$CM" \
    app.kubernetes.io/managed-by=hyperlane-svm-deployer \
    app.kubernetes.io/component=deployment-artifacts \
    --overwrite
done

# -------------------------------------------------------
# Clean up deployer keypair
# -------------------------------------------------------
rm -f "${DEPLOYER_KEY_FILE}"

echo ""
echo "=== Deployment complete ==="
echo "Artifacts written to ConfigMaps:"
echo "  - hyperlane-program-ids"
echo "  - hyperlane-agent-config"
echo "  - hyperlane-gas-oracle-config"
echo "  - hyperlane-multisig-config"
echo "  - hyperlane-registry"
