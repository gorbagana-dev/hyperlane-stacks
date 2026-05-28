# MinIO per-validator users and bucket-scoped IAM policies

**Date:** 2026-05-27
**Status:** Approved — PR2 of the MinIO migration. PR1 (external-services + Caddy routing) landed as #17.

---

## 1. Goal

Replace the shared MinIO root credentials used by all consumers with per-validator IAM users, each scoped to exactly one bucket. A compromised validator credential cannot reach another validator's checkpoint storage. The provisioning mechanism supports adding validators post-deployment without redeploying MinIO.

---

## 2. Background

PR1 routed validators and the relayer to MinIO via `external-services:` selector mode (dev) and public DNS with Caddy TLS (prod). All consumers shared a single `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` sourced from the MinIO root user. Access control is the next layer.

### Relayer uses anonymous S3 access

The Hyperlane agent (`hyperlane-base/src/types/s3_storage.rs`) has two distinct S3 clients:

- **`authenticated_client`** — uses `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`; used by **validators** to write checkpoint files.
- **Anonymous client** (`.no_credentials()`) — used by the **relayer** to read checkpoint files.

Checkpoint files are signed attestations of the mailbox Merkle root — public data by design in the Hyperlane protocol. Validator buckets remain publicly readable (`mc anonymous set download`), so the relayer needs no credentials. The relayer's `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` keys are removed from its spec in this PR.

---

## 3. Architecture

```
gorchain-primary validator
  AWS_ACCESS_KEY_ID  = GORCHAIN_PRIMARY_KEY_ID  (from hyperlane-validator-gorchain-secrets)
  AWS_SECRET_ACCESS_KEY = GORCHAIN_PRIMARY_SECRET
  → IAM policy: s3:* on hyperlane-validator-gorchain-primary only
  → writes checkpoints to: hyperlane-validator-gorchain-primary

solana-primary validator
  AWS_ACCESS_KEY_ID  = SOLANA_PRIMARY_KEY_ID    (from hyperlane-validator-solana-secrets)
  AWS_SECRET_ACCESS_KEY = SOLANA_PRIMARY_SECRET
  → IAM policy: s3:* on hyperlane-validator-solana-primary only
  → writes checkpoints to: hyperlane-validator-solana-primary

relayer
  → no AWS credentials
  → reads from both buckets via anonymous S3 client (public read)

minio-provision CronJob
  → mounts hyperlane-minio-secrets  (root admin access)
  → mounts minio-validator-secrets  (MINIO_USERS list + all per-validator creds)
  → creates buckets, IAM policies, users idempotently
```

---

## 4. Key decisions

### 4.1 Operator-assigned labels

Each validator instance has an operator-assigned short label (e.g. `gorchain-primary`, `gorchain-backup`, `solana-primary`). Labels are stable identifiers independent of key material or ordering.

### 4.2 Naming convention

Everything is derived from the label using a fixed convention. No extra configuration needed per user.

| Label | Bucket | Key ID env var | Secret env var |
|---|---|---|---|
| `gorchain-primary` | `hyperlane-validator-gorchain-primary` | `GORCHAIN_PRIMARY_KEY_ID` | `GORCHAIN_PRIMARY_SECRET` |
| `gorchain-backup` | `hyperlane-validator-gorchain-backup` | `GORCHAIN_BACKUP_KEY_ID` | `GORCHAIN_BACKUP_SECRET` |
| `solana-primary` | `hyperlane-validator-solana-primary` | `SOLANA_PRIMARY_KEY_ID` | `SOLANA_PRIMARY_SECRET` |

Transformation: label → uppercase, hyphens → underscores, append `_KEY_ID` / `_SECRET`.

### 4.3 Two secrets in the minio namespace

Root credentials are separated from validator credentials:

| Secret | Contents | Who mounts it |
|---|---|---|
| `hyperlane-minio-secrets` | `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD` | MinIO container, CronJob |
| `minio-validator-secrets` | `MINIO_USERS` (comma-separated labels), all `<LABEL>_KEY_ID` / `<LABEL>_SECRET` pairs | CronJob only |

