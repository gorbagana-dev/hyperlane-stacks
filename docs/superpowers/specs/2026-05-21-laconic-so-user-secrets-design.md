# laconic-so user secrets — design

**Status:** approved 2026-05-21
**Repo of change:** [stack-orchestrator](../../../../stack-orchestrator/) (consumer: hyperlane-stacks)

## Goal

Let laconic-so create user-declared k8s Secrets in each stack's own namespace at
`deploy_start` time, sourcing values from env vars or files. Remove the need for
operators (and the e2e test harness) to `kubectl create secret` out-of-band before
running `laconic-so deploy start`.

## Problem

Today SO's `secrets:` spec block is reference-only: the consumer (`cluster_info.py:733`)
mounts each declared Secret name via `env_from` with `optional=True`, but SO does
**not** create the Secret. The operator must run `kubectl create secret generic …`
before `deploy start`, in the right namespace.

Consequences:

1. **Test harness chicken-and-egg.** With `--perform-cluster-management` as the
   default and SO owning Kind cluster creation, no cluster exists until the first
   `deploy_start` runs. The current conftest's `kubectl create namespace …` then
   `kubectl create secret …` calls fail with `connection refused`.
2. **Multi-machine prod friction.** Every stack on every host needs an out-of-band
   secret-creation step in the Ansible playbook before `deploy_start`. Stacks are
   not self-sufficient from the spec alone.
3. **Inconsistent with `image-pull-secret:`.** SO already creates the dockerconfigjson
   Secret from `image-pull-secret.token-env` / `token-file` at `deploy_start`
   (`deployment_create.py:create_registry_secret`). User secrets should follow the
   same pattern.

## Design

### Schema

Extend the existing `secrets:` block. Each entry can be either:

- **List of key names** (legacy, unchanged) — reference-only, operator creates the
  Secret out-of-band. Mounted with `optional=True`.
- **Dict with `keys:` map** (new) — SO creates the Secret from declared sources.

```yaml
secrets:
  hyperlane-deployer-secrets:
    keys:
      DEPLOYER_KEYPAIR:           { env: DEPLOYER_KEYPAIR }
      HARDWARE_WALLET_PUBKEY:     { env: HARDWARE_WALLET_PUBKEY }
      IGP_ORACLE_PUBKEY:          { env: IGP_ORACLE_PUBKEY }
      GORCHAIN_VALIDATOR_ADDRESS: { env: GORCHAIN_VALIDATOR_ADDRESS }
      SOLANA_VALIDATOR_ADDRESS:   { env: SOLANA_VALIDATOR_ADDRESS }
```

### Source types

Each key declares exactly one source:

- `{ env: VAR_NAME }` — reads `os.environ[VAR_NAME]` at `deploy_start` time.
- `{ file: PATH }` — reads file contents (UTF-8). `PATH` supports `~` expansion.

If the env var is unset/empty, or the file does not exist / is unreadable, SO
errors out before namespace work with a message naming the secret + key + missing
source. No silent fallbacks. No literal-value form (use `env:` and set the literal
in the caller's environment).

### Lifecycle

In `K8sDeployer.up()`, after `_ensure_namespace()` and before pod/job creation,
SO calls a new `_create_user_secrets()` method. For each spec `secrets:` entry
of dict form:

1. Resolve each key's source value.
2. Build a `V1Secret` with `type: Opaque` and base64-encoded `data:` keyed by
   the variable name.
3. `create_namespaced_secret(namespace=…, body=…)`. On 409 → `replace_namespaced_secret`
   (matches the existing `image-pull-secret` and `generated-secrets` idempotency).

The downstream `env_from secret_ref` reference in `cluster_info.py:733` is unchanged.
`optional=True` stays — it's harmless when the Secret is guaranteed to exist, and
preserves legacy list-form semantics.

### Backward compatibility

The list form is unchanged. SO inspects the value: `list` → legacy reference-only;
`dict` with `keys:` → new create-from-sources path. Specs that use the list form
continue to require operator-created Secrets.

## Implementation surface (SO repo)

- `stack_orchestrator/deploy/k8s/deploy_k8s.py`
  - New `_create_user_secrets(self)` method (similar shape to `_create_ca_secret`).
  - Call it from `up()` between `_ensure_namespace()` and the existing pod-creation
    block.
- `stack_orchestrator/deploy/spec.py` — no change (`get_secrets()` already returns
  the dict; type discrimination happens in the new consumer).
- New unit tests covering: env source happy path, file source happy path, missing
  env var errors, missing file errors, idempotent re-apply (409 → replace),
  legacy list form untouched.

## Consumer impact (hyperlane-stacks)

### Test fixtures

Each `tests/e2e/fixtures/test-spec-*.yml`: convert `secrets:` from list to keyed-dict
form with `{ env: VAR }` sources.

`tests/e2e/conftest.py`: each `*_deployment` fixture drops its
`create_namespace(…)` + `create_*_secrets(…)` calls. Instead, before `deploy_start`,
it exports the relevant env vars (keypair values, minio creds, etc.) into
`os.environ`. SO reads them inside `up()`.

`tests/e2e/lib/keygen.py`: drop all `create_*_secrets()` helpers (~150 lines).

### Prod specs

Each `deployment/spec-*.yml`: same conversion, mostly `{ file: PATH }` sources
(keyfiles, minio creds placed by ops Ansible) plus a few `{ env: VAR }` for
non-sensitive identifiers.

### Cross-stack handoffs (minio → validator/relayer)

Today: minio uses `$generate:base64:N$` tokens, test code reads back, recreates
in downstream namespaces. New model (tests): generate minio creds in conftest at
session start, export as env vars, every spec (minio + validator + relayer)
references them via `{ env: MINIO_ROOT_USER }` etc. Minio's spec stops using
`$generate:*$` for these keys. Prod: ops Ansible places credentials in files on
each host; downstream specs reference via `{ file: … }`.

## Out of scope

- Hot-reload of secret values (re-run `deploy_start` to pick up changes).
- Literal-value source (always go through env or file).
- Cross-namespace secret reads (each stack owns its namespace's secrets).
- TLS/cert sources (Caddy handles those independently).

## Compatibility

- Legacy list form: preserved, unchanged behavior.
- Existing `image-pull-secret:`, `$generate:*$` tokens, `credentials-files:`:
  all unchanged.
- Spec key name: stays `secrets:`. No new top-level keys.
- Idempotency: 409 → replace, matching existing patterns.
