# MinIO Per-Validator Users Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single shared MinIO root credential with per-validator IAM users and bucket-scoped policies, provisioned via a suspended k8s CronJob created by a `commands.py start()` hook.

**Architecture:** The `hyperlane-minio-init` Job (which creates buckets using root creds) is replaced by a `minio-provision` CronJob (`suspend: true`) created at deploy time via the k8s Python SDK in `commands.py`. The CronJob is triggered immediately after creation for the initial setup and can be retriggered on-demand to add subsequent validators. Each validator label (e.g., `gorchain-primary`) gets its own IAM user, a dedicated bucket (`hyperlane-validator-gorchain-primary`), and a bucket-scoped IAM policy. Root credentials are kept in `hyperlane-minio-secrets`; all validator credentials live in a separate `minio-validator-secrets` Secret.

**Tech Stack:** Python k8s SDK (`kubernetes` package from laconic-so shiv), `minio/mc:RELEASE.2025-08-13T08-35-41Z`, pytest, PyYAML (via conftest).

---

## File Map

| Action | File | Responsibility |
|---|---|---|
| **DELETE** | `stack_orchestrator/data/compose-jobs/docker-compose-hyperlane-minio-init.yml` | Old init job — replaced by CronJob |
| **MODIFY** | `stack_orchestrator/data/stacks/hyperlane-minio/stack.yml` | Remove `jobs:` block |
| **CREATE** | `stack_orchestrator/data/stacks/hyperlane-minio/deploy/commands.py` | `start()` hook: creates CronJob + triggers initial job |
| **MODIFY** | `tests/e2e/fixtures/test-spec-minio.yml` | Split into two Secrets; add validator cred keys |
| **MODIFY** | `deployment/spec-minio.yml` | Same split + updated operator comments |
| **MODIFY** | `tests/e2e/fixtures/test-spec-validator-gorchain.yml` | New bucket name, label-specific cred env vars |
| **MODIFY** | `tests/e2e/fixtures/test-spec-validator-solana.yml` | Same for solana |
| **MODIFY** | `deployment/spec-validator-gorchain.yml` | Same for prod gorchain spec |
| **MODIFY** | `deployment/spec-validator-solana.yml` | Same for prod solana spec |
| **MODIFY** | `tests/e2e/fixtures/test-spec-relayer.yml` | Remove `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` |
| **MODIFY** | `deployment/spec-relayer.yml` | Same removal + updated comments |
| **MODIFY** | `stack_orchestrator/data/compose/docker-compose-hyperlane-relayer.yml` | Update env comment (anonymous S3) |
| **MODIFY** | `tests/e2e/conftest.py` | `MinioInfo`, `_recover_minio_credentials`, `minio_deployment`, `_deploy_validator`, `relayer_deployment` |
| **MODIFY** | `tests/e2e/test_03_minio.py` | Update 2 existing tests; add 4 new tests |

---

## Task 1: Remove the init job

**Files:**
- Delete: `stack_orchestrator/data/compose-jobs/docker-compose-hyperlane-minio-init.yml`
- Modify: `stack_orchestrator/data/stacks/hyperlane-minio/stack.yml`

- [ ] **Step 1: Delete the init job compose file**

```bash
git rm stack_orchestrator/data/compose-jobs/docker-compose-hyperlane-minio-init.yml
```

- [ ] **Step 2: Remove the `jobs:` block from `stack.yml`**

Current `stack_orchestrator/data/stacks/hyperlane-minio/stack.yml`:
```yaml
version: "1.1"
name: hyperlane-minio
description: "S3-compatible checkpoint storage for Hyperlane validators (MinIO)"
pods:
  - hyperlane-minio
jobs:
  - hyperlane-minio-init
```

Replace with:
```yaml
version: "1.1"
name: hyperlane-minio
description: "S3-compatible checkpoint storage for Hyperlane validators (MinIO)"
pods:
  - hyperlane-minio
```

- [ ] **Step 3: Verify**

```bash
git diff stack_orchestrator/data/stacks/hyperlane-minio/stack.yml
git status
```

Expected: `stack.yml` shows `jobs:` block removed. Compose file shown as deleted.

- [ ] **Step 4: Commit**

```bash
git add stack_orchestrator/data/stacks/hyperlane-minio/stack.yml
git commit -m "feat(minio): remove minio-init job — replaced by commands.py CronJob"
```

---

## Task 2: Create `commands.py` with CronJob provisioner

**Files:**
- Create: `stack_orchestrator/data/stacks/hyperlane-minio/deploy/__init__.py` (empty)
- Create: `stack_orchestrator/data/stacks/hyperlane-minio/deploy/commands.py`

The `start()` hook runs inside SO's `up()` after `_create_deployment()` (which includes `_create_user_secrets()`). Both Secrets already exist by the time this hook fires.

- [ ] **Step 1: Create the `deploy/` directory and `__init__.py`**

```bash
mkdir -p stack_orchestrator/data/stacks/hyperlane-minio/deploy
touch stack_orchestrator/data/stacks/hyperlane-minio/deploy/__init__.py
```

- [ ] **Step 2: Write `commands.py`**

Create `stack_orchestrator/data/stacks/hyperlane-minio/deploy/commands.py`:

```python
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
```

- [ ] **Step 3: Verify file structure**

```bash
ls stack_orchestrator/data/stacks/hyperlane-minio/deploy/
```

Expected: `__init__.py  commands.py`

- [ ] **Step 4: Commit**

```bash
git add stack_orchestrator/data/stacks/hyperlane-minio/deploy/
git commit -m "feat(minio): add commands.py — create suspended minio-provision CronJob on start"
```

---

## Task 3: Split MinIO secrets in specs

**Files:**
- Modify: `tests/e2e/fixtures/test-spec-minio.yml`
- Modify: `deployment/spec-minio.yml`

- [ ] **Step 1: Update `test-spec-minio.yml`**

Replace the current `secrets:` block:

```yaml
secrets:
  hyperlane-minio-secrets:
    keys:
      MINIO_ROOT_USER: { env: MINIO_ROOT_USER }
      MINIO_ROOT_PASSWORD: { env: MINIO_ROOT_PASSWORD }
  minio-validator-secrets:
    keys:
      MINIO_USERS:                 { env: MINIO_USERS }
      GORCHAIN_PRIMARY_KEY_ID:     { env: GORCHAIN_PRIMARY_KEY_ID }
      GORCHAIN_PRIMARY_SECRET:     { env: GORCHAIN_PRIMARY_SECRET }
      SOLANA_PRIMARY_KEY_ID:       { env: SOLANA_PRIMARY_KEY_ID }
      SOLANA_PRIMARY_SECRET:       { env: SOLANA_PRIMARY_SECRET }
```