Validator pods mount only their own namespace-local secret (created by SO from the same env vars the operator exports). Validators never see root credentials; MinIO never sees validator credentials.

### 4.4 Suspended CronJob for idempotent provisioning

A CronJob `minio-provision` with `suspend: true` is created in `laconic-hyperlane-minio` by `commands.py start()` using the k8s Python SDK. It never auto-runs. Triggered explicitly:

- **At deploy time**: `commands.py start()` creates the CronJob then immediately triggers an initial run via `BatchV1Api.create_namespaced_job()`.
- **When adding a validator**: operator patches `minio-validator-secrets` (add new `<LABEL>_KEY_ID` / `<LABEL>_SECRET`) and the `MINIO_USERS` value, then triggers: `kubectl create job --from=cronjob/minio-provision minio-provision-<timestamp> -n laconic-hyperlane-minio`.

The script is fully idempotent: `mc mb --ignore-existing`, `mc admin user add` and `mc admin policy attach` are no-ops if the resource already exists.

### 4.5 CronJob uses minio/mc image (no custom image)

The provisioning script is pure POSIX shell. The `minio/mc` image is sufficient — no Python, no yq. The user list in `MINIO_USERS` is a comma-separated string iterated with `tr ',' '\n'`.

### 4.6 commands.py uses the k8s Python SDK

`laconic-so` is distributed as a shiv that includes `kubernetes` in its site-packages. `commands.py` can import and use `client.BatchV1Api()` directly. No subprocess `kubectl` calls.

### 4.7 Init job replaced by CronJob

The existing `docker-compose-hyperlane-minio-init.yml` is deleted. The CronJob handles all setup (bucket creation, anonymous read, user creation, policy attachment) in one idempotent script. The stack's `stack.yml` `jobs:` entry is also removed.

---

## 5. Provisioning script logic

```
alias MinIO as "local" using root credentials

for each label in split($MINIO_USERS, ","):
  bucket    = "hyperlane-validator-" + label
  env_prefix = uppercase(replace(label, "-", "_"))
  key_id    = env(env_prefix + "_KEY_ID")
  secret    = env(env_prefix + "_SECRET")

  mc mb --ignore-existing local/<bucket>
  mc anonymous set download local/<bucket>

  write /tmp/<label>-policy.json:
    { s3:* on arn:aws:s3:::<bucket> and arn:aws:s3:::<bucket>/* }

  mc admin policy create local <label>-policy /tmp/<label>-policy.json  (idempotent)
  mc admin user add local <key_id> <secret>                              (idempotent)
  mc admin policy attach local <label>-policy --user <key_id>           (idempotent)
```

---

## 6. Files affected

### Stack definition

- `stack_orchestrator/data/stacks/hyperlane-minio/stack.yml`
  - Remove `jobs:` entry for `hyperlane-minio-init`
- `stack_orchestrator/data/stacks/hyperlane-minio/deploy/commands.py`
  - New file. `start()` hook: creates CronJob via k8s SDK, triggers initial run.
- `stack_orchestrator/data/compose-jobs/docker-compose-hyperlane-minio-init.yml`
  - **Deleted** (replaced by CronJob)

### MinIO specs

- `tests/e2e/fixtures/test-spec-minio.yml`
  - `hyperlane-minio-secrets`: keep root creds only
  - Add `minio-validator-secrets` block with `MINIO_USERS` + per-label credentials
- `deployment/spec-minio.yml`
  - Same split; update operator comments for new env vars

### Validator specs

- `tests/e2e/fixtures/test-spec-validator-gorchain.yml`
  - `AWS_ACCESS_KEY_ID: { env: GORCHAIN_PRIMARY_KEY_ID }`
  - `AWS_SECRET_ACCESS_KEY: { env: GORCHAIN_PRIMARY_SECRET }`
