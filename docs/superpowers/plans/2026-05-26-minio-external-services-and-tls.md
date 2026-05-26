# MinIO External-Services + Caddy TLS Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut MinIO consumers (both validators, relayer) over from the cross-namespace FQDN to `external-services:` and route the data path through Caddy with TLS in both dev and prod.

**Architecture:** Validators and relayer reach MinIO only through Caddy. In dev, each consumer's `external-services:` entry creates a Service named `hyperlane-minio` in its own namespace, routing to the Kind gateway IP (port 443). In prod, the URL targets the public hostname `s3.bridge.gorbagana.wtf` and cluster DNS forwards to upstream resolvers. mkcert root CA is distributed to consumers via a per-stack `minio-ca-config` ConfigMap so dev exercises the same HTTPS path as prod.

**Tech Stack:** docker-compose, laconic-so, kubernetes, Caddy ingress, mkcert (dev), AWS Signature v4 / S3 path-style addressing, pytest.

---

## File map

**Compose & stack (SO data dir):**
- `stack_orchestrator/data/compose/docker-compose-hyperlane-validator.yml` — passthrough `AWS_ENDPOINT_URL_S3` + `AWS_CA_BUNDLE`; mount `minio-ca-config`.
- `stack_orchestrator/data/compose/docker-compose-hyperlane-relayer.yml` — same three changes.
- `stack_orchestrator/data/stacks/hyperlane-minio/deploy/commands.py` — `start()` becomes a no-op.
- `stack_orchestrator/data/config/minio-ca-config/.gitkeep` — new empty source dir.

**Production specs:**
- `deployment/spec-minio.yml` — add `s3.bridge.gorbagana.wtf` http-proxy entry.
- `deployment/spec-validator-gorchain.yml`, `deployment/spec-validator-solana.yml`, `deployment/spec-relayer.yml` — add `AWS_ENDPOINT_URL_S3` to `config:`; add `minio-ca-config` to `configmaps:`.

**Test fixtures:**
- `tests/e2e/fixtures/test-spec-minio.yml` — add `hyperlane-minio` http-proxy entry.
- `tests/e2e/fixtures/test-spec-validator-gorchain.yml`, `test-spec-validator-solana.yml`, `test-spec-relayer.yml` — add `external-services` entry, `AWS_ENDPOINT_URL_S3` + `AWS_CA_BUNDLE` to `config:`, `minio-ca-config` to `configmaps:`.

**Test code:**
- `tests/e2e/lib/cluster.py` — add `hyperlane-minio` to `TEST_HOSTNAMES`.
- `tests/e2e/lib/state_loader.py` — new helper `write_mkcert_root_to_configmap()`.
- `tests/e2e/test_00_cluster_helpers.py` — unit test for the new helper.
- `tests/e2e/conftest.py` — call new helper after `bridge_state_loader.populate(...)` for validator and relayer fixtures.

---

## Task 1: Repo prep — gitkeep and mkcert SAN

Adds the empty ConfigMap source dir and extends mkcert's SAN list to cover `hyperlane-minio`. Both changes are independently harmless — no consumer references them yet.

**Files:**
- Create: `stack_orchestrator/data/config/minio-ca-config/.gitkeep`
- Modify: `tests/e2e/lib/cluster.py` (the `TEST_HOSTNAMES` tuple)

- [ ] **Step 1: Create the empty ConfigMap source dir**

```bash
mkdir -p stack_orchestrator/data/config/minio-ca-config
touch stack_orchestrator/data/config/minio-ca-config/.gitkeep
```

- [ ] **Step 2: Add `hyperlane-minio` to `TEST_HOSTNAMES`**

Edit `tests/e2e/lib/cluster.py`. The current tuple lives at lines 11–19:

```python
TEST_HOSTNAMES: tuple[str, ...] = (
    "bridge.test",
    "grafana.test",
    "prometheus.test",
    "validator-gorchain.test",
    "validator-solana.test",
    "relayer.test",
    "minio-console.test",
)
```

Replace with:

```python
TEST_HOSTNAMES: tuple[str, ...] = (
    "bridge.test",
    "grafana.test",
    "prometheus.test",
    "validator-gorchain.test",
    "validator-solana.test",
    "relayer.test",
    "minio-console.test",
    "hyperlane-minio",
)
```

