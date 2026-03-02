#!/bin/bash
# keygen.sh — Keypair generation and funding for Hyperlane e2e tests
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

KEYS_DIR="${E2E_DIR}/.keys"

# ---------------------------------------------------------------------------
# generate_test_keypairs — create all required test keypairs
# ---------------------------------------------------------------------------
generate_test_keypairs() {
    log_info "Generating test keypairs..."

    mkdir -p "${KEYS_DIR}"

    # --- Ed25519 keypairs (Solana format) ---

    # Deployer keypair
    log_info "  Generating deployer keypair..."
    solana-keygen new --no-bip39-passphrase -o "${KEYS_DIR}/deployer.json" --force
    DEPLOYER_PUBKEY="$(solana-keygen pubkey "${KEYS_DIR}/deployer.json")"
    DEPLOYER_KEYPAIR="$(cat "${KEYS_DIR}/deployer.json")"
    export DEPLOYER_PUBKEY DEPLOYER_KEYPAIR

    # Hardware wallet keypair (simulated — we just need the pubkey)
    log_info "  Generating hardware wallet keypair..."
    solana-keygen new --no-bip39-passphrase -o "${KEYS_DIR}/hardware-wallet.json" --force
    HARDWARE_WALLET_PUBKEY="$(solana-keygen pubkey "${KEYS_DIR}/hardware-wallet.json")"
    export HARDWARE_WALLET_PUBKEY

    # IGP oracle keypair
    log_info "  Generating IGP oracle keypair..."
    solana-keygen new --no-bip39-passphrase -o "${KEYS_DIR}/igp-oracle.json" --force
    IGP_ORACLE_PUBKEY="$(solana-keygen pubkey "${KEYS_DIR}/igp-oracle.json")"
    export IGP_ORACLE_PUBKEY

    # --- secp256k1 keypairs (for validator signing) ---

    # Gorchain validator key
    log_info "  Generating Gorchain validator secp256k1 key..."
    cast wallet new --json > "${KEYS_DIR}/gorchain-validator.json"
    GORCHAIN_VALIDATOR_ADDRESS="$(jq -r '.address' "${KEYS_DIR}/gorchain-validator.json")"
    export GORCHAIN_VALIDATOR_ADDRESS

    # Solana validator key
    log_info "  Generating Solana validator secp256k1 key..."
    cast wallet new --json > "${KEYS_DIR}/solana-validator.json"
    SOLANA_VALIDATOR_ADDRESS="$(jq -r '.address' "${KEYS_DIR}/solana-validator.json")"
    export SOLANA_VALIDATOR_ADDRESS

    log_info "Test keypairs generated:"
    log_info "  Deployer pubkey:            ${DEPLOYER_PUBKEY}"
    log_info "  Hardware wallet pubkey:     ${HARDWARE_WALLET_PUBKEY}"
    log_info "  IGP oracle pubkey:          ${IGP_ORACLE_PUBKEY}"
    log_info "  Gorchain validator (H160):  ${GORCHAIN_VALIDATOR_ADDRESS}"
    log_info "  Solana validator (H160):    ${SOLANA_VALIDATOR_ADDRESS}"
}

# ---------------------------------------------------------------------------
# fund_wallets — airdrop SOL to test wallets on both chains
#   $1: gorchain RPC URL
#   $2: solana RPC URL
# ---------------------------------------------------------------------------
fund_wallets() {
    local gorchain_rpc="${1:-http://localhost:8899}"
    local solana_rpc="${2:-http://localhost:18899}"

    log_info "Funding test wallets..."

    local deployer_pubkey
    deployer_pubkey="$(solana-keygen pubkey "${KEYS_DIR}/deployer.json")"

    local hw_pubkey
    hw_pubkey="$(solana-keygen pubkey "${KEYS_DIR}/hardware-wallet.json")"

    local oracle_pubkey
    oracle_pubkey="$(solana-keygen pubkey "${KEYS_DIR}/igp-oracle.json")"

    # Fund on Solana test validator
    log_info "  Funding wallets on Solana (${solana_rpc})..."
    solana airdrop 100 "${deployer_pubkey}" --url "${solana_rpc}" || log_error "Airdrop to deployer on Solana failed"
    solana airdrop 1 "${hw_pubkey}" --url "${solana_rpc}" || log_error "Airdrop to hardware wallet on Solana failed"
    solana airdrop 1 "${oracle_pubkey}" --url "${solana_rpc}" || log_error "Airdrop to IGP oracle on Solana failed"

    # Fund on Gorchain (if available)
    log_info "  Funding wallets on Gorchain (${gorchain_rpc})..."
    solana airdrop 100 "${deployer_pubkey}" --url "${gorchain_rpc}" || log_error "Airdrop to deployer on Gorchain failed (gorchain may not be running)"
    solana airdrop 1 "${hw_pubkey}" --url "${gorchain_rpc}" || log_error "Airdrop to hardware wallet on Gorchain failed"
    solana airdrop 1 "${oracle_pubkey}" --url "${gorchain_rpc}" || log_error "Airdrop to IGP oracle on Gorchain failed"

    log_info "Wallet funding complete"
}

# ---------------------------------------------------------------------------
# create_deployer_secrets — create k8s Secret for the core deployer
#   $1: namespace
# ---------------------------------------------------------------------------
create_deployer_secrets() {
    local namespace="${1:-$(get_namespace)}"

    log_info "Creating deployer secrets in namespace ${namespace}..."

    kubectl create secret generic hyperlane-deployer-secrets \
        -n "${namespace}" \
        --from-literal=DEPLOYER_KEYPAIR="${DEPLOYER_KEYPAIR}" \
        --from-literal=HARDWARE_WALLET_PUBKEY="${HARDWARE_WALLET_PUBKEY}" \
        --from-literal=IGP_ORACLE_PUBKEY="${IGP_ORACLE_PUBKEY}" \
        --from-literal=GORCHAIN_VALIDATOR_ADDRESS="${GORCHAIN_VALIDATOR_ADDRESS}" \
        --from-literal=SOLANA_VALIDATOR_ADDRESS="${SOLANA_VALIDATOR_ADDRESS}" \
        --dry-run=client -o yaml | kubectl apply -f -

    log_info "Deployer secrets created"
}

# ---------------------------------------------------------------------------
# create_warp_deployer_secrets — create k8s Secret for the warp deployer
#   $1: namespace
# ---------------------------------------------------------------------------
create_warp_deployer_secrets() {
    local namespace="${1:-$(get_namespace)}"

    log_info "Creating warp deployer secrets in namespace ${namespace}..."

    kubectl create secret generic hyperlane-warp-deployer-secrets \
        -n "${namespace}" \
        --from-literal=DEPLOYER_KEYPAIR="${DEPLOYER_KEYPAIR}" \
        --from-literal=HARDWARE_WALLET_PUBKEY="${HARDWARE_WALLET_PUBKEY}" \
        --dry-run=client -o yaml | kubectl apply -f -

    log_info "Warp deployer secrets created"
}
