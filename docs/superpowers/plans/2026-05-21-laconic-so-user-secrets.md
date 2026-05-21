# laconic-so user secrets — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Have laconic-so create user-declared k8s Secrets in each stack's
namespace at `deploy_start`, sourcing values from env vars or files. Remove
out-of-band `kubectl create secret` calls from the e2e harness.

**Architecture:** Extend SO's existing `secrets:` block: in addition to the
legacy list form (reference-only, `optional=True`), accept a dict form with a
`keys:` map declaring `{ env: VAR }` or `{ file: PATH }` sources per key. SO
creates the Secret inside `up()` between `_ensure_namespace()` and pod creation,
with 409→replace idempotency matching `image-pull-secret`. Consumers
(hyperlane-stacks test fixtures + prod specs + conftest) convert to the keyed
form and stop doing kubectl prep.

**Tech Stack:** Python (kubernetes client), pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-05-21-laconic-so-user-secrets-design.md`

**Repos touched:**
- SO: `/home/dev/git_puller/repos/stack-orchestrator/`
- Consumer: `/home/dev/git_puller/repos/hyperlane-stacks/` (this repo)

---

## File structure

### SO repo (changes)

- Modify: `stack_orchestrator/deploy/k8s/deploy_k8s.py` — add
  `_create_user_secrets()` method, wire into `up()` after `_ensure_namespace()`.
  (`spec.get_secrets()` already returns the dict; legacy/new discrimination
  happens inline.)
- Create: `tests/unit/test_user_secrets.py` — stdlib-`unittest` tests with k8s
  client mocked. Covers env source, file source, missing env error, missing file
  error, idempotency (409→replace), legacy list form is skipped (no creation).

### hyperlane-stacks (consumer changes)

- Modify (8 test specs): `tests/e2e/fixtures/test-spec-{deployer,warp-deployer,validator-gorchain,validator-solana,relayer,gas-oracle,monitoring,minio}.yml`
- Modify (8 prod specs): `deployment/spec-{deployer,warp-deployer,validator-gorchain,validator-solana,relayer,gas-oracle,monitoring,minio}.yml`
- Modify: `tests/e2e/conftest.py` — drop `create_namespace` + `create_*_secrets`
  calls; add per-stack env-var exports before each `deploy_start`.
- Modify: `tests/e2e/lib/keygen.py` — drop `create_*_secrets` helpers (~150
  lines).
- Modify: `tests/e2e/lib/cluster.py` — drop `create_namespace` (unused after
  conftest update).
- Modify: `docs/architecture-decisions.md` — replace "operator runs kubectl
  create secret" wording in the secrets section with the new SO-managed model.

---

## Task 1: SO — failing tests for env + file source resolution

**Repo:** `/home/dev/git_puller/repos/stack-orchestrator`
**Files:**
- Create: `tests/unit/__init__.py` (empty)
- Create: `tests/unit/test_user_secrets.py`

- [ ] **Step 1: Write failing test file**

```python
# tests/unit/test_user_secrets.py
"""Unit tests for K8sDeployer._create_user_secrets()."""
import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from kubernetes.client.exceptions import ApiException


