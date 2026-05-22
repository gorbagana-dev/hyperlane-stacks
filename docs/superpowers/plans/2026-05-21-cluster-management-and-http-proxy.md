# Cluster Management Switch + http-proxy Wiring — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hand kind-cluster + ingress + TLS to stack-orchestrator. Tests use the same Caddy + Ingress + TLS-termination path as prod (mkcert in dev, ACME in prod).

**Architecture:** Every long-running stack spec declares `kind-mount-root` + `acme-email`. SO's `--perform-cluster-management` (passed unconditionally by `deploy_start`) creates the kind cluster on first invocation and installs Caddy; subsequent stacks reuse. mkcert generates a multi-SAN cert; the fixture writes it to `<kind_mount_root>/caddy-cert-backup/caddy-secrets.yaml` so SO's `_restore_caddy_certs` pre-loads it before Caddy starts. The hand-rolled cert-manager + nginx + Ingress code in conftest is deleted.

**Tech Stack:** stack-orchestrator (pinned `v1.1.0-b3e9366-202605111309`, no changes); Caddy ingress controller; mkcert; Python 3.10+ / pytest; kind.

**Spec:** `docs/superpowers/specs/2026-05-21-cluster-management-and-http-proxy-design.md` (commit `d0dd7dd`).

---

## File Structure

### Files created

| File | Responsibility |
|---|---|
| `tests/e2e/test_ingress_endpoints.py` | One probe test per Ingress URL (catches `http-proxy:` drift) |

### Files modified

| File | Change |
|---|---|
| `tests/e2e/lib/cluster.py` | Remove `create_kind_cluster`, `install_cert_manager`, `create_selfsigned_issuer`, `install_ingress_nginx`. Add `KIND_CLUSTER_NAME`, `TEST_HOSTNAMES`, `ensure_mkcert_installed`, `ensure_mkcert_cert`, `write_caddy_cert_backup` |
| `tests/e2e/lib/deploy.py` | `deploy_start()` passes `--perform-cluster-management` |
| `tests/e2e/conftest.py` | Rename `kind_cluster` → `host_prep`; rewrite body; delete `*_INGRESS_TEMPLATE` constants + all hand-rolled Ingress + cert polling blocks; drop `REPLACE_KIND_MOUNT_ROOT` substitution |
| `tests/e2e/fixtures/test-spec-*.yml` (×9) | Add `kind-mount-root`, `acme-email`; long-running stacks with HTTP get `network.http-proxy:`; replace `kind-cluster-name: REPLACE_KIND_CLUSTER` with `hyperlane` |
| `tests/e2e/README.md` | New "First-time machine setup" section; rewrite TLS section |
| `.github/workflows/e2e.yml` | Add `mkcert` install step |
| `deployment/spec-*.yml` (×9) | Add `kind-mount-root: /srv/kind/hyperlane-bridge`, `acme-email: ops@example.com` (placeholder); add `http-proxy:` for validator-gorchain, validator-solana, relayer, minio |
| `docs/architecture-decisions.md` | Rewrite "Kind Cluster Management" section; add "Multi-Machine Prod Principle" section; append note to "Artifact Passing" |

### Files deleted

| File | Reason |
|---|---|
| `tests/e2e/fixtures/kind-config.yaml` | SO generates from `kind-mount-root` |
| `tests/e2e/fixtures/cert-manager-issuer.yaml` | No cert-manager |

---

## Task 1: Add `TEST_HOSTNAMES` and `KIND_CLUSTER_NAME` constants

**Files:**
- Modify: `tests/e2e/lib/cluster.py:10`

- [ ] **Step 1: Replace `KIND_CLUSTER_NAME` value**

Edit `tests/e2e/lib/cluster.py` line 10:

```python
KIND_CLUSTER_NAME = "hyperlane"
```

