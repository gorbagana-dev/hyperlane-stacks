"""commands.py — hyperlane-minio post-start hook.

Creates a suspended `minio-provision` CronJob in the minio namespace
and immediately triggers it as `minio-provision-initial` to perform
first-run bucket + IAM setup.

The CronJob reads `MINIO_USERS` (comma-separated validator labels) and
per-label creds from `minio-validator-secrets`, plus root creds from
`hyperlane-minio-secrets`. It is idempotent: re-triggering is safe.

To add a subsequent validator, update the `minio-validator-secrets`
Secret and run:
  kubectl create job -n laconic-hyperlane-minio minio-provision-<name> \
    --from=cronjob/minio-provision
"""

from pathlib import Path

from stack_orchestrator.deploy.deployment_context import DeploymentContext

# Provisioning script — see provision.sh in the same directory.
_PROVISION_SCRIPT = (Path(__file__).parent / "provision.sh").read_text()


def start(context: DeploymentContext, extra_args=None) -> None:
    """Create suspended minio-provision CronJob and trigger initial provisioning."""
    from kubernetes import client, config

    namespace = context.spec.get_namespace() or "laconic-hyperlane-minio"
    kind_cluster = context.spec.get_kind_cluster_name() or context.id
    config.load_kube_config(context=f"kind-{kind_cluster}")

    batch_api = client.BatchV1Api()

    cronjob_name = "minio-provision"
    mc_image = "minio/mc:RELEASE.2025-08-13T08-35-41Z"

    # SO names the single-pod MinIO Service "{deployment-id}-service" (app_name ==
    # deployment-id). The provision job runs in this same namespace, so target that
    # name; hardcoding "minio-service" only matches when deployment-id is "minio".
    minio_url = f"http://{context.get_deployment_id()}-service:9000"

    job_template_spec = client.V1JobSpec(
        template=client.V1PodTemplateSpec(
            spec=client.V1PodSpec(
                restart_policy="OnFailure",
                containers=[
                    client.V1Container(
                        name="minio-provision",
                        image=mc_image,
                        command=["/bin/sh", "-c"],
                        args=[_PROVISION_SCRIPT],
                        env=[client.V1EnvVar(name="MINIO_URL", value=minio_url)],
                        env_from=[
                            client.V1EnvFromSource(
                                secret_ref=client.V1SecretEnvSource(
                                    name="hyperlane-minio-secrets"
                                )
                            ),
                            client.V1EnvFromSource(
                                secret_ref=client.V1SecretEnvSource(
                                    name="minio-validator-secrets"
                                )
                            ),
                        ],
                    )
                ],
            )
        )
    )

    cronjob = client.V1CronJob(
        metadata=client.V1ObjectMeta(
            name=cronjob_name,
            namespace=namespace,
        ),
        spec=client.V1CronJobSpec(
            # Unreachable schedule — this CronJob is never auto-triggered.
            # Trigger manually with:
            #   kubectl create job -n <ns> <name> --from=cronjob/minio-provision
            schedule="0 0 31 2 *",
            suspend=True,
            job_template=client.V1JobTemplateSpec(spec=job_template_spec),
        ),
    )

    try:
        batch_api.create_namespaced_cron_job(namespace=namespace, body=cronjob)
        print(f"Created CronJob {cronjob_name} in {namespace}")
    except client.exceptions.ApiException as e:
        if e.status == 409:
            batch_api.patch_namespaced_cron_job(
                name=cronjob_name, namespace=namespace, body=cronjob
            )
            print(f"Patched existing CronJob {cronjob_name} in {namespace}")
        else:
            raise

    # Trigger the initial provisioning run immediately.
    initial_job_name = "minio-provision-initial"
    initial_job = client.V1Job(
        metadata=client.V1ObjectMeta(
            name=initial_job_name,
            namespace=namespace,
            annotations={"cronjob.kubernetes.io/instantiate": "manual"},
        ),
        spec=job_template_spec,
    )

    try:
        batch_api.create_namespaced_job(namespace=namespace, body=initial_job)
        print(f"Triggered initial provisioning job: {initial_job_name}")
    except client.exceptions.ApiException as e:
        if e.status == 409:
            print(f"Initial provisioning job {initial_job_name} already exists — skipping")
        else:
            raise
