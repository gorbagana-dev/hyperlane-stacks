from pathlib import Path
from stack_orchestrator.deploy.deployment_context import DeploymentContext


def init(deploy_command_context):
    """Return default spec content for this stack.

    Provides http-proxy configuration for ingress routing.
    """
    return {
        "network": {
            "http-proxy": [
                {
                    "host-name": "bridge.example.com",
                    "routes": [
                        {
                            "path": "/",
                            "proxy-to": "warp-ui:3000"
                        }
                    ]
                }
            ],
            "acme-email": "admin@example.com"
        }
    }


def create(context: DeploymentContext, extra_args):
    """No additional setup needed for warp UI."""
    pass