(Was `"hyperlane-e2e"` — switching to a stable name shared with every spec's `kind-cluster-name:` field.)

- [ ] **Step 2: Add `TEST_HOSTNAMES` constant**

Insert after line 10:

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

- [ ] **Step 3: Verify import path is clean**

Run: `python -c "from tests.e2e.lib.cluster import KIND_CLUSTER_NAME, TEST_HOSTNAMES; print(KIND_CLUSTER_NAME, len(TEST_HOSTNAMES))"`
Expected: `hyperlane 7`

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/lib/cluster.py
git commit -m "tests: add TEST_HOSTNAMES constant and pin kind cluster name"
```

---

## Task 2: Add mkcert helpers (TDD)

**Files:**
- Modify: `tests/e2e/lib/cluster.py`
- Test: `tests/e2e/test_cluster_helpers.py` (new)

`write_caddy_cert_backup` is a pure YAML-rendering function — testable without mkcert installed. `ensure_mkcert_installed` and `ensure_mkcert_cert` are subprocess wrappers; we'll test the YAML rendering thoroughly and smoke-test the subprocess paths in CI integration.

- [ ] **Step 1: Write failing test for `write_caddy_cert_backup`**

Create `tests/e2e/test_cluster_helpers.py`:

```python
import base64
from pathlib import Path

import pytest
import yaml

from tests.e2e.lib.cluster import write_caddy_cert_backup


def test_write_caddy_cert_backup_writes_one_secret_per_hostname(tmp_path: Path):
    cert = tmp_path / "test.crt"
    key = tmp_path / "test.key"
    cert.write_bytes(b"CERT-CONTENT")
    key.write_bytes(b"KEY-CONTENT")
    out = tmp_path / "out" / "caddy-secrets.yaml"

    write_caddy_cert_backup(out, cert, key, ["a.test", "b.test"])

    assert out.is_file()
    docs = list(yaml.safe_load_all(out.read_text()))
    docs = [d for d in docs if d]  # drop trailing None from --- separators
    assert len(docs) == 2
    names = {d["metadata"]["name"] for d in docs}
    assert names == {
        "caddy.ingress--certificates.acme-v02.api.letsencrypt.org-directory--a.test",
        "caddy.ingress--certificates.acme-v02.api.letsencrypt.org-directory--b.test",
    }
    for d in docs:
        assert d["metadata"]["namespace"] == "caddy-system"
        assert d["type"] == "Opaque"
        assert base64.b64decode(d["data"]["tls.crt"]) == b"CERT-CONTENT"
        assert base64.b64decode(d["data"]["tls.key"]) == b"KEY-CONTENT"


def test_write_caddy_cert_backup_creates_parent_dirs(tmp_path: Path):
    cert = tmp_path / "c.crt"
    key = tmp_path / "c.key"
    cert.write_bytes(b"x")
    key.write_bytes(b"y")
    out = tmp_path / "deep" / "nested" / "caddy-secrets.yaml"

    write_caddy_cert_backup(out, cert, key, ["only.test"])

    assert out.is_file()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd tests/e2e && pytest test_cluster_helpers.py -v`
Expected: FAIL with `ImportError: cannot import name 'write_caddy_cert_backup' from tests.e2e.lib.cluster`

- [ ] **Step 3: Implement `write_caddy_cert_backup`**

Append to `tests/e2e/lib/cluster.py`:

```python
import base64

CADDY_SECRET_PREFIX = (
    "caddy.ingress--certificates.acme-v02.api.letsencrypt.org-directory"
)
CADDY_NAMESPACE = "caddy-system"


def write_caddy_cert_backup(
    backup_path: Path, cert_path: Path, key_path: Path, hostnames: list[str]
) -> None:
    """Render caddy-secrets.yaml with one k8s Secret per hostname referencing
    the same cert+key, formatted for SO's _restore_caddy_certs to load before
    Caddy starts.
    """
    cert_b64 = base64.b64encode(cert_path.read_bytes()).decode("ascii")
    key_b64 = base64.b64encode(key_path.read_bytes()).decode("ascii")

    docs = []
    for host in hostnames:
        docs.append({
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": f"{CADDY_SECRET_PREFIX}--{host}",
                "namespace": CADDY_NAMESPACE,
            },
            "type": "Opaque",
            "data": {"tls.crt": cert_b64, "tls.key": key_b64},
        })

    backup_path.parent.mkdir(parents=True, exist_ok=True)
    import yaml as _yaml
    backup_path.write_text(_yaml.safe_dump_all(docs))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd tests/e2e && pytest test_cluster_helpers.py -v`
Expected: 2 passed.

- [ ] **Step 5: Implement `ensure_mkcert_installed` and `ensure_mkcert_cert`**

Append to `tests/e2e/lib/cluster.py`:

```python
def ensure_mkcert_installed() -> None:
    """Run `mkcert -install` idempotently. Skip if CAROOT already contains a
    rootCA. Raise with a pointer to README if mkcert isn't on PATH.
    """
    check = run_cmd(["which", "mkcert"], check=False, quiet=True)
    if check.returncode != 0:
        fail_exit(
            "mkcert not found on PATH. See tests/e2e/README.md "
            "for one-time setup instructions."
        )

    caroot = run_cmd(["mkcert", "-CAROOT"], quiet=True).stdout.strip()
    if (Path(caroot) / "rootCA.pem").is_file():
        log_info(f"mkcert CA already installed at {caroot}")
        return

    log_info("Running `mkcert -install` (installs root CA into system trust store)")
    run_cmd(["mkcert", "-install"])


def ensure_mkcert_cert(
    cert_dir: Path, hostnames: list[str]
) -> tuple[Path, Path]:
    """Generate (or reuse) a multi-SAN mkcert cert covering hostnames.
    Returns (cert_path, key_path). Idempotent — regenerates if the existing
    cert's SANs don't cover the requested hostnames.
    """
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert = cert_dir / "hyperlane.test.crt"
    key = cert_dir / "hyperlane.test.key"

    if cert.is_file() and key.is_file():
        # Check existing cert covers all requested SANs
        result = run_cmd(
            ["openssl", "x509", "-in", str(cert), "-noout", "-ext",
             "subjectAltName"],
            check=False, quiet=True,
        )
        if result.returncode == 0 and all(h in result.stdout for h in hostnames):
            log_info(f"Reusing existing mkcert cert at {cert}")
            return cert, key
        log_info("Existing cert SANs out of date; regenerating")

    log_info(f"Generating mkcert cert for {len(hostnames)} hostnames")
    run_cmd(
        ["mkcert",
         "-cert-file", str(cert),
         "-key-file", str(key),
         *hostnames],
    )
    return cert, key