(`hyperlane-minio` is the in-cluster Service name validators/relayer dial in dev. mkcert generates a multi-SAN cert covering all `TEST_HOSTNAMES`; the existing `host_prep` fixture writes that cert and the Caddy cert backup, so this addition propagates automatically.)

- [ ] **Step 3: Commit**

```bash
git add stack_orchestrator/data/config/minio-ca-config/.gitkeep \
        tests/e2e/lib/cluster.py
git commit -m "tests: stub minio-ca-config configmap source and mkcert SAN"
```

---

## Task 2: State loader helper — write_mkcert_root_to_configmap

Add a helper that copies mkcert's root CA (from `$(mkcert -CAROOT)/rootCA.pem`) into a consumer's `{deploy_dir}/configmaps/minio-ca-config/rootCA.pem`. Used by conftest after `bridge_state_loader.populate(...)` for validator and relayer fixtures. TDD via a unit test in the same style as the existing `test_00_cluster_helpers.py`.

**Files:**
- Modify: `tests/e2e/lib/state_loader.py`
- Test: `tests/e2e/test_00_cluster_helpers.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/e2e/test_00_cluster_helpers.py`:

```python
from lib.state_loader import write_mkcert_root_to_configmap


def test_write_mkcert_root_to_configmap_copies_cert(tmp_path: Path, monkeypatch):
    """Helper copies CAROOT/rootCA.pem into {deploy_dir}/configmaps/minio-ca-config/."""
    fake_caroot = tmp_path / "fake-caroot"
    fake_caroot.mkdir()
    (fake_caroot / "rootCA.pem").write_bytes(b"--MKCERT-CA--")
    monkeypatch.setenv("CAROOT", str(fake_caroot))

    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()

    write_mkcert_root_to_configmap(deploy_dir)

    dest = deploy_dir / "configmaps" / "minio-ca-config" / "rootCA.pem"
    assert dest.is_file()
    assert dest.read_bytes() == b"--MKCERT-CA--"


def test_write_mkcert_root_to_configmap_creates_parent_dir(tmp_path: Path, monkeypatch):
    """Helper creates configmaps/minio-ca-config/ if it doesn't exist yet."""
    fake_caroot = tmp_path / "fake-caroot"
    fake_caroot.mkdir()
    (fake_caroot / "rootCA.pem").write_bytes(b"X")
    monkeypatch.setenv("CAROOT", str(fake_caroot))

    deploy_dir = tmp_path / "fresh-deploy"
    deploy_dir.mkdir()

    write_mkcert_root_to_configmap(deploy_dir)

    assert (deploy_dir / "configmaps" / "minio-ca-config").is_dir()


def test_write_mkcert_root_to_configmap_raises_when_caroot_missing(tmp_path: Path, monkeypatch):
    """Helper raises FileNotFoundError if mkcert root is absent."""
    monkeypatch.setenv("CAROOT", str(tmp_path / "nope"))
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()

    import pytest as _pytest
    with _pytest.raises(FileNotFoundError):
        write_mkcert_root_to_configmap(deploy_dir)
```

- [ ] **Step 2: Run to confirm the tests fail**

```bash
cd tests/e2e
pytest test_00_cluster_helpers.py -v
```

Expected: ImportError or AttributeError — `write_mkcert_root_to_configmap` doesn't exist yet.

- [ ] **Step 3: Implement the helper**

Append to `tests/e2e/lib/state_loader.py`:

```python
def _resolve_caroot() -> Path:
    """Return the mkcert CA root directory.

    Honors the CAROOT env var (set by the test harness or by the user
    running `export CAROOT=$(mkcert -CAROOT)`). Falls back to invoking
    `mkcert -CAROOT` on PATH if the env var is unset.
    """
    import os
    import subprocess

    env_caroot = os.environ.get("CAROOT")
    if env_caroot:
        return Path(env_caroot)
    result = subprocess.run(
        ["mkcert", "-CAROOT"], capture_output=True, text=True, check=True,
    )
    return Path(result.stdout.strip())


def write_mkcert_root_to_configmap(deploy_dir: Path) -> None:
    """Copy mkcert's root CA into a consumer's minio-ca-config configmap dir.

    Used in dev fixtures so validator/relayer pods trust Caddy's
    mkcert-signed TLS cert when reaching MinIO over HTTPS. In prod the
    source dir stays empty and this helper isn't called.
    """
    src = _resolve_caroot() / "rootCA.pem"
    if not src.is_file():
        raise FileNotFoundError(
            f"mkcert rootCA.pem not found at {src} — did you run `mkcert -install`?"
        )
    dst_dir = deploy_dir / "configmaps" / "minio-ca-config"
    dst_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst_dir / "rootCA.pem")
```

