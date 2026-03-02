#!/bin/bash
# cluster.sh — Kind cluster lifecycle for Hyperlane e2e tests
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/common.sh"

KIND_CLUSTER_NAME="hyperlane-e2e"

# ---------------------------------------------------------------------------
# create_kind_cluster — create the kind cluster
# ---------------------------------------------------------------------------
create_kind_cluster() {
    log_info "Creating kind cluster '${KIND_CLUSTER_NAME}'..."

    if kind get clusters 2>/dev/null | grep -q "^${KIND_CLUSTER_NAME}$"; then
        log_info "Kind cluster '${KIND_CLUSTER_NAME}' already exists, reusing"
        return 0
    fi

    kind create cluster \
        --name "${KIND_CLUSTER_NAME}" \
        --config "${E2E_DIR}/fixtures/kind-config.yaml" \
        --wait 120s

    log_info "Kind cluster '${KIND_CLUSTER_NAME}' created"

    # Wait for all system pods to be ready
    kubectl wait --for=condition=Ready pods --all -n kube-system --timeout=120s
    log_info "Kind cluster system pods are ready"
}

# ---------------------------------------------------------------------------
# install_cert_manager — install cert-manager and wait for readiness
# ---------------------------------------------------------------------------
install_cert_manager() {
    log_info "Installing cert-manager..."

    kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.14.5/cert-manager.yaml

    log_info "Waiting for cert-manager pods to be ready..."
    kubectl wait --for=condition=Available deployment/cert-manager \
        -n cert-manager --timeout=120s
    kubectl wait --for=condition=Available deployment/cert-manager-webhook \
        -n cert-manager --timeout=120s
    kubectl wait --for=condition=Available deployment/cert-manager-cainjector \
        -n cert-manager --timeout=120s

    # Give webhooks a moment to register
    sleep 5

    log_info "cert-manager is ready"
}

# ---------------------------------------------------------------------------
# create_selfsigned_issuer — apply self-signed ClusterIssuer
# ---------------------------------------------------------------------------
create_selfsigned_issuer() {
    log_info "Creating self-signed ClusterIssuer..."
    kubectl apply -f "${E2E_DIR}/fixtures/cert-manager-issuer.yaml"
    log_info "Self-signed ClusterIssuer created"
}

# ---------------------------------------------------------------------------
# apply_host_chain_services — create k8s Services pointing to host chain nodes
# ---------------------------------------------------------------------------
apply_host_chain_services() {
    local namespace="${1:-$(get_namespace)}"

    log_info "Detecting host IP for kind network..."
    local host_ip
    host_ip="$(docker network inspect kind -f '{{range .IPAM.Config}}{{.Gateway}}{{end}}' | head -1)"

    if [[ -z "${host_ip}" ]]; then
        fail_exit "Could not detect host IP from kind network"
    fi
    log_info "Host IP: ${host_ip}"

    log_info "Applying host-chain-services to namespace ${namespace}..."
    sed -e "s/\${HOST_IP}/${host_ip}/g" \
        -e "s/\${NAMESPACE}/${namespace}/g" \
        "${E2E_DIR}/fixtures/host-chain-services.yaml" \
        | kubectl apply -f -

    log_info "Host chain services applied"
}

# ---------------------------------------------------------------------------
# apply_rbac — apply RBAC to the deployment namespace
# ---------------------------------------------------------------------------
apply_rbac() {
    local namespace="${1:-$(get_namespace)}"

    log_info "Applying RBAC to namespace ${namespace}..."

    local rbac_source="${REPO_ROOT}/stack_orchestrator/data/stacks/hyperlane-svm-deployer/deploy/rbac.yaml"

    # Apply RBAC with correct namespace
    sed "s/namespace: default/namespace: ${namespace}/g" "${rbac_source}" \
        | kubectl apply -n "${namespace}" -f -

    log_info "RBAC applied to namespace ${namespace}"
}

# ---------------------------------------------------------------------------
# destroy_kind_cluster — delete the kind cluster
# ---------------------------------------------------------------------------
destroy_kind_cluster() {
    log_info "Destroying kind cluster '${KIND_CLUSTER_NAME}'..."
    kind delete cluster --name "${KIND_CLUSTER_NAME}" 2>/dev/null || true
    log_info "Kind cluster destroyed"
}