class TestCreateUserSecrets(unittest.TestCase):
    def setUp(self):
        # Import lazily to keep this file importable without kubeconfig
        from stack_orchestrator.deploy.k8s.deploy_k8s import K8sDeployer

        self.deployer = K8sDeployer.__new__(K8sDeployer)
        self.deployer.k8s_namespace = "test-ns"
        self.deployer.core_api = MagicMock()
        self.deployer.cluster_info = MagicMock()

    def _set_spec_secrets(self, secrets):
        self.deployer.cluster_info.spec.get_secrets.return_value = secrets

    def test_env_source_creates_secret(self):
        self._set_spec_secrets({
            "app-secrets": {"keys": {"FOO": {"env": "MY_FOO"}}},
        })
        with patch.dict(os.environ, {"MY_FOO": "bar"}):
            self.deployer._create_user_secrets()
        self.deployer.core_api.create_namespaced_secret.assert_called_once()
        _, kwargs = self.deployer.core_api.create_namespaced_secret.call_args
        body = kwargs.get("body") or self.deployer.core_api.create_namespaced_secret.call_args.args[1]
        self.assertEqual(body.metadata.name, "app-secrets")
        self.assertEqual(base64.b64decode(body.data["FOO"]).decode(), "bar")

    def test_file_source_creates_secret(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("file-value")
            path = f.name
        try:
            self._set_spec_secrets({
                "app-secrets": {"keys": {"K": {"file": path}}},
            })
            self.deployer._create_user_secrets()
            _, kwargs = self.deployer.core_api.create_namespaced_secret.call_args
            body = kwargs.get("body") or self.deployer.core_api.create_namespaced_secret.call_args.args[1]
            self.assertEqual(base64.b64decode(body.data["K"]).decode(), "file-value")
        finally:
            Path(path).unlink()

    def test_missing_env_raises(self):
        self._set_spec_secrets({
            "app-secrets": {"keys": {"FOO": {"env": "UNSET_VAR"}}},
        })
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(Exception) as ctx:
                self.deployer._create_user_secrets()
            self.assertIn("UNSET_VAR", str(ctx.exception))
            self.assertIn("app-secrets", str(ctx.exception))

    def test_missing_file_raises(self):
        self._set_spec_secrets({
            "app-secrets": {"keys": {"K": {"file": "/nonexistent/path"}}},
        })
        with self.assertRaises(Exception) as ctx:
            self.deployer._create_user_secrets()
        self.assertIn("/nonexistent/path", str(ctx.exception))

    def test_legacy_list_form_skipped(self):
        self._set_spec_secrets({"app-secrets": ["KEY1", "KEY2"]})
        self.deployer._create_user_secrets()
        self.deployer.core_api.create_namespaced_secret.assert_not_called()

    def test_idempotent_409_replaces(self):
        self._set_spec_secrets({
            "app-secrets": {"keys": {"FOO": {"env": "MY_FOO"}}},
        })
        self.deployer.core_api.create_namespaced_secret.side_effect = ApiException(
            status=409, reason="AlreadyExists"
        )
        with patch.dict(os.environ, {"MY_FOO": "bar"}):
            self.deployer._create_user_secrets()
        self.deployer.core_api.replace_namespaced_secret.assert_called_once()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run, verify failure**

```
cd /home/dev/git_puller/repos/stack-orchestrator
python -m unittest tests.unit.test_user_secrets -v
```

Expected: FAIL — `AttributeError: 'K8sDeployer' object has no attribute '_create_user_secrets'`.

- [ ] **Step 3: Commit the failing tests**

```
git add tests/unit/__init__.py tests/unit/test_user_secrets.py
git commit -m "test(secrets): failing tests for user-secret creation from env/file sources"
```

---

## Task 2: SO — implement `_create_user_secrets` + wire into `up()`

**Repo:** `/home/dev/git_puller/repos/stack-orchestrator`
**Files:**
- Modify: `stack_orchestrator/deploy/k8s/deploy_k8s.py`

- [ ] **Step 1: Add the new method to `K8sDeployer`**

Add this method near `_create_ca_secret` (around line 625):

```python
def _create_user_secrets(self):
    """Create k8s Secrets declared with sources in spec.secrets.

    Spec form (legacy list form is ignored — operator owns those):
        secrets:
          my-secret:
            keys:
              KEY1: { env: ENV_VAR_NAME }
              KEY2: { file: /path/to/file }

    Reads values from os.environ or files, creates one V1Secret per entry
    in the deployer's namespace. 409 -> replace, matching the existing
    image-pull-secret / generated-secrets idempotency pattern.
    """
    import base64
    from kubernetes import client
    from kubernetes.client.exceptions import ApiException

    secrets_spec = self.cluster_info.spec.get_secrets()
    for secret_name, entry in secrets_spec.items():
        if not isinstance(entry, dict):
            continue  # legacy list form: reference-only, operator-managed
        keys = entry.get("keys") or {}
        if not keys:
            continue

        data = {}
        for key_name, source in keys.items():
            if not isinstance(source, dict):
                raise DeployerException(
                    f"secrets.{secret_name}.keys.{key_name}: expected mapping "
                    f"with 'env' or 'file', got {type(source).__name__}"
                )
            if "env" in source:
                env_var = source["env"]
                value = os.environ.get(env_var)
                if value is None or value == "":
                    raise DeployerException(
                        f"secrets.{secret_name}.keys.{key_name}: "
                        f"environment variable '{env_var}' is unset or empty"
                    )
            elif "file" in source:
                path = Path(source["file"]).expanduser()
                if not path.is_file():
                    raise DeployerException(
                        f"secrets.{secret_name}.keys.{key_name}: "
                        f"file '{source['file']}' does not exist"
                    )
                try:
                    value = path.read_text()
                except OSError as e:
                    raise DeployerException(
                        f"secrets.{secret_name}.keys.{key_name}: "
                        f"cannot read '{source['file']}': {e}"
                    )
            else:
                raise DeployerException(
                    f"secrets.{secret_name}.keys.{key_name}: source must "
                    f"declare 'env' or 'file'"
                )
            data[key_name] = base64.b64encode(value.encode()).decode()

        body = client.V1Secret(
            metadata=client.V1ObjectMeta(name=secret_name),
            type="Opaque",
            data=data,
        )
        try:
            self.core_api.create_namespaced_secret(
                namespace=self.k8s_namespace, body=body
            )
            print(f"Created user Secret '{secret_name}' in {self.k8s_namespace}")
        except ApiException as e:
            if e.status == 409:
                self.core_api.replace_namespaced_secret(
                    name=secret_name,
                    namespace=self.k8s_namespace,
                    body=body,
                )
                print(f"Updated user Secret '{secret_name}' in {self.k8s_namespace}")
            else:
                raise
```

Top-of-file imports — verify `os`, `Path`, `DeployerException` are already imported (they are). If not, add them.

- [ ] **Step 2: Wire into `up()`**

In `up()` (around line 1012), after the existing `self._setup_cluster()` line and before the `# Create registry secret if configured` block, add:

```python
        self._create_user_secrets()
```

Order: `_setup_cluster` (which calls `_ensure_namespace`) → `_create_user_secrets` → `create_registry_secret` → pod/job creation.

- [ ] **Step 3: Run tests, verify pass**

```
cd /home/dev/git_puller/repos/stack-orchestrator
python -m unittest tests.unit.test_user_secrets -v
```

Expected: all 6 tests pass.

- [ ] **Step 4: Lint**

```
cd /home/dev/git_puller/repos/stack-orchestrator
python -m py_compile stack_orchestrator/deploy/k8s/deploy_k8s.py
```

Expected: no output (success). If the repo has ruff/flake8 configured, run that too.

- [ ] **Step 5: Commit**

```
git add stack_orchestrator/deploy/k8s/deploy_k8s.py
git commit -m "feat(secrets): create user-declared Secrets from env/file sources

Extends the spec.secrets block to accept a keyed-dict form where each
key declares an env-var or file source. SO resolves the values at
deploy_start (inside up(), after _ensure_namespace, before pod/job
creation) and creates one Opaque k8s Secret per spec entry in the
stack's own namespace.

The legacy list form is preserved unchanged: reference-only,
optional=True, operator-managed.

Source design: docs/superpowers/specs/2026-05-21-laconic-so-user-secrets-design.md"
```

---

## Task 3: Consumer — convert 8 test fixture specs to keyed form

**Repo:** `/home/dev/git_puller/repos/hyperlane-stacks`
**Mechanical:** spec reviewer only (per [memory](memory/feedback_review_cadence.md)).

**Files (each gets its `secrets:` block converted):**
- `tests/e2e/fixtures/test-spec-deployer.yml`
- `tests/e2e/fixtures/test-spec-warp-deployer.yml`
- `tests/e2e/fixtures/test-spec-validator-gorchain.yml`
- `tests/e2e/fixtures/test-spec-validator-solana.yml`
- `tests/e2e/fixtures/test-spec-relayer.yml`
- `tests/e2e/fixtures/test-spec-gas-oracle.yml`
- `tests/e2e/fixtures/test-spec-monitoring.yml`
- `tests/e2e/fixtures/test-spec-minio.yml`

- [ ] **Step 1: Convert each file**

Example for `test-spec-deployer.yml`:

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

For every file: convert each key in the existing list to a single-line
`{ env: <SAME_NAME> }` entry under a new `keys:` map. Use the env var name
identical to the secret key name. No source-type variations in test specs —
all entries use `env:`.

- [ ] **Step 2: Verify YAML loads**

```
cd /home/dev/git_puller/repos/hyperlane-stacks
python -c "import yaml; [yaml.safe_load(open(f)) for f in __import__('glob').glob('tests/e2e/fixtures/test-spec-*.yml')]"
```

Expected: no output (success).

- [ ] **Step 3: Commit**

```
git add tests/e2e/fixtures/test-spec-*.yml
git commit -m "test fixtures: convert secrets blocks to keyed env-source form

SO now creates user secrets from spec sources at deploy_start. Each
key declares { env: VAR } so SO reads from os.environ at start time.
"
```

---

## Task 4: Consumer — conftest env-var exports + drop kubectl prep

**Repo:** `/home/dev/git_puller/repos/hyperlane-stacks`
**Files:**
- Modify: `tests/e2e/conftest.py`

This task replaces every `create_namespace(...)` + `create_*_secrets(...)`
call before a `deploy_start` with an `os.environ.update({...})` block. The
*_deployment fixtures stay; their content shrinks.

- [ ] **Step 1: Drop the lib.keygen imports of create_*_secrets**

In `conftest.py` around line 73-80, remove from the import list:

```python
    create_deployer_secrets,
    create_gas_oracle_secrets,
    create_minio_secrets,
    create_monitoring_secrets,
    create_relayer_secrets,
    create_validator_secrets,
    create_warp_deployer_secrets,
```

Also remove the `create_namespace` import from `lib.cluster` (line ~24) if
it's only used for the deletions below.

- [ ] **Step 2: Replace each fixture's pre-deploy_start block**

For each of these fixtures, replace the namespace-create + secret-create
calls with an `os.environ.update(...)` block placed immediately before
`deploy_start(deploy_info.deploy_dir)`.

**`minio_deployment` (~line 519-525):**

Remove:
```python
log.info("Creating namespace %s...", namespace)
create_namespace(namespace)

log.info("Creating minio secrets in namespace %s...", namespace)
create_minio_secrets(namespace, minio_user, minio_password)
```

Add (just before `deploy_start(...)`):
```python
os.environ["MINIO_ROOT_USER"] = minio_user
os.environ["MINIO_ROOT_PASSWORD"] = minio_password
```

**`deployer_deployment` (~line 592-599):**

Remove the `create_namespace` and `create_deployer_secrets` calls.

Add just before `deploy_start(...)`:
```python
os.environ.update({
    "DEPLOYER_KEYPAIR":           keypairs.deployer_keypair,
    "HARDWARE_WALLET_PUBKEY":     keypairs.hardware_wallet_pubkey,
    "IGP_ORACLE_PUBKEY":          keypairs.igp_oracle_pubkey,
    "GORCHAIN_VALIDATOR_ADDRESS": keypairs.gorchain_validator_address,
    "SOLANA_VALIDATOR_ADDRESS":   keypairs.solana_validator_address,
})
```

**`warp_deployer_deployment` (~line 744-747):**

Remove `create_namespace` and `create_warp_deployer_secrets` calls.

Add:
```python
os.environ.update({
    "DEPLOYER_KEYPAIR":       keypairs.deployer_keypair,
    "HARDWARE_WALLET_PUBKEY": keypairs.hardware_wallet_pubkey,
})
```

**`validator_gorchain` / `validator_solana` (~line 900-908):**

Remove the `create_namespace` and `create_validator_secrets` calls.

Add (using each fixture's `chain_signer_key` variable):
```python
os.environ.update({
    "PRIVY_APP_ID":           "test-app-id",
    "PRIVY_APP_SECRET":       "test-app-secret",
    "AWS_ACCESS_KEY_ID":      minio.user,
    "AWS_SECRET_ACCESS_KEY":  minio.password,
    "HYP_DEFAULTSIGNER_KEY":  chain_signer_key,
})
```

**`relayer_deployment` (~line 1054-1064):**

Remove `create_namespace` and `create_relayer_secrets` calls.

Add:
```python
os.environ.update({
    "HYP_CHAINS_GORCHAIN_SIGNER_KEY": gorchain_signer_key,
    "HYP_CHAINS_SOLANA_SIGNER_KEY":   solana_signer_key,
    "AWS_ACCESS_KEY_ID":              minio_deployment.user,
    "AWS_SECRET_ACCESS_KEY":          minio_deployment.password,
    "RELAYER_KEYPAIR_JSON":           relayer_keypair_json,
})
```

**`gas_oracle_deployment` (~line 1214-1218):**

Remove `create_namespace` and `create_gas_oracle_secrets` calls.

Add:
```python
os.environ.update({
    "PRIVY_APP_ID":           "test-app-id",
    "PRIVY_APP_SECRET":       "test-app-secret",
    "PRIVY_ORACLE_WALLET_ID": ORACLE_WALLET_ID,
})
```

**`monitoring_deployment` (~line 1407-1411):**

Remove `create_namespace` and `create_monitoring_secrets` calls.

Add:
```python
os.environ["GF_SECURITY_ADMIN_PASSWORD"] = GRAFANA_ADMIN_PASSWORD
```

- [ ] **Step 3: Verify no stale references**

```
cd /home/dev/git_puller/repos/hyperlane-stacks
grep -n "create_namespace\|create_.*_secrets" tests/e2e/conftest.py
```

Expected: no matches (or only matches inside a comment).

- [ ] **Step 4: Lint**

```
cd /home/dev/git_puller/repos/hyperlane-stacks
ruff check tests/e2e/conftest.py
```

Expected: no errors.

- [ ] **Step 5: Commit**

```
git add tests/e2e/conftest.py
git commit -m "tests: stop creating namespaces and secrets out-of-band

laconic-so now creates user Secrets in each stack's namespace at
deploy_start (and ensures the namespace itself). Each fixture exports
the source env vars before calling deploy_start; SO reads them and
materialises the Secret. Drops the chicken-and-egg where kubectl
prep ran before any cluster existed."
```

---

## Task 5: Consumer — drop create_*_secrets helpers from keygen.py

**Repo:** `/home/dev/git_puller/repos/hyperlane-stacks`
**Mechanical:** spec reviewer only.
**Files:**
- Modify: `tests/e2e/lib/keygen.py`

- [ ] **Step 1: Delete all `create_*_secrets` functions**

Remove these functions and their `run_cmd`/`log_info` calls:
- `create_deployer_secrets`
- `create_minio_secrets`
- `create_validator_secrets`
- `create_relayer_secrets`
- `create_warp_deployer_secrets`
- `create_gas_oracle_secrets` (if present)
- `create_monitoring_secrets` (if present)

Run `grep -n "^def create_" tests/e2e/lib/keygen.py` to find their exact line
ranges before deleting.

If any kubectl-only import (e.g. `run_cmd`) becomes unused after deletion,
remove it too.

- [ ] **Step 2: Verify no callers**

```
cd /home/dev/git_puller/repos/hyperlane-stacks
grep -rn "create_deployer_secrets\|create_minio_secrets\|create_validator_secrets\|create_relayer_secrets\|create_warp_deployer_secrets\|create_gas_oracle_secrets\|create_monitoring_secrets" tests/
```

Expected: no matches.

- [ ] **Step 3: Lint**

```
ruff check tests/e2e/lib/keygen.py
```

- [ ] **Step 4: Commit**

```
git add tests/e2e/lib/keygen.py
git commit -m "tests: drop create_*_secrets helpers, superseded by SO secret sources"
```

---

## Task 6: Consumer — drop `create_namespace` from lib/cluster.py if unused

**Repo:** `/home/dev/git_puller/repos/hyperlane-stacks`
**Mechanical:** spec reviewer only.
**Files:**
- Modify: `tests/e2e/lib/cluster.py`

- [ ] **Step 1: Check for callers**

```
grep -rn "create_namespace" tests/ stack_orchestrator/
```

If no callers remain after Task 4, delete the `create_namespace` function
from `lib/cluster.py`. If still used somewhere (e.g. a CI helper), leave it.

- [ ] **Step 2: Lint**

```
ruff check tests/e2e/lib/cluster.py
```

- [ ] **Step 3: Commit (if changes)**

```
git add tests/e2e/lib/cluster.py
git commit -m "tests: drop unused create_namespace helper"
```

---

## Task 7: Consumer — convert 8 prod specs to keyed form

**Repo:** `/home/dev/git_puller/repos/hyperlane-stacks`
**Mechanical:** spec reviewer only.

**Files:**
- `deployment/spec-deployer.yml`
- `deployment/spec-warp-deployer.yml`
- `deployment/spec-validator-gorchain.yml`
- `deployment/spec-validator-solana.yml`
- `deployment/spec-relayer.yml`
- `deployment/spec-gas-oracle.yml`
- `deployment/spec-monitoring.yml`
- `deployment/spec-minio.yml`

- [ ] **Step 1: Convert each prod spec**

Use `file:` sources for sensitive material (keyfiles, AWS creds) and `env:`
for IDs/identifiers. Document the expected file paths in the YAML comments
that already live above each `secrets:` block — update those to reflect the
new model (no more `kubectl create secret` instructions; instead, list which
files / env vars must exist on the host).

Example for `spec-deployer.yml`:

```yaml
# Before deploying, place these on the deploy host:
#   ~/secrets/hyperlane/deployer-keypair.json (Solana keypair JSON array)
# and export these env vars:
#   HARDWARE_WALLET_PUBKEY, IGP_ORACLE_PUBKEY,
#   GORCHAIN_VALIDATOR_ADDRESS, SOLANA_VALIDATOR_ADDRESS
secrets:
  hyperlane-deployer-secrets:
    keys:
      DEPLOYER_KEYPAIR:           { file: ~/secrets/hyperlane/deployer-keypair.json }
      HARDWARE_WALLET_PUBKEY:     { env: HARDWARE_WALLET_PUBKEY }
      IGP_ORACLE_PUBKEY:          { env: IGP_ORACLE_PUBKEY }
      GORCHAIN_VALIDATOR_ADDRESS: { env: GORCHAIN_VALIDATOR_ADDRESS }
      SOLANA_VALIDATOR_ADDRESS:   { env: SOLANA_VALIDATOR_ADDRESS }
```

Default source choices (use these unless a spec has a documented reason to
differ):

| Spec | Key | Source |
|---|---|---|
| deployer | DEPLOYER_KEYPAIR | file: `~/secrets/hyperlane/deployer-keypair.json` |
| deployer | (other 4) | env |
| warp-deployer | DEPLOYER_KEYPAIR | file: `~/secrets/hyperlane/deployer-keypair.json` |
| warp-deployer | HARDWARE_WALLET_PUBKEY | env |
| validator-gorchain | HYP_DEFAULTSIGNER_KEY | file: `~/secrets/hyperlane/validator-gorchain.key` |
| validator-gorchain | (other 4) | env |
| validator-solana | HYP_DEFAULTSIGNER_KEY | file: `~/secrets/hyperlane/validator-solana.key` |
| validator-solana | (other 4) | env |
| relayer | HYP_CHAINS_*_SIGNER_KEY | file: `~/secrets/hyperlane/relayer-{chain}.key` |
| relayer | RELAYER_KEYPAIR_JSON | file: `~/secrets/hyperlane/relayer-fee-claim.json` |
| relayer | AWS_*_KEY | env (operator copies from minio host) |
| gas-oracle | PRIVY_* | env |
| monitoring | GF_SECURITY_ADMIN_PASSWORD | env |
| minio | MINIO_ROOT_* | env |

- [ ] **Step 2: Verify YAML loads**

```
python -c "import yaml; [yaml.safe_load(open(f)) for f in __import__('glob').glob('deployment/spec-*.yml')]"
```

- [ ] **Step 3: Commit**

```
git add deployment/spec-*.yml
git commit -m "prod specs: convert secrets blocks to keyed file/env sources

Each stack's spec now self-declares its secret sources. SO creates
the k8s Secret in the stack's own namespace at deploy_start, reading
keyfiles and env vars from the deploy host. Operators no longer run
kubectl create secret out-of-band — each stack is fully self-bootstrapping
from its spec on its own host (multi-machine prod principle)."
```

---

## Task 8: Update docs

**Repo:** `/home/dev/git_puller/repos/hyperlane-stacks`
**Mechanical:** spec reviewer only.
**Files:**
- Modify: `docs/architecture-decisions.md`

- [ ] **Step 1: Update the secrets section**

Locate the section that documents the "operator runs kubectl create secret"
model. Replace with: "Each stack's spec declares its `secrets:` block with
`keys:` and either `env:` or `file:` sources. SO resolves and creates the
k8s Secret in the stack's own namespace at `deploy_start`. No out-of-band
`kubectl` prep is required." Link to the design spec.

If `docs/architecture-decisions.md` has no explicit secrets section, add a
short one after "Kind Cluster Management".

- [ ] **Step 2: Commit**

```
git add docs/architecture-decisions.md
git commit -m "docs: SO now creates user secrets from spec sources"
```

---

## Task 9: Final e2e verification (controller only — not a subagent task)

**Out of scope for subagents.** The controller dispatches this back to the
human operator. The plan does not contain TDD steps for this; it is a hand-off.

- [ ] User runs `pytest tests/e2e/` locally (or triggers CI) after Tasks 1-8
  are committed in both repos.
- [ ] Failures surface → controller dispatches targeted fix subagents.

---

## Notes

- Both repos receive separate commit streams. The SO commits (Tasks 1-2) must
  land first or be on the local path/install used by the e2e tests.
- For local dev: after Task 2, run `pip install -e
  /home/dev/git_puller/repos/stack-orchestrator` (if not already editable) so
  the e2e fixtures use the new method.
- No backwards-compatibility shims in either repo: the consumer always
  uses the new keyed form once it lands.
