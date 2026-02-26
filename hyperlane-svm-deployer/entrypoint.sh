#!/usr/bin/env bash
# entrypoint.sh — Deploy Hyperlane core contracts on two SVM chains
#
# Required environment variables:
#   GORCHAIN_RPC_URL          — Gorchain RPC endpoint
#   SOLANA_RPC_URL            — Solana RPC endpoint
#   GORCHAIN_DOMAIN_ID        — Gorchain Hyperlane domain ID (default: 99999)
#   SOLANA_DOMAIN_ID          — Solana Hyperlane domain ID (default: 99998)
#   DEPLOYER_KEYPAIR          — JSON array of deployer secret key bytes
#   HARDWARE_WALLET_PUBKEY    — Pubkey to receive program ownership
#   IGP_ORACLE_PUBKEY         — Pubkey for IGP account ownership (Privy oracle wallet)
#   GORCHAIN_VALIDATOR_ADDRESS — H160 validator address for Gorchain's ISM
#   SOLANA_VALIDATOR_ADDRESS  — H160 validator address for Solana's ISM
#
# Optional:
#   IGP_GAS_ORACLE_TOKEN_EXCHANGE_RATE — default 1000000000
#   IGP_GAS_ORACLE_GAS_PRICE           — default 1
#   IGP_BENEFICIARY                    — default: deployer pubkey
#   DRY_RUN                            — if "true", print commands without executing
#   SKIP_VERIFICATION                  — if "true", skip post-deploy hash verification
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PROGRAM_DIR="/opt/hyperlane/programs"
ARTIFACTS_DIR="/opt/hyperlane/artifacts"
CLIENT="hyperlane-sealevel-client"

GORCHAIN_DOMAIN_ID="${GORCHAIN_DOMAIN_ID:-99999}"
SOLANA_DOMAIN_ID="${SOLANA_DOMAIN_ID:-99998}"
GORCHAIN_CHAIN_NAME="${GORCHAIN_CHAIN_NAME:-gorchain}"
SOLANA_CHAIN_NAME="${SOLANA_CHAIN_NAME:-solanasvm}"

IGP_GAS_ORACLE_TOKEN_EXCHANGE_RATE="${IGP_GAS_ORACLE_TOKEN_EXCHANGE_RATE:-1000000000}"
IGP_GAS_ORACLE_GAS_PRICE="${IGP_GAS_ORACLE_GAS_PRICE:-1}"

DRY_RUN="${DRY_RUN:-false}"
SKIP_VERIFICATION="${SKIP_VERIFICATION:-false}"

mkdir -p "${ARTIFACTS_DIR}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log() { echo "[deployer] $(date -u +%Y-%m-%dT%H:%M:%SZ) $*"; }
die() { log "FATAL: $*"; exit 1; }

run() {
    if [[ "${DRY_RUN}" == "true" ]]; then
        log "[dry-run] $*"
    else
        log "=> $*"
        "$@"
    fi
}

require_env() {
    for var in "$@"; do
        [[ -n "${!var:-}" ]] || die "Required env var ${var} is not set"
    done
}

# Write deployer keypair to file for solana CLI
setup_deployer_key() {
    local keyfile="/tmp/deployer-key.json"
    echo "${DEPLOYER_KEYPAIR}" > "${keyfile}"
    chmod 600 "${keyfile}"
    export DEPLOYER_KEYFILE="${keyfile}"

    DEPLOYER_PUBKEY="$(solana-keygen pubkey "${keyfile}")"
    export DEPLOYER_PUBKEY
    log "Deployer pubkey: ${DEPLOYER_PUBKEY}"
}

# Extract program ID from deploy output or keypair
get_program_id() {
    local program_so="$1"
    local keypair="${program_so%.so}-keypair.json"
    if [[ -f "${keypair}" ]]; then
        solana-keygen pubkey "${keypair}"
    fi
}