```

- [ ] **Step 6: Commit**

```bash
git add tests/e2e/lib/cluster.py tests/e2e/test_cluster_helpers.py
git commit -m "tests: add mkcert + caddy cert-backup helpers"
```

---

## Task 3: Add `kind-mount-root` + `acme-email` to test fixture specs

**Files:**
- Modify: `tests/e2e/fixtures/test-spec-deployer.yml`
- Modify: `tests/e2e/fixtures/test-spec-warp-deployer.yml`
- Modify: `tests/e2e/fixtures/test-spec-validator-gorchain.yml`
- Modify: `tests/e2e/fixtures/test-spec-validator-solana.yml`
- Modify: `tests/e2e/fixtures/test-spec-relayer.yml`
- Modify: `tests/e2e/fixtures/test-spec-minio.yml`
- Modify: `tests/e2e/fixtures/test-spec-gas-oracle.yml`
- Modify: `tests/e2e/fixtures/test-spec-monitoring.yml`
- Modify: `tests/e2e/fixtures/test-spec-warp-ui.yml`

The deployer and warp-deployer already have `kind-mount-root`. The others need it added. Every spec gets `acme-email`.

- [ ] **Step 1: Add `kind-mount-root` to specs that lack it**

For each of validator-gorchain, validator-solana, relayer, minio, gas-oracle, monitoring, warp-ui:
Insert at the top level (after `kind-cluster-name:` line):

```yaml
kind-mount-root: /tmp/hyperlane-bridge-e2e
```

For deployer + warp-deployer specs: verify they already have this line; if any value differs, change to `/tmp/hyperlane-bridge-e2e`.

- [ ] **Step 2: Add `acme-email` to every spec**

For every test-spec-*.yml file, insert at the top level (after `kind-mount-root:` line):

```yaml
acme-email: e2e@example.test
```

- [ ] **Step 3: Replace `kind-cluster-name: REPLACE_KIND_CLUSTER` with literal**

In every test-spec-*.yml file:

```yaml
kind-cluster-name: hyperlane
```

(Replacing the `REPLACE_KIND_CLUSTER` sentinel that SPEC_REPLACEMENTS used to patch.)

- [ ] **Step 4: Sanity-check YAML**

Run: `cd tests/e2e && python -c "import yaml, pathlib; [yaml.safe_load(p.read_text()) for p in pathlib.Path('fixtures').glob('test-spec-*.yml')]"`
Expected: silent (no exceptions).

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/fixtures/test-spec-*.yml
git commit -m "tests: declare kind-mount-root and acme-email on every spec"
```

---

## Task 4: Add `http-proxy:` to long-running test fixture specs

**Files:**
- Modify: `tests/e2e/fixtures/test-spec-validator-gorchain.yml`
- Modify: `tests/e2e/fixtures/test-spec-validator-solana.yml`
- Modify: `tests/e2e/fixtures/test-spec-relayer.yml`
- Modify: `tests/e2e/fixtures/test-spec-minio.yml`
- Modify: `tests/e2e/fixtures/test-spec-monitoring.yml`
- Modify: `tests/e2e/fixtures/test-spec-warp-ui.yml`

`http-proxy:` blocks go under a top-level `network:` key. The compose services exposed are `validator`, `relayer`, `minio`, `grafana`, `prometheus`, `warp-ui`.

- [ ] **Step 1: Add http-proxy to test-spec-validator-gorchain.yml**

Append at top level:

```yaml
network:
  http-proxy:
    - host-name: validator-gorchain.test
      routes:
        - path: /
          proxy-to: validator:9090
```

- [ ] **Step 2: Add http-proxy to test-spec-validator-solana.yml**

Append at top level:

```yaml
network:
  http-proxy:
    - host-name: validator-solana.test
      routes:
        - path: /
          proxy-to: validator:9090
```

- [ ] **Step 3: Add http-proxy to test-spec-relayer.yml**

Append at top level:

```yaml
network:
  http-proxy:
    - host-name: relayer.test
      routes:
        - path: /
          proxy-to: relayer:9091
```

- [ ] **Step 4: Add http-proxy to test-spec-minio.yml**

Append at top level:

```yaml
network:
  http-proxy:
    - host-name: minio-console.test
      routes:
        - path: /
          proxy-to: minio:9001
```

- [ ] **Step 5: Add http-proxy to test-spec-monitoring.yml**

Append at top level:

```yaml
network:
  http-proxy:
    - host-name: grafana.test
      routes:
        - path: /
          proxy-to: grafana:3000
    - host-name: prometheus.test
      routes:
        - path: /
          proxy-to: prometheus:9090
```

- [ ] **Step 6: Add http-proxy to test-spec-warp-ui.yml**

Append at top level:

```yaml
network:
  http-proxy:
    - host-name: bridge.test
      routes:
        - path: /
          proxy-to: warp-ui:3000
```

- [ ] **Step 7: Sanity-check YAML**

Run: `cd tests/e2e && python -c "import yaml, pathlib; [yaml.safe_load(p.read_text()) for p in pathlib.Path('fixtures').glob('test-spec-*.yml')]"`
Expected: silent.

- [ ] **Step 8: Commit**

```bash
git add tests/e2e/fixtures/test-spec-*.yml
git commit -m "tests: route .test hostnames through SO http-proxy"
```

---

## Task 5: Update prod specs (prescriptive values + http-proxy gaps)

