#!/bin/bash
# test-deployer.sh — Test the Hyperlane SVM core deployer stack
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../lib/common.sh"
source "${SCRIPT_DIR}/../lib/deploy.sh"

TEST_NAME="test-deployer"
PASS_COUNT=0
FAIL_COUNT=0

pass() { PASS_COUNT=$((PASS_COUNT + 1)); log_pass "$*"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); log_fail "$*"; }

# ===========================================================================
# Test: Verify core contract deployment
# ===========================================================================
log_info "=== ${TEST_NAME}: Starting core deployer test ==="
# NOTE: The deployer stack is already started by run.sh before this script runs.

# Step 1: Wait for deployer pod to complete
log_info "Step 1: Waiting for deployer pod to complete..."
if wait_for_pod_phase "${DEPLOY_NAMESPACE}" "app.kubernetes.io/name=deployer" "Succeeded" 600; then
    pass "Deployer pod completed successfully"
else
    fail "Deployer pod did not reach Succeeded phase"
    # Dump logs for debugging
    log_info "--- Deployer pod logs ---"
    kubectl logs -n "${DEPLOY_NAMESPACE}" -l "app.kubernetes.io/name=deployer" --tail=100 2>/dev/null || true
    log_info "--- End deployer pod logs ---"
fi

# Step 2: Assert ConfigMaps exist
log_info "Step 2: Verifying ConfigMaps..."

if wait_for_configmap "${DEPLOY_NAMESPACE}" "hyperlane-program-ids" 30; then
    pass "ConfigMap hyperlane-program-ids exists"
else
    fail "ConfigMap hyperlane-program-ids not found"
fi

if wait_for_configmap "${DEPLOY_NAMESPACE}" "hyperlane-agent-config" 30; then
    pass "ConfigMap hyperlane-agent-config exists"
else
    fail "ConfigMap hyperlane-agent-config not found"
fi

if wait_for_configmap "${DEPLOY_NAMESPACE}" "hyperlane-gas-oracle-config" 30; then
    pass "ConfigMap hyperlane-gas-oracle-config exists"
else
    fail "ConfigMap hyperlane-gas-oracle-config not found"
fi

if wait_for_configmap "${DEPLOY_NAMESPACE}" "hyperlane-multisig-config" 30; then
    pass "ConfigMap hyperlane-multisig-config exists"
else
    fail "ConfigMap hyperlane-multisig-config not found"
fi

# Step 3: Verify ConfigMap contents
log_info "Step 3: Verifying ConfigMap contents..."

# Check program-ids has keys for both chains
PROGRAM_IDS_DATA="$(kubectl get configmap hyperlane-program-ids \
    -n "${DEPLOY_NAMESPACE}" -o json 2>/dev/null || echo "{}")"

GORCHAIN_IDS="$(echo "${PROGRAM_IDS_DATA}" | jq -r '.data["gorchain-program-ids.json"] // empty')"
SOLANA_IDS="$(echo "${PROGRAM_IDS_DATA}" | jq -r '.data["solanasvm-program-ids.json"] // empty')"

if [[ -n "${GORCHAIN_IDS}" ]]; then
    pass "program-ids contains gorchain data"
else
    fail "program-ids missing gorchain data"
fi

if [[ -n "${SOLANA_IDS}" ]]; then
    pass "program-ids contains solana data"
else
    fail "program-ids missing solana data"
fi

# Check agent-config is valid JSON
AGENT_CONFIG="$(kubectl get configmap hyperlane-agent-config \
    -n "${DEPLOY_NAMESPACE}" -o jsonpath='{.data.agent-config\.json}' 2>/dev/null || echo "")"

if [[ -n "${AGENT_CONFIG}" ]]; then
    if echo "${AGENT_CONFIG}" | python3 -m json.tool > /dev/null 2>&1; then
        pass "agent-config is valid JSON"
    else
        fail "agent-config is not valid JSON"
    fi
else
    fail "agent-config data is empty"
fi

# Step 4: Dump deployer pod logs for debugging
log_info "Step 4: Deployer pod logs..."
log_info "--- Deployer pod logs ---"
kubectl logs -n "${DEPLOY_NAMESPACE}" -l "app.kubernetes.io/name=deployer" --tail=200 2>/dev/null || true
log_info "--- End deployer pod logs ---"

# ===========================================================================
# Summary
# ===========================================================================
log_info "=== ${TEST_NAME}: Summary ==="
log_info "  Passed: ${PASS_COUNT}"
log_info "  Failed: ${FAIL_COUNT}"

if [[ ${FAIL_COUNT} -gt 0 ]]; then
    log_fail "${TEST_NAME}: ${FAIL_COUNT} assertion(s) failed"
    exit 1
fi

log_pass "${TEST_NAME}: All ${PASS_COUNT} assertions passed"
exit 0
