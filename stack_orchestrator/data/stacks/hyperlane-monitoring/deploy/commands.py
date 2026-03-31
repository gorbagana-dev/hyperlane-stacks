from pathlib import Path
from stack_orchestrator.deploy.deployment_context import DeploymentContext


def init(deploy_command_context):
    """Return default spec content for the monitoring stack."""
    return {}


def create(context: DeploymentContext, extra_args):
    """Apply RBAC for Prometheus kubernetes_sd_configs service discovery.

    Grants the default ServiceAccount (used by SO deployments) permission
    to list/watch pods so Prometheus can discover scrape targets.
    """
    import subprocess

    rbac_path = Path(__file__).parent / "rbac.yaml"
    result = subprocess.run(
        ["kubectl", "apply", "-f", str(rbac_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"Warning: RBAC apply failed: {result.stderr}")
    else:
        print("Applied Prometheus RBAC (ClusterRole + ClusterRoleBinding)")
