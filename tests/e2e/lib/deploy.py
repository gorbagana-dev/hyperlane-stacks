"""Stack-Orchestrator deployment helpers for Hyperlane e2e tests."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .common import E2E_DIR, REPO_ROOT, fail_exit, force_rmtree, log_info, run_cmd

DEPLOY_DIR = E2E_DIR / ".deployments"
DEPLOY_DIR.mkdir(parents=True, exist_ok=True)

# TODO: Pin remaining third-party images (prom/prometheus, prom/pushgateway,
# grafana/grafana) to specific versions to avoid Docker Hub rate limits and
# imagePullPolicy: Always on :latest tags. ghcr.io/gorbagana-dev/* images are
# fine as :latest — we control that registry.
DEPLOYER_IMAGE = "ghcr.io/gorbagana-dev/hyperlane-svm-deployer:latest"
AGENT_IMAGE = "ghcr.io/gorbagana-dev/hyperlane-agent:latest"
KMS_PROXY_IMAGE = "ghcr.io/gorbagana-dev/hyperlane-kms-proxy:latest"
MINIO_IMAGE = "minio/minio:RELEASE.2025-09-07T16-13-09Z"
MINIO_MC_IMAGE = "minio/mc:RELEASE.2025-08-13T08-35-41Z"
WARP_UI_IMAGE = "ghcr.io/gorbagana-dev/hyperlane-warp-ui:latest"
GAS_ORACLE_IMAGE = "ghcr.io/gorbagana-dev/hyperlane-gas-oracle:latest"

# Local build tags (used by build-from-source path)
AGENT_IMAGE_LOCAL = "gorbagana-dev/hyperlane-agent:local"
KMS_PROXY_IMAGE_LOCAL = "gorbagana-dev/hyperlane-kms-proxy:local"
WARP_UI_IMAGE_LOCAL = "gorbagana-dev/hyperlane-warp-ui:local"


def ensure_ghcr_pat() -> None:
    """Set GHCR_PAT env var from ~/.docker/config.json if not already set.

    laconic-so's image-pull-secret reads the token from the env var
    specified in ``token-env`` (GHCR_PAT) to create a k8s pull secret.
    This function extracts the ghcr.io token from Docker's config so the
    user only needs ``docker login ghcr.io`` as a prerequisite.
    """
    if os.environ.get("GHCR_PAT"):
        return

    docker_config_path = Path.home() / ".docker" / "config.json"
    if not docker_config_path.exists():
        fail_exit(
            "~/.docker/config.json not found. "
            "Run 'docker login ghcr.io' first (see README)."
        )

    docker_config = json.loads(docker_config_path.read_text())

    # Check for credential helper (e.g. "credHelpers": {"ghcr.io": "..."})
    cred_helpers = docker_config.get("credHelpers", {})
    cred_store = docker_config.get("credsStore")
    if "ghcr.io" in cred_helpers or cred_store:
        fail_exit(
            "Docker is using a credential helper for ghcr.io. "
            "Set GHCR_PAT env var manually instead: "
            "export GHCR_PAT=ghp_xxxx"
        )

    auths = docker_config.get("auths", {})
    ghcr_auth = auths.get("ghcr.io", {})
    auth_b64 = ghcr_auth.get("auth")
    if not auth_b64:
        fail_exit(
            "No ghcr.io credentials in ~/.docker/config.json. "
            "Run 'docker login ghcr.io' first (see README)."
        )

    # auth is base64("username:token")
    decoded = base64.b64decode(auth_b64).decode()
    _, _, token = decoded.partition(":")
    if not token:
        fail_exit("Could not extract token from ghcr.io docker auth.")

    os.environ["GHCR_PAT"] = token
    log_info("Set GHCR_PAT from ~/.docker/config.json")


@dataclass
class DeploymentInfo:
    deploy_dir: Path
    deployment_id: str
    namespace: str


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def resolve_stack_path(stack_name: str) -> Path:
    return REPO_ROOT / "stack_orchestrator" / "data" / "stacks" / stack_name


def prepare_spec(src: Path, dst: Path, replacements: dict[str, str] | None = None) -> None:
    shutil.copy2(src, dst)

    content = dst.read_text()
    # Convert relative stack path to absolute:
    #   "stack: stack_orchestrator/..." -> "stack: /abs/path/stack_orchestrator/..."
    content = re.sub(
        r"^(stack:\s+)stack_orchestrator/",
        rf"\g<1>{REPO_ROOT}/stack_orchestrator/",
        content,
        flags=re.MULTILINE,
    )
    # Apply any additional placeholder replacements
    if replacements:
        for placeholder, value in replacements.items():
            content = content.replace(placeholder, value)
    dst.write_text(content)


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
def build_deployer_image(stack_name: str = "hyperlane-svm-deployer") -> None:
    stack_path = resolve_stack_path(stack_name)

    log_info("Building deployer container image...")

    log_info("Setting up repositories...")
    run_cmd(["laconic-so", "--stack", str(stack_path), "setup-repositories"])

    log_info("Building container image...")
    run_cmd(["laconic-so", "--stack", str(stack_path), "build-containers"])

    log_info("Deployer image built successfully")


def prefetch_deployer_image() -> None:
    """Pull the published deployer image to host Docker.

    SO preloads it into the kind cluster at deploy_start via image-overrides:.
    """
    log_info(f"Pulling deployer image {DEPLOYER_IMAGE}...")
    run_cmd(["docker", "pull", DEPLOYER_IMAGE])
    log_info("Pulled deployer image to host Docker")


def build_kms_proxy_image() -> None:
    """Build the kms-proxy image from source into host Docker.

    SO preloads it into the kind cluster at deploy_start via image-overrides:.
    """
    kms_proxy_dir = REPO_ROOT / "hyperlane-kms-proxy"
    log_info(f"Building kms-proxy image from {kms_proxy_dir}...")
    run_cmd(["docker", "build", "-t", KMS_PROXY_IMAGE_LOCAL, str(kms_proxy_dir)])
    log_info("KMS proxy image built into host Docker")


def build_agent_image(stack_name: str = "hyperlane-validator") -> None:
    """Build the patched hyperlane-agent image via laconic-so into host Docker.

    Uses setup-repositories to clone the monorepo at the pinned commit, then
    build-containers to run the Dockerfile with the KMS endpoint patch.
    SO preloads the images into the kind cluster at deploy_start via image-overrides:.
    """
    stack_path = resolve_stack_path(stack_name)

    log_info("Setting up repositories for agent build...")
    run_cmd(["laconic-so", "--stack", str(stack_path), "setup-repositories"])

    log_info("Building agent and kms-proxy container images...")
    run_cmd(["laconic-so", "--stack", str(stack_path), "build-containers"])

    log_info("Agent and kms-proxy images built into host Docker")


def prefetch_agent_images() -> None:
    """Pull published agent + kms-proxy images to host Docker.

    SO preloads them into the kind cluster at deploy_start via image-overrides:.
    """
    for image in (AGENT_IMAGE, KMS_PROXY_IMAGE):
        log_info(f"Pulling {image}...")
        run_cmd(["docker", "pull", image])
    log_info("Pulled agent images to host Docker")


def build_warp_ui_image(stack_name: str = "hyperlane-warp-ui") -> None:
    """Build the warp-ui image via laconic-so into host Docker.

    SO preloads it into the kind cluster at deploy_start via image-overrides:.
    """
    stack_path = resolve_stack_path(stack_name)

    log_info("Setting up repositories for warp-ui build...")
    run_cmd(["laconic-so", "--stack", str(stack_path), "setup-repositories"])

    log_info("Building warp-ui container image...")
    run_cmd(["laconic-so", "--stack", str(stack_path), "build-containers"])

    log_info("Warp UI image built into host Docker")


def prefetch_warp_ui_image() -> None:
    """Pull the published warp-ui image to host Docker.

    SO preloads it into the kind cluster at deploy_start via image-overrides:.
    """
    log_info(f"Pulling warp-ui image {WARP_UI_IMAGE}...")
    run_cmd(["docker", "pull", WARP_UI_IMAGE])
    log_info("Pulled warp-ui image to host Docker")


def prefetch_minio_images() -> None:
    """Pull MinIO images to host Docker.

    SO preloads them into the kind cluster at deploy_start via image-overrides:.
    """
    for image in (MINIO_IMAGE, MINIO_MC_IMAGE):
        log_info(f"Pulling {image}...")
        run_cmd(["docker", "pull", image])
    log_info("Pulled MinIO images to host Docker")


def prefetch_gas_oracle_image() -> None:
    """Pull the published gas oracle image to host Docker.

    SO preloads it into the kind cluster at deploy_start via image-overrides:.
    """
    log_info(f"Pulling gas oracle image {GAS_ORACLE_IMAGE}...")
    run_cmd(["docker", "pull", GAS_ORACLE_IMAGE])
    log_info("Pulled gas oracle image to host Docker")


# Monitoring stack images (all public — no GHCR auth needed)
PROMETHEUS_IMAGE = "prom/prometheus:latest"
PUSHGATEWAY_IMAGE = "prom/pushgateway:latest"
GRAFANA_IMAGE = "grafana/grafana:latest"
BALANCE_MONITOR_IMAGE = "python:3.12-alpine"


def prefetch_monitoring_images() -> None:
    """Pull monitoring stack images to host Docker.

    SO preloads them into the kind cluster at deploy_start via image-overrides:.
    """
    for image in (PROMETHEUS_IMAGE, PUSHGATEWAY_IMAGE, GRAFANA_IMAGE, BALANCE_MONITOR_IMAGE):
        log_info(f"Pulling {image}...")
        run_cmd(["docker", "pull", image])
    log_info("Pulled monitoring images to host Docker")


# ---------------------------------------------------------------------------
# Deploy lifecycle
# ---------------------------------------------------------------------------
def set_deployment_id(deploy_dir: Path, deployment_id: str) -> None:
    """Patch deployment.yml to use a human-readable deployment-id.

    Must be called after ``deploy create`` but before ``deploy start``.
    The deployment-id becomes the prefix for all k8s resource names
    (Deployments, Jobs, Services, ConfigMaps, PVs). It is distinct
    from ``cluster-id`` (which identifies the shared kind cluster);
    every stack joining the cluster gets its own deployment-id but
    they all share the cluster.
    """
    deployment_yml = deploy_dir / "deployment.yml"
    content = deployment_yml.read_text()
    content = re.sub(
        r"^(deployment-id:\s*).*$",
        rf"\g<1>{deployment_id}",
        content,
        flags=re.MULTILINE,
    )
    deployment_yml.write_text(content)
    log_info(f"Patched deployment-id to '{deployment_id}'")


def deploy_prepare(
    stack_name: str,
    spec_file: Path,
    deploy_dir: Path | None = None,
    namespace: str | None = None,
    spec_replacements: dict[str, str] | None = None,
    deployment_id: str | None = None,
) -> DeploymentInfo:
    if deploy_dir is None:
        deploy_dir = DEPLOY_DIR / stack_name

    stack_path = resolve_stack_path(stack_name)

    log_info(f"Preparing stack '{stack_name}' from spec '{spec_file}'...")

    # Clean up stale deployment directory from a previous run
    # (Docker containers create root-owned files, so may need sudo)
    if deploy_dir.exists():
        log_info(f"Removing stale deployment directory: {deploy_dir}")
        force_rmtree(deploy_dir)

    # Write spec file outside deploy_dir — deploy create expects the dir not to exist
    init_spec = DEPLOY_DIR / f"{stack_name}-spec.yml"
    DEPLOY_DIR.mkdir(parents=True, exist_ok=True)
    log_info("Running deploy init...")
    run_cmd(
        [
            "laconic-so",
            "--stack",
            str(stack_path),
            "deploy",
            "init",
            "--output",
            str(init_spec),
        ]
    )

    # Overwrite with our pre-configured test spec (resolving stack path + placeholders)
    prepare_spec(spec_file, init_spec, replacements=spec_replacements)

    # Create deployment directory from spec
    log_info("Running deploy create...")
    run_cmd(
        [
            "laconic-so",
            "--stack",
            str(stack_path),
            "deploy",
            "create",
            "--spec-file",
            str(init_spec),
            "--deployment-dir",
            str(deploy_dir),
        ]
    )

    # Optionally replace the random deployment-id with a human-readable name
    # so k8s resources (pods, jobs, services) are easy to identify.
    if deployment_id:
        set_deployment_id(deploy_dir, deployment_id)
    else:
        deployment_id = get_deployment_id(deploy_dir)

    # Mirror SO's namespace derivation: laconic-{stack_name} unless caller
    # overrides. The caller may pass `namespace=` to match a spec.yml override
    # (multi-instance stacks like multiple validators sharing one stack name).
    resolved_namespace = namespace or f"laconic-{stack_name}"

    log_info(f"Stack '{stack_name}' prepared — deployment-id: {deployment_id}, namespace: {resolved_namespace}")

    return DeploymentInfo(
        deploy_dir=deploy_dir,
        deployment_id=deployment_id,
        namespace=resolved_namespace,
    )


def deploy_start(deploy_dir: Path) -> None:
    log_info(f"Starting deployment in {deploy_dir}...")
    run_cmd([
        "laconic-so", "deployment", "--dir", str(deploy_dir),
        "start", "--perform-cluster-management",
    ])
    log_info(f"Deployment in {deploy_dir} started")


def deploy_stack(
    stack_name: str,
    spec_file: Path,
    deploy_dir: Path | None = None,
    namespace: str | None = None,
    spec_replacements: dict[str, str] | None = None,
) -> DeploymentInfo:
    info = deploy_prepare(
        stack_name, spec_file, deploy_dir, namespace, spec_replacements
    )
    deploy_start(info.deploy_dir)
    return info


def stop_stack(stack_name: str, deploy_dir: Path | None = None) -> None:
    if deploy_dir is None:
        deploy_dir = DEPLOY_DIR / stack_name

    if not deploy_dir.is_dir():
        log_info(f"Deployment directory for '{stack_name}' not found, skipping stop")
        return

    log_info(f"Stopping stack '{stack_name}'...")
    run_cmd(
        [
            "laconic-so",
            "deployment",
            "--dir",
            str(deploy_dir),
            "stop",
            "--delete-volumes",
        ],
        check=False,
    )
    log_info(f"Stack '{stack_name}' stopped")


# ---------------------------------------------------------------------------
# Deployment-id helpers
# ---------------------------------------------------------------------------
def get_deployment_id(deploy_dir: Path) -> str:
    deployment_yml = deploy_dir / "deployment.yml"
    if not deployment_yml.is_file():
        fail_exit(f"deployment.yml not found in {deploy_dir}")

    content = deployment_yml.read_text()
    match = re.search(r"^deployment-id:\s*['\"]?([^'\"\s]+)", content, re.MULTILINE)
    if not match:
        fail_exit(f"Could not extract deployment-id from {deployment_yml}")
        return ""  # unreachable

    return match.group(1)


