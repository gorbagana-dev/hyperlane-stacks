from pathlib import Path

from stack_orchestrator.deploy.deployment_context import DeploymentContext


def create(context: DeploymentContext, extra_args):
    """Apply RBAC for Prometheus kubernetes_sd_configs service discovery.

    Grants the default ServiceAccount (used by SO deployments) permission
    to list/watch pods so Prometheus can discover scrape targets.
    The namespace in the ClusterRoleBinding subject is patched to match
    the deployment's namespace (varies between prod and test).
    """
    import subprocess

    namespace = context.spec.get_namespace() or "default"

    rbac_path = Path(__file__).parent / "rbac.yaml"
    rbac_content = rbac_path.read_text().replace(
        "namespace: default", f"namespace: {namespace}"
    )

    result = subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=rbac_content,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"Warning: RBAC apply failed: {result.stderr}")
    else:
        print(f"Applied Prometheus RBAC (namespace={namespace})")
