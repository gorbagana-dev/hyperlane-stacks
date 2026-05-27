#!/bin/sh
# provision.sh — MinIO bucket + IAM provisioner for Hyperlane validators.
#
# Runs inside minio/mc container as part of the minio-provision CronJob.
# Env vars injected from two k8s Secrets:
#   hyperlane-minio-secrets   → MINIO_ROOT_USER, MINIO_ROOT_PASSWORD
#   minio-validator-secrets   → MINIO_USERS (comma-separated labels),
#                               <LABEL_UPPER>_KEY_ID, <LABEL_UPPER>_SECRET per label
#
# For each label in MINIO_USERS, creates:
#   - bucket:  hyperlane-validator-<label>  (with anonymous read for relayer)
#   - user:    <KEY_ID>
#   - policy:  policy-<label>  (s3:* scoped to the bucket)
#
# Idempotent: safe to re-run to add subsequent validators.
# To add a new validator: patch minio-validator-secrets, then trigger:
#   kubectl create job -n laconic-hyperlane-minio <name> --from=cronjob/minio-provision

set -e

MINIO_URL="http://minio-service:9000"

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
printf '%s\n' "${MINIO_USERS}" | tr ',' '\n' | while IFS= read -r label; do
  label="$(printf '%s' "${label}" | tr -d '[:space:]')"
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

  # Create IAM user (idempotent: no-op if already exists)
  mc admin user add local "${key_id}" "${secret}" || true

  # Create bucket-scoped IAM policy (s3:* on this bucket only)
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
  mc admin policy create local "${policy_name}" "${tmp_policy}" || true
  rm -f "${tmp_policy}"

  # Attach policy to user
  mc admin policy attach local "${policy_name}" --user "${key_id}"

  echo "Provisioned ${label}: user=${key_id}, bucket=${bucket}, policy=${policy_name}"
done

echo "All validators provisioned successfully"