- [ ] **Step 4: Run the tests to confirm they pass**

```bash
cd tests/e2e
pytest test_00_cluster_helpers.py -v
```

Expected: all `test_write_mkcert_root_to_configmap*` tests PASS, plus the existing `test_write_caddy_cert_backup_*` tests still pass.

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/lib/state_loader.py tests/e2e/test_00_cluster_helpers.py
git commit -m "tests: helper to seed mkcert root into minio-ca-config configmap"
```

---

## Task 3: MinIO stack — add new http-proxy hosts

Extends the minio stack so Caddy serves the S3 API on the new hostname (`hyperlane-minio` in dev, `s3.bridge.gorbagana.wtf` in prod) in addition to the existing console host.

**Files:**
- Modify: `tests/e2e/fixtures/test-spec-minio.yml`
- Modify: `deployment/spec-minio.yml`

- [ ] **Step 1: Add `hyperlane-minio` route to the dev minio fixture**

Edit `tests/e2e/fixtures/test-spec-minio.yml`. Current `http-proxy:` block:

```yaml
network:
  acme-email: e2e@example.test
  http-proxy:
    - host-name: minio-console.test
      routes:
        - path: /
          proxy-to: minio:9001
```

Replace with:

```yaml
network:
  acme-email: e2e@example.test
  http-proxy:
    - host-name: hyperlane-minio
      routes:
        - path: /
          proxy-to: minio:9000
    - host-name: minio-console.test
      routes:
        - path: /
          proxy-to: minio:9001
```

- [ ] **Step 2: Add `s3.bridge.gorbagana.wtf` route to the prod minio spec**

Edit `deployment/spec-minio.yml`. Current `http-proxy:` block:

```yaml
network:
  acme-email: admin@gorbagana.wtf
  http-proxy:
    - host-name: minio-console.bridge.gorbagana.wtf
      routes:
        - path: /
          proxy-to: minio:9001
```

Replace with:

```yaml
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
```

- [ ] **Step 3: Commit**

```bash
git add deployment/spec-minio.yml tests/e2e/fixtures/test-spec-minio.yml
git commit -m "minio: expose S3 API through Caddy on dedicated host-name"
```

---

## Task 4: Validator stack — compose, specs, fixture, conftest

This is the validator cut-over. All four file changes must land together: compose stops hardcoding the FQDN, both prod specs and the dev fixture supply the new env vars, the fixture adds the `external-services:` entry, and conftest seeds the mkcert root into the configmap source.

**Files:**
- Modify: `stack_orchestrator/data/compose/docker-compose-hyperlane-validator.yml`
- Modify: `deployment/spec-validator-gorchain.yml`
- Modify: `deployment/spec-validator-solana.yml`
- Modify: `tests/e2e/fixtures/test-spec-validator-gorchain.yml`
- Modify: `tests/e2e/fixtures/test-spec-validator-solana.yml`
- Modify: `tests/e2e/conftest.py`

- [ ] **Step 1: Replace the hardcoded FQDN and add mounts in the validator compose**

Edit `stack_orchestrator/data/compose/docker-compose-hyperlane-validator.yml`. In the `validator` service's `environment:` block, replace:

```yaml
      # MinIO for checkpoint storage (cross-namespace FQDN; PR2 replaces with external-services)
      AWS_ENDPOINT_URL_S3: "http://hyperlane-minio.laconic-hyperlane-minio.svc.cluster.local:9000"
      AWS_REGION: us-east-1