# ---------------------------------------------------------------------------
# Deploy programs on a single chain
# ---------------------------------------------------------------------------
deploy_chain() {
    local chain_name="$1"
    local rpc_url="$2"
    local domain_id="$3"
    local remote_domain_id="$4"
    local validator_address="$5"

    log "======================================"
    log "Deploying Hyperlane core on ${chain_name} (domain ${domain_id})"
    log "RPC: ${rpc_url}"
    log "======================================"

    local chain_artifacts="${ARTIFACTS_DIR}/${chain_name}"
    mkdir -p "${chain_artifacts}"

    # Configure solana CLI for this chain
    solana config set --url "${rpc_url}" --keypair "${DEPLOYER_KEYFILE}"

    # Check deployer balance
    local balance
    balance="$(solana balance "${DEPLOYER_PUBKEY}" --url "${rpc_url}" | awk '{print $1}')"
    log "Deployer balance on ${chain_name}: ${balance} SOL"

    # --- Deploy core programs ---

    # 1. Mailbox
    log "Deploying Mailbox..."
    run ${CLIENT} --url "${rpc_url}" \
        core deploy \
        --built-so-dir "${PROGRAM_DIR}" \
        --local-domain "${domain_id}" \
        --keypair "${DEPLOYER_KEYFILE}" \
        --chain "${chain_name}" \
        --write-program-ids "${chain_artifacts}/program-ids.json"

    # Read back program IDs
    local program_ids="${chain_artifacts}/program-ids.json"
    if [[ ! -f "${program_ids}" ]]; then
        die "program-ids.json not found after deploy on ${chain_name}"
    fi

    local mailbox_id igp_id ism_id va_id merkle_hook_id
    mailbox_id="$(jq -r '.mailbox' "${program_ids}")"
    igp_id="$(jq -r '.igp // .interchain_gas_paymaster // empty' "${program_ids}")"
    ism_id="$(jq -r '.multisig_ism_message_id // .ism // empty' "${program_ids}")"
    va_id="$(jq -r '.validator_announce // empty' "${program_ids}")"
    merkle_hook_id="$(jq -r '.merkle_tree_hook // empty' "${program_ids}")"

    log "Deployed on ${chain_name}:"
    log "  Mailbox:           ${mailbox_id}"
    log "  IGP:               ${igp_id}"
    log "  Multisig ISM:      ${ism_id}"
    log "  Validator Announce: ${va_id}"
    log "  Merkle Tree Hook:  ${merkle_hook_id}"

    # --- Configure Multisig ISM (1-of-1) ---
    log "Configuring Multisig ISM on ${chain_name} for remote domain ${remote_domain_id}..."

    local multisig_config="${chain_artifacts}/multisig-config.json"
    cat > "${multisig_config}" <<MCEOF
{
  "domain_id": ${remote_domain_id},
  "validators": ["${validator_address}"],
  "threshold": 1
}
MCEOF

    run ${CLIENT} --url "${rpc_url}" \
        multisig-ism set \
        --program-id "${ism_id}" \
        --keypair "${DEPLOYER_KEYFILE}" \
        --domain "${remote_domain_id}" \
        --validators "${validator_address}" \
        --threshold 1

    # --- Configure IGP gas oracle ---
    log "Configuring IGP gas oracle on ${chain_name}..."

    local gas_oracle_config="${chain_artifacts}/gas-oracle-config.json"
    cat > "${gas_oracle_config}" <<GOEOF
{
  "remote_domain": ${remote_domain_id},
  "token_exchange_rate": ${IGP_GAS_ORACLE_TOKEN_EXCHANGE_RATE},
  "gas_price": ${IGP_GAS_ORACLE_GAS_PRICE}
}
GOEOF

    run ${CLIENT} --url "${rpc_url}" \
        igp set-gas-oracle-configs \
        --program-id "${igp_id}" \
        --keypair "${DEPLOYER_KEYFILE}" \
        --remote-domain "${remote_domain_id}" \
        --token-exchange-rate "${IGP_GAS_ORACLE_TOKEN_EXCHANGE_RATE}" \
        --gas-price "${IGP_GAS_ORACLE_GAS_PRICE}"

    # Set IGP beneficiary
    local igp_beneficiary="${IGP_BENEFICIARY:-${DEPLOYER_PUBKEY}}"
    log "Setting IGP beneficiary to ${igp_beneficiary}..."

    run ${CLIENT} --url "${rpc_url}" \
        igp set-beneficiary \
        --program-id "${igp_id}" \
        --keypair "${DEPLOYER_KEYFILE}" \
        --beneficiary "${igp_beneficiary}"

    # --- Post-deploy hash verification ---
    if [[ "${SKIP_VERIFICATION}" != "true" ]]; then
        log "Verifying deployed program hashes on ${chain_name}..."
        verify_programs "${rpc_url}" "${chain_name}" "${program_ids}"
    else
        log "Skipping hash verification (SKIP_VERIFICATION=true)"
    fi

    # --- Transfer ownership ---
    log "Transferring ownership on ${chain_name}..."
    transfer_ownership "${rpc_url}" "${chain_name}" "${program_ids}"

    log "Deployment complete on ${chain_name}"
}

