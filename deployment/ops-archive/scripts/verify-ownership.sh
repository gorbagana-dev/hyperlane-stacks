set -euo pipefail

PASS=0
FAIL=0

log() { echo "[verify] $(date -u +%Y-%m-%dT%H:%M:%SZ) $*"; }

check_upgrade_authority() {
  local chain_name="$1"
  local rpc_url="$2"
  local prog_key="$3"
  local prog_id="$4"
  local expected_authority="$5"

  local actual_authority
  actual_authority="$(solana program show "${prog_id}" --url "${rpc_url}" --output json 2>/dev/null \
    | jq -r '.authority // "NONE"')" || actual_authority="ERROR"

  if [[ "${actual_authority}" == "${expected_authority}" ]]; then
    log "  PASS: ${chain_name}/${prog_key} upgrade authority = ${actual_authority}"
    PASS=$((PASS + 1))
  else
    log "  FAIL: ${chain_name}/${prog_key} upgrade authority"
    log "    Expected: ${expected_authority}"
    log "    Actual:   ${actual_authority}"
    FAIL=$((FAIL + 1))
  fi
}

verify_chain() {
  local chain_name="$1"
  local rpc_url="$2"
  local programs_file="$3"

  log "======================================"
  log "Verifying ownership on ${chain_name}"
  log "======================================"

  local prog_keys
  prog_keys="$(jq -r 'keys[]' "${programs_file}")"

  for prog_key in ${prog_keys}; do
    local prog_id
    prog_id="$(jq -r ".${prog_key}" "${programs_file}")"

    # All programs should have upgrade authority = hardware wallet
    check_upgrade_authority "${chain_name}" "${rpc_url}" \
      "${prog_key}" "${prog_id}" "${HARDWARE_WALLET_PUBKEY}"
  done

  # Verify IGP account ownership -> oracle wallet
  # WARNING: `igp get-owner` does not exist as a hyperlane-sealevel-client subcommand.
  # There is no known CLI command to query IGP account ownership directly.
  # The `igp query` subcommand may provide this info, but its output format is unverified.
  # For now, this check is skipped. Verify IGP ownership manually or via on-chain inspection.
  local igp_id
  igp_id="$(jq -r '.igp // .interchain_gas_paymaster // empty' "${programs_file}")"
  if [[ -n "${igp_id}" && -n "${IGP_ORACLE_PUBKEY:-}" ]]; then
    log "  SKIP: ${chain_name}/igp account owner check — no CLI command available to query IGP ownership"
    log "    IGP program: ${igp_id}"
    log "    Expected owner: ${IGP_ORACLE_PUBKEY}"
    log "    Verify manually with: hyperlane-sealevel-client --url ${rpc_url} igp query --program-id ${igp_id}"
  fi
}

verify_chain "gorchain" "${GORCHAIN_RPC_URL}" \
  /config/program-ids/gorchain-program-ids.json

verify_chain "solana" "${SOLANA_RPC_URL}" \
  /config/program-ids/solana-program-ids.json

log ""
log "======================================"
log "Results: ${PASS} passed, ${FAIL} failed"
log "======================================"

if [[ "${FAIL}" -gt 0 ]]; then
  log "OWNERSHIP VERIFICATION FAILED"
  exit 1
else
  log "All ownership checks passed"
fi