```

With:

```yaml
      # MinIO endpoint — set in each spec's config: block.
      # Dev: https://hyperlane-minio:443 (Caddy via external-services Service).
      # Prod: https://s3.bridge.gorbagana.wtf:443 (Caddy via public DNS).
      AWS_ENDPOINT_URL_S3: ${AWS_ENDPOINT_URL_S3}
      # mkcert root CA bundle path (dev only). Empty in prod → SDK uses
      # the container's built-in public CA bundle for LE verification.
      AWS_CA_BUNDLE: ${AWS_CA_BUNDLE:-}
      AWS_REGION: us-east-1
```

In the same `validator` service's `volumes:` block, replace:

```yaml
    volumes:
      - agent-config:/config:ro
      - validator-data:/data
```

With:

```yaml
    volumes:
      - agent-config:/config:ro
      - minio-ca-config:/etc/ssl/certs/minio-ca:ro
      - validator-data:/data
```

Finally, add `minio-ca-config:` to the bottom `volumes:` declaration:

```yaml
volumes:
  # agent-config: ConfigMap volume sourced from BridgeStateLoader at deploy-create
  agent-config:
  # minio-ca-config: ConfigMap volume for mkcert root CA (dev only; empty in prod)
  minio-ca-config:
  validator-data:
```

- [ ] **Step 2: Add the prod gorchain validator spec entries**

Edit `deployment/spec-validator-gorchain.yml`. Extend the existing `config:` block:

```yaml
config:
  ORIGIN_CHAIN_NAME: gorchain
  CHECKPOINT_BUCKET: hyperlane-validator-gorchain
  PRIVY_WALLET_ID: "REPLACE_WITH_WALLET_ID"
  # MinIO via Caddy — public DNS resolves to the Caddy ingress.
  AWS_ENDPOINT_URL_S3: "https://s3.bridge.gorbagana.wtf:443"
```

Extend the existing `configmaps:` block:

```yaml
configmaps:
  agent-config: ./configmaps/agent-config
  # Empty in prod — only populated in dev with the mkcert root CA.
  minio-ca-config: ./configmaps/minio-ca-config
```

- [ ] **Step 3: Add the prod solana validator spec entries**

Edit `deployment/spec-validator-solana.yml`. Extend `config:`:

```yaml
config:
  ORIGIN_CHAIN_NAME: solana
  CHECKPOINT_BUCKET: hyperlane-validator-solana
  PRIVY_WALLET_ID: "REPLACE_WITH_WALLET_ID"
  # MinIO via Caddy — public DNS resolves to the Caddy ingress.
  AWS_ENDPOINT_URL_S3: "https://s3.bridge.gorbagana.wtf:443"
```

Extend `configmaps:`:

```yaml
configmaps:
  agent-config: ./configmaps/agent-config
  # Empty in prod — only populated in dev with the mkcert root CA.
  minio-ca-config: ./configmaps/minio-ca-config
```

- [ ] **Step 4: Add the dev gorchain validator fixture entries**

Edit `tests/e2e/fixtures/test-spec-validator-gorchain.yml`. Extend `config:` with the dev endpoint URL and the CA bundle path:

```yaml
config:
  ORIGIN_CHAIN_NAME: gorchain
  CHECKPOINT_BUCKET: hyperlane-validator-gorchain
  PRIVY_API_URL: "http://privy-mock:19876"
  PRIVY_WALLET_ID: REPLACE_PRIVY_WALLET_ID
  # MinIO via Caddy — external-services creates the in-NS Service below.
  AWS_ENDPOINT_URL_S3: "https://hyperlane-minio:443"
  AWS_CA_BUNDLE: "/etc/ssl/certs/minio-ca/rootCA.pem"
```

Extend `configmaps:`:

```yaml
configmaps:
  agent-config: ./configmaps/agent-config
  # Seeded with mkcert root CA by conftest (write_mkcert_root_to_configmap).
  minio-ca-config: ./configmaps/minio-ca-config
```

Extend `external-services:` (dev only — see comment):

```yaml
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
  # Routes the in-cluster name `hyperlane-minio` to the Kind gateway
  # so traffic exits the cluster, re-enters via Caddy on 443, and gets
  # reverse_proxied to minio:9000.
  hyperlane-minio:
    ip: REPLACE_HOST_IP
    port: 443