Full updated `tests/e2e/fixtures/test-spec-minio.yml`:

```yaml
stack: stack_orchestrator/data/stacks/hyperlane-minio
deploy-to: k8s-kind
image-overrides:
  minio: minio/minio:RELEASE.2025-09-07T16-13-09Z
kind-cluster-name: hyperlane
kind-mount-root: /tmp/hyperlane-bridge-e2e
resources:
  containers:
    minio:
      reservations:
        cpus: "0.1"
        memory: 128M
      limits:
        cpus: "0.5"
        memory: 256M
volumes:
  minio-data:
secrets:
  hyperlane-minio-secrets:
    keys:
      MINIO_ROOT_USER:     { env: MINIO_ROOT_USER }
      MINIO_ROOT_PASSWORD: { env: MINIO_ROOT_PASSWORD }
  minio-validator-secrets:
    keys:
      MINIO_USERS:             { env: MINIO_USERS }
      GORCHAIN_PRIMARY_KEY_ID: { env: GORCHAIN_PRIMARY_KEY_ID }
      GORCHAIN_PRIMARY_SECRET: { env: GORCHAIN_PRIMARY_SECRET }
      SOLANA_PRIMARY_KEY_ID:   { env: SOLANA_PRIMARY_KEY_ID }
      SOLANA_PRIMARY_SECRET:   { env: SOLANA_PRIMARY_SECRET }
network:
  acme-email: e2e@example.test
  http-proxy:
    - host-name: minio-s3.test
      routes:
        - path: /
          proxy-to: minio:9000
    - host-name: minio-console.test
      routes:
        - path: /
          proxy-to: minio:9001
```

- [ ] **Step 2: Update `deployment/spec-minio.yml`**

Full updated file:

```yaml
# Hyperlane MinIO - deployment spec
# S3-compatible storage for validator checkpoints.
stack: stack_orchestrator/data/stacks/hyperlane-minio
deploy-to: k8s-kind
kind-cluster-name: hyperlane
kind-mount-root: /srv/kind/hyperlane-bridge
volumes:
  minio-data:
resources:
  volumes:
    minio-data:
      reservations:
        storage: 10Gi
network:
  acme-email: admin@gorbagana.wtf
  http-proxy:
    - host-name: s3.bridge.gorbagana.wtf
      routes:
        - path: /
          proxy-to: minio:9000
    - host-name: minio-console.bridge.gorbagana.wtf
      routes:
        - path: /
          proxy-to: minio:9001
# Before deploying, export these env vars in the shell that runs `laconic-so`:
#
#   MINIO_ROOT_USER, MINIO_ROOT_PASSWORD      — MinIO root credentials
#
#   MINIO_USERS                               — Comma-separated validator labels
#                                               e.g. "gorchain-primary,solana-primary"
#
#   For each label in MINIO_USERS, export two creds:
#   <LABEL_UPPER>_KEY_ID, <LABEL_UPPER>_SECRET
#   e.g. GORCHAIN_PRIMARY_KEY_ID, GORCHAIN_PRIMARY_SECRET
#        SOLANA_PRIMARY_KEY_ID, SOLANA_PRIMARY_SECRET
#
# To add a subsequent validator post-deployment, update the minio-validator-secrets
# Secret and trigger the CronJob:
#   kubectl create job -n laconic-hyperlane-minio minio-provision-<name> \
#     --from=cronjob/minio-provision
secrets:
  hyperlane-minio-secrets:
    keys:
      MINIO_ROOT_USER:     { env: MINIO_ROOT_USER }
      MINIO_ROOT_PASSWORD: { env: MINIO_ROOT_PASSWORD }
  minio-validator-secrets:
    keys:
      MINIO_USERS:             { env: MINIO_USERS }
      GORCHAIN_PRIMARY_KEY_ID: { env: GORCHAIN_PRIMARY_KEY_ID }
      GORCHAIN_PRIMARY_SECRET: { env: GORCHAIN_PRIMARY_SECRET }
      SOLANA_PRIMARY_KEY_ID:   { env: SOLANA_PRIMARY_KEY_ID }
      SOLANA_PRIMARY_SECRET:   { env: SOLANA_PRIMARY_SECRET }
```

- [ ] **Step 3: Verify**

```bash
git diff tests/e2e/fixtures/test-spec-minio.yml deployment/spec-minio.yml
```

Expected: both files show `minio-validator-secrets` block added, root creds moved to `hyperlane-minio-secrets` only.

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/fixtures/test-spec-minio.yml deployment/spec-minio.yml
git commit -m "feat(minio): split secrets — root creds in hyperlane-minio-secrets, validator creds in minio-validator-secrets"
```

---

## Task 4: Update validator specs (bucket name + credential env vars)

**Files:**
- Modify: `tests/e2e/fixtures/test-spec-validator-gorchain.yml`
- Modify: `tests/e2e/fixtures/test-spec-validator-solana.yml`
- Modify: `deployment/spec-validator-gorchain.yml`
- Modify: `deployment/spec-validator-solana.yml`

The bucket name changes from `hyperlane-validator-gorchain` → `hyperlane-validator-gorchain-primary` (and same for solana). The secret keys `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` now source from label-specific env vars instead of the shared `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`.

- [ ] **Step 1: Update `test-spec-validator-gorchain.yml`**

Change `CHECKPOINT_BUCKET` and `secrets:` block. Full updated file:

```yaml
stack: stack_orchestrator/data/stacks/hyperlane-validator
deploy-to: k8s-kind
image-overrides:
  validator: REPLACE_AGENT_IMAGE
  kms-proxy: REPLACE_KMS_PROXY_IMAGE
namespace: laconic-hyperlane-validator-gorchain
kind-cluster-name: hyperlane
kind-mount-root: /tmp/hyperlane-bridge-e2e
config:
  ORIGIN_CHAIN_NAME: gorchain
  CHECKPOINT_BUCKET: hyperlane-validator-gorchain-primary
  PRIVY_API_URL: "http://privy-mock:19876"
  PRIVY_WALLET_ID: REPLACE_PRIVY_WALLET_ID
  # Dev only — direct cross-NS to the MinIO pod via external-services
  # selector mode below. No Caddy in this path.
  AWS_ENDPOINT_URL_S3: "http://hyperlane-minio:9000"
annotations:
  prometheus.io/scrape: "true"
  prometheus.io/port: "9090"
resources:
  containers:
    validator:
      reservations:
        cpus: "0.25"
        memory: 256M
      limits:
        cpus: "1"
        memory: 512M
    kms-proxy:
      reservations:
        cpus: "0.1"
        memory: 64M
      limits:
        cpus: "0.25"
        memory: 128M
