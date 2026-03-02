#!/bin/bash
# deploy.sh — Stack-Orchestrator deployment helpers for Hyperlane e2e tests
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

DEPLOY_DIR="${E2E_DIR}/.deployments"
mkdir -p "${DEPLOY_DIR}"

# Track whether this is the first deployment (creates the cluster)
_FIRST_DEPLOY="${_FIRST_DEPLOY:-true}"

# ---------------------------------------------------------------------------
# build_deployer_image — build the deployer container image
# ---------------------------------------------------------------------------
build_deployer_image() {
    log_info "Building deployer container image..."

    log_info "Setting up repositories..."
    laconic-so setup-repositories \
        --include github.com/hyperlane-xyz/hyperlane-monorepo@agents-v2.0.0

    log_info "Building container image..."
    laconic-so build-containers \
        --include laconicnetwork/hyperlane-svm-deployer

    log_info "Deployer image built successfully"
}

# ---------------------------------------------------------------------------
# so_deploy_prepare — init + create a stack (does NOT start pods)
#   $1: stack-name (used as deployment directory name)
#   $2: spec-file path
#
# After first call, exports CLUSTER_ID and DEPLOY_NAMESPACE.
# ---------------------------------------------------------------------------
so_deploy_prepare() {
    local stack_name="$1"
    local spec_file="$2"
    local deploy_dir="${DEPLOY_DIR}/${stack_name}"

    log_info "Preparing stack '${stack_name}' from spec '${spec_file}'..."

    mkdir -p "${deploy_dir}"

    # Initialize deployment
    log_info "Running deploy init..."
    laconic-so deploy init \
        --output "${deploy_dir}" \
        --spec-file "${spec_file}"

    # If not the first deploy, patch cluster-id to reuse existing cluster
    if [[ "${_FIRST_DEPLOY}" != "true" && -n "${CLUSTER_ID:-}" ]]; then
        log_info "Patching cluster-id to ${CLUSTER_ID} for shared cluster..."
        patch_cluster_id "${deploy_dir}" "${CLUSTER_ID}"
    fi

    # Create deployment (sets up k8s resources, kind cluster on first run)
    log_info "Running deploy create..."
    laconic-so deploy create \
        --deployment-dir "${deploy_dir}"

    # If first deploy, extract cluster-id and namespace
    if [[ "${_FIRST_DEPLOY}" == "true" ]]; then
        CLUSTER_ID="$(get_cluster_id "${deploy_dir}")"
        export CLUSTER_ID
        DEPLOY_NAMESPACE="laconic-${CLUSTER_ID}"
        export DEPLOY_NAMESPACE
        log_info "First deployment — cluster-id: ${CLUSTER_ID}, namespace: ${DEPLOY_NAMESPACE}"
        _FIRST_DEPLOY="false"
    fi

    log_info "Stack '${stack_name}' prepared (not yet started)"
}

# ---------------------------------------------------------------------------
# so_deploy_start — start a previously prepared stack
#   $1: stack-name
#   $2: (optional) "first" if this is the first stack (creates cluster)
# ---------------------------------------------------------------------------
so_deploy_start() {
    local stack_name="$1"
    local is_first="${2:-}"
    local deploy_dir="${DEPLOY_DIR}/${stack_name}"

    log_info "Starting stack '${stack_name}'..."
    if [[ "${is_first}" == "first" ]]; then
        laconic-so deployment --dir "${deploy_dir}" start
    else
        laconic-so deployment --dir "${deploy_dir}" start --skip-cluster-management
    fi
    log_info "Stack '${stack_name}' started"
}

# ---------------------------------------------------------------------------
# so_deploy_stack — prepare + start a stack in one call (for non-first stacks)
#   $1: stack-name
#   $2: spec-file path
# ---------------------------------------------------------------------------
so_deploy_stack() {
    local stack_name="$1"
    local spec_file="$2"

    so_deploy_prepare "${stack_name}" "${spec_file}"
    so_deploy_start "${stack_name}"
}

# ---------------------------------------------------------------------------
# so_stop_stack — stop a deployed stack
#   $1: stack-name
# ---------------------------------------------------------------------------
so_stop_stack() {
    local stack_name="$1"
    local deploy_dir="${DEPLOY_DIR}/${stack_name}"

    if [[ ! -d "${deploy_dir}" ]]; then
        log_info "Deployment directory for '${stack_name}' not found, skipping stop"
        return 0
    fi

    log_info "Stopping stack '${stack_name}'..."
    laconic-so deployment --dir "${deploy_dir}" stop --delete-volumes 2>/dev/null || true
    log_info "Stack '${stack_name}' stopped"
}

# ---------------------------------------------------------------------------
# get_cluster_id — read cluster-id from a deployment directory
#   $1: deployment directory
# ---------------------------------------------------------------------------
get_cluster_id() {
    local deploy_dir="$1"
    local deployment_yml="${deploy_dir}/deployment.yml"

    if [[ ! -f "${deployment_yml}" ]]; then
        fail_exit "deployment.yml not found in ${deploy_dir}"
    fi

    local cluster_id
    cluster_id="$(grep 'cluster-id:' "${deployment_yml}" | awk '{print $2}' | tr -d '"' | tr -d "'")"

    if [[ -z "${cluster_id}" ]]; then
        fail_exit "Could not extract cluster-id from ${deployment_yml}"
    fi

    echo "${cluster_id}"
}

# ---------------------------------------------------------------------------
# patch_cluster_id — overwrite cluster-id in a deployment.yml
#   $1: deployment directory
#   $2: cluster-id
# ---------------------------------------------------------------------------
patch_cluster_id() {
    local deploy_dir="$1"
    local cluster_id="$2"
    local deployment_yml="${deploy_dir}/deployment.yml"

    if [[ ! -f "${deployment_yml}" ]]; then
        # The file may not exist yet before deploy create — patch the spec instead
        local spec_file="${deploy_dir}/spec.yml"
        if [[ -f "${spec_file}" ]]; then
            if grep -q 'cluster-id:' "${spec_file}"; then
                sed -i "s/cluster-id:.*/cluster-id: ${cluster_id}/" "${spec_file}"
            else
                echo "cluster-id: ${cluster_id}" >> "${spec_file}"
            fi
        fi
        return 0
    fi

    sed -i "s/cluster-id:.*/cluster-id: ${cluster_id}/" "${deployment_yml}"
    log_info "Patched cluster-id to ${cluster_id} in ${deployment_yml}"
}