```

- [ ] **Step 5: Add the dev solana validator fixture entries**

Edit `tests/e2e/fixtures/test-spec-validator-solana.yml` with the same three blocks as Step 4 (the file is identical in structure to validator-gorchain except for `ORIGIN_CHAIN_NAME`/`CHECKPOINT_BUCKET`).

Extend `config:`:

```yaml
config:
  ORIGIN_CHAIN_NAME: solana
  CHECKPOINT_BUCKET: hyperlane-validator-solana
  PRIVY_API_URL: "http://privy-mock:19876"
  PRIVY_WALLET_ID: REPLACE_PRIVY_WALLET_ID
  AWS_ENDPOINT_URL_S3: "https://hyperlane-minio:443"
  AWS_CA_BUNDLE: "/etc/ssl/certs/minio-ca/rootCA.pem"
```

Extend `configmaps:`:

```yaml
configmaps:
  agent-config: ./configmaps/agent-config
  minio-ca-config: ./configmaps/minio-ca-config
```

Extend `external-services:`:

```yaml
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
  hyperlane-minio:
    ip: REPLACE_HOST_IP
    port: 443
```

- [ ] **Step 6: Wire the populator call into conftest**

Edit `tests/e2e/conftest.py`. Update the `from lib.state_loader import ...` line (look for the existing `BridgeStateLoader` import near the top) so `write_mkcert_root_to_configmap` is also imported:

```python
from lib.state_loader import BridgeStateLoader, write_mkcert_root_to_configmap
```

Then in `_deploy_validator` (around line 921), the existing line:

```python
    bridge_state_loader.populate("hyperlane-validator", deploy_info.deploy_dir)
```

becomes:

```python
    bridge_state_loader.populate("hyperlane-validator", deploy_info.deploy_dir)
    write_mkcert_root_to_configmap(deploy_info.deploy_dir)
```

- [ ] **Step 7: Run the validator e2e test to verify the cut-over**

```bash
cd tests/e2e
pytest test_04_validator.py -v -s
```

Expected: validator pods start (both gorchain and solana), reach `Running`, become Ready (HTTPS TLS handshake with Caddy succeeds, S3 signature verification succeeds with path-style addressing). Watch for any of these failure signatures and treat as task failure:
- `failed to find rootCA.pem` — CAROOT not propagated.
- `certificate signed by unknown authority` — `AWS_CA_BUNDLE` not effective or wrong path.
- `SignatureDoesNotMatch` — should not happen (Host preservation already verified empirically on 2026-05-26); if it does, capture Caddy logs.
- `dial tcp: lookup hyperlane-minio` — `external-services:` Service not created in the validator namespace.

- [ ] **Step 8: Commit**

```bash
git add stack_orchestrator/data/compose/docker-compose-hyperlane-validator.yml \
        deployment/spec-validator-gorchain.yml \
        deployment/spec-validator-solana.yml \
        tests/e2e/fixtures/test-spec-validator-gorchain.yml \
        tests/e2e/fixtures/test-spec-validator-solana.yml \
        tests/e2e/conftest.py
git commit -m "validator: reach MinIO via Caddy with external-services + mkcert CA"
```

---

## Task 5: Relayer stack — compose, spec, fixture, conftest

Same shape as Task 4 but for the relayer. The relayer has only one prod spec and one dev fixture, so the diff is smaller.

**Files:**
- Modify: `stack_orchestrator/data/compose/docker-compose-hyperlane-relayer.yml`
- Modify: `deployment/spec-relayer.yml`
- Modify: `tests/e2e/fixtures/test-spec-relayer.yml`
- Modify: `tests/e2e/conftest.py`

- [ ] **Step 1: Replace the hardcoded FQDN and add mounts in the relayer compose**

Edit `stack_orchestrator/data/compose/docker-compose-hyperlane-relayer.yml`. In the `relayer` service's `environment:` block, replace:

```yaml
      # MinIO for reading validator checkpoints (cross-namespace FQDN; PR2 replaces with external-services)
      AWS_ENDPOINT_URL_S3: "http://hyperlane-minio.laconic-hyperlane-minio.svc.cluster.local:9000"
      AWS_REGION: us-east-1