volumes:
  validator-data:
configmaps:
  agent-config: ./configmaps/agent-config
secrets:
  hyperlane-validator-gorchain-secrets:
    keys:
      PRIVY_APP_ID:          { env: PRIVY_APP_ID }
      PRIVY_APP_SECRET:      { env: PRIVY_APP_SECRET }
      AWS_ACCESS_KEY_ID:     { env: GORCHAIN_PRIMARY_KEY_ID }
      AWS_SECRET_ACCESS_KEY: { env: GORCHAIN_PRIMARY_SECRET }
      HYP_DEFAULTSIGNER_KEY: { env: HYP_DEFAULTSIGNER_KEY }
image-pull-secret:
  server: ghcr.io
  username: gorbagana-dev
  token-env: GHCR_PAT
external-services:
  gorchain-rpc:
    ip: REPLACE_HOST_IP
    port: 8899
  solana-rpc:
    ip: REPLACE_HOST_IP
    port: 18899
  privy-mock:
    ip: REPLACE_HOST_IP
    port: 19876
  # Dev only — prod uses public DNS to resolve s3.bridge.gorbagana.wtf.
  # Selector mode: SO creates a headless Service named `hyperlane-minio`
  # in this NS with Endpoints discovered from MinIO pods in the
  # laconic-hyperlane-minio namespace at deploy time. Pod restart
  # invalidates the Endpoints; tests do full re-deploys, so this is fine
  # in dev. Prod uses the Caddy-fronted public hostname instead.
  hyperlane-minio:
    selector:
      app.kubernetes.io/stack: hyperlane-minio
    namespace: laconic-hyperlane-minio
    port: 9000
network:
  acme-email: e2e@example.test
  http-proxy:
    - host-name: validator-gorchain.test
      routes:
        - path: /
          proxy-to: validator:9090
```

- [ ] **Step 2: Update `test-spec-validator-solana.yml`**

Same pattern — full updated file:

```yaml
stack: stack_orchestrator/data/stacks/hyperlane-validator
deploy-to: k8s-kind
image-overrides:
  validator: REPLACE_AGENT_IMAGE
  kms-proxy: REPLACE_KMS_PROXY_IMAGE
namespace: laconic-hyperlane-validator-solana
kind-cluster-name: hyperlane
kind-mount-root: /tmp/hyperlane-bridge-e2e
config:
  ORIGIN_CHAIN_NAME: solana
  CHECKPOINT_BUCKET: hyperlane-validator-solana-primary
  PRIVY_API_URL: "http://privy-mock:19876"
  PRIVY_WALLET_ID: REPLACE_PRIVY_WALLET_ID
  # Dev only — direct cross-NS to the MinIO pod via external-services
  # selector mode below. No Caddy in this path.
  AWS_ENDPOINT_URL_S3: "http://hyperlane-minio:9000"
annotations:
  prometheus.io/scrape: "true"
  prometheus.io/port: "9090"
resources:
  containers:
    validator:
      reservations:
        cpus: "0.25"
        memory: 256M
      limits:
        cpus: "1"
        memory: 512M
    kms-proxy:
      reservations:
        cpus: "0.1"
        memory: 64M
      limits:
        cpus: "0.25"
        memory: 128M
volumes:
  validator-data:
configmaps:
  agent-config: ./configmaps/agent-config
secrets:
  hyperlane-validator-solana-secrets:
    keys:
      PRIVY_APP_ID:          { env: PRIVY_APP_ID }
      PRIVY_APP_SECRET:      { env: PRIVY_APP_SECRET }
      AWS_ACCESS_KEY_ID:     { env: SOLANA_PRIMARY_KEY_ID }
      AWS_SECRET_ACCESS_KEY: { env: SOLANA_PRIMARY_SECRET }
      HYP_DEFAULTSIGNER_KEY: { env: HYP_DEFAULTSIGNER_KEY }
image-pull-secret:
  server: ghcr.io
  username: gorbagana-dev
  token-env: GHCR_PAT
external-services:
  gorchain-rpc:
    ip: REPLACE_HOST_IP
    port: 8899
  solana-rpc:
    ip: REPLACE_HOST_IP
    port: 18899
  privy-mock:
    ip: REPLACE_HOST_IP
    port: 19876
  # Dev only — prod uses public DNS to resolve s3.bridge.gorbagana.wtf.
  # Selector mode: SO creates a headless Service named `hyperlane-minio`
  # in this NS with Endpoints discovered from MinIO pods in the
  # laconic-hyperlane-minio namespace at deploy time. Pod restart
  # invalidates the Endpoints; tests do full re-deploys, so this is fine
  # in dev. Prod uses the Caddy-fronted public hostname instead.
  hyperlane-minio:
    selector:
      app.kubernetes.io/stack: hyperlane-minio
    namespace: laconic-hyperlane-minio
    port: 9000
network:
  acme-email: e2e@example.test
  http-proxy:
    - host-name: validator-solana.test
      routes:
        - path: /
          proxy-to: validator:9090
```

- [ ] **Step 3: Update `deployment/spec-validator-gorchain.yml`**

Full updated file:

```yaml
# Hyperlane Validator (Gorchain) - deployment spec
# Runs a validator for Gorchain using the hyperlane-validator stack.
stack: stack_orchestrator/data/stacks/hyperlane-validator
deploy-to: k8s-kind
kind-cluster-name: hyperlane
kind-mount-root: /srv/kind/hyperlane-bridge
config:
  ORIGIN_CHAIN_NAME: gorchain
  CHECKPOINT_BUCKET: hyperlane-validator-gorchain-primary
  PRIVY_WALLET_ID: "REPLACE_WITH_WALLET_ID"
  # MinIO via Caddy — public DNS resolves to the Caddy ingress.
  AWS_ENDPOINT_URL_S3: "https://s3.bridge.gorbagana.wtf"
annotations:
  prometheus.io/scrape: "true"
  prometheus.io/port: "9090"
volumes:
  validator-data:
resources:
  volumes:
    validator-data:
      reservations:
        storage: 5Gi
configmaps:
  agent-config: ./configmaps/agent-config
# Before deploying, place the keyfile on the host:
#   ~/.credentials/hyperlane/validator-gorchain.key (hex-encoded validator signing key)
# and export these env vars in the shell that runs `laconic-so`:
#   PRIVY_APP_ID, PRIVY_APP_SECRET
#   GORCHAIN_PRIMARY_KEY_ID, GORCHAIN_PRIMARY_SECRET  — per-validator MinIO IAM creds
secrets:
  hyperlane-validator-secrets:
    keys:
      HYP_DEFAULTSIGNER_KEY:  { file: ~/.credentials/hyperlane/validator-gorchain.key }
      PRIVY_APP_ID:           { env: PRIVY_APP_ID }
      PRIVY_APP_SECRET:       { env: PRIVY_APP_SECRET }
      AWS_ACCESS_KEY_ID:      { env: GORCHAIN_PRIMARY_KEY_ID }
      AWS_SECRET_ACCESS_KEY:  { env: GORCHAIN_PRIMARY_SECRET }
