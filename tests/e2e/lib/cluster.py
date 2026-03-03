"""Kind cluster lifecycle for Hyperlane e2e tests."""

from __future__ import annotations

import time
from pathlib import Path

from .common import E2E_DIR, REPO_ROOT, fail_exit, log_info, run_cmd

KIND_CLUSTER_NAME = "hyperlane-e2e"


def create_kind_cluster(config_path: Path | None = None) -> None:
    log_info(f"Creating kind cluster '{KIND_CLUSTER_NAME}'...")

    result = run_cmd(["kind", "get", "clusters"], check=False, quiet=True)
    if result.returncode == 0:
        existing = result.stdout.strip().splitlines()
        if KIND_CLUSTER_NAME in existing:
            log_info(f"Kind cluster '{KIND_CLUSTER_NAME}' already exists, reusing")
            return

    if config_path is None:
        config_path = E2E_DIR / "fixtures" / "kind-config.yaml"

    run_cmd(
        [
            "kind",
            "create",
            "cluster",
            "--name",
            KIND_CLUSTER_NAME,
            "--config",
            str(config_path),
            "--wait",
            "120s",
        ]
    )
    log_info(f"Kind cluster '{KIND_CLUSTER_NAME}' created")

    run_cmd(
        [
            "kubectl",
            "wait",
            "--for=condition=Ready",
            "pods",
            "--all",
            "-n",
            "kube-system",
            "--timeout=120s",
        ]
    )
    log_info("Kind cluster system pods are ready")


def install_cert_manager() -> None:
    log_info("Installing cert-manager...")

    run_cmd(
        [
            "kubectl",
            "apply",
            "-f",
            "https://github.com/cert-manager/cert-manager/releases/download/v1.14.5/cert-manager.yaml",
        ]
    )

    log_info("Waiting for cert-manager pods to be ready...")
    for deployment in ("cert-manager", "cert-manager-webhook", "cert-manager-cainjector"):
        run_cmd(
            [
                "kubectl",
                "wait",
                "--for=condition=Available",
                f"deployment/{deployment}",
                "-n",
                "cert-manager",
                "--timeout=120s",
            ]
        )

    # Give webhooks a moment to register
    time.sleep(5)
    log_info("cert-manager is ready")


def create_selfsigned_issuer(fixture_path: Path | None = None) -> None:
    log_info("Creating self-signed ClusterIssuer...")
    if fixture_path is None:
        fixture_path = E2E_DIR / "fixtures" / "cert-manager-issuer.yaml"
    run_cmd(["kubectl", "apply", "-f", str(fixture_path)])
    log_info("Self-signed ClusterIssuer created")


def create_namespace(namespace: str) -> None:
    log_info(f"Creating namespace {namespace}...")
    result = run_cmd(["kubectl", "create", "namespace", namespace], check=False)
    if result.returncode == 0:
        log_info(f"Namespace {namespace} created")
    elif "AlreadyExists" in (result.stderr or ""):
        log_info(f"Namespace {namespace} already exists")
    else:
        from .common import fail_exit
        fail_exit(f"Failed to create namespace {namespace}: {result.stderr}")


def apply_host_chain_services(namespace: str, fixture_path: Path | None = None) -> None:
    log_info("Detecting host IP for kind network...")
    result = run_cmd(
        [
            "docker",
            "network",
            "inspect",
            "kind",
            "-f",
            '{{range .IPAM.Config}}{{if .Gateway}}{{.Gateway}}\n{{end}}{{end}}',
        ]
    )
    # Pick the first IPv4 address (skip IPv6 lines containing ':')
    host_ip = ""
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if line and ":" not in line:
            host_ip = line
            break

    if not host_ip:
        fail_exit(f"Could not detect IPv4 host IP from kind network. Raw output: {result.stdout.strip()}")

    log_info(f"Host IP: {host_ip}")

    if fixture_path is None:
        fixture_path = E2E_DIR / "fixtures" / "host-chain-services.yaml"

    log_info(f"Applying host-chain-services to namespace {namespace}...")
    template = fixture_path.read_text()
    rendered = template.replace("${HOST_IP}", host_ip).replace("${NAMESPACE}", namespace)

    run_cmd(["kubectl", "apply", "-f", "-"], input_text=rendered)
    log_info("Host chain services applied")


def apply_rbac(namespace: str) -> None:
    log_info(f"Applying RBAC to namespace {namespace}...")

    rbac_source = (
        REPO_ROOT / "stack_orchestrator" / "data" / "stacks" / "hyperlane-svm-deployer" / "deploy" / "rbac.yaml"
    )

    content = rbac_source.read_text()
    rendered = content.replace("namespace: default", f"namespace: {namespace}")

    run_cmd(["kubectl", "apply", "-n", namespace, "-f", "-"], input_text=rendered)
    log_info(f"RBAC applied to namespace {namespace}")


def destroy_kind_cluster() -> None:
    log_info(f"Destroying kind cluster '{KIND_CLUSTER_NAME}'...")
    run_cmd(
        ["kind", "delete", "cluster", "--name", KIND_CLUSTER_NAME],
        check=False,
    )
    log_info("Kind cluster destroyed")
