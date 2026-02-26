from pathlib import Path
from stack_orchestrator.deploy.deployment_context import DeploymentContext


def init(deploy_command_context):
    """Return default spec content for this stack."""
    return {}


def create(context: DeploymentContext, extra_args):
    """Create a k8s Service named 'hyperlane-minio' pointing to the MinIO pod.

    SO-generated service names follow the pattern {deployment-id}-service,
    so compose hostnames don't resolve across stacks.  This hook creates
    a static Service named 'hyperlane-minio' so that compose files can
    reference http://hyperlane-minio:9000 regardless of the MinIO
    deployment's generated ID.
    """
    import subprocess
    import json

    namespace = "default"

    svc_manifest = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": "hyperlane-minio",
            "namespace": namespace,
        },
        "spec": {
            "selector": {
                "app": "hyperlane-minio",
            },
            "ports": [
                {
                    "name": "s3",
                    "port": 9000,
                    "targetPort": 9000,
                    "protocol": "TCP",
                }
            ],
        },
    }

    svc_file = Path(f"/tmp/hyperlane-minio-service.json")
    svc_file.write_text(json.dumps(svc_manifest, indent=2))

    print("Creating hyperlane-minio k8s Service for cross-stack S3 access...")
    result = subprocess.run(
        ["kubectl", "apply", "-f", str(svc_file), "-n", namespace],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"Warning: Failed to create hyperlane-minio Service: {result.stderr}")
    else:
        print("hyperlane-minio Service created successfully")

    svc_file.unlink(missing_ok=True)