# Private registry auth — set GHCR_PAT env var before deploy start:
#   export GHCR_PAT=ghp_xxxx  # GitHub PAT with packages:read scope
image-pull-secret:
  server: ghcr.io
  username: REPLACE_WITH_GITHUB_USERNAME
  token-env: GHCR_PAT
network:
  acme-email: admin@gorbagana.wtf
  http-proxy:
    - host-name: validator-gorchain.bridge.gorbagana.wtf
      routes:
        - path: /
          proxy-to: validator:9090
# Optional: override container images (service name → full image ref)
# image-overrides:
#   validator: ghcr.io/gorbagana-dev/hyperlane-agent:v1.0.0
#   kms-proxy: ghcr.io/gorbagana-dev/hyperlane-kms-proxy:v1.0.0
```

- [ ] **Step 4: Update `deployment/spec-validator-solana.yml`**

Full updated file:

```yaml
# Hyperlane Validator (Solana) - deployment spec
# Runs a validator for Solana using the hyperlane-validator stack.
stack: stack_orchestrator/data/stacks/hyperlane-validator
deploy-to: k8s-kind
kind-cluster-name: hyperlane
kind-mount-root: /srv/kind/hyperlane-bridge
config:
  ORIGIN_CHAIN_NAME: solana
  CHECKPOINT_BUCKET: hyperlane-validator-solana-primary
  PRIVY_WALLET_ID: "REPLACE_WITH_WALLET_ID"
  # MinIO via Caddy — public DNS resolves to the Caddy ingress.
  AWS_ENDPOINT_URL_S3: "https://s3.bridge.gorbagana.wtf"
annotations:
  prometheus.io/scrape: "true"
  prometheus.io/port: "9090"
volumes:
  validator-data:
resources:
  volumes:
    validator-data:
      reservations:
        storage: 5Gi
configmaps:
  agent-config: ./configmaps/agent-config
# Before deploying, place the keyfile on the host:
#   ~/.credentials/hyperlane/validator-solana.key (hex-encoded validator signing key)
# and export these env vars in the shell that runs `laconic-so`:
#   PRIVY_APP_ID, PRIVY_APP_SECRET
#   SOLANA_PRIMARY_KEY_ID, SOLANA_PRIMARY_SECRET    — per-validator MinIO IAM creds
secrets:
  hyperlane-validator-secrets:
    keys:
      HYP_DEFAULTSIGNER_KEY:  { file: ~/.credentials/hyperlane/validator-solana.key }
      PRIVY_APP_ID:           { env: PRIVY_APP_ID }
      PRIVY_APP_SECRET:       { env: PRIVY_APP_SECRET }
      AWS_ACCESS_KEY_ID:      { env: SOLANA_PRIMARY_KEY_ID }
      AWS_SECRET_ACCESS_KEY:  { env: SOLANA_PRIMARY_SECRET }
# Private registry auth — set GHCR_PAT env var before deploy start:
#   export GHCR_PAT=ghp_xxxx  # GitHub PAT with packages:read scope
image-pull-secret:
  server: ghcr.io
  username: REPLACE_WITH_GITHUB_USERNAME
  token-env: GHCR_PAT
network:
  acme-email: admin@gorbagana.wtf
  http-proxy:
    - host-name: validator-solana.bridge.gorbagana.wtf
      routes:
        - path: /
          proxy-to: validator:9090
# Optional: override container images (service name → full image ref)
# image-overrides:
#   validator: ghcr.io/gorbagana-dev/hyperlane-agent:v1.0.0
#   kms-proxy: ghcr.io/gorbagana-dev/hyperlane-kms-proxy:v1.0.0
```

- [ ] **Step 5: Verify**

```bash
git diff tests/e2e/fixtures/test-spec-validator-gorchain.yml
git diff tests/e2e/fixtures/test-spec-validator-solana.yml
git diff deployment/spec-validator-gorchain.yml
git diff deployment/spec-validator-solana.yml
```

Expected: all four files show `CHECKPOINT_BUCKET` updated to include `-primary`, and `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` sourced from label-specific env vars.

- [ ] **Step 6: Commit**

```bash
git add tests/e2e/fixtures/test-spec-validator-gorchain.yml \
        tests/e2e/fixtures/test-spec-validator-solana.yml \
        deployment/spec-validator-gorchain.yml \
        deployment/spec-validator-solana.yml
git commit -m "feat(validator): use label-specific MinIO IAM creds + renamed buckets (-primary suffix)"
```

---

## Task 5: Update relayer — remove AWS credentials

**Files:**
- Modify: `tests/e2e/fixtures/test-spec-relayer.yml`
- Modify: `deployment/spec-relayer.yml`
- Modify: `stack_orchestrator/data/compose/docker-compose-hyperlane-relayer.yml`

The relayer uses an anonymous S3 client (no credentials). `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` are not needed and should be removed everywhere.

- [ ] **Step 1: Update `test-spec-relayer.yml`**

Remove the two AWS credential keys from `secrets:`. Updated `secrets:` block:

```yaml
secrets:
  hyperlane-relayer-secrets:
    keys:
      HYP_CHAINS_GORCHAIN_SIGNER_KEY: { env: HYP_CHAINS_GORCHAIN_SIGNER_KEY }
      HYP_CHAINS_SOLANA_SIGNER_KEY: { env: HYP_CHAINS_SOLANA_SIGNER_KEY }
      RELAYER_KEYPAIR_JSON: { env: RELAYER_KEYPAIR_JSON }
```

- [ ] **Step 2: Update `deployment/spec-relayer.yml`**

Remove `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` from `secrets:` and update the comment. Updated `secrets:` block and preceding comment:

```yaml
# Before deploying, place these keyfiles on the host:
#   ~/.credentials/hyperlane/relayer-gorchain.key  (hex-encoded relayer signing key for Gorchain)
#   ~/.credentials/hyperlane/relayer-solana.key    (hex-encoded relayer signing key for Solana)
#   ~/.credentials/hyperlane/relayer-fee-claim.json (Solana keypair JSON array for IGP fee claims)
# No MinIO credentials needed — the relayer uses an anonymous S3 client.
secrets:
  hyperlane-relayer-secrets:
    keys:
      HYP_CHAINS_GORCHAIN_SIGNER_KEY: { file: ~/.credentials/hyperlane/relayer-gorchain.key }
      HYP_CHAINS_SOLANA_SIGNER_KEY:   { file: ~/.credentials/hyperlane/relayer-solana.key }
      RELAYER_KEYPAIR_JSON:            { file: ~/.credentials/hyperlane/relayer-fee-claim.json }
