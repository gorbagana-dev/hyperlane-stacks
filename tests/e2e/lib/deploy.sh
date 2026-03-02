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
# _resolve_stack_path — resolve stack name to absolute path
#   $1: stack name (e.g. hyperlane-svm-deployer)
# ---------------------------------------------------------------------------
_resolve_stack_path() {
    local stack_name="$1"
    echo "${REPO_ROOT}/stack_orchestrator/data/stacks/${stack_name}"
}

# ---------------------------------------------------------------------------
# build_deployer_image — build the deployer container image
# ---------------------------------------------------------------------------
build_deployer_image() {
    local stack_path
    stack_path="$(_resolve_stack_path "hyperlane-svm-deployer")"

    log_info "Building deployer container image..."

    log_info "Setting up repositories..."
    laconic-so --stack "${stack_path}" setup-repositories

    log_info "Building container image..."
    laconic-so --stack "${stack_path}" build-containers

    log_info "Deployer image built successfully"
}

# ---------------------------------------------------------------------------
# _prepare_spec — copy a spec fixture and resolve the stack path to absolute
#   $1: source spec file
#   $2: destination spec file
#
# Replaces relative stack: paths with absolute paths based on REPO_ROOT.
# ---------------------------------------------------------------------------
_prepare_spec() {
    local src="$1"
    local dst="$2"

    cp "${src}" "${dst}"

    # Convert relative stack path to absolute
    # e.g. "stack: stack_orchestrator/..." → "stack: /abs/path/stack_orchestrator/..."
    if grep -q '^stack: stack_orchestrator/' "${dst}"; then
        sed -i "s|^stack: stack_orchestrator/|stack: ${REPO_ROOT}/stack_orchestrator/|" "${dst}"
    fi
}

# ---------------------------------------------------------------------------
# so_deploy_prepare — init + create a stack (does NOT start pods)
#   $1: stack-name (used as deployment directory name)
#   $2: spec-file path (fixture)
#
# After first call, exports CLUSTER_ID and DEPLOY_NAMESPACE.
# ---------------------------------------------------------------------------
so_deploy_prepare() {
    local stack_name="$1"
    local spec_file="$2"
    local deploy_dir="${DEPLOY_DIR}/${stack_name}"
    local stack_path
    stack_path="$(_resolve_stack_path "${stack_name}")"

    log_info "Preparing stack '${stack_name}' from spec '${spec_file}'..."

    mkdir -p "${deploy_dir}"

    # Generate default spec, then overwrite with our fixture
    log_info "Running deploy init..."
    local init_spec="${deploy_dir}/spec.yml"
    laconic-so --stack "${stack_path}" deploy init --output "${init_spec}"

    # Overwrite with our pre-configured test spec (resolving stack path)
    _prepare_spec "${spec_file}" "${init_spec}"

    # If not the first deploy, inject cluster-id into the spec
    if [[ "${_FIRST_DEPLOY}" != "true" && -n "${CLUSTER_ID:-}" ]]; then
        log_info "Patching cluster-id to ${CLUSTER_ID} for shared cluster..."
        patch_cluster_id "${init_spec}" "${CLUSTER_ID}"
    fi

    # Create deployment directory from spec
    log_info "Running deploy create..."
    laconic-so --stack "${stack_path}" deploy create \
        --spec-file "${init_spec}" \
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
#   $2: (optional) "first" — omit --skip-cluster-management for the first stack
# ---------------------------------------------------------------------------
so_deploy_start() {
    local stack_name="$1"
    local is_first="${2:-}"
    local deploy_dir="${DEPLOY_DIR}/${stack_name}"

    log_info "Starting stack '${stack_name}'..."
    if [[ "${is_first}" == "first" ]]; then
        # First stack: let SO create the kind cluster, load images, install ingress
        laconic-so deployment --dir "${deploy_dir}" start
    else
        # Subsequent stacks: skip cluster creation, deploy into existing cluster
        laconic-so deployment --dir "${deploy_dir}" start --skip-cluster-management
    fi
    log_info "Stack '${stack_name}' started"
}

# ---------------------------------------------------------------------------
# so_deploy_stack — prepare + start a stack in one call
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
    laconic-so deployment --dir "${deploy_dir}" stop \
        --delete-volumes --skip-cluster-management 2>/dev/null || true
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
# patch_cluster_id — inject or overwrite cluster-id in a spec/deployment file
#   $1: file path (spec.yml or deployment.yml)
#   $2: cluster-id
# ---------------------------------------------------------------------------
patch_cluster_id() {
    local file="$1"
    local cluster_id="$2"

    if [[ ! -f "${file}" ]]; then
        fail_exit "Cannot patch cluster-id: file not found: ${file}"
    fi

    if grep -q 'cluster-id:' "${file}"; then
        sed -i "s/cluster-id:.*/cluster-id: ${cluster_id}/" "${file}"
    else
        echo "cluster-id: ${cluster_id}" >> "${file}"
    fi

    log_info "Patched cluster-id to ${cluster_id} in ${file}"
}