**Files:**
- Modify: `deployment/spec-deployer.yml`
- Modify: `deployment/spec-warp-deployer.yml`
- Modify: `deployment/spec-validator-gorchain.yml`
- Modify: `deployment/spec-validator-solana.yml`
- Modify: `deployment/spec-relayer.yml`
- Modify: `deployment/spec-minio.yml`
- Modify: `deployment/spec-gas-oracle.yml`
- Modify: `deployment/spec-monitoring.yml` (already has http-proxy for grafana + prometheus)
- Modify: `deployment/spec-warp-ui.yml` (already has http-proxy for bridge)

- [ ] **Step 1: Add `kind-mount-root` and `acme-email` to every prod spec**

For each `deployment/spec-*.yml`, insert at the top level (after `kind-cluster-name:` line):

```yaml
kind-mount-root: /srv/kind/hyperlane-bridge

# Replace with operator email for Let's Encrypt registration:
acme-email: ops@example.com
```

If a spec already has `kind-mount-root` with a different value, change it to `/srv/kind/hyperlane-bridge`.

- [ ] **Step 2: Add http-proxy to spec-validator-gorchain.yml**

Append at top level (or under existing `network:` if present):

```yaml
network:
  http-proxy:
    - host-name: validator-gorchain.example.com
      routes:
        - path: /
          proxy-to: validator:9090
```

- [ ] **Step 3: Add http-proxy to spec-validator-solana.yml**

```yaml
network:
  http-proxy:
    - host-name: validator-solana.example.com
      routes:
        - path: /
          proxy-to: validator:9090
```

- [ ] **Step 4: Add http-proxy to spec-relayer.yml**

```yaml
network:
  http-proxy:
    - host-name: relayer.example.com
      routes:
        - path: /
          proxy-to: relayer:9091
```

- [ ] **Step 5: Add http-proxy to spec-minio.yml**

```yaml
network:
  http-proxy:
    - host-name: minio-console.example.com
      routes:
        - path: /
          proxy-to: minio:9001
```

- [ ] **Step 6: Sanity-check YAML**

Run: `python -c "import yaml, pathlib; [yaml.safe_load(p.read_text()) for p in pathlib.Path('deployment').glob('spec-*.yml')]"`
Expected: silent.

- [ ] **Step 7: Commit**

```bash
git add deployment/spec-*.yml
git commit -m "prod specs: declare kind-mount-root + acme-email; add ingress for validator, relayer, minio"
```

---

## Task 6: `deploy_start()` passes `--perform-cluster-management`

**Files:**
- Modify: `tests/e2e/lib/deploy.py:397-403`

- [ ] **Step 1: Add the flag**

Replace the body of `deploy_start` at `tests/e2e/lib/deploy.py:397`:

```python
def deploy_start(deploy_dir: Path) -> None:
    log_info(f"Starting deployment in {deploy_dir}...")
    run_cmd([
        "laconic-so", "deployment", "--dir", str(deploy_dir),
        "start", "--perform-cluster-management",
    ])
    log_info(f"Deployment in {deploy_dir} started")
```

- [ ] **Step 2: Verify no other callers depend on the old behavior**

Run: `grep -rn "deploy_start\|--perform-cluster-management\|--skip-cluster-management" tests/e2e/`
Expected: only `deploy_start` definition + its call sites in conftest + a few fixture files. No conflicting flag usage.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/lib/deploy.py
git commit -m "tests: deploy_start passes --perform-cluster-management"
```

---

## Task 7: Rewrite `kind_cluster` fixture as `host_prep`

**Files:**
- Modify: `tests/e2e/conftest.py:275-309` (the `kind_cluster` fixture)

- [ ] **Step 1: Update imports in conftest**

Replace the existing imports at `tests/e2e/conftest.py:20-30` (around the `from .lib.cluster import ...` block) — remove `create_kind_cluster`, `install_cert_manager`, `create_selfsigned_issuer`, `install_ingress_nginx`; add `ensure_mkcert_cert`, `ensure_mkcert_installed`, `write_caddy_cert_backup`, `TEST_HOSTNAMES`.

The resulting import block should be:

```python
from .lib.cluster import (
    KIND_CLUSTER_NAME,
    TEST_HOSTNAMES,
    create_namespace,
    destroy_kind_cluster,
    ensure_hosts_entry,
    ensure_mkcert_cert,
    ensure_mkcert_installed,
    get_host_ip,
    write_caddy_cert_backup,
)
```

- [ ] **Step 2: Replace the `kind_cluster` fixture body**

Replace `tests/e2e/conftest.py:275-309` with:

```python
@pytest.fixture(scope="session")
def host_prep(
    request: pytest.FixtureRequest,
    bridge_state_root: Path,
) -> Generator[None, None, None]:
    """Host-side prep: /etc/hosts entries + mkcert cert + Caddy cert-backup.
    Cluster creation happens via SO at the first `deploy_start
    --perform-cluster-management`.
    """
    skip_setup = request.config.getoption("--skip-cluster-setup")
    skip_cleanup = request.config.getoption("--skip-cleanup")

    if not skip_setup:
        log.info("Adding test hostnames to /etc/hosts...")
        for hostname in TEST_HOSTNAMES:
            ensure_hosts_entry(hostname)

        log.info("Ensuring mkcert is installed...")
        ensure_mkcert_installed()

        log.info("Generating mkcert cert covering test hostnames...")
        cert, key = ensure_mkcert_cert(
            bridge_state_root / "local-certs", list(TEST_HOSTNAMES)
        )

        log.info("Writing Caddy cert-backup for SO to pre-load...")
        write_caddy_cert_backup(
            bridge_state_root / "caddy-cert-backup" / "caddy-secrets.yaml",
            cert, key, list(TEST_HOSTNAMES),
        )
    else:
        log.info("Skipping host prep (--skip-cluster-setup)")

    host_ip = get_host_ip()
    SPEC_REPLACEMENTS["REPLACE_HOST_IP"] = host_ip

    yield

    if not skip_cleanup and not skip_setup:
        log.info("Destroying kind cluster...")
        destroy_kind_cluster()
