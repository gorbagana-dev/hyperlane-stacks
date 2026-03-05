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
      "interchainGasPaymaster": "$(jq -r '.interchain_gas_paymaster' "$GORCHAIN_PROGRAMS")",
      "validatorAnnounce": "$(jq -r '.validator_announce' "$GORCHAIN_PROGRAMS")",
      "merkleTreeHook": "$(jq -r '.merkle_tree_hook // .mailbox' "$GORCHAIN_PROGRAMS")",
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
      "interchainGasPaymaster": "$(jq -r '.interchain_gas_paymaster' "$SOLANA_PROGRAMS")",
      "validatorAnnounce": "$(jq -r '.validator_announce' "$SOLANA_PROGRAMS")",
      "merkleTreeHook": "$(jq -r '.merkle_tree_hook // .mailbox' "$SOLANA_PROGRAMS")",
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

# Multisig config (skip if not mounted)
if [ -f "${MULTISIG_CONFIG_DIR}/gorchain-multisig.json" ]; then
  kubectl create configmap hyperlane-multisig-config \
    --from-file="gorchain-multisig.json=${MULTISIG_CONFIG_DIR}/gorchain-multisig.json" \
    --from-file="solana-multisig.json=${MULTISIG_CONFIG_DIR}/solana-multisig.json" \
    --dry-run=client -o yaml | kubectl apply -f -
else
  echo "WARNING: Multisig config not found at ${MULTISIG_CONFIG_DIR}/, skipping ConfigMap"
fi

# Registry metadata (skip if not mounted)
if [ -d "${REGISTRY_DIR}" ] && [ -n "$(ls -A "${REGISTRY_DIR}/" 2>/dev/null)" ]; then
  kubectl create configmap hyperlane-registry \
    --from-file="${REGISTRY_DIR}/" \
    --dry-run=client -o yaml | kubectl apply -f -
else
  echo "WARNING: Registry config not found at ${REGISTRY_DIR}/, skipping ConfigMap"
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
