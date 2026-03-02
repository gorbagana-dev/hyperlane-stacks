#!/bin/bash
# chain.sh — Chain node lifecycle for Hyperlane e2e tests
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

SOLANA_VALIDATOR_PID_FILE="${E2E_DIR}/.solana-validator.pid"
SOLANA_LEDGER_DIR="/tmp/solana-test-ledger"

# ---------------------------------------------------------------------------
# Gorchain (placeholder — must be running externally for now)
# ---------------------------------------------------------------------------
start_gorchain() {
    log_info "WARNING: Gorchain must be running externally on port 8899."
    log_info "TODO: implement gorchain start via laconic-so"
    log_info "To start gorchain manually, use the gorchain-stacks repo:"
    log_info "  laconic-so --stack gorchain-stacks/... deploy init / create / start"
}

stop_gorchain() {
    log_info "WARNING: Gorchain stop is a placeholder."
    log_info "TODO: implement gorchain stop via laconic-so"
}

# ---------------------------------------------------------------------------
# Solana test validator
# ---------------------------------------------------------------------------
start_solana_test_validator() {
    log_info "Starting Solana test validator on port 18899..."

    # Kill any existing validator
    if [[ -f "${SOLANA_VALIDATOR_PID_FILE}" ]]; then
        local old_pid
        old_pid="$(cat "${SOLANA_VALIDATOR_PID_FILE}")"
        kill "${old_pid}" 2>/dev/null || true
        rm -f "${SOLANA_VALIDATOR_PID_FILE}"
    fi

    rm -rf "${SOLANA_LEDGER_DIR}"

    solana-test-validator \
        --ledger "${SOLANA_LEDGER_DIR}" \
        --rpc-port 18899 \
        --quiet &
    local pid=$!
    echo "${pid}" > "${SOLANA_VALIDATOR_PID_FILE}"

    log_info "Solana test validator started (PID ${pid})"

    wait_for_rpc_health "http://localhost:18899" 30
}

stop_solana_test_validator() {
    log_info "Stopping Solana test validator..."

    if [[ -f "${SOLANA_VALIDATOR_PID_FILE}" ]]; then
        local pid
        pid="$(cat "${SOLANA_VALIDATOR_PID_FILE}")"
        kill "${pid}" 2>/dev/null || true
        rm -f "${SOLANA_VALIDATOR_PID_FILE}"
        log_info "Solana test validator stopped (PID ${pid})"
    else
        log_info "No Solana test validator PID file found"
    fi

    rm -rf "${SOLANA_LEDGER_DIR}"
}