```

- [ ] **Step 3: Update every fixture that depends on `kind_cluster`**

Run: `grep -n "kind_cluster" tests/e2e/conftest.py`

For each match (likely 6-10 fixture parameter lists), replace `kind_cluster` with `host_prep`. Example:

```python
# Before:
def deployer_deployment(request, kind_cluster, chain_nodes, ...): ...

# After:
def deployer_deployment(request, host_prep, chain_nodes, ...): ...
```

- [ ] **Step 4: Drop `REPLACE_KIND_MOUNT_ROOT` substitution**

In `tests/e2e/conftest.py`, find and delete the line:

```python
SPEC_REPLACEMENTS["REPLACE_KIND_MOUNT_ROOT"] = str(bridge_state_root)
```

(Likely inside the old `kind_cluster` fixture; if it's outside, find via `grep -n REPLACE_KIND_MOUNT_ROOT tests/e2e/conftest.py`.)

- [ ] **Step 5: Verify import paths**

Run: `cd tests/e2e && python -c "import conftest"`
Expected: silent (no import errors).

- [ ] **Step 6: Commit**

```bash
git add tests/e2e/conftest.py
git commit -m "tests: replace kind_cluster fixture with host_prep (mkcert + cert-backup)"
```

---

## Task 8: Remove hand-rolled Ingress + cert-manager code

**Files:**
- Modify: `tests/e2e/conftest.py:1244-1301` (Ingress template constants)
- Modify: `tests/e2e/conftest.py:1485-1525` (monitoring fixture: hand-rolled Ingress + cert wait)
- Modify: `tests/e2e/conftest.py:1761-1801` (WARP_UI_INGRESS_TEMPLATE constant — find via grep, line range approximate)
- Modify: `tests/e2e/conftest.py:1897-1926` (warp_ui fixture: hand-rolled Ingress + cert wait)
- Modify: `tests/e2e/lib/cluster.py:56-141` (drop `install_cert_manager`, `create_selfsigned_issuer`, `install_ingress_nginx`, `create_kind_cluster`)

- [ ] **Step 1: Delete Ingress template constants in conftest**

In `tests/e2e/conftest.py`, find and delete:
- `GRAFANA_INGRESS_TEMPLATE = """..."""`
- `PROMETHEUS_INGRESS_TEMPLATE = """..."""`
- `WARP_UI_INGRESS_TEMPLATE = """..."""`

(Use `grep -n "INGRESS_TEMPLATE" tests/e2e/conftest.py` to find them.)

- [ ] **Step 2: Delete the monitoring fixture's hand-rolled Ingress block**

In the monitoring fixture (around line 1485-1525), delete the block that starts with `# Create ingress for Grafana and Prometheus` and ends after the cert-wait loop. Replace with one readiness probe:

```python
# Caddy serves grafana.test and prometheus.test via SO's http-proxy emission;
# the cert was pre-loaded into caddy-system at install time.
for url, health_path in [(GRAFANA_URL, "/api/health"),
                         (PROMETHEUS_URL, "/-/healthy")]:
    log.info("Waiting for %s to respond via Caddy ingress...", url)
    for _ in range(30):
        probe = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             f"{url}{health_path}"],
            capture_output=True, text=True, check=False,
        )
        if probe.returncode == 0 and probe.stdout.strip() == "200":
            break
        time.sleep(2)
    else:
        log.warning("%s not returning 200 after 60s", url)
    log.info("Ingress ready at %s", url)
```

Note: `curl -k` is no longer needed because mkcert installed the root CA into the system trust store.

- [ ] **Step 3: Delete the warp-ui fixture's hand-rolled Ingress block**

In the warp_ui fixture (around line 1897-1926), delete the block that starts with `# Create Ingress with TLS via cert-manager self-signed issuer` and ends after the readiness probe loop. Replace with:

```python
# Caddy serves bridge.test via SO's http-proxy emission.
log.info("Waiting for warp-ui to respond via Caddy ingress...")
for _ in range(30):
    probe = subprocess.run(
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
         f"{WARP_UI_URL}/"],
        capture_output=True, text=True, check=False,
    )
    if probe.returncode == 0 and probe.stdout.strip() == "200":
        break
    time.sleep(2)
else:
    log.warning("Warp UI not returning 200 after 60s")
log.info("Warp UI ingress ready at %s", WARP_UI_URL)
```

- [ ] **Step 4: Remove obsolete functions from `lib/cluster.py`**

Delete from `tests/e2e/lib/cluster.py`:
- `create_kind_cluster()` (lines 13-53)
- `install_cert_manager()` (lines 56-84)
- `create_selfsigned_issuer()` (lines 87-92)
- `install_ingress_nginx()` (lines 119-141)