# ---------------------------------------------------------------------------
# Verify deployed program hashes match local .so files
# ---------------------------------------------------------------------------
verify_programs() {
    local rpc_url="$1"
    local chain_name="$2"
    local program_ids_file="$3"

    local all_passed=true

    # Map program names to .so files
    declare -A program_so_map
    program_so_map=(
        ["mailbox"]="hyperlane_sealevel_mailbox.so"
        ["igp"]="hyperlane_sealevel_igp.so"
        ["multisig_ism_message_id"]="hyperlane_sealevel_multisig_ism_message_id.so"
        ["validator_announce"]="hyperlane_sealevel_validator_announce.so"
        ["merkle_tree_hook"]="hpl_hook_merkle_tree.so"
    )

    for prog_key in "${!program_so_map[@]}"; do
        local prog_id
        prog_id="$(jq -r ".${prog_key} // empty" "${program_ids_file}")"
        if [[ -z "${prog_id}" ]]; then
            continue
        fi

        local so_file="${PROGRAM_DIR}/${program_so_map[${prog_key}]}"
        if [[ ! -f "${so_file}" ]]; then
            log "WARNING: .so file not found for ${prog_key}: ${so_file}"
            continue
        fi

        log "  Verifying ${prog_key} (${prog_id})..."

        local local_hash on_chain_hash
        local_hash="$(solana-verify get-executable-hash "${so_file}" 2>/dev/null || echo "UNKNOWN")"
        on_chain_hash="$(solana-verify get-program-hash -u "${rpc_url}" "${prog_id}" 2>/dev/null || echo "UNKNOWN")"

        if [[ "${local_hash}" == "${on_chain_hash}" && "${local_hash}" != "UNKNOWN" ]]; then
            log "    PASS: ${prog_key} hash matches (${local_hash})"
        else
            log "    FAIL: ${prog_key} hash mismatch!"
            log "      Local:    ${local_hash}"
            log "      On-chain: ${on_chain_hash}"
            all_passed=false
        fi
    done

    if [[ "${all_passed}" != "true" ]]; then
        die "Program hash verification failed on ${chain_name}! Aborting."
    fi

    log "All program hashes verified on ${chain_name}"
}

# ---------------------------------------------------------------------------
# Transfer ownership: programs → hardware wallet, IGP → oracle wallet
# ---------------------------------------------------------------------------
transfer_ownership() {
    local rpc_url="$1"
    local chain_name="$2"
    local program_ids_file="$3"

    # Transfer program upgrade authority to hardware wallet for all programs
    log "Transferring program upgrade authority to ${HARDWARE_WALLET_PUBKEY}..."

    local programs
    programs="$(jq -r 'to_entries[] | .value' "${program_ids_file}")"

    for prog_id in ${programs}; do
        log "  Transferring upgrade authority for ${prog_id}..."
        run solana program set-upgrade-authority "${prog_id}" \
            --new-upgrade-authority "${HARDWARE_WALLET_PUBKEY}" \
            --keypair "${DEPLOYER_KEYFILE}" \
            --url "${rpc_url}"
    done

    # Transfer IGP account ownership to Privy oracle wallet
    local igp_id
    igp_id="$(jq -r '.igp // .interchain_gas_paymaster // empty' "${program_ids_file}")"
    if [[ -n "${igp_id}" && -n "${IGP_ORACLE_PUBKEY:-}" ]]; then
        log "  Transferring IGP account ownership to oracle wallet ${IGP_ORACLE_PUBKEY}..."
        run ${CLIENT} --url "${rpc_url}" \
            igp transfer-ownership \
            --program-id "${igp_id}" \
            --keypair "${DEPLOYER_KEYFILE}" \
            --new-owner "${IGP_ORACLE_PUBKEY}"
    fi

    # Transfer other program account ownerships to hardware wallet
    for prog_key in mailbox multisig_ism_message_id validator_announce merkle_tree_hook; do
        local prog_id
        prog_id="$(jq -r ".${prog_key} // empty" "${program_ids_file}")"
        if [[ -n "${prog_id}" ]]; then
            log "  Transferring ${prog_key} account ownership to ${HARDWARE_WALLET_PUBKEY}..."
            run ${CLIENT} --url "${rpc_url}" \
                core transfer-ownership \
                --program-id "${prog_id}" \
                --keypair "${DEPLOYER_KEYFILE}" \
                --new-owner "${HARDWARE_WALLET_PUBKEY}" || true
        fi
    done
}

