from pathlib import Path
from stack_orchestrator.deploy.deployment_context import DeploymentContext


def init(deploy_command_context):
    """Return default spec content for this stack."""
    return {}


def create(context: DeploymentContext, extra_args):
    """Apply RBAC so the deployer pod can create k8s ConfigMaps.

    The deployer script uses kubectl to write deployment artifacts
    (program IDs, agent config, etc.) as ConfigMaps that downstream
    stacks (validators, relayer, warp-deployer) consume.
    """
    import subprocess

    # Apply RBAC manifests (Role + RoleBinding granting the default
    # ServiceAccount permission to manage ConfigMaps in the namespace)
    rbac_path = Path(__file__).parent / "rbac.yaml"
    result = subprocess.run(
        ["kubectl", "apply", "-f", str(rbac_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"Warning: RBAC apply failed: {result.stderr}")
    else:
        print("Applied deployer RBAC (Role + RoleBinding for ConfigMap access)")
