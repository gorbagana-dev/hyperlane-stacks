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

from stack_orchestrator.deploy.deployment_context import DeploymentContext

# Provisioning script embedded in the CronJob container.
# Reads MINIO_USERS (comma-separated labels, e.g. "gorchain-primary,solana-primary")
# and creates: bucket, anonymous-read policy, IAM user, bucket-scoped IAM policy.
_PROVISION_SCRIPT = r"""
set -e

MINIO_URL="http://minio:9000"

echo "Waiting for MinIO at ${MINIO_URL}..."
retries=0
until mc alias set local "${MINIO_URL}" "${MINIO_ROOT_USER}" "${MINIO_ROOT_PASSWORD}" 2>/dev/null \
      && mc ls local 2>/dev/null; do
  retries=$((retries + 1))
  if [ "${retries}" -ge 90 ]; then
    echo "MinIO not ready after 180s, giving up"
    exit 1
  fi
  echo "MinIO not ready yet, retrying in 2s..."
  sleep 2
done
echo "MinIO is ready"

# MINIO_USERS: comma-separated validator labels, e.g. "gorchain-primary,solana-primary"
IFS=',' read -r -a USERS <<< "${MINIO_USERS}"

for label in "${USERS[@]}"; do
  label="$(echo "${label}" | tr -d '[:space:]')"
  # Derive env var prefix: gorchain-primary -> GORCHAIN_PRIMARY
  prefix="$(echo "${label}" | tr '[:lower:]-' '[:upper:]_')"

  key_id_var="${prefix}_KEY_ID"
  secret_var="${prefix}_SECRET"
  key_id="$(eval echo "\${${key_id_var}}")"
  secret="$(eval echo "\${${secret_var}}")"

  if [ -z "${key_id}" ] || [ -z "${secret}" ]; then
    echo "ERROR: ${key_id_var} or ${secret_var} not set for label '${label}'"
    exit 1
  fi

  bucket="hyperlane-validator-${label}"
  policy_name="policy-${label}"

  echo "Provisioning label=${label} bucket=${bucket}..."

  # Create bucket
  mc mb --ignore-existing "local/${bucket}"

  # Allow anonymous read so relayer can fetch checkpoints without credentials
  retries=0
  until mc anonymous set download "local/${bucket}"; do
    retries=$((retries + 1))
    if [ "${retries}" -ge 30 ]; then
      echo "Failed to set anonymous policy on ${bucket} after 30 retries"
      exit 1
    fi
    echo "Retrying anonymous set for ${bucket} in 3s..."
    sleep 3
  done

  # Create IAM user
  mc admin user add local "${key_id}" "${secret}"

  # Create bucket-scoped IAM policy (read+write this bucket only)
  tmp_policy="$(mktemp)"
  cat > "${tmp_policy}" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:*"],
      "Resource": [
        "arn:aws:s3:::${bucket}",
        "arn:aws:s3:::${bucket}/*"
      ]
    }
  ]
}
EOF
  mc admin policy create local "${policy_name}" "${tmp_policy}"
  rm -f "${tmp_policy}"

  # Attach policy to user
  mc admin policy attach local "${policy_name}" --user "${key_id}"

  echo "Provisioned ${label}: user=${key_id}, bucket=${bucket}, policy=${policy_name}"
done

echo "All validators provisioned successfully"
"""


def start(context: DeploymentContext, extra_args) -> None:
    """Create suspended minio-provision CronJob and trigger initial provisioning."""
    from kubernetes import client, config

    namespace = context.spec.get_namespace() or "laconic-hyperlane-minio"
    kind_cluster = context.spec.get_kind_cluster_name() or context.id
    config.load_kube_config(context=f"kind-{kind_cluster}")

    batch_api = client.BatchV1Api()

    cronjob_name = "minio-provision"
    mc_image = "minio/mc:RELEASE.2025-08-13T08-35-41Z"

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
            batch_api.replace_namespaced_cron_job(
                name=cronjob_name, namespace=namespace, body=cronjob
            )
            print(f"Replaced existing CronJob {cronjob_name} in {namespace}")
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