Keep: `KIND_CLUSTER_NAME`, `TEST_HOSTNAMES`, `ensure_hosts_entry`, `create_namespace`, `get_host_ip`, `destroy_kind_cluster`, and the mkcert helpers added in Task 2.

- [ ] **Step 5: Run a quick import sanity check**

Run: `cd tests/e2e && python -c "import conftest"`
Expected: silent.

- [ ] **Step 6: Commit**

```bash
git add tests/e2e/conftest.py tests/e2e/lib/cluster.py
git commit -m "tests: drop hand-rolled Ingress + cert-manager + nginx code paths"
```

---

## Task 9: Delete obsolete fixture files

**Files:**
- Delete: `tests/e2e/fixtures/kind-config.yaml`
- Delete: `tests/e2e/fixtures/cert-manager-issuer.yaml`

- [ ] **Step 1: Delete kind-config.yaml**

```bash
git rm tests/e2e/fixtures/kind-config.yaml
```

SO generates its own kind-config from `kind-mount-root` and `network.http-proxy:` declarations in the spec at `deploy_start --perform-cluster-management` time.

- [ ] **Step 2: Delete cert-manager-issuer.yaml**

```bash
git rm tests/e2e/fixtures/cert-manager-issuer.yaml
```

No cert-manager in the new architecture; Caddy serves TLS autonomously.

- [ ] **Step 3: Commit**

```bash
git commit -m "tests: drop kind-config and cert-manager-issuer fixtures (SO-managed now)"
```

---

## Task 10: Add Ingress endpoint probe tests

**Files:**
- Create: `tests/e2e/test_ingress_endpoints.py`

One probe test per Ingress URL. These run after the relevant fixture is up — they parametrize fixture dependencies via pytest's fixture system so each probe waits for its stack.

- [ ] **Step 1: Create the test file**

Create `tests/e2e/test_ingress_endpoints.py`:

```python
"""Probe each Ingress URL to catch http-proxy: drift or Caddy misconfig.

These don't replace functional tests (grafana dashboards, prometheus queries,
warp-ui Playwright) — they're the safety net for Ingress wiring itself.
"""
from __future__ import annotations

import subprocess
import time

import pytest


def _wait_for_https(url: str, expected_status: int = 200, timeout: int = 60) -> int:
    """Probe url every 2s until expected_status arrives or timeout elapses.
    Returns the last observed HTTP status code."""
    deadline = time.time() + timeout
    last = -1
    while time.time() < deadline:
        result = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", url],
            capture_output=True, text=True, check=False,
        )
        if result.returncode == 0:
            try:
                last = int(result.stdout.strip())
            except ValueError:
                last = -1
            if last == expected_status:
                return last
        time.sleep(2)
    return last


def test_warp_ui_ingress(warp_ui_deployment):
    status = _wait_for_https("https://bridge.test/")
    assert status == 200, f"warp-ui Ingress not serving 200 (got {status})"


def test_grafana_ingress(monitoring_deployment):
    status = _wait_for_https("https://grafana.test/api/health")
    assert status == 200, f"grafana Ingress not serving 200 (got {status})"


def test_prometheus_ingress(monitoring_deployment):
    status = _wait_for_https("https://prometheus.test/-/healthy")
    assert status == 200, f"prometheus Ingress not serving 200 (got {status})"


def test_validator_gorchain_metrics_ingress(validator_gorchain_deployment):
    status = _wait_for_https("https://validator-gorchain.test/metrics")
    assert status == 200, (
        f"validator-gorchain metrics Ingress not serving 200 (got {status})"
    )


def test_validator_solana_metrics_ingress(validator_solana_deployment):
    status = _wait_for_https("https://validator-solana.test/metrics")
    assert status == 200, (
        f"validator-solana metrics Ingress not serving 200 (got {status})"
    )


def test_relayer_metrics_ingress(relayer_deployment):
    status = _wait_for_https("https://relayer.test/metrics")
    assert status == 200, f"relayer metrics Ingress not serving 200 (got {status})"


def test_minio_console_ingress(minio_deployment):
    status = _wait_for_https("https://minio-console.test/minio/health/cluster")
    assert status == 200, f"minio console Ingress not serving 200 (got {status})"
```

- [ ] **Step 2: Verify fixture names exist in conftest**

Run: `grep -n "def warp_ui_deployment\|def monitoring_deployment\|def validator_gorchain_deployment\|def validator_solana_deployment\|def relayer_deployment\|def minio_deployment" tests/e2e/conftest.py`
Expected: all 6 fixture functions found. If any are named differently (e.g. `warp_ui` vs `warp_ui_deployment`), adjust the test file's parameter names to match exactly.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/test_ingress_endpoints.py
git commit -m "tests: probe each Ingress URL to catch http-proxy drift"
```

---

## Task 11: Update CI workflow with mkcert install

**Files:**
- Modify: `.github/workflows/e2e.yml`

- [ ] **Step 1: Add mkcert install step**

In `.github/workflows/e2e.yml`, insert immediately after the "Install Foundry (cast)" step (around line 72) and before "Run E2E tests":

```yaml
      - name: Install mkcert
        run: |
          sudo apt-get update
          sudo apt-get install -y libnss3-tools
          curl -L -o /tmp/mkcert \
            https://github.com/FiloSottile/mkcert/releases/download/v1.4.4/mkcert-v1.4.4-linux-amd64
          sudo install /tmp/mkcert /usr/local/bin/mkcert
          mkcert -install
