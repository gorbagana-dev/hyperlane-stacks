from stack_orchestrator.deploy.deployment_context import DeploymentContext


def init(deploy_command_context):
    """Return default spec content for this stack."""
    return {}


def create(context: DeploymentContext, extra_args):
    """No-op for create phase."""
    pass


def start(context: DeploymentContext):
    """No-op — the minio-service ClusterIP Service (port 9000) is created
    automatically by SO from the http-proxy: minio:9000 route in the spec.
    The minio-init Job uses http://minio-service:9000 to reach the S3 API.
    Cross-namespace consumers (validators, relayers) use selector-mode
    external-services: in their own namespaces (dev) or public DNS (prod).
    """
    pass