# ---------------------------------------------------------------------------
# Generate and apply k8s ConfigMaps/Secrets
# ---------------------------------------------------------------------------
create_k8s_artifacts() {
    log "Creating Kubernetes ConfigMaps and Secrets..."

    local gorchain_ids="${ARTIFACTS_DIR}/${GORCHAIN_CHAIN_NAME}/program-ids.json"
    local solana_ids="${ARTIFACTS_DIR}/${SOLANA_CHAIN_NAME}/program-ids.json"

    # Build agent-config.json
    local agent_config="${ARTIFACTS_DIR}/agent-config.json"
    build_agent_config "${agent_config}" \
        "${gorchain_ids}" "${solana_ids}"

    # Create ConfigMap: hyperlane-agent-config
    run kubectl create configmap hyperlane-agent-config \
        --from-file=agent-config.json="${agent_config}" \
        --dry-run=client -o yaml > "${ARTIFACTS_DIR}/configmap-agent-config.yaml"

    # Create ConfigMap: hyperlane-program-ids (per chain)
    run kubectl create configmap hyperlane-program-ids \
        --from-file="${GORCHAIN_CHAIN_NAME}-program-ids.json=${gorchain_ids}" \
        --from-file="${SOLANA_CHAIN_NAME}-program-ids.json=${solana_ids}" \
        --dry-run=client -o yaml > "${ARTIFACTS_DIR}/configmap-program-ids.yaml"

    # Create ConfigMaps for gas oracle and multisig configs
    local gorchain_gas="${ARTIFACTS_DIR}/${GORCHAIN_CHAIN_NAME}/gas-oracle-config.json"
    local solana_gas="${ARTIFACTS_DIR}/${SOLANA_CHAIN_NAME}/gas-oracle-config.json"
    local gorchain_multisig="${ARTIFACTS_DIR}/${GORCHAIN_CHAIN_NAME}/multisig-config.json"
    local solana_multisig="${ARTIFACTS_DIR}/${SOLANA_CHAIN_NAME}/multisig-config.json"

    run kubectl create configmap hyperlane-gas-oracle-config \
        --from-file="${GORCHAIN_CHAIN_NAME}.json=${gorchain_gas}" \
        --from-file="${SOLANA_CHAIN_NAME}.json=${solana_gas}" \
        --dry-run=client -o yaml > "${ARTIFACTS_DIR}/configmap-gas-oracle.yaml"

    run kubectl create configmap hyperlane-multisig-config \
        --from-file="${GORCHAIN_CHAIN_NAME}.json=${gorchain_multisig}" \
        --from-file="${SOLANA_CHAIN_NAME}.json=${solana_multisig}" \
        --dry-run=client -o yaml > "${ARTIFACTS_DIR}/configmap-multisig.yaml"

    # Apply ConfigMaps if we have kubectl access
    if kubectl cluster-info &>/dev/null; then
        log "Applying ConfigMaps to cluster..."
        run kubectl apply -f "${ARTIFACTS_DIR}/configmap-agent-config.yaml"
        run kubectl apply -f "${ARTIFACTS_DIR}/configmap-program-ids.yaml"
        run kubectl apply -f "${ARTIFACTS_DIR}/configmap-gas-oracle.yaml"
        run kubectl apply -f "${ARTIFACTS_DIR}/configmap-multisig.yaml"
    else
        log "No cluster access — ConfigMap YAMLs written to ${ARTIFACTS_DIR}/"
    fi

    log "Kubernetes artifacts created in ${ARTIFACTS_DIR}/"
}