```

- [ ] **Step 3: Update relayer compose comment**

In `stack_orchestrator/data/compose/docker-compose-hyperlane-relayer.yml`, update the `environment:` comment to remove the AWS credential reference. Replace:

```yaml
      # Key management — injected via secrets: in spec.yml (envFrom.secretRef)
      # HYP_CHAINS_GORCHAIN_SIGNER_KEY, HYP_CHAINS_SOLANA_SIGNER_KEY
      # AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
```

With:

```yaml
      # Key management — injected via secrets: in spec.yml (envFrom.secretRef)
      # HYP_CHAINS_GORCHAIN_SIGNER_KEY, HYP_CHAINS_SOLANA_SIGNER_KEY
      # No MinIO credentials needed — relayer uses anonymous S3 client (.no_credentials()).
```

- [ ] **Step 4: Verify**

```bash
grep -n "AWS_ACCESS_KEY_ID\|AWS_SECRET_ACCESS_KEY" \
  tests/e2e/fixtures/test-spec-relayer.yml \
  deployment/spec-relayer.yml \
  stack_orchestrator/data/compose/docker-compose-hyperlane-relayer.yml
```

Expected: no output (all references removed).

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/fixtures/test-spec-relayer.yml \
        deployment/spec-relayer.yml \
        stack_orchestrator/data/compose/docker-compose-hyperlane-relayer.yml
git commit -m "feat(relayer): remove AWS credentials — relayer uses anonymous S3 client"
```

---

## Task 6: Update `conftest.py` — MinioInfo dataclass + minio_deployment fixture

**Files:**
- Modify: `tests/e2e/conftest.py`

This task updates the `MinioInfo` dataclass and the `minio_deployment` fixture (including the `_recover_minio_credentials` helper). The fixture now generates per-validator credentials, exports `MINIO_USERS`, and waits for `minio-provision-initial` instead of the old init job.

- [ ] **Step 1: Update `MinioInfo` dataclass (lines 181–199)**

Replace:

```python
class MinioInfo:
    """Minio deployment info with credentials."""
    deployment: DeploymentInfo
    user: str
    password: str

    # Delegate common fields for convenience
    @property
    def namespace(self) -> str:
        return self.deployment.namespace

    @property
    def deployment_id(self) -> str:
        return self.deployment.deployment_id

    @property
    def deploy_dir(self) -> Path:
        return self.deployment.deploy_dir
```

With:

```python
class MinioInfo:
    """Minio deployment info with credentials.

    user/password: MinIO root credentials (used for admin operations in tests).
    gorchain_key_id/gorchain_secret: IAM creds for the gorchain-primary validator.
    solana_key_id/solana_secret: IAM creds for the solana-primary validator.
    """
    deployment: DeploymentInfo
    user: str
    password: str
    gorchain_key_id: str
    gorchain_secret: str
    solana_key_id: str
    solana_secret: str

    # Delegate common fields for convenience
    @property
    def namespace(self) -> str:
        return self.deployment.namespace

    @property
    def deployment_id(self) -> str:
        return self.deployment.deployment_id

    @property
    def deploy_dir(self) -> Path:
        return self.deployment.deploy_dir
```

- [ ] **Step 2: Update `_recover_minio_credentials` (lines 467–482)**

Replace:

```python
def _recover_minio_credentials(namespace: str) -> tuple[str, str]:
    """Read minio credentials from k8s secret (for --skip-minio-deploy reuse)."""
    user = password = ""
    for field in ("MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD"):
        result = subprocess.run(
            ["kubectl", "get", "secret", "hyperlane-minio-secrets", "-n", namespace,
             "-o", f"jsonpath={{.data.{field}}}"],
            capture_output=True, text=True, check=True,
        )
        value = base64.b64decode(result.stdout.strip()).decode()
        if field == "MINIO_ROOT_USER":
            user = value
        else:
            password = value
    log.info("Recovered minio credentials from k8s secret")
    return user, password
```

With:

```python
def _recover_minio_credentials(
    namespace: str,
) -> tuple[str, str, str, str, str, str]:
    """Read minio credentials from k8s secrets (for --skip-minio-deploy reuse).

    Returns: (root_user, root_password, gorchain_key_id, gorchain_secret,
               solana_key_id, solana_secret)

    Also re-exports all values to os.environ so subsequent SO deploy calls
    can find them as env vars.
    """
    def _read_secret(secret_name: str, field: str) -> str:
        result = subprocess.run(
            ["kubectl", "get", "secret", secret_name, "-n", namespace,
             "-o", f"jsonpath={{.data.{field}}}"],
            capture_output=True, text=True, check=True,
        )
        return base64.b64decode(result.stdout.strip()).decode()

    user = _read_secret("hyperlane-minio-secrets", "MINIO_ROOT_USER")
    password = _read_secret("hyperlane-minio-secrets", "MINIO_ROOT_PASSWORD")
    gorchain_key_id = _read_secret("minio-validator-secrets", "GORCHAIN_PRIMARY_KEY_ID")
    gorchain_secret = _read_secret("minio-validator-secrets", "GORCHAIN_PRIMARY_SECRET")
    solana_key_id = _read_secret("minio-validator-secrets", "SOLANA_PRIMARY_KEY_ID")
    solana_secret = _read_secret("minio-validator-secrets", "SOLANA_PRIMARY_SECRET")

    os.environ.update({
        "MINIO_ROOT_USER": user,
        "MINIO_ROOT_PASSWORD": password,
        "MINIO_USERS": "gorchain-primary,solana-primary",
        "GORCHAIN_PRIMARY_KEY_ID": gorchain_key_id,
        "GORCHAIN_PRIMARY_SECRET": gorchain_secret,
        "SOLANA_PRIMARY_KEY_ID": solana_key_id,
        "SOLANA_PRIMARY_SECRET": solana_secret,
    })

    log.info("Recovered minio credentials from k8s secrets")
    return user, password, gorchain_key_id, gorchain_secret, solana_key_id, solana_secret
```

- [ ] **Step 3: Update `minio_deployment` fixture (lines 485–559)**

Replace the entire fixture:

