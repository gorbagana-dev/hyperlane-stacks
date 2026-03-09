from stack_orchestrator.deploy.deployment_context import DeploymentContext


def init(deploy_command_context):
    """Return default spec content for this stack."""
    return {}


def create(context: DeploymentContext, extra_args):
    """No-op — cross-stack services are created by the providing stack's start() hook."""
    pass
