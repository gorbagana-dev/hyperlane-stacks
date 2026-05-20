from stack_orchestrator.deploy.deployment_context import DeploymentContext


def init(deploy_command_context):
    """Return default spec content for this stack."""
    return {}


def create(context: DeploymentContext, extra_args):
    """No post-create hooks. Deployer reads/writes via /state mount."""
    pass