```python
@pytest.fixture(scope="session")
def minio_deployment(
    request: pytest.FixtureRequest,
    host_prep: None,
    bridge_state_loader: BridgeStateLoader,
) -> Generator[MinioInfo, None, None]:
    """Deploy the hyperlane-minio stack.

    Self-contained: only requires a Kind cluster. Creates its own namespace
    and secrets. Uses an independent deployment-id for unique resource names,
    with spec-level namespace override to share the e2e namespace.

    Generates per-validator IAM credentials for gorchain-primary and
    solana-primary. These are provisioned by the commands.py CronJob
    (minio-provision-initial) triggered during deploy start.
    """
    skip_cleanup = request.config.getoption("--skip-cleanup")
    skip_minio = request.config.getoption("--skip-minio-deploy", default=False)

    if skip_minio:
        deploy_dir = DEPLOY_DIR / "hyperlane-minio"
        if deployment_exists(deploy_dir):
            deployment_id = get_deployment_id(deploy_dir)
            namespace = "laconic-hyperlane-minio"
            log.info("Reusing existing minio deployment (namespace: %s)", namespace)
            user, password, gorchain_key_id, gorchain_secret, solana_key_id, solana_secret = (
                _recover_minio_credentials(namespace)
            )
            yield MinioInfo(
                deployment=DeploymentInfo(
                    deploy_dir=deploy_dir, deployment_id=deployment_id, namespace=namespace
                ),
                user=user,
                password=password,
                gorchain_key_id=gorchain_key_id,
                gorchain_secret=gorchain_secret,
                solana_key_id=solana_key_id,
                solana_secret=solana_secret,
            )
            return
        log.info("--skip-minio-deploy set but %s missing — deploying fresh", deploy_dir)

    minio_user = f"minio-{secrets.token_hex(4)}"
    minio_password = secrets.token_hex(16)
    gorchain_key_id = f"gc-{secrets.token_hex(8)}"
    gorchain_secret = secrets.token_hex(24)
    solana_key_id = f"sol-{secrets.token_hex(8)}"
    solana_secret = secrets.token_hex(24)

    log.info("Pre-fetching MinIO images to host Docker...")
    prefetch_minio_images()

    log.info("Preparing minio stack...")
    deploy_info = deploy_prepare(
        "hyperlane-minio", MINIO_SPEC,
        spec_replacements=SPEC_REPLACEMENTS,
        deployment_id="minio",
    )
    namespace = deploy_info.namespace
    deployment_id = deploy_info.deployment_id

    bridge_state_loader.populate("hyperlane-minio", deploy_info.deploy_dir)

    os.environ.update({
        "MINIO_ROOT_USER":         minio_user,
        "MINIO_ROOT_PASSWORD":     minio_password,
        "MINIO_USERS":             "gorchain-primary,solana-primary",
        "GORCHAIN_PRIMARY_KEY_ID": gorchain_key_id,
        "GORCHAIN_PRIMARY_SECRET": gorchain_secret,
        "SOLANA_PRIMARY_KEY_ID":   solana_key_id,
        "SOLANA_PRIMARY_SECRET":   solana_secret,
    })

    log.info("Starting minio stack...")
    deploy_start(deploy_info.deploy_dir)

    try:
        log.info("Waiting for minio pod to be running...")
        wait_for_pod_phase(namespace, f"app={deployment_id}", "Running", timeout=120)

        log.info("Waiting for minio-provision-initial job to complete...")
        provision_job = "minio-provision-initial"
        wait_for_job_complete(namespace, provision_job, timeout=300)
        save_job_logs(namespace, provision_job)
        log.info("MinIO stack deployed and initialized")
    except Exception:
        save_job_logs(namespace, "minio-provision-initial")
        save_job_describe(namespace, "minio-provision-initial")
        save_pod_logs(namespace, f"app={deployment_id}", "minio")
        save_pod_describe(namespace, f"app={deployment_id}", "minio")
        raise

    yield MinioInfo(
        deployment=deploy_info,
        user=minio_user,
        password=minio_password,
        gorchain_key_id=gorchain_key_id,
        gorchain_secret=gorchain_secret,
        solana_key_id=solana_key_id,
        solana_secret=solana_secret,
    )

    save_pod_logs(namespace, f"app={deployment_id}", "minio")
    if not skip_cleanup:
        log.info("Stopping minio stack...")
        stop_stack("hyperlane-minio")
```

- [ ] **Step 4: Run the linter to check for errors**

```bash
cd tests/e2e && python -m ruff check conftest.py
```

Expected: no output (no errors).

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/conftest.py
git commit -m "feat(conftest): MinioInfo per-validator creds, minio_deployment waits for minio-provision-initial"
```

---

## Task 7: Update `conftest.py` — validator + relayer fixtures

**Files:**
- Modify: `tests/e2e/conftest.py`

Remove the shared `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` from `_deploy_validator` and `relayer_deployment`. Add chain-specific credential env vars in `_deploy_validator`.

- [ ] **Step 1: Update `_deploy_validator` env setup (lines 923–929)**

Replace:

```python
    os.environ.update({
        "PRIVY_APP_ID":           "test-app-id",
        "PRIVY_APP_SECRET":       "test-app-secret",
        "AWS_ACCESS_KEY_ID":      minio.user,
        "AWS_SECRET_ACCESS_KEY":  minio.password,
        "HYP_DEFAULTSIGNER_KEY":  chain_signer_key,
    })
```

With:

```python
    # Chain-specific MinIO IAM credentials.
    # Naming: "{chain}-primary" label → "GORCHAIN_PRIMARY_KEY_ID" / "GORCHAIN_PRIMARY_SECRET"
    # These map to AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY inside the validator container
    # via the spec's secrets: block (e.g. AWS_ACCESS_KEY_ID: { env: GORCHAIN_PRIMARY_KEY_ID }).
    chain_upper = chain.upper()
    label_upper = f"{chain_upper}_PRIMARY"
    validator_key_id = minio.gorchain_key_id if chain == "gorchain" else minio.solana_key_id
    validator_secret = minio.gorchain_secret if chain == "gorchain" else minio.solana_secret

    os.environ.update({
        "PRIVY_APP_ID":            "test-app-id",
        "PRIVY_APP_SECRET":        "test-app-secret",
        f"{label_upper}_KEY_ID":   validator_key_id,
        f"{label_upper}_SECRET":   validator_secret,
        "HYP_DEFAULTSIGNER_KEY":   chain_signer_key,
    })
```

- [ ] **Step 2: Update relayer `os.environ.update` (lines 1087–1093)**

Replace:

```python
    os.environ.update({
        "HYP_CHAINS_GORCHAIN_SIGNER_KEY": gorchain_signer_key,
        "HYP_CHAINS_SOLANA_SIGNER_KEY":   solana_signer_key,
        "AWS_ACCESS_KEY_ID":              minio_deployment.user,
        "AWS_SECRET_ACCESS_KEY":          minio_deployment.password,
        "RELAYER_KEYPAIR_JSON":           relayer_keypair_json,
    })
