"""Stack-Orchestrator deployment helpers for Hyperlane e2e tests."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .common import E2E_DIR, REPO_ROOT, fail_exit, log_info, run_cmd

DEPLOY_DIR = E2E_DIR / ".deployments"
DEPLOY_DIR.mkdir(parents=True, exist_ok=True)



@dataclass
class DeploymentInfo:
    deploy_dir: Path
    cluster_id: str
    namespace: str


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def resolve_stack_path(stack_name: str) -> Path:
    return REPO_ROOT / "stack_orchestrator" / "data" / "stacks" / stack_name


def prepare_spec(src: Path, dst: Path) -> None:
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



# ---------------------------------------------------------------------------
# Deploy lifecycle
# ---------------------------------------------------------------------------
def deploy_prepare(
    stack_name: str,
    spec_file: Path,
    deploy_dir: Path | None = None,
    cluster_id: str | None = None,
) -> DeploymentInfo:
    if deploy_dir is None:
        deploy_dir = DEPLOY_DIR / stack_name

    stack_path = resolve_stack_path(stack_name)

    log_info(f"Preparing stack '{stack_name}' from spec '{spec_file}'...")

    # Clean up stale deployment directory from a previous run
    if deploy_dir.exists():
        log_info(f"Removing stale deployment directory: {deploy_dir}")
        shutil.rmtree(deploy_dir)

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

    # Overwrite with our pre-configured test spec (resolving stack path)
    prepare_spec(spec_file, init_spec)

    # If a cluster_id was provided, inject it into the spec
    if cluster_id:
        log_info(f"Patching cluster-id to {cluster_id} for shared cluster...")
        patch_cluster_id(init_spec, cluster_id)

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

    # Extract cluster-id from the created deployment
    resolved_cluster_id = cluster_id or get_cluster_id(deploy_dir)
    namespace = f"laconic-{resolved_cluster_id}"

    log_info(f"Stack '{stack_name}' prepared — cluster-id: {resolved_cluster_id}, namespace: {namespace}")

    return DeploymentInfo(
        deploy_dir=deploy_dir,
        cluster_id=resolved_cluster_id,
        namespace=namespace,
    )


def deploy_start(deploy_dir: Path, first: bool = False) -> None:
    log_info(f"Starting deployment in {deploy_dir}...")
    cmd = ["laconic-so", "deployment", "--dir", str(deploy_dir), "start"]
    if not first:
        cmd.append("--skip-cluster-management")
    run_cmd(cmd)
    log_info(f"Deployment in {deploy_dir} started")


def deploy_stack(
    stack_name: str,
    spec_file: Path,
    deploy_dir: Path | None = None,
    cluster_id: str | None = None,
    first: bool = False,
) -> DeploymentInfo:
    info = deploy_prepare(stack_name, spec_file, deploy_dir, cluster_id)
    deploy_start(info.deploy_dir, first=first)
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
            "--skip-cluster-management",
        ],
        check=False,
    )
    log_info(f"Stack '{stack_name}' stopped")


# ---------------------------------------------------------------------------
# Cluster-id helpers
# ---------------------------------------------------------------------------
def get_cluster_id(deploy_dir: Path) -> str:
    deployment_yml = deploy_dir / "deployment.yml"
    if not deployment_yml.is_file():
        fail_exit(f"deployment.yml not found in {deploy_dir}")

    content = deployment_yml.read_text()
    match = re.search(r"cluster-id:\s*['\"]?([^'\"\s]+)", content)
    if not match:
        fail_exit(f"Could not extract cluster-id from {deployment_yml}")
        return ""  # unreachable

    return match.group(1)


def patch_cluster_id(file_path: Path, cluster_id: str) -> None:
    if not file_path.is_file():
        fail_exit(f"Cannot patch cluster-id: file not found: {file_path}")

    content = file_path.read_text()

    if "cluster-id:" in content:
        content = re.sub(r"cluster-id:.*", f"cluster-id: {cluster_id}", content)
    else:
        content += f"\ncluster-id: {cluster_id}\n"

    file_path.write_text(content)
    log_info(f"Patched cluster-id to {cluster_id} in {file_path}")