- `tests/e2e/fixtures/test-spec-validator-solana.yml`
  - `AWS_ACCESS_KEY_ID: { env: SOLANA_PRIMARY_KEY_ID }`
  - `AWS_SECRET_ACCESS_KEY: { env: SOLANA_PRIMARY_SECRET }`
- `deployment/spec-validator-gorchain.yml` — same changes
- `deployment/spec-validator-solana.yml` — same changes

### Relayer specs

- `tests/e2e/fixtures/test-spec-relayer.yml`
  - Remove `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` from `secrets:` block
  - Add comment: "relayer reads checkpoints via anonymous S3 client; no credentials needed"
- `deployment/spec-relayer.yml` — same change

### Test code

- `tests/e2e/conftest.py`
  - `MinioInfo` dataclass: add `gorchain_key_id`, `gorchain_secret`, `solana_key_id`, `solana_secret` fields
  - `minio_deployment` fixture: generate per-validator credentials, export as env vars alongside root credentials before `deploy_start`
  - `_recover_minio_credentials`: recover all credential fields from `minio-validator-secrets` for `--skip-minio-deploy` reuse; re-export to `os.environ`
  - `_deploy_validator`: remove `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` from `os.environ.update` — they come from chain-specific env vars already in the environment
  - `relayer_deployment`: remove `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` from `os.environ.update`
- `tests/e2e/test_03_minio.py`
  - Add `run_mc_as(key_id, secret, ...)` helper for per-validator mc commands
  - `test_minio_users_created` — `mc admin user list` shows both initial users
  - `test_minio_policies_attached` — each user has its bucket-scoped policy
  - `test_bucket_isolation` — gorchain-primary credentials get `Access Denied` writing to solana-primary bucket
  - `test_subsequent_validator_provisioning` — patches `minio-validator-secrets` to add `gorchain-extra`, triggers CronJob, verifies new bucket and user exist

### Compose

- `stack_orchestrator/data/compose/docker-compose-hyperlane-relayer.yml`
  - Update comment: remove reference to `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`; add note on anonymous S3

---

## 7. Verification

PR2 is green when:

- `test_03_minio.py` all pass including the four new tests
- `test_04_validator.py` passes — validators connect with per-label credentials
- `test_05_relayer.py` passes — relayer reads checkpoints without credentials
- `mc admin user list` shows two non-root users after initial deploy
- `mc admin policy list` shows two bucket-scoped policies
- No validator pod's `AWS_ACCESS_KEY_ID` contains the root user value
- No relayer pod has `AWS_ACCESS_KEY_ID` in its environment

---

## 8. Out of scope

- Ansible playbook for prod operator workflow (separate PR)
- `laconic-so deployment update-envs` integration for adding validators (separate PR)
- File-based credential sources for prod specs (`{ file: … }`)
- Multiple validators per chain sharing a bucket (not required by current Hyperlane agent design)

---

## 9. Known follow-ups (post-merge, not yet scheduled)

Surfaced during the 2026-05-28 prod-ops design review of the merged impl. To
be triaged into their own PR before this is depended on for prod.

1. **`MINIO_USERS` source-of-truth for GitOps.** Current shape forces operator
   to edit a comma-separated env-var value directly in the spec. For the
   GitOps "operator edits a structured file" model, the validator list should
   live in `deployment/bridges/<bridge>/operator/validators.yaml` and ansible
   should template the env-var value from it. CronJob runtime contract stays
   the same.
2. **Secret rotation has no path.** `mc admin user add … || true` and
   `mc admin policy create … || true` mask all errors, so re-running with a
   rotated `<LABEL>_SECRET` silently keeps the old MinIO secret. Replace with
   `mc admin user info … || mc admin user add …` and add a `ROTATE=<label>`
   mode that does `remove` + `add`.
3. **No end-of-run verification.** Add `mc admin user info local "$key_id" |
   grep -q "policy-${label}"` per label to catch silent failures from the
   error-masking pattern.