# ---------------------------------------------------------------------------
# Build the agent-config.json consumed by validators and relayer
# ---------------------------------------------------------------------------
build_agent_config() {
    local output_file="$1"
    local gorchain_ids="$2"
    local solana_ids="$3"

    local gor_mailbox gor_igp gor_va gor_merkle
    gor_mailbox="$(jq -r '.mailbox' "${gorchain_ids}")"
    gor_igp="$(jq -r '.igp // .interchain_gas_paymaster // ""' "${gorchain_ids}")"
    gor_va="$(jq -r '.validator_announce // ""' "${gorchain_ids}")"
    gor_merkle="$(jq -r '.merkle_tree_hook // ""' "${gorchain_ids}")"

    local sol_mailbox sol_igp sol_va sol_merkle
    sol_mailbox="$(jq -r '.mailbox' "${solana_ids}")"
    sol_igp="$(jq -r '.igp // .interchain_gas_paymaster // ""' "${solana_ids}")"
    sol_va="$(jq -r '.validator_announce // ""' "${solana_ids}")"
    sol_merkle="$(jq -r '.merkle_tree_hook // ""' "${solana_ids}")"

    cat > "${output_file}" <<ACEOF
{
  "chains": {
    "${GORCHAIN_CHAIN_NAME}": {
      "name": "${GORCHAIN_CHAIN_NAME}",
      "chainId": ${GORCHAIN_DOMAIN_ID},
      "domainId": ${GORCHAIN_DOMAIN_ID},
      "protocol": "sealevel",
      "mailbox": "${gor_mailbox}",
      "interchainGasPaymaster": "${gor_igp}",
      "validatorAnnounce": "${gor_va}",
      "merkleTreeHook": "${gor_merkle}",
      "rpcUrls": [
        { "http": "${GORCHAIN_RPC_URL}" }
      ],
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
    "${SOLANA_CHAIN_NAME}": {
      "name": "${SOLANA_CHAIN_NAME}",
      "chainId": ${SOLANA_DOMAIN_ID},
      "domainId": ${SOLANA_DOMAIN_ID},
      "protocol": "sealevel",
      "mailbox": "${sol_mailbox}",
      "interchainGasPaymaster": "${sol_igp}",
      "validatorAnnounce": "${sol_va}",
      "merkleTreeHook": "${sol_merkle}",
      "rpcUrls": [
        { "http": "${SOLANA_RPC_URL}" }
      ],
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
ACEOF

    log "Generated agent-config.json with both chains"
}

# ---------------------------------------------------------------------------
# Discard deployer key
# ---------------------------------------------------------------------------
discard_deployer_key() {
    log "Discarding hot deployer key..."
    if [[ -f "${DEPLOYER_KEYFILE}" ]]; then
        # Overwrite with random data before removing
        dd if=/dev/urandom of="${DEPLOYER_KEYFILE}" bs=1 count=256 2>/dev/null
        rm -f "${DEPLOYER_KEYFILE}"
    fi
    unset DEPLOYER_KEYPAIR
    log "Deployer key discarded"
}

# ===========================================================================
# Main
# ===========================================================================
main() {
    log "Hyperlane SVM Deployer starting"
    log "Gorchain domain: ${GORCHAIN_DOMAIN_ID}, Solana domain: ${SOLANA_DOMAIN_ID}"

    # Validate required env vars
    require_env GORCHAIN_RPC_URL SOLANA_RPC_URL DEPLOYER_KEYPAIR \
        HARDWARE_WALLET_PUBKEY IGP_ORACLE_PUBKEY \
        GORCHAIN_VALIDATOR_ADDRESS SOLANA_VALIDATOR_ADDRESS

    # Setup deployer key
    setup_deployer_key

    # Deploy on Gorchain
    deploy_chain \
        "${GORCHAIN_CHAIN_NAME}" \
        "${GORCHAIN_RPC_URL}" \
        "${GORCHAIN_DOMAIN_ID}" \
        "${SOLANA_DOMAIN_ID}" \
        "${SOLANA_VALIDATOR_ADDRESS}"

    # Deploy on Solana
    deploy_chain \
        "${SOLANA_CHAIN_NAME}" \
        "${SOLANA_RPC_URL}" \
        "${SOLANA_DOMAIN_ID}" \
        "${GORCHAIN_DOMAIN_ID}" \
        "${GORCHAIN_VALIDATOR_ADDRESS}"

    # Create k8s artifacts
    create_k8s_artifacts

    # Discard deployer key
    discard_deployer_key

    log "======================================"
    log "Deployment complete on both chains!"
    log "Artifacts: ${ARTIFACTS_DIR}/"
    log "======================================"
}

main "$@"
