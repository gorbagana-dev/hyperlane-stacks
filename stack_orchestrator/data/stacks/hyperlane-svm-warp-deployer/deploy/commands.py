from pathlib import Path
from stack_orchestrator.deploy.deployment_context import DeploymentContext


def init(deploy_command_context):
    """Return default spec content for this stack."""
    return {}


def create(context: DeploymentContext, extra_args):
    """Apply RBAC so the warp deployer pod can manage k8s ConfigMaps.

    The warp deployer needs to:
    1. Read hyperlane-program-ids ConfigMap (from core deployer)
    2. Create/update ConfigMaps for warp route deployment artifacts
    """
    import subprocess

    namespace = "default"

    # Apply RBAC manifests — reuses the same Role as the core deployer
    # (both need ConfigMap read/write in the same namespace)
    rbac_path = Path(__file__).parent / "rbac.yaml"
    result = subprocess.run(
        ["kubectl", "apply", "-f", str(rbac_path), "-n", namespace],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"Warning: RBAC apply failed: {result.stderr}")
    else:
        print("Applied warp deployer RBAC (Role + RoleBinding for ConfigMap access)")
