from pathlib import Path
from stack_orchestrator.deploy.deployment_context import DeploymentContext


def init(deploy_command_context):
    """Return default spec content for the monitoring stack."""
    return {}


def create(context: DeploymentContext, extra_args):
    """Apply RBAC for Prometheus kubernetes_sd_configs service discovery.

    Creates ServiceAccount, ClusterRole, and ClusterRoleBinding so
    Prometheus can discover pods with prometheus.io/scrape annotations.
    """
    import subprocess

    namespace = context.spec.obj.get("namespace", "default")
    deployment_id = context.id

    rbac_manifest = f"""
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {deployment_id}-prometheus
  namespace: {namespace}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: {deployment_id}-prometheus
rules:
  - apiGroups: [""]
    resources: ["pods", "nodes", "endpoints", "services"]
    verbs: ["get", "list", "watch"]
  - apiGroups: [""]
    resources: ["configmaps"]
    verbs: ["get"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: {deployment_id}-prometheus
subjects:
  - kind: ServiceAccount
    name: {deployment_id}-prometheus
    namespace: {namespace}
roleRef:
  kind: ClusterRole
  name: {deployment_id}-prometheus
  apiGroup: rbac.authorization.k8s.io
"""

    rbac_file = Path(f"/tmp/{deployment_id}-prometheus-rbac.yaml")
    rbac_file.write_text(rbac_manifest)

    print(f"Applying Prometheus RBAC for {deployment_id}...")
    result = subprocess.run(
        ["kubectl", "apply", "-f", str(rbac_file)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"Warning: Failed to apply Prometheus RBAC: {result.stderr}")
    else:
        print(f"Prometheus RBAC applied successfully")

    rbac_file.unlink(missing_ok=True)