```

With:

```python
    # No MinIO credentials for the relayer — it uses an anonymous S3 client (.no_credentials())
    # to read validator checkpoints. Buckets are publicly readable (anonymous download policy).
    os.environ.update({
        "HYP_CHAINS_GORCHAIN_SIGNER_KEY": gorchain_signer_key,
        "HYP_CHAINS_SOLANA_SIGNER_KEY":   solana_signer_key,
        "RELAYER_KEYPAIR_JSON":           relayer_keypair_json,
    })
```

- [ ] **Step 3: Run the linter**

```bash
cd tests/e2e && python -m ruff check conftest.py
```

Expected: no output.

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/conftest.py
git commit -m "feat(conftest): _deploy_validator uses label-specific creds, relayer drops AWS creds"
```

---

## Task 8: Update and extend `test_03_minio.py`

**Files:**
- Modify: `tests/e2e/test_03_minio.py`

Update `test_minio_init_job_completed` (checks for CronJob-created job instead of old init job), update `test_minio_buckets_exist` (new bucket names), and add 4 new tests: `test_minio_users_created`, `test_minio_policies_attached`, `test_bucket_isolation`, and `test_subsequent_validator_provisioning`.

- [ ] **Step 1: Add `run_mc_as` helper below the existing `run_mc_command`**

Add after line 44 (end of `run_mc_command`):

```python
def run_mc_as(
    key_id: str,
    secret: str,
    *args: str,
    host_port: int = 9000,
) -> subprocess.CompletedProcess[str]:
    """Run a minio/mc command with explicit IAM user credentials.

    Used to verify per-user bucket access isolation.
    """
    mc_args = " ".join(str(a) for a in args)
    return subprocess.run(
        [
            "docker", "run", "--rm", "--network", "host",
            "--entrypoint", "sh",
            MC_IMAGE,
            "-c",
            f"mc alias set user http://localhost:{host_port} "
            f"{key_id} {secret} && {mc_args}",
        ],
        capture_output=True,
        text=True,
    )
```

- [ ] **Step 2: Update `test_minio_init_job_completed`**

Replace:

```python
    def test_minio_init_job_completed(self, minio_deployment: MinioInfo) -> None:
        """minio-init job completed successfully (buckets created)."""
        ns = minio_deployment.namespace
        deployment_id = minio_deployment.deployment_id
        job_name = f"{deployment_id}-job-hyperlane-minio-init"

        result = subprocess.run(
            ["kubectl", "get", "job", job_name, "-n", ns,
             "-o", "jsonpath={.status.succeeded}"],
            capture_output=True, text=True, check=True,
        )
        assert result.stdout.strip() == "1", f"Init job not succeeded: {result.stdout}"
```

With:

```python
    def test_minio_provision_job_completed(self, minio_deployment: MinioInfo) -> None:
        """minio-provision-initial job completed successfully."""
        ns = minio_deployment.namespace

        # Verify CronJob exists and is suspended
        cj_result = subprocess.run(
            ["kubectl", "get", "cronjob", "minio-provision", "-n", ns,
             "-o", "jsonpath={.spec.suspend}"],
            capture_output=True, text=True, check=True,
        )
        assert cj_result.stdout.strip() == "true", (
            f"Expected CronJob to be suspended, got: {cj_result.stdout}"
        )

        # Verify the initial provisioning job succeeded
        job_result = subprocess.run(
            ["kubectl", "get", "job", "minio-provision-initial", "-n", ns,
             "-o", "jsonpath={.status.succeeded}"],
            capture_output=True, text=True, check=True,
        )
        assert job_result.stdout.strip() == "1", (
            f"minio-provision-initial not succeeded: {job_result.stdout}"
        )
```

- [ ] **Step 3: Update `test_minio_buckets_exist` — new bucket names**

Replace the two `assert` statements:

```python
            assert "hyperlane-validator-gorchain" in buckets, (
                f"gorchain bucket not found in: {buckets}"
            )
            assert "hyperlane-validator-solana" in buckets, (
                f"solana bucket not found in: {buckets}"
            )
```

With:

```python
            assert "hyperlane-validator-gorchain-primary" in buckets, (
                f"gorchain-primary bucket not found in: {buckets}"
            )
            assert "hyperlane-validator-solana-primary" in buckets, (
                f"solana-primary bucket not found in: {buckets}"
            )
```

- [ ] **Step 4: Add `test_minio_users_created` test**

Append to the `TestMinio` class:

```python
    def test_minio_users_created(self, minio_deployment: MinioInfo) -> None:
        """Per-validator IAM users were created by minio-provision-initial."""
        ns = minio_deployment.namespace
        deployment_id = minio_deployment.deployment_id
        pod_label = f"app={deployment_id}"

        pod_name = subprocess.run(
            ["kubectl", "get", "pods", "-n", ns, "-l", pod_label,
             "-o", "jsonpath={.items[0].metadata.name}"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        with PortForward(ns, f"pod/{pod_name}", 19000, 9000):
            result = run_mc_command(
                "mc", "admin", "user", "list", "test",
                minio=minio_deployment, host_port=19000,
            )
            assert result.returncode == 0, f"mc admin user list failed: {result.stderr}"
            users = result.stdout
            assert minio_deployment.gorchain_key_id in users, (
                f"gorchain-primary user '{minio_deployment.gorchain_key_id}' not found: {users}"
            )
            assert minio_deployment.solana_key_id in users, (
                f"solana-primary user '{minio_deployment.solana_key_id}' not found: {users}"
            )
```

- [ ] **Step 5: Add `test_minio_policies_attached` test**

```python
    def test_minio_policies_attached(self, minio_deployment: MinioInfo) -> None:
        """Bucket-scoped IAM policies are attached to each validator user."""
        ns = minio_deployment.namespace
        deployment_id = minio_deployment.deployment_id
        pod_label = f"app={deployment_id}"

        pod_name = subprocess.run(
            ["kubectl", "get", "pods", "-n", ns, "-l", pod_label,
             "-o", "jsonpath={.items[0].metadata.name}"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        with PortForward(ns, f"pod/{pod_name}", 19000, 9000):
            for key_id, label in [
                (minio_deployment.gorchain_key_id, "gorchain-primary"),
                (minio_deployment.solana_key_id, "solana-primary"),
            ]:
                result = run_mc_command(
                    "mc", "admin", "user", "info", "test", key_id,
                    minio=minio_deployment, host_port=19000,
                )
                assert result.returncode == 0, (
                    f"mc admin user info failed for {key_id}: {result.stderr}"
                )
                policy_name = f"policy-{label}"
                assert policy_name in result.stdout, (
                    f"Policy '{policy_name}' not attached to user '{key_id}': {result.stdout}"
                )
```