```

With:

```yaml
      # MinIO endpoint — set in each spec's config: block.
      # Dev: https://hyperlane-minio:443 (Caddy via external-services Service).
      # Prod: https://s3.bridge.gorbagana.wtf:443 (Caddy via public DNS).
      AWS_ENDPOINT_URL_S3: ${AWS_ENDPOINT_URL_S3}
      # mkcert root CA bundle path (dev only). Empty in prod → SDK uses
      # the container's built-in public CA bundle for LE verification.
      AWS_CA_BUNDLE: ${AWS_CA_BUNDLE:-}
      AWS_REGION: us-east-1
```

In the same `relayer` service's `volumes:` block, replace:

```yaml
    volumes:
      - agent-config:/config:ro
      - relayer-data:/data
```

With:

```yaml
    volumes:
      - agent-config:/config:ro
      - minio-ca-config:/etc/ssl/certs/minio-ca:ro
      - relayer-data:/data
```

Add `minio-ca-config:` to the bottom `volumes:` declaration:

```yaml
volumes:
  # agent-config: ConfigMap volume sourced from BridgeStateLoader at deploy-create
  agent-config:
  # minio-ca-config: ConfigMap volume for mkcert root CA (dev only; empty in prod)
  minio-ca-config:
  relayer-data:
  igp-fee-claim-scripts-config:
```

- [ ] **Step 2: Add the prod relayer spec entries**

Edit `deployment/spec-relayer.yml`. Extend `config:`:

```yaml
config:
  GORCHAIN_RPC_URL: "REPLACE_WITH_GORCHAIN_RPC_URL"
  SOLANA_RPC_URL: "REPLACE_WITH_SOLANA_RPC_URL"
  GORCHAIN_IGP_PROGRAM_ID: "REPLACE_WITH_GORCHAIN_IGP_PROGRAM_ID"
  SOLANA_IGP_PROGRAM_ID: "REPLACE_WITH_SOLANA_IGP_PROGRAM_ID"
  GORCHAIN_IGP_ACCOUNT: "REPLACE_WITH_GORCHAIN_IGP_ACCOUNT"
  SOLANA_IGP_ACCOUNT: "REPLACE_WITH_SOLANA_IGP_ACCOUNT"
  # CLAIM_INTERVAL_SECONDS: "21600"  # IGP fee claim interval (default: 6h)
  # MinIO via Caddy — public DNS resolves to the Caddy ingress.
  AWS_ENDPOINT_URL_S3: "https://s3.bridge.gorbagana.wtf:443"
```

Extend `configmaps:`:

```yaml
configmaps:
  igp-fee-claim-scripts-config: ./configmaps/igp-fee-claim-scripts-config
  agent-config: ./configmaps/agent-config
  # Empty in prod — only populated in dev with the mkcert root CA.
  minio-ca-config: ./configmaps/minio-ca-config
```

- [ ] **Step 3: Add the dev relayer fixture entries**

Edit `tests/e2e/fixtures/test-spec-relayer.yml`. Extend `config:`:

```yaml
config:
  GORCHAIN_RPC_URL: "http://gorchain-rpc:8899"
  SOLANA_RPC_URL: "http://solana-rpc:18899"
  GORCHAIN_IGP_PROGRAM_ID: "REPLACE_AT_RUNTIME"
  SOLANA_IGP_PROGRAM_ID: "REPLACE_AT_RUNTIME"
  GORCHAIN_IGP_ACCOUNT: "REPLACE_AT_RUNTIME"
  SOLANA_IGP_ACCOUNT: "REPLACE_AT_RUNTIME"
  CLAIM_INTERVAL_SECONDS: "600"
  # MinIO via Caddy — external-services creates the in-NS Service below.
  AWS_ENDPOINT_URL_S3: "https://hyperlane-minio:443"
  AWS_CA_BUNDLE: "/etc/ssl/certs/minio-ca/rootCA.pem"
```

Extend `configmaps:`:

```yaml
configmaps:
  igp-fee-claim-scripts-config: ./configmaps/igp-fee-claim-scripts-config
  agent-config: ./configmaps/agent-config
  # Seeded with mkcert root CA by conftest (write_mkcert_root_to_configmap).
  minio-ca-config: ./configmaps/minio-ca-config