```

- [ ] **Step 2: Sanity-check YAML**

Run: `python -c "import yaml; yaml.safe_load(open('.github/workflows/e2e.yml').read())"`
Expected: silent.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/e2e.yml
git commit -m "ci: install mkcert and trust its root CA before running e2e"
```

---

## Task 12: Update tests/e2e/README.md

**Files:**
- Modify: `tests/e2e/README.md`

- [ ] **Step 1: Add "First-time machine setup" section**

Insert near the top of `tests/e2e/README.md`, after the title/intro paragraph and before any existing "Running tests" section:

```markdown
## First-time machine setup

These tests need `mkcert` to generate browser-trusted TLS certificates for the
`*.test` hostnames Caddy serves. `mkcert -install` installs a root CA into your
system + browser trust stores so `curl` / Playwright / Python `requests` don't
need to disable cert verification.

```bash
# Linux (Ubuntu/Debian):
sudo apt-get install -y libnss3-tools
curl -L -o /tmp/mkcert \
  https://github.com/FiloSottile/mkcert/releases/download/v1.4.4/mkcert-v1.4.4-linux-amd64
sudo install /tmp/mkcert /usr/local/bin/mkcert

# macOS:
brew install mkcert nss

# One-time CA install (both platforms):
mkcert -install
```

Remove with `mkcert -uninstall` later if needed; the generated cert files persist
under the test state directory and are wiped on full teardown.
```

- [ ] **Step 2: Rewrite the TLS section**

Find the existing TLS / cert-manager passage (search for `letsencrypt-prod` or `cert-manager` — around lines 180-200 per spec). Replace the whole passage with:

```markdown
## TLS in tests

Tests serve TLS via Caddy (same as prod). At session start the `host_prep`
fixture:

1. Generates one multi-SAN cert with mkcert covering all `*.test` hostnames at
   `<BRIDGE_STATE_ROOT>/local-certs/hyperlane.test.{crt,key}`.
2. Writes a `caddy-secrets.yaml` to
   `<BRIDGE_STATE_ROOT>/caddy-cert-backup/caddy-secrets.yaml` — one k8s Secret
   per hostname at the fake-ACME path Caddy uses for its `secret_store`.

When the first `deploy_start --perform-cluster-management` fires, SO creates
the kind cluster and runs `install_ingress_for_kind`. Phase 2 of that install
(`_restore_caddy_certs`) reads our `caddy-secrets.yaml` and creates the
Secrets before Caddy starts. Caddy then serves them at request time without
ever attempting ACME.

No cert-manager. No nginx-ingress. No `ClusterIssuer`. The TLS path matches
prod (Caddy + ACME-shaped flow); only the cert source differs (mkcert in dev,
Let's Encrypt in prod).
```

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/README.md
git commit -m "docs: README first-time mkcert setup + new TLS section"
```

---

## Task 13: Update architecture-decisions.md

**Files:**
- Modify: `docs/architecture-decisions.md:562-595` (Kind Cluster Management section)
- Modify: `docs/architecture-decisions.md` (insert Multi-Machine Prod Principle section)
- Modify: `docs/architecture-decisions.md` (Artifact Passing section, append one note)

- [ ] **Step 1: Rewrite the Kind Cluster Management section**

In `docs/architecture-decisions.md`, replace the entire "## Kind Cluster Management" section (currently lines 562-595, ending before "## Warp Route Token") with:

```markdown
## Kind Cluster Management

**Decision (2026-05-21, supersedes the 2026-05-20 bypass):** SO owns kind
cluster lifecycle. Every `deploy_start` for a k8s-kind deployment passes
`--perform-cluster-management`. SO's `create_cluster()` is single-cluster-per-host
with reuse semantics, so whichever stack starts first on a host creates the
cluster + installs the Caddy ingress controller; subsequent stacks no-op at
the cluster level and proceed straight to their own k8s resources.

Three consequences flow from this:

1. **Every long-running stack spec declares `kind-mount-root` and `acme-email`.**
   Any stack can be first on its host — particularly true in multi-machine
   prod where ansible fans stacks out across machines. See the "Multi-Machine
   Prod Principle" section below.

2. **TLS termination is Caddy's job.** SO's `cluster_info.get_ingress()`
   correctly skips emitting a `tls:` block on Kind (`deploy_k8s.py:916`) —
   Caddy handles cert provisioning autonomously from the host names in
   Ingress objects with class `caddy`. In prod, Caddy uses ACME via
   `acme-email`. In dev tests, mkcert-generated certs are pre-loaded into
   Caddy's `secret_store` at the fake-ACME path
   (`<kind_mount_root>/caddy-cert-backup/caddy-secrets.yaml`); SO's
   `_restore_caddy_certs()` loads them before Caddy starts, so Caddy serves
   them without calling Let's Encrypt.

3. **Cert backups are per-host.** SO's auto-installed `caddy-cert-backup`
   CronJob writes the current Caddy secret_store to
   `<kind_mount_root>/caddy-cert-backup/` periodically, so restarting the
   cluster restores certs (no re-ACME). In multi-machine prod, each host
   maintains its own backup.

