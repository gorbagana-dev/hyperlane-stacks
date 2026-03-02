#!/bin/bash
# run.sh — Main e2e test orchestrator for Hyperlane SVM stacks
#
# Usage:
#   ./tests/e2e/run.sh [options]
#
# Options:
#   --skip-cluster-setup   Skip kind cluster creation (reuse existing)
#   --skip-chain-setup     Skip starting chain nodes (assume running)
#   --skip-build           Skip container image builds (assume cached)
#   --skip-cleanup         Don't tear down after tests
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source all library files
source "${SCRIPT_DIR}/lib/common.sh"
source "${SCRIPT_DIR}/lib/cluster.sh"
source "${SCRIPT_DIR}/lib/chain.sh"
source "${SCRIPT_DIR}/lib/deploy.sh"
source "${SCRIPT_DIR}/lib/keygen.sh"

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
SKIP_CLUSTER_SETUP=false
SKIP_CHAIN_SETUP=false
SKIP_BUILD=false
SKIP_CLEANUP=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-cluster-setup) SKIP_CLUSTER_SETUP=true; shift ;;
        --skip-chain-setup)   SKIP_CHAIN_SETUP=true; shift ;;
        --skip-build)         SKIP_BUILD=true; shift ;;
        --skip-cleanup)       SKIP_CLEANUP=true; shift ;;
        *)
            log_error "Unknown option: $1"
            log_info "Usage: $0 [--skip-cluster-setup] [--skip-chain-setup] [--skip-build] [--skip-cleanup]"
            exit 1
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Test result tracking
# ---------------------------------------------------------------------------
TESTS_PASSED=0
TESTS_FAILED=0
TESTS_RUN=()

run_test() {
    local test_script="$1"
    local test_name
    test_name="$(basename "${test_script}" .sh)"

    log_info "=========================================="
    log_info "Running test: ${test_name}"
    log_info "=========================================="

    TESTS_RUN+=("${test_name}")

    if bash "${test_script}"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        log_pass "Test ${test_name} PASSED"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        log_fail "Test ${test_name} FAILED"
    fi
}

# ---------------------------------------------------------------------------
# Cleanup handler
# ---------------------------------------------------------------------------
cleanup() {
    local exit_code=$?

    if [[ "${SKIP_CLEANUP}" == "true" ]]; then
        log_info "Skipping cleanup (--skip-cleanup)"
        return
    fi

    log_info "=========================================="
    log_info "Teardown"
    log_info "=========================================="

    # Stop all deployed stacks
    so_stop_stack "hyperlane-svm-warp-deployer" || true
    so_stop_stack "hyperlane-svm-deployer" || true

    # Stop Solana test validator
    stop_solana_test_validator || true

    # Stop gorchain
    stop_gorchain || true

    # Destroy kind cluster
    if [[ "${SKIP_CLUSTER_SETUP}" != "true" ]]; then
        destroy_kind_cluster || true
    fi

    # Clean up temp files
    rm -rf "${E2E_DIR}/.keys" "${E2E_DIR}/.deployments" "${E2E_DIR}/.solana-validator.pid" "${E2E_DIR}/.warp-spec-patched.yml"

    log_info "Teardown complete"
    exit "${exit_code}"
}

trap cleanup EXIT

# ===========================================================================
# Phase: Setup
# ===========================================================================
log_info "=========================================="
log_info "Hyperlane E2E Tests — Setup Phase"
log_info "=========================================="

# Step A: Create kind cluster
if [[ "${SKIP_CLUSTER_SETUP}" != "true" ]]; then
    log_info "Step A: Creating kind cluster..."
    create_kind_cluster
    install_cert_manager
    create_selfsigned_issuer
else
    log_info "Step A: Skipping cluster setup (--skip-cluster-setup)"
fi

# Step B: Start chain nodes
if [[ "${SKIP_CHAIN_SETUP}" != "true" ]]; then
    log_info "Step B: Starting chain nodes..."
    start_solana_test_validator
    start_gorchain
else
    log_info "Step B: Skipping chain setup (--skip-chain-setup)"
fi

# Step C: Build container images
if [[ "${SKIP_BUILD}" != "true" ]]; then
    log_info "Step C: Building container images..."
    build_deployer_image
else
    log_info "Step C: Skipping image build (--skip-build)"
fi

# Step D: Generate keypairs
log_info "Step D: Generating test keypairs..."
generate_test_keypairs

# Step E: Prepare first stack (init + create) to get cluster-id and namespace
# This creates the kind cluster but does NOT start the deployer pod yet.
log_info "Step E: Preparing first stack to establish cluster..."
so_deploy_prepare "hyperlane-svm-deployer" "${E2E_DIR}/fixtures/test-spec-deployer.yml"

# Step F: Apply host-chain-services to namespace
log_info "Step F: Applying host-chain-services..."
apply_host_chain_services "${DEPLOY_NAMESPACE}"

# Step G: Apply RBAC to namespace
log_info "Step G: Applying RBAC..."
apply_rbac "${DEPLOY_NAMESPACE}"

# Step H: Create secrets in namespace
log_info "Step H: Creating secrets..."
create_deployer_secrets "${DEPLOY_NAMESPACE}"
create_warp_deployer_secrets "${DEPLOY_NAMESPACE}"

# Step I: Fund wallets
log_info "Step I: Funding wallets..."
fund_wallets "http://localhost:8899" "http://localhost:18899"

# Step J: Now start the deployer (prerequisites are in place)
log_info "Step J: Starting deployer stack..."
so_deploy_start "hyperlane-svm-deployer" "first"

# ===========================================================================
# Phase: Tests
# ===========================================================================
log_info "=========================================="
log_info "Hyperlane E2E Tests — Test Phase"
log_info "=========================================="

# Test 1: Core deployer
run_test "${E2E_DIR}/tests/test-deployer.sh"

# Test 2: Warp deployer
run_test "${E2E_DIR}/tests/test-warp-deployer.sh"

# ===========================================================================
# Summary
# ===========================================================================
log_info "=========================================="
log_info "Hyperlane E2E Tests — Summary"
log_info "=========================================="
log_info "Tests run:    ${#TESTS_RUN[@]}"
log_info "Tests passed: ${TESTS_PASSED}"
log_info "Tests failed: ${TESTS_FAILED}"

for t in "${TESTS_RUN[@]}"; do
    log_info "  - ${t}"
done

if [[ ${TESTS_FAILED} -gt 0 ]]; then
    log_fail "E2E tests FAILED (${TESTS_FAILED} failure(s))"
    exit 1
fi

log_pass "All E2E tests PASSED"
exit 0