```

Extend `external-services:`:

```yaml
external-services:
  gorchain-rpc:
    ip: REPLACE_HOST_IP
    port: 8899
  solana-rpc:
    ip: REPLACE_HOST_IP
    port: 18899
  # Dev only — prod uses public DNS to resolve s3.bridge.gorbagana.wtf.
  hyperlane-minio:
    ip: REPLACE_HOST_IP
    port: 443
```

- [ ] **Step 4: Wire the populator call into the relayer conftest fixture**

Edit `tests/e2e/conftest.py`. In the `relayer_deployment` fixture (around line 1085), the existing line:

```python
    bridge_state_loader.populate("hyperlane-relayer", deploy_info.deploy_dir)
```

becomes:

```python
    bridge_state_loader.populate("hyperlane-relayer", deploy_info.deploy_dir)
    write_mkcert_root_to_configmap(deploy_info.deploy_dir)
```

(The import was already added in Task 4 Step 6.)

- [ ] **Step 5: Run the relayer e2e test to verify the cut-over**

```bash
cd tests/e2e
pytest test_05_relayer.py -v -s
```

Expected: relayer pod runs, becomes Ready, no `SignatureDoesNotMatch` / `ConnectError` / `NoSuchBucket` / `InvalidAccessKeyId` in logs (this is asserted by `test_relayer_checkpoint_syncer_connected`).

- [ ] **Step 6: Commit**

```bash
git add stack_orchestrator/data/compose/docker-compose-hyperlane-relayer.yml \
        deployment/spec-relayer.yml \
        tests/e2e/fixtures/test-spec-relayer.yml \
        tests/e2e/conftest.py
git commit -m "relayer: reach MinIO via Caddy with external-services + mkcert CA"
```

---

## Task 6: Remove cross-stack ClusterIP Service from minio stack

The cross-stack `hyperlane-minio` Service in the minio namespace existed to back the old FQDN (`hyperlane-minio.laconic-hyperlane-minio.svc.cluster.local:9000`). After Tasks 4 and 5, no consumer dials that FQDN — they all dial through Caddy via the in-NS Service created by their own `external-services:` entry (dev) or public DNS (prod). This task deletes the cross-stack Service hook so it stops being created.

**Files:**
- Modify: `stack_orchestrator/data/stacks/hyperlane-minio/deploy/commands.py`

- [ ] **Step 1: Make `start()` a no-op**

Edit `stack_orchestrator/data/stacks/hyperlane-minio/deploy/commands.py`. Replace the entire `start()` function (lines 19–67) with:

```python
def start(context: DeploymentContext):
    """No-op — consumers now reach MinIO through Caddy via external-services
    (dev) or public DNS (prod). The previous cross-stack ClusterIP Service
    is no longer required.
    """
    pass
```

(`init` and `create` are already no-ops above; the `from kubernetes import ...` imports and `from pathlib import Path` can stay — leaving them avoids churn and the file is short.)

- [ ] **Step 2: Run the minio e2e test to confirm nothing depends on the Service**

```bash
cd tests/e2e
pytest test_03_minio.py -v -s
```

Expected: minio tests pass. If any test specifically checks for a `hyperlane-minio` Service in the minio namespace, that assertion needs to be removed in the same task.

- [ ] **Step 3: Verify the Service is no longer created**

After running the minio test above, with the cluster still alive (i.e. use `--skip-cleanup`), check that the cross-stack Service is gone:

```bash
kubectl -n laconic-hyperlane-minio get svc
```

Expected: only the per-pod Service (named `minio`, created by SO from the compose service definition) appears. No `hyperlane-minio` Service.

- [ ] **Step 4: Commit**

```bash
git add stack_orchestrator/data/stacks/hyperlane-minio/deploy/commands.py
git commit -m "minio: drop cross-stack ClusterIP Service hook"
```

---

## Task 7: Full e2e verification

End-to-end run of the bridge tests that exercise the MinIO data path. This is the gate the spec's §6 calls out. Also greps for the old FQDN literal to confirm the cut-over is complete.

**Files:**
- None modified — verification only.

- [ ] **Step 1: Run the full MinIO-touching test suite**

From a clean cluster (no `--skip-cluster-setup`):

```bash
cd tests/e2e
pytest test_03_minio.py test_04_validator.py test_05_relayer.py test_08_bridge.py -v -s
```

Expected: all tests PASS. The relayer test's `test_relayer_checkpoint_syncer_connected` confirms no S3 errors. `test_08_bridge.py` confirms end-to-end transfers work, which requires the relayer to be reading validator-written checkpoints from MinIO over HTTPS.

- [ ] **Step 2: Confirm the old FQDN literal is gone**

```bash
grep -rn "hyperlane-minio.laconic-hyperlane-minio.svc.cluster.local" \
  stack_orchestrator/data/compose/ \
  deployment/ \
  tests/e2e/fixtures/
