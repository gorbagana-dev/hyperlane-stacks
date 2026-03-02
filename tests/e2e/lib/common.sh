#!/bin/bash
# common.sh — Shared utilities for Hyperlane e2e tests
set -euo pipefail

# ---------------------------------------------------------------------------
# Path variables
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
E2E_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${E2E_DIR}/../.." && pwd)"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
_ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

log_info()  { echo -e "\033[0;34m[$(_ts)] [INFO]\033[0m  $*"; }
log_error() { echo -e "\033[0;31m[$(_ts)] [ERROR]\033[0m $*"; }
log_pass()  { echo -e "\033[0;32m[$(_ts)] [PASS]\033[0m  $*"; }
log_fail()  { echo -e "\033[0;31m[$(_ts)] [FAIL]\033[0m  $*"; }

# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------
fail_exit() {
    log_error "$*"
    exit 1
}

assert_eq() {
    local expected="$1"
    local actual="$2"
    local msg="${3:-assert_eq failed}"
    if [[ "${expected}" != "${actual}" ]]; then
        log_fail "${msg}: expected '${expected}', got '${actual}'"
        return 1
    fi
}

assert_not_empty() {
    local value="$1"
    local msg="${2:-assert_not_empty failed}"
    if [[ -z "${value}" ]]; then
        log_fail "${msg}: value is empty"
        return 1
    fi
}

assert_contains() {
    local haystack="$1"
    local needle="$2"
    local msg="${3:-assert_contains failed}"
    if [[ "${haystack}" != *"${needle}"* ]]; then
        log_fail "${msg}: '${haystack}' does not contain '${needle}'"
        return 1
    fi
}

# ---------------------------------------------------------------------------
# Wait helpers
# ---------------------------------------------------------------------------

# wait_for — generic retry loop
#   $1: command to run (as a string)
#   $2: interval in seconds (default 5)
#   $3: timeout in seconds (default 120)
wait_for() {
    local cmd="$1"
    local interval="${2:-5}"
    local timeout="${3:-120}"

    local deadline=$(( $(date +%s) + timeout ))
    while true; do
        if eval "${cmd}" &>/dev/null; then
            return 0
        fi
        if (( $(date +%s) >= deadline )); then
            log_error "Timed out after ${timeout}s waiting for: ${cmd}"
            return 1
        fi
        sleep "${interval}"
    done
}

# wait_for_pod_phase — wait for a pod matching a label selector to reach a phase
#   $1: namespace
#   $2: label selector (e.g. "app=deployer")
#   $3: expected phase (Running or Succeeded)
#   $4: timeout in seconds (default 600)
wait_for_pod_phase() {
    local namespace="$1"
    local selector="$2"
    local expected_phase="$3"
    local timeout="${4:-600}"

    log_info "Waiting for pod (${selector}) in ${namespace} to reach phase ${expected_phase} (timeout ${timeout}s)..."

    local deadline=$(( $(date +%s) + timeout ))
    while true; do
        local phase
        phase="$(kubectl get pods -n "${namespace}" -l "${selector}" \
            -o jsonpath='{.items[0].status.phase}' 2>/dev/null || echo "")"

        if [[ "${phase}" == "${expected_phase}" ]]; then
            log_info "Pod (${selector}) reached phase: ${expected_phase}"
            return 0
        fi

        if [[ "${phase}" == "Failed" ]]; then
            log_error "Pod (${selector}) entered Failed phase"
            kubectl logs -n "${namespace}" -l "${selector}" --tail=50 2>/dev/null || true
            return 1
        fi

        if (( $(date +%s) >= deadline )); then
            log_error "Timed out waiting for pod (${selector}) — current phase: ${phase}"
            kubectl describe pods -n "${namespace}" -l "${selector}" 2>/dev/null || true
            return 1
        fi
        sleep 5
    done
}

# wait_for_rpc_health — wait until an RPC endpoint responds with HTTP 200
#   $1: url
#   $2: timeout in seconds (default 120)
wait_for_rpc_health() {
    local url="$1"
    local timeout="${2:-120}"

    log_info "Waiting for RPC health at ${url} (timeout ${timeout}s)..."
    wait_for "curl -sf '${url}' -o /dev/null" 5 "${timeout}"
    log_info "RPC at ${url} is healthy"
}

# wait_for_configmap — wait for a ConfigMap to exist and have data
#   $1: namespace
#   $2: configmap name
#   $3: timeout in seconds (default 120)
wait_for_configmap() {
    local namespace="$1"
    local name="$2"
    local timeout="${3:-120}"

    log_info "Waiting for ConfigMap ${name} in ${namespace} (timeout ${timeout}s)..."

    local cmd="kubectl get configmap '${name}' -n '${namespace}' -o jsonpath='{.data}' 2>/dev/null | grep -q '.'"
    wait_for "${cmd}" 5 "${timeout}"
    log_info "ConfigMap ${name} exists and has data"
}

# ---------------------------------------------------------------------------
# Namespace helper
# ---------------------------------------------------------------------------
get_namespace() {
    if [[ -n "${DEPLOY_NAMESPACE:-}" ]]; then
        echo "${DEPLOY_NAMESPACE}"
    elif [[ -n "${CLUSTER_ID:-}" ]]; then
        echo "laconic-${CLUSTER_ID}"
    else
        fail_exit "Neither DEPLOY_NAMESPACE nor CLUSTER_ID is set"
    fi
}