No cert-manager. No nginx-ingress. No hand-rolled Ingress in test code.
```

- [ ] **Step 2: Insert Multi-Machine Prod Principle section**

In `docs/architecture-decisions.md`, immediately after the "Kind Cluster Management" section (i.e. before "## Warp Route Token"), insert:

```markdown
## Multi-Machine Prod Principle

**Decision:** Every long-running stack spec is self-sufficient enough to
bootstrap on its own host. No spec assumes "some other stack ran here first".

**Why:** Production fans stacks out across machines via ansible (see PR3 plan
in the bridge-state design doc). On each host, whichever stack runs first
triggers cluster creation + Caddy install via SO's `--perform-cluster-management`
path. If a stack's spec is incomplete on the assumption that another stack
would already be present, the deployment fails on hosts where it lands alone.

**Concrete applications:**

- Every long-running spec declares `kind-mount-root` and `acme-email`.
- Every spec with externally-reachable HTTP endpoints declares
  `network.http-proxy:`.
- Cross-stack artifacts (deployer-produced state files, mkcert certs, cert
  backups) live on disk under `kind-mount-root` and are populated by the
  fixture (dev) or ansible (prod) — never assumed to come from a peer pod in
  the same cluster.

This principle is what makes the bridge-state distribution model
(`docs/superpowers/specs/2026-05-20-bridge-state-extract-and-distribution-design.md`)
sensible: each host's stacks read their inputs from local disk, not from peer
namespaces.

---
```

- [ ] **Step 3: Append note to Artifact Passing section**

In `docs/architecture-decisions.md`, find the "## Artifact Passing (Deployer → Agents)" section. At the end of its bullet list (before the next `---` separator or section heading), append:

```markdown
The `kind-mount-root` umbrella mount also hosts Caddy's cert backup
(`<kind_mount_root>/caddy-cert-backup/`), making it the single per-host
directory for everything stateful — bridge state, warp deploy outputs, and
TLS material.
```

- [ ] **Step 4: Commit**

```bash
git add docs/architecture-decisions.md
git commit -m "docs: update architecture-decisions for SO-managed cluster + multi-machine principle"
```

---

## Task 14: Full end-to-end test run

**Files:** none modified.

- [ ] **Step 1: Verify upstream cluster is clean**

Run: `kind delete cluster --name hyperlane 2>&1 || true && kind delete cluster --name hyperlane-e2e 2>&1 || true`
Expected: clean state, no clusters remain (or "cluster does not exist" messages).

- [ ] **Step 2: Wipe state root**

Run: `sudo rm -rf /tmp/hyperlane-bridge-e2e`
Expected: silent.

- [ ] **Step 3: Run the full test suite**

Run: `cd tests/e2e && pytest -v -x`
Expected: all tests pass, including the 7 new probe tests in `test_ingress_endpoints.py`.

If any test fails:
- For probe-test failures: `kubectl get ingress -A` and check the relevant stack's `http-proxy:` block matches the spec's compose service name + port.
- For cluster-creation failures: `kubectl cluster-info --context kind-hyperlane` and check `kind-mount-root` is consistent across specs.
- For TLS errors: verify `mkcert -CAROOT` shows a populated CA, and `/etc/hosts` has all 7 test hostnames.

- [ ] **Step 4: (No commit if all green — task is verification only)**

If the suite passes, the PR is ready for review. If anything fails, fix on a new commit and re-run.

---

## Self-Review Notes

After this plan was written, the author ran the standard self-review:

1. **Spec coverage:** Each of the 12 implementation-order items from the design spec maps to one or two tasks here. Specifically:
   - Spec step 1 (`kind-mount-root` + `acme-email` on test specs) → Task 3
   - Spec step 2 (`http-proxy:` on test specs) → Task 4
   - Spec step 3 (prod spec mirror) → Task 5
   - Spec step 4 (mkcert helpers) → Task 2 (with `KIND_CLUSTER_NAME` + `TEST_HOSTNAMES` carved out into Task 1 for cleaner commits)
   - Spec step 5 (fixture rename) → Task 7
   - Spec step 6 (`--perform-cluster-management`) → Task 6
   - Spec step 7 (drop cert-manager + nginx + hand-rolled Ingress) → Task 8
   - Spec step 8 (delete obsolete fixture files) → Task 9
   - Spec step 9 (probe tests) → Task 10
   - Spec step 10 (CI workflow) → Task 11
   - Spec step 11 (README) → Task 12
   - Spec step 12 (architecture-decisions) → Task 13
   - Plus Task 14 as a final integration check.

2. **Placeholder scan:** No `TBD`/`TODO`/"add appropriate handling" placeholders found in the task bodies. All code blocks are concrete.

3. **Type consistency:** `KIND_CLUSTER_NAME`, `TEST_HOSTNAMES`, `ensure_mkcert_installed`, `ensure_mkcert_cert`, `write_caddy_cert_backup` names are used identically in Task 1, Task 2, Task 7, and Task 8. Hostname list (`bridge.test`, `grafana.test`, `prometheus.test`, `validator-gorchain.test`, `validator-solana.test`, `relayer.test`, `minio-console.test`) appears consistently in Task 1 (constant), Task 4 (test fixture http-proxy), and Task 10 (probe tests).
