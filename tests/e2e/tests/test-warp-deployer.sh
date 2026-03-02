#!/bin/bash
# test-warp-deployer.sh — Test the Hyperlane SVM warp route deployer stack
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/../lib/common.sh"
source "${SCRIPT_DIR}/../lib/deploy.sh"

TEST_NAME="test-warp-deployer"
PASS_COUNT=0
FAIL_COUNT=0

pass() { PASS_COUNT=$((PASS_COUNT + 1)); log_pass "$*"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); log_fail "$*"; }

# ===========================================================================
# Test: Deploy warp routes
# ===========================================================================
log_info "=== ${TEST_NAME}: Starting warp deployer test ==="

# Step 1: Create test SPL token on Solana
log_info "Step 1: Creating test SPL token on Solana..."
TOKEN_OUTPUT="$(spl-token create-token --url http://localhost:18899 2>&1)"
WARP_TOKEN_MINT="$(echo "${TOKEN_OUTPUT}" | grep -oP 'Creating token \K[A-Za-z0-9]+')"

if [[ -z "${WARP_TOKEN_MINT}" ]]; then
    # Fallback: try alternate output format
    WARP_TOKEN_MINT="$(echo "${TOKEN_OUTPUT}" | grep -oP 'Address:\s+\K[A-Za-z0-9]+')"
fi

if [[ -z "${WARP_TOKEN_MINT}" ]]; then
    log_error "Failed to create SPL token. Output: ${TOKEN_OUTPUT}"
    exit 1
fi

log_info "Test SPL token mint: ${WARP_TOKEN_MINT}"

# Step 2: Patch test spec with real token mint address
log_info "Step 2: Patching warp deployer spec with token mint..."
WARP_SPEC="${E2E_DIR}/fixtures/test-spec-warp-deployer.yml"
WARP_SPEC_PATCHED="${E2E_DIR}/.warp-spec-patched.yml"
sed "s/REPLACE_AT_RUNTIME/${WARP_TOKEN_MINT}/g" "${WARP_SPEC}" > "${WARP_SPEC_PATCHED}"

# Step 3: Deploy the warp deployer stack
log_info "Step 3: Deploying hyperlane-svm-warp-deployer stack..."
so_deploy_stack "hyperlane-svm-warp-deployer" "${WARP_SPEC_PATCHED}"

# Step 4: Wait for warp deployer pod to complete
log_info "Step 4: Waiting for warp deployer pod to complete..."
if wait_for_pod_phase "${DEPLOY_NAMESPACE}" "app.kubernetes.io/name=warp-deployer" "Succeeded" 600; then
    pass "Warp deployer pod completed successfully"
else
    fail "Warp deployer pod did not reach Succeeded phase"
    # Dump logs for debugging
    log_info "--- Warp deployer pod logs ---"
    kubectl logs -n "${DEPLOY_NAMESPACE}" -l "app.kubernetes.io/name=warp-deployer" --tail=100 2>/dev/null || true
    log_info "--- End warp deployer pod logs ---"
fi

# Step 5: Assert ConfigMaps exist
log_info "Step 5: Verifying warp route ConfigMaps..."

if wait_for_configmap "${DEPLOY_NAMESPACE}" "hyperlane-token-config" 30; then
    pass "ConfigMap hyperlane-token-config exists"
else
    fail "ConfigMap hyperlane-token-config not found"
fi

if wait_for_configmap "${DEPLOY_NAMESPACE}" "hyperlane-warp-deploy-outputs" 30; then
    pass "ConfigMap hyperlane-warp-deploy-outputs exists"
else
    fail "ConfigMap hyperlane-warp-deploy-outputs not found"
fi

# Step 6: Verify token-config content
log_info "Step 6: Verifying token-config content..."
TOKEN_CONFIG="$(kubectl get configmap hyperlane-token-config \
    -n "${DEPLOY_NAMESPACE}" -o jsonpath='{.data.token-config\.json}' 2>/dev/null || echo "")"

if [[ -n "${TOKEN_CONFIG}" ]]; then
    if echo "${TOKEN_CONFIG}" | python3 -m json.tool > /dev/null 2>&1; then
        pass "token-config is valid JSON"

        # Verify it contains the correct token mint
        CONFIG_MINT="$(echo "${TOKEN_CONFIG}" | jq -r '.warpRoute.tokenMint // empty')"
        if [[ "${CONFIG_MINT}" == "${WARP_TOKEN_MINT}" ]]; then
            pass "token-config contains correct token mint"
        else
            fail "token-config has wrong token mint: expected ${WARP_TOKEN_MINT}, got ${CONFIG_MINT}"
        fi
    else
        fail "token-config is not valid JSON"
    fi
else
    fail "token-config data is empty"
fi

# Step 7: Dump warp deployer pod logs
log_info "Step 7: Warp deployer pod logs..."
log_info "--- Warp deployer pod logs ---"
kubectl logs -n "${DEPLOY_NAMESPACE}" -l "app.kubernetes.io/name=warp-deployer" --tail=200 2>/dev/null || true
log_info "--- End warp deployer pod logs ---"

# Clean up patched spec
rm -f "${WARP_SPEC_PATCHED}"

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
