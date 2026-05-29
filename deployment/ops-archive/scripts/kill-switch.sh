set -euo pipefail

NULL_VALIDATOR="0x0000000000000000000000000000000000000000"
OUTPUT_DIR="/output"
CLIENT="hyperlane-sealevel-client"
mkdir -p "${OUTPUT_DIR}"

log() { echo "[kill-switch] $(date -u +%Y-%m-%dT%H:%M:%SZ) $*"; }

# Load program IDs from deployer-created ConfigMap
GORCHAIN_ISM_ID="$(jq -r '.multisig_ism_message_id // .multisig_ism // .ism' /config/program-ids/gorchain-program-ids.json)"
SOLANA_ISM_ID="$(jq -r '.multisig_ism_message_id // .multisig_ism // .ism' /config/program-ids/solana-program-ids.json)"

log "Gorchain ISM: ${GORCHAIN_ISM_ID}"
log "Solana ISM:   ${SOLANA_ISM_ID}"

# =======================================================
# WARNING: The `multisig-ism-message-id` subcommand args below are UNVERIFIED.
# The flags --authority, --domain, --validators, --threshold, and
# --output-unsigned-tx have NOT been confirmed against `hyperlane-sealevel-client --help`.
# The old subcommand name `multisig-ism set` was changed to `multisig-ism-message-id`
# but the exact subcommand and arguments need verification before production use.
# TODO: Run `hyperlane-sealevel-client multisig-ism-message-id --help` to verify.
# =======================================================

# Step 1: Scale agents to 0 (best-effort — may not have RBAC)
log "Scaling agent deployments to 0..."
for deploy in hyperlane-validator-gorchain hyperlane-validator-solana hyperlane-relayer; do
  kubectl scale deployment "${deploy}" --replicas=0 2>/dev/null && \
    log "  Scaled ${deploy} to 0" || \
    log "  WARNING: Could not scale ${deploy} (may need manual intervention)"
done

# Step 2: Build unsigned ISM reconfiguration transactions
# Gorchain — set validators to null for messages FROM Solana
log "Building unsigned tx: reconfigure ISM on Gorchain..."
# TODO: UNVERIFIED — subcommand and args need confirmation from --help
${CLIENT} --url "${GORCHAIN_RPC_URL}" \
  multisig-ism-message-id set \
  --program-id "${GORCHAIN_ISM_ID}" \
  --authority "${HARDWARE_WALLET_PUBKEY}" \
  --domain "${SOLANA_DOMAIN_ID}" \
  --validators "${NULL_VALIDATOR}" \
  --threshold 1 \
  --output-unsigned-tx "${OUTPUT_DIR}/kill-gorchain-01.json" \
  2>&1 || true

cat > "${OUTPUT_DIR}/kill-gorchain-01.summary.txt" <<SUMMARY
Operation: Kill Switch — Gorchain ISM Reconfiguration
Program:   ${GORCHAIN_ISM_ID}
Action:    Set Multisig ISM validators to null address for remote domain ${SOLANA_DOMAIN_ID}
Validators: ${NULL_VALIDATOR}
Threshold:  1
Effect:     No relayer can deliver messages FROM Solana TO Gorchain
Signer:     ${HARDWARE_WALLET_PUBKEY} (hardware wallet — ISM owner)

To sign:  solana sign-offloaded-transaction kill-gorchain-01.json --signer usb://ledger
To submit: solana send-signed-transaction kill-gorchain-01.json --url ${GORCHAIN_RPC_URL}
SUMMARY

# Solana — set validators to null for messages FROM Gorchain
log "Building unsigned tx: reconfigure ISM on Solana..."
# TODO: UNVERIFIED — subcommand and args need confirmation from --help
${CLIENT} --url "${SOLANA_RPC_URL}" \
  multisig-ism-message-id set \
  --program-id "${SOLANA_ISM_ID}" \
  --authority "${HARDWARE_WALLET_PUBKEY}" \
  --domain "${GORCHAIN_DOMAIN_ID}" \
  --validators "${NULL_VALIDATOR}" \
  --threshold 1 \
  --output-unsigned-tx "${OUTPUT_DIR}/kill-solana-01.json" \
  2>&1 || true

cat > "${OUTPUT_DIR}/kill-solana-01.summary.txt" <<SUMMARY
Operation: Kill Switch — Solana ISM Reconfiguration
Program:   ${SOLANA_ISM_ID}
Action:    Set Multisig ISM validators to null address for remote domain ${GORCHAIN_DOMAIN_ID}
Validators: ${NULL_VALIDATOR}
Threshold:  1
Effect:     No relayer can deliver messages FROM Gorchain TO Solana
Signer:     ${HARDWARE_WALLET_PUBKEY} (hardware wallet — ISM owner)

To sign:  solana sign-offloaded-transaction kill-solana-01.json --signer usb://ledger
To submit: solana send-signed-transaction kill-solana-01.json --url ${SOLANA_RPC_URL}
SUMMARY

log "Unsigned transactions written to ${OUTPUT_DIR}/"
ls -la "${OUTPUT_DIR}/"
log "Kill switch preparation complete. Sign and submit the transactions with your hardware wallet."
