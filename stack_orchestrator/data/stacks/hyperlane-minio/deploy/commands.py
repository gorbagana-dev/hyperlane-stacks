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
    """Create a ClusterIP service named after the stack (`hyperlane-minio`)
    so cross-namespace consumers can reach MinIO via:
        hyperlane-minio.<minio-ns>.svc.cluster.local:9000
    """
    stack_name = Path(context.stack.name).name
    namespace = context.spec.get_namespace() or f"laconic-{stack_name}"

    service = client.V1Service(
        api_version="v1",
        kind="Service",
        metadata=client.V1ObjectMeta(
            name=stack_name,
            namespace=namespace,
            labels={"app": context.id},
        ),
        spec=client.V1ServiceSpec(
            type="ClusterIP",
            selector={"app.kubernetes.io/stack": stack_name},
            ports=[
                client.V1ServicePort(name="s3", port=9000, target_port=9000, protocol="TCP"),
                client.V1ServicePort(name="console", port=9001, target_port=9001, protocol="TCP"),
            ],
        ),
    )

    kind_cluster_name = context.spec.get_kind_cluster_name()
    try:
        if kind_cluster_name:
            k8s_config.load_kube_config(context=f"kind-{kind_cluster_name}")
        else:
            k8s_config.load_kube_config()
    except Exception:
        try:
            k8s_config.load_incluster_config()
        except Exception as e:
            print(f"Warning: could not load kube config, skipping Service creation: {e}")
            return

    core_api = client.CoreV1Api()

    try:
        core_api.create_namespaced_service(namespace=namespace, body=service)
        print(f"Created Service {stack_name!r} in namespace {namespace!r}")
    except ApiException as e:
        if e.status != 409:
            raise
        core_api.patch_namespaced_service(name=stack_name, namespace=namespace, body=service)
        print(f"Patched existing Service {stack_name!r} in namespace {namespace!r}")
