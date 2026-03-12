import json
import subprocess
from pathlib import Path

from stack_orchestrator.deploy.deployment_context import DeploymentContext


def init(deploy_command_context):
    """Return default spec content for this stack."""
    return {}


def create(context: DeploymentContext, extra_args):
    """No-op for create phase."""
    pass


def start(context: DeploymentContext):
    """Create a ClusterIP service for cross-stack S3 access.

    Other stacks (validators, relayer) connect to MinIO via this service.
    The service selects pods using app.kubernetes.io/stack label, which SO
    adds automatically to all pods.
    """
    namespace = context.spec.get_namespace() or f"laconic-{context.id}"
    # stack.name may be an absolute path; extract the directory basename
    stack_name = Path(context.stack.name).name

    svc_manifest = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": stack_name,
            "namespace": namespace,
            "labels": {
                "app": context.id,
            },
        },
        "spec": {
            "type": "ClusterIP",
            "selector": {
                "app.kubernetes.io/stack": stack_name,
            },
            "ports": [
                {
                    "name": "s3",
                    "port": 9000,
                    "targetPort": 9000,
                    "protocol": "TCP",
                },
                {
                    "name": "console",
                    "port": 9001,
                    "targetPort": 9001,
                    "protocol": "TCP",
                },
            ],
        },
    }

    svc_file = Path(f"/tmp/{stack_name}-service.json")
    svc_file.write_text(json.dumps(svc_manifest, indent=2))

    print(f"Creating {stack_name} ClusterIP service in namespace {namespace}...")
    result = subprocess.run(
        ["kubectl", "apply", "-f", str(svc_file), "-n", namespace],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"Warning: Failed to create {stack_name} Service: {result.stderr}")
    else:
        print(f"{stack_name} Service created successfully")

    svc_file.unlink(missing_ok=True)
