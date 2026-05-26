from pathlib import Path

from kubernetes import client, config as k8s_config
from kubernetes.client.rest import ApiException

from stack_orchestrator.deploy.deployment_context import DeploymentContext


def init(deploy_command_context):
    """Return default spec content for this stack."""
    return {}


def create(context: DeploymentContext, extra_args):
    """No-op for create phase."""
    pass


def start(context: DeploymentContext):
    """No-op — consumers now reach MinIO through Caddy via external-services
    (dev) or public DNS (prod). The previous cross-stack ClusterIP Service
    is no longer required.
    """
    pass