```

Expected: no matches.

- [ ] **Step 3: Confirm the cross-stack Service is gone in dev**

With the cluster still alive from Step 1 (`--skip-cleanup`):

```bash
kubectl -n laconic-hyperlane-minio get svc -o name
```

Expected: only `service/minio` (or similar per-pod Service) — no `service/hyperlane-minio`.

- [ ] **Step 4: Confirm validator + relayer pods carry the new env vars**

```bash
for ns in laconic-hyperlane-validator-gorchain \
          laconic-hyperlane-validator-solana \
          laconic-hyperlane-relayer; do
  echo "=== $ns ==="
  kubectl -n "$ns" get pods -o jsonpath='{.items[0].metadata.name}' | \
    xargs -I{} kubectl -n "$ns" exec {} -c validator -- env 2>/dev/null \
    | grep -E '^(AWS_ENDPOINT_URL_S3|AWS_CA_BUNDLE)=' || \
    kubectl -n "$ns" get pods -o jsonpath='{.items[0].metadata.name}' | \
    xargs -I{} kubectl -n "$ns" exec {} -c relayer -- env \
    | grep -E '^(AWS_ENDPOINT_URL_S3|AWS_CA_BUNDLE)='
done
```

Expected for each NS:
```
AWS_ENDPOINT_URL_S3=https://hyperlane-minio:443
AWS_CA_BUNDLE=/etc/ssl/certs/minio-ca/rootCA.pem
```

- [ ] **Step 5: Confirm the mkcert root CA is present in the consumer pods**

```bash
for ns in laconic-hyperlane-validator-gorchain \
          laconic-hyperlane-validator-solana \
          laconic-hyperlane-relayer; do
  echo "=== $ns ==="
  kubectl -n "$ns" get pods -o jsonpath='{.items[0].metadata.name}' | \
    xargs -I{} kubectl -n "$ns" exec {} -c validator -- \
      head -1 /etc/ssl/certs/minio-ca/rootCA.pem 2>/dev/null || \
  kubectl -n "$ns" get pods -o jsonpath='{.items[0].metadata.name}' | \
    xargs -I{} kubectl -n "$ns" exec {} -c relayer -- \
      head -1 /etc/ssl/certs/minio-ca/rootCA.pem
done
```

Expected: each pod prints `-----BEGIN CERTIFICATE-----`.

- [ ] **Step 6: No commit needed — verification only**

This task produces no diff. If any step above fails, return to the relevant earlier task to fix.

---

## Self-review notes

Coverage against the spec:
- §3 architecture diagram — implemented across Tasks 3 (Caddy host-names), 4 + 5 (consumer env / external-services), and exercised in Task 7.
- §4.1 (Caddy fronts both envs) — Task 3.
- §4.2 (external-services dev-only) — Tasks 4 Steps 4/5 and 5 Step 3 add the entry with explanatory comment; prod specs in Tasks 4 Steps 2/3 and 5 Step 2 deliberately omit it.
- §4.3 (mkcert CA distribution) — Task 1 (source dir), Task 2 (helper + tests), Tasks 4/5 (compose mount + conftest wiring), Task 7 Step 5 (verification).
- §4.4 (cross-stack Service removed) — Task 6.
- §4.5 (spec-driven endpoint URL) — Tasks 4/5 Step 1 (compose passthrough), Steps 2–5 (specs supply value).
- §5 file list — all paths accounted for in Tasks 1, 3, 4, 5, 6.
- §6 verification — Task 7 covers all four bullets.
- §7 (out of scope) — explicit PR2 split is honored; nothing in this plan touches per-validator users.
