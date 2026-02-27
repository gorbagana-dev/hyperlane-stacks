set -euo pipefail

OUTPUT_DIR="/output"
CLIENT="hyperlane-sealevel-client"
mkdir -p "${OUTPUT_DIR}"
SEQ=0

DRY_RUN="${DRY_RUN:-true}"
CONFIRM_TEARDOWN="${CONFIRM_TEARDOWN:-no}"

log() { echo "[teardown] $(date -u +%Y-%m-%dT%H:%M:%SZ) $*"; }

if [[ "${DRY_RUN}" != "true" && "${CONFIRM_TEARDOWN}" != "yes" ]]; then
  log "FATAL: CONFIRM_TEARDOWN must be 'yes' when DRY_RUN=false"
  log "This is a destructive operation. Set CONFIRM_TEARDOWN=yes to proceed."
  exit 1
fi

if [[ "${DRY_RUN}" == "true" ]]; then
  log "=== DRY RUN MODE — no transactions will be built or submitted ==="
fi

next_seq() { SEQ=$((SEQ + 1)); printf "%02d" "${SEQ}"; }

write_summary() {
  local file="$1" content="$2"
  echo "${content}" > "${OUTPUT_DIR}/${file}"
}

# Load program IDs for both chains
GORCHAIN_PROGRAMS="$(cat /config/program-ids/gorchain-program-ids.json)"
SOLANA_PROGRAMS="$(cat /config/program-ids/solana-program-ids.json)"

teardown_chain() {
  local chain_name="$1"
  local rpc_url="$2"
  local programs_json="$3"

  log "======================================"
  log "Teardown: ${chain_name}"
  log "RPC: ${rpc_url}"
  log "======================================"

  # Step 1: Scale agents to 0
  log "Step 1: Scale agents to 0..."
  if [[ "${DRY_RUN}" == "true" ]]; then
    log "  [dry-run] Would scale validator-${chain_name}, relayer to 0"
  else
    kubectl scale deployment "hyperlane-validator-${chain_name}" --replicas=0 2>/dev/null || true
    kubectl scale deployment hyperlane-relayer --replicas=0 2>/dev/null || true
  fi

  # Step 2: Claim remaining IGP fees
  local igp_id
  igp_id="$(echo "${programs_json}" | jq -r '.igp // .interchain_gas_paymaster // empty')"
  if [[ -n "${igp_id}" ]]; then
    local n; n="$(next_seq)"
    log "Step 2: Claim remaining IGP fees from ${igp_id}..."
    if [[ "${DRY_RUN}" == "true" ]]; then
      log "  [dry-run] Would claim IGP fees"
    else
      ${CLIENT} --url "${rpc_url}" \
        igp claim \
        --program-id "${igp_id}" \
        --authority "${HARDWARE_WALLET_PUBKEY}" \
        --output-unsigned-tx "${OUTPUT_DIR}/teardown-${chain_name}-${n}.json" \
        2>&1 || true

      write_summary "teardown-${chain_name}-${n}.summary.txt" \
        "Operation: Claim remaining IGP fees on ${chain_name}
Program: ${igp_id}
Signer: ${HARDWARE_WALLET_PUBKEY}"
    fi
  fi

  # Step 3: Close all programs (recovers rent)
  log "Step 3: Close programs on ${chain_name}..."
  local prog_keys
  prog_keys="$(echo "${programs_json}" | jq -r 'keys[]')"

  for prog_key in ${prog_keys}; do
    local prog_id
    prog_id="$(echo "${programs_json}" | jq -r ".${prog_key}")"
    local n; n="$(next_seq)"

    log "  Closing ${prog_key} (${prog_id})..."
    if [[ "${DRY_RUN}" == "true" ]]; then
      log "    [dry-run] Would close program ${prog_id}, rent recovered to ${TREASURY_ADDRESS}"
    else
      solana program close "${prog_id}" \
        --recipient "${TREASURY_ADDRESS}" \
        --authority "${HARDWARE_WALLET_PUBKEY}" \
        --url "${rpc_url}" \
        --output-unsigned-tx "${OUTPUT_DIR}/teardown-${chain_name}-${n}.json" \
        2>&1 || true

      write_summary "teardown-${chain_name}-${n}.summary.txt" \
        "Operation: Close program ${prog_key} on ${chain_name}
Program: ${prog_id}
Rent recovered to: ${TREASURY_ADDRESS}
Signer: ${HARDWARE_WALLET_PUBKEY} (upgrade authority)"
    fi
  done

  # Step 4: Close orphaned buffer accounts
  local n; n="$(next_seq)"
  log "Step 4: Close orphaned buffer accounts on ${chain_name}..."
  if [[ "${DRY_RUN}" == "true" ]]; then
    log "  [dry-run] Would close buffer accounts owned by ${HARDWARE_WALLET_PUBKEY}"
  else
    solana program close --buffers \
      --authority "${HARDWARE_WALLET_PUBKEY}" \
      --recipient "${TREASURY_ADDRESS}" \
      --url "${rpc_url}" \
      --output-unsigned-tx "${OUTPUT_DIR}/teardown-${chain_name}-${n}.json" \
      2>&1 || true

    write_summary "teardown-${chain_name}-${n}.summary.txt" \
      "Operation: Close orphaned deploy buffer accounts on ${chain_name}
Rent recovered to: ${TREASURY_ADDRESS}
Signer: ${HARDWARE_WALLET_PUBKEY}"
  fi

  log "Teardown prepared for ${chain_name}"
}

# Run teardown on both chains
teardown_chain "gorchain" "${GORCHAIN_RPC_URL}" "${GORCHAIN_PROGRAMS}"
teardown_chain "solana" "${SOLANA_RPC_URL}" "${SOLANA_PROGRAMS}"

log ""
if [[ "${DRY_RUN}" == "true" ]]; then
  log "=== DRY RUN COMPLETE ==="
  log "Re-run with DRY_RUN=false CONFIRM_TEARDOWN=yes to generate unsigned transactions"
else
  log "Unsigned transactions written to ${OUTPUT_DIR}/"
  ls -la "${OUTPUT_DIR}/"
  log ""
  log "Sign each transaction with hardware wallet and submit in order:"
  log "  solana sign-offloaded-transaction <file>.json --signer usb://ledger"
  log "  solana send-signed-transaction <file>.json --url <RPC_URL>"
fi
