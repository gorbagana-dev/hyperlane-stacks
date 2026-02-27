set -euo pipefail

OUTPUT_DIR="/output"
CLIENT="hyperlane-sealevel-client"
mkdir -p "${OUTPUT_DIR}"

log() { echo "[restore] $(date -u +%Y-%m-%dT%H:%M:%SZ) $*"; }

# Validate required env
for var in GORCHAIN_VALIDATOR_ADDRESS SOLANA_VALIDATOR_ADDRESS HARDWARE_WALLET_PUBKEY; do
  [[ -n "${!var:-}" ]] || { log "FATAL: ${var} not set"; exit 1; }
done

# Load program IDs
GORCHAIN_ISM_ID="$(jq -r '.multisig_ism_message_id // .multisig_ism // .ism' /config/program-ids/gorchain-program-ids.json)"
SOLANA_ISM_ID="$(jq -r '.multisig_ism_message_id // .multisig_ism // .ism' /config/program-ids/solana-program-ids.json)"

log "Restoring ISM configuration..."
log "Gorchain ISM: ${GORCHAIN_ISM_ID} -> validator ${SOLANA_VALIDATOR_ADDRESS}"
log "Solana ISM:   ${SOLANA_ISM_ID} -> validator ${GORCHAIN_VALIDATOR_ADDRESS}"

# Gorchain — restore real Solana validator (messages FROM Solana need Solana validator)
log "Building unsigned tx: restore ISM on Gorchain..."
${CLIENT} --url "${GORCHAIN_RPC_URL}" \
  multisig-ism set \
  --program-id "${GORCHAIN_ISM_ID}" \
  --authority "${HARDWARE_WALLET_PUBKEY}" \
  --domain "${SOLANA_DOMAIN_ID}" \
  --validators "${SOLANA_VALIDATOR_ADDRESS}" \
  --threshold 1 \
  --output-unsigned-tx "${OUTPUT_DIR}/restore-gorchain-01.json" \
  2>&1 || true

cat > "${OUTPUT_DIR}/restore-gorchain-01.summary.txt" <<SUMMARY
Operation: Restore — Gorchain ISM Reconfiguration
Program:   ${GORCHAIN_ISM_ID}
Action:    Restore Multisig ISM validators for remote domain ${SOLANA_DOMAIN_ID}
Validators: ${SOLANA_VALIDATOR_ADDRESS}
Threshold:  1
Effect:     Re-enables message delivery FROM Solana TO Gorchain
Signer:     ${HARDWARE_WALLET_PUBKEY} (hardware wallet — ISM owner)

To sign:  solana sign-offloaded-transaction restore-gorchain-01.json --signer usb://ledger
To submit: solana send-signed-transaction restore-gorchain-01.json --url ${GORCHAIN_RPC_URL}
SUMMARY

# Solana — restore real Gorchain validator
log "Building unsigned tx: restore ISM on Solana..."
${CLIENT} --url "${SOLANA_RPC_URL}" \
  multisig-ism set \
  --program-id "${SOLANA_ISM_ID}" \
  --authority "${HARDWARE_WALLET_PUBKEY}" \
  --domain "${GORCHAIN_DOMAIN_ID}" \
  --validators "${GORCHAIN_VALIDATOR_ADDRESS}" \
  --threshold 1 \
  --output-unsigned-tx "${OUTPUT_DIR}/restore-solana-01.json" \
  2>&1 || true

cat > "${OUTPUT_DIR}/restore-solana-01.summary.txt" <<SUMMARY
Operation: Restore — Solana ISM Reconfiguration
Program:   ${SOLANA_ISM_ID}
Action:    Restore Multisig ISM validators for remote domain ${GORCHAIN_DOMAIN_ID}
Validators: ${GORCHAIN_VALIDATOR_ADDRESS}
Threshold:  1
Effect:     Re-enables message delivery FROM Gorchain TO Solana
Signer:     ${HARDWARE_WALLET_PUBKEY} (hardware wallet — ISM owner)

To sign:  solana sign-offloaded-transaction restore-solana-01.json --signer usb://ledger
To submit: solana send-signed-transaction restore-solana-01.json --url ${SOLANA_RPC_URL}
SUMMARY

log "Unsigned transactions written to ${OUTPUT_DIR}/"
ls -la "${OUTPUT_DIR}/"

log ""
log "NEXT STEPS after signing and submitting the above transactions:"
log "  1. Scale agents back up:"
log "     kubectl scale deployment hyperlane-validator-gorchain --replicas=1"
log "     kubectl scale deployment hyperlane-validator-solana --replicas=1"
log "     kubectl scale deployment hyperlane-relayer --replicas=1"
log "  2. Messages dispatched during the pause will be delivered once agents resume."