- [ ] **Step 6: Add `test_bucket_isolation` test**

```python
    def test_bucket_isolation(self, minio_deployment: MinioInfo) -> None:
        """Each validator's IAM user can write to its own bucket but not the other's."""
        ns = minio_deployment.namespace
        deployment_id = minio_deployment.deployment_id
        pod_label = f"app={deployment_id}"

        pod_name = subprocess.run(
            ["kubectl", "get", "pods", "-n", ns, "-l", pod_label,
             "-o", "jsonpath={.items[0].metadata.name}"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        with PortForward(ns, f"pod/{pod_name}", 19000, 9000):
            # gorchain-primary user can write to its own bucket
            result = run_mc_as(
                minio_deployment.gorchain_key_id, minio_deployment.gorchain_secret,
                "mc", "ls", "user/hyperlane-validator-gorchain-primary",
                host_port=19000,
            )
            assert result.returncode == 0, (
                f"gorchain user could not list own bucket: {result.stderr}"
            )

            # gorchain-primary user cannot access solana bucket
            result = run_mc_as(
                minio_deployment.gorchain_key_id, minio_deployment.gorchain_secret,
                "mc", "ls", "user/hyperlane-validator-solana-primary",
                host_port=19000,
            )
            assert result.returncode != 0, (
                "gorchain user should NOT have access to solana-primary bucket"
            )

            # solana-primary user can write to its own bucket
            result = run_mc_as(
                minio_deployment.solana_key_id, minio_deployment.solana_secret,
                "mc", "ls", "user/hyperlane-validator-solana-primary",
                host_port=19000,
            )
            assert result.returncode == 0, (
                f"solana user could not list own bucket: {result.stderr}"
            )
```

- [ ] **Step 7: Add `test_subsequent_validator_provisioning` test**

```python
    def test_subsequent_validator_provisioning(self, minio_deployment: MinioInfo) -> None:
        """Triggering the CronJob provisions a new validator label without redeploying MinIO.

        Simulates the operator workflow for adding a second validator per chain post-deployment.
        """
        import secrets as _secrets

        ns = minio_deployment.namespace
        deployment_id = minio_deployment.deployment_id
        pod_label = f"app={deployment_id}"

        # Generate credentials for a new validator label
        new_label = "gorchain-secondary"
        new_key_id = f"gc2-{_secrets.token_hex(6)}"
        new_secret = _secrets.token_hex(20)
        new_bucket = f"hyperlane-validator-{new_label}"

        # Patch the minio-validator-secrets Secret to add the new validator
        # (In prod this is done via `kubectl edit secret` or Ansible before triggering the CronJob)
        subprocess.run(
            ["kubectl", "patch", "secret", "minio-validator-secrets", "-n", ns,
             "--type=json",
             "-p", (
                 f'[{{"op":"replace","path":"/data/MINIO_USERS",'
                 f'"value":"{__import__("base64").b64encode(b"gorchain-primary,solana-primary,gorchain-secondary").decode()}"}},'
                 f'{{"op":"add","path":"/data/GORCHAIN_SECONDARY_KEY_ID",'
                 f'"value":"{__import__("base64").b64encode(new_key_id.encode()).decode()}"}},'
                 f'{{"op":"add","path":"/data/GORCHAIN_SECONDARY_SECRET",'
                 f'"value":"{__import__("base64").b64encode(new_secret.encode()).decode()}"}}]'
             )],
            capture_output=True, text=True, check=True,
        )

        # Trigger the CronJob
        additional_job = "minio-provision-secondary-test"
        subprocess.run(
            ["kubectl", "create", "job", additional_job,
             "--from=cronjob/minio-provision", "-n", ns],
            capture_output=True, text=True, check=True,
        )

        try:
            from lib.common import wait_for_job_complete
            wait_for_job_complete(ns, additional_job, timeout=300)

            # Verify new bucket and user exist
            pod_name = subprocess.run(
                ["kubectl", "get", "pods", "-n", ns, "-l", pod_label,
                 "-o", "jsonpath={.items[0].metadata.name}"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()

            with PortForward(ns, f"pod/{pod_name}", 19000, 9000):
                result = run_mc_command(
                    "mc", "ls", "test/",
                    minio=minio_deployment, host_port=19000,
                )
                assert new_bucket in result.stdout, (
                    f"New bucket '{new_bucket}' not found after re-provision: {result.stdout}"
                )

                result = run_mc_command(
                    "mc", "admin", "user", "list", "test",
                    minio=minio_deployment, host_port=19000,
                )
                assert new_key_id in result.stdout, (
                    f"New user '{new_key_id}' not found after re-provision: {result.stdout}"
                )
        finally:
            # Clean up the test job regardless of outcome
            subprocess.run(
                ["kubectl", "delete", "job", additional_job, "-n", ns, "--ignore-not-found"],
                capture_output=True, text=True,
            )
```

- [ ] **Step 8: Run linter**

```bash
cd tests/e2e && python -m ruff check test_03_minio.py
```

Expected: no output.

- [ ] **Step 9: Commit**

```bash
git add tests/e2e/test_03_minio.py
git commit -m "tests(minio): update for CronJob provisioner, add user/policy/isolation/subsequent-validator tests"
```

---

## Spec Coverage Self-Check

| Spec requirement | Task |
|---|---|
| Remove minio-init job | Task 1 |
| `commands.py start()` creates CronJob with k8s SDK | Task 2 |
| CronJob uses suspended mode (`suspend: true`) | Task 2 |
| Initial trigger fires immediately as `minio-provision-initial` | Task 2 |
| `minio-validator-secrets` contains MINIO_USERS + per-label creds | Task 3 |
| `hyperlane-minio-secrets` contains root creds only | Task 3 |
| Validator specs use label-specific env vars for AWS creds | Task 4 |
| Bucket names include operator label suffix (`-primary`) | Task 4 |
| Relayer specs drop AWS credentials | Task 5 |
| `MinioInfo` has per-validator cred fields | Task 6 |
| `minio_deployment` fixture generates + exports per-label creds | Task 6 |
| `_recover_minio_credentials` reads from both secrets | Task 6 |
| `_deploy_validator` uses chain-specific cred env vars | Task 7 |
| `relayer_deployment` drops AWS creds | Task 7 |
| Tests verify CronJob exists and is suspended | Task 8 |
| Tests verify per-validator IAM users created | Task 8 |
| Tests verify bucket-scoped policies attached | Task 8 |
| Tests verify bucket isolation between validators | Task 8 |
| Test for subsequent-validator provisioning via CronJob | Task 8 |
