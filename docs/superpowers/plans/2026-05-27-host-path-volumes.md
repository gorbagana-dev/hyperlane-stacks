# Host-Path Volumes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace dynamic PVCs with host-path volumes for all hyperlane stacks so data persists across cluster recreations.

**Architecture:** All stacks share a single `kind-mount-root: /srv/kind/hyperlane`. Each stack's data volume maps to a named subdir within that tree (`bridge/generated`, `minio/data`, etc.). Tests mirror the same layout under `/tmp/hyperlane-bridge-e2e/`. The `conftest.py` `bridge_state_root` fixture pre-creates every subdir so the cluster can bind-mount them immediately at deploy time.

**Tech Stack:** YAML spec files, pytest fixtures (Python), Markdown docs.

---

## File Map

| File | Change |
|---|---|
| `deployment/spec-deployer.yml` | `kind-mount-root` + volume paths |
| `deployment/spec-warp-deployer.yml` | `kind-mount-root` + volume paths |
| `deployment/spec-minio.yml` | `kind-mount-root` + volume path |
| `deployment/spec-validator-gorchain.yml` | `kind-mount-root` + volume path |
| `deployment/spec-validator-solana.yml` | `kind-mount-root` + volume path |
| `deployment/spec-relayer.yml` | `kind-mount-root` + volume path |
| `deployment/spec-monitoring.yml` | `kind-mount-root` + volume paths |
| `deployment/spec-gas-oracle.yml` | `kind-mount-root` only (no data volumes) |
| `deployment/spec-warp-ui.yml` | `kind-mount-root` only (no data volumes) |
| `tests/e2e/fixtures/test-spec-deployer.yml` | volume paths (`REPLACE_KIND_MOUNT_ROOT/bridge/…`) |
| `tests/e2e/fixtures/test-spec-warp-deployer.yml` | volume paths (`REPLACE_KIND_MOUNT_ROOT/bridge/…`) |
| `tests/e2e/fixtures/test-spec-minio.yml` | volume path |
| `tests/e2e/fixtures/test-spec-validator-gorchain.yml` | volume path |
| `tests/e2e/fixtures/test-spec-validator-solana.yml` | volume path |
| `tests/e2e/fixtures/test-spec-relayer.yml` | volume path |
| `tests/e2e/fixtures/test-spec-monitoring.yml` | volume paths |
| `tests/e2e/conftest.py` | `bridge_state_root` fixture + `bridge_state_dir` + `bridge_state_logs_dir` |
| `specs/stack-specifications.md` | storage description at lines 19, 422, 458 |

---

### Task 1: Update prod specs — deployers

**Files:**
- Modify: `deployment/spec-deployer.yml`
- Modify: `deployment/spec-warp-deployer.yml`

Both deployers share the same bridge state dir (warp-deployer reads files written by svm-deployer).

- [ ] **Step 1: Edit `deployment/spec-deployer.yml`**

Change `kind-mount-root` and both volume paths:

```yaml
kind-mount-root: /srv/kind/hyperlane
# ...
volumes:
  bridge-state: /srv/kind/hyperlane/bridge/generated
  bridge-logs: /srv/kind/hyperlane/bridge/logs
```

- [ ] **Step 2: Edit `deployment/spec-warp-deployer.yml`**

Same values (shared bridge state dir):

```yaml
kind-mount-root: /srv/kind/hyperlane
# ...
volumes:
  bridge-state: /srv/kind/hyperlane/bridge/generated
  bridge-logs: /srv/kind/hyperlane/bridge/logs
```

- [ ] **Step 3: Verify**

```bash
grep -h "kind-mount-root\|bridge-state\|bridge-logs" \
  deployment/spec-deployer.yml deployment/spec-warp-deployer.yml
```

Expected output (both files, same values):
```
kind-mount-root: /srv/kind/hyperlane
  bridge-state: /srv/kind/hyperlane/bridge/generated
  bridge-logs: /srv/kind/hyperlane/bridge/logs
kind-mount-root: /srv/kind/hyperlane
  bridge-state: /srv/kind/hyperlane/bridge/generated
  bridge-logs: /srv/kind/hyperlane/bridge/logs
```

- [ ] **Step 4: Commit**

```bash
git add deployment/spec-deployer.yml deployment/spec-warp-deployer.yml
git commit -m "feat(specs): move deployer volumes to /srv/kind/hyperlane/bridge/"
```

---

### Task 2: Update prod specs — long-running stacks

**Files:**
- Modify: `deployment/spec-minio.yml`
- Modify: `deployment/spec-validator-gorchain.yml`
- Modify: `deployment/spec-validator-solana.yml`
- Modify: `deployment/spec-relayer.yml`
- Modify: `deployment/spec-monitoring.yml`
- Modify: `deployment/spec-gas-oracle.yml`
- Modify: `deployment/spec-warp-ui.yml`

- [ ] **Step 1: Edit `deployment/spec-minio.yml`**

```yaml
kind-mount-root: /srv/kind/hyperlane
# ...
volumes:
  minio-data: /srv/kind/hyperlane/minio/data
```

- [ ] **Step 2: Edit `deployment/spec-validator-gorchain.yml`**

```yaml
kind-mount-root: /srv/kind/hyperlane
# ...
volumes:
  validator-data: /srv/kind/hyperlane/validator-gorchain/data
```

- [ ] **Step 3: Edit `deployment/spec-validator-solana.yml`**

```yaml
kind-mount-root: /srv/kind/hyperlane
# ...
volumes:
  validator-data: /srv/kind/hyperlane/validator-solana/data
```

- [ ] **Step 4: Edit `deployment/spec-relayer.yml`**

```yaml
kind-mount-root: /srv/kind/hyperlane
# ...
volumes:
  relayer-data: /srv/kind/hyperlane/relayer/data
```

- [ ] **Step 5: Edit `deployment/spec-monitoring.yml`**

```yaml
kind-mount-root: /srv/kind/hyperlane
# ...
volumes:
  prometheus-data: /srv/kind/hyperlane/monitoring/prometheus
  grafana-data: /srv/kind/hyperlane/monitoring/grafana
```

- [ ] **Step 6: Edit `deployment/spec-gas-oracle.yml` and `deployment/spec-warp-ui.yml`**

These have no data volumes — only update `kind-mount-root`:

```yaml
kind-mount-root: /srv/kind/hyperlane
```

- [ ] **Step 7: Verify all prod specs**

```bash
grep "kind-mount-root" deployment/spec-*.yml
```

Expected: every file shows `kind-mount-root: /srv/kind/hyperlane`.

```bash
grep -h "minio-data\|validator-data\|relayer-data\|prometheus-data\|grafana-data" \
  deployment/spec-*.yml
```

Expected:
```
  minio-data: /srv/kind/hyperlane/minio/data
  validator-data: /srv/kind/hyperlane/validator-gorchain/data
  validator-data: /srv/kind/hyperlane/validator-solana/data
  relayer-data: /srv/kind/hyperlane/relayer/data
  prometheus-data: /srv/kind/hyperlane/monitoring/prometheus
  grafana-data: /srv/kind/hyperlane/monitoring/grafana
```

- [ ] **Step 8: Commit**

```bash
git add deployment/spec-minio.yml deployment/spec-validator-gorchain.yml \
        deployment/spec-validator-solana.yml deployment/spec-relayer.yml \
        deployment/spec-monitoring.yml deployment/spec-gas-oracle.yml \
        deployment/spec-warp-ui.yml
git commit -m "feat(specs): map all data volumes to /srv/kind/hyperlane host paths"
```

---

### Task 3: Update test fixtures — deployers

**Files:**
- Modify: `tests/e2e/fixtures/test-spec-deployer.yml`
- Modify: `tests/e2e/fixtures/test-spec-warp-deployer.yml`

`kind-mount-root` stays `/tmp/hyperlane-bridge-e2e`. The volume paths use the `REPLACE_KIND_MOUNT_ROOT` placeholder (substituted at test runtime by conftest), so only the suffix changes — from `/generated` to `/bridge/generated`.

- [ ] **Step 1: Edit `tests/e2e/fixtures/test-spec-deployer.yml`**

```yaml
volumes:
  bridge-state: REPLACE_KIND_MOUNT_ROOT/bridge/generated
  bridge-logs: REPLACE_KIND_MOUNT_ROOT/bridge/logs
```

- [ ] **Step 2: Edit `tests/e2e/fixtures/test-spec-warp-deployer.yml`**

```yaml
volumes:
  bridge-state: REPLACE_KIND_MOUNT_ROOT/bridge/generated
  bridge-logs: REPLACE_KIND_MOUNT_ROOT/bridge/logs
```

- [ ] **Step 3: Verify**

```bash
grep "bridge-state\|bridge-logs" \
  tests/e2e/fixtures/test-spec-deployer.yml \
  tests/e2e/fixtures/test-spec-warp-deployer.yml
```

Expected:
```
tests/e2e/fixtures/test-spec-deployer.yml:  bridge-state: REPLACE_KIND_MOUNT_ROOT/bridge/generated
tests/e2e/fixtures/test-spec-deployer.yml:  bridge-logs: REPLACE_KIND_MOUNT_ROOT/bridge/logs
tests/e2e/fixtures/test-spec-warp-deployer.yml:  bridge-state: REPLACE_KIND_MOUNT_ROOT/bridge/generated
tests/e2e/fixtures/test-spec-warp-deployer.yml:  bridge-logs: REPLACE_KIND_MOUNT_ROOT/bridge/logs
```

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/fixtures/test-spec-deployer.yml \
        tests/e2e/fixtures/test-spec-warp-deployer.yml
git commit -m "feat(fixtures): move deployer volume paths to bridge/ subdir"
```

---

### Task 4: Update test fixtures — long-running stacks

**Files:**
- Modify: `tests/e2e/fixtures/test-spec-minio.yml`
- Modify: `tests/e2e/fixtures/test-spec-validator-gorchain.yml`
- Modify: `tests/e2e/fixtures/test-spec-validator-solana.yml`
- Modify: `tests/e2e/fixtures/test-spec-relayer.yml`
- Modify: `tests/e2e/fixtures/test-spec-monitoring.yml`

These fixtures use literal `/tmp/hyperlane-bridge-e2e/…` paths (not the `REPLACE_KIND_MOUNT_ROOT` placeholder — only deployer fixtures use that).

- [ ] **Step 1: Edit `tests/e2e/fixtures/test-spec-minio.yml`**

```yaml
volumes:
  minio-data: /tmp/hyperlane-bridge-e2e/minio/data
```

- [ ] **Step 2: Edit `tests/e2e/fixtures/test-spec-validator-gorchain.yml`**

```yaml
volumes:
  validator-data: /tmp/hyperlane-bridge-e2e/validator-gorchain/data
```

- [ ] **Step 3: Edit `tests/e2e/fixtures/test-spec-validator-solana.yml`**

```yaml
volumes:
  validator-data: /tmp/hyperlane-bridge-e2e/validator-solana/data
```

- [ ] **Step 4: Edit `tests/e2e/fixtures/test-spec-relayer.yml`**

```yaml
volumes:
  relayer-data: /tmp/hyperlane-bridge-e2e/relayer/data
```

- [ ] **Step 5: Edit `tests/e2e/fixtures/test-spec-monitoring.yml`**

```yaml
volumes:
  prometheus-data: /tmp/hyperlane-bridge-e2e/monitoring/prometheus
  grafana-data: /tmp/hyperlane-bridge-e2e/monitoring/grafana
```

- [ ] **Step 6: Verify**

```bash
grep -h "minio-data\|validator-data\|relayer-data\|prometheus-data\|grafana-data" \
  tests/e2e/fixtures/test-spec-*.yml
```

Expected:
```
  minio-data: /tmp/hyperlane-bridge-e2e/minio/data
  validator-data: /tmp/hyperlane-bridge-e2e/validator-gorchain/data
  validator-data: /tmp/hyperlane-bridge-e2e/validator-solana/data
  relayer-data: /tmp/hyperlane-bridge-e2e/relayer/data
  prometheus-data: /tmp/hyperlane-bridge-e2e/monitoring/prometheus
  grafana-data: /tmp/hyperlane-bridge-e2e/monitoring/grafana
```

- [ ] **Step 7: Commit**

```bash
git add tests/e2e/fixtures/test-spec-minio.yml \
        tests/e2e/fixtures/test-spec-validator-gorchain.yml \
        tests/e2e/fixtures/test-spec-validator-solana.yml \
        tests/e2e/fixtures/test-spec-relayer.yml \
        tests/e2e/fixtures/test-spec-monitoring.yml
git commit -m "feat(fixtures): add host-path volume mappings for long-running stacks"
```

---

### Task 5: Update `conftest.py` — fixtures and pre-creation

**Files:**
- Modify: `tests/e2e/conftest.py:141-172`

Three things change:
1. `bridge_state_root` docstring — mentions `generated/` and `logs/` subdirs at root
2. `bridge_state_root` body — pre-creates `generated/` and `logs/` directly under root; now pre-creates every stack's data dir too, and bridge state moves to `bridge/` subdir
3. `bridge_state_dir` — was `root / "generated"`, becomes `root / "bridge" / "generated"`
4. `bridge_state_logs_dir` — was `root / "logs"`, becomes `root / "bridge" / "logs"`

- [ ] **Step 1: Replace the three fixtures in `tests/e2e/conftest.py`**

Find this block (lines 141–172):

```python
@pytest.fixture(scope="session")
def bridge_state_root(request: pytest.FixtureRequest) -> Generator[Path, None, None]:
    """Kind umbrella root. SO emits a single extraMount (hostPath=this →
    containerPath=/mnt). Deployer Jobs write to subdirs (generated/, logs/)
    which become writable hostPath PVs inside the cluster.

    Lifecycle is paired with the kind cluster: removed at session teardown
    unless --skip-cleanup or --skip-cluster-setup is set, so a kept cluster
    keeps its state and a fresh cluster always starts with fresh state."""
    p = BRIDGE_STATE_ROOT
    p.mkdir(parents=True, exist_ok=True)
    (p / "generated").mkdir(exist_ok=True)
    (p / "logs").mkdir(exist_ok=True)
    log.info("Bridge state root for this session: %s", p)
    yield p
    skip_setup = request.config.getoption("--skip-cluster-setup")
    skip_cleanup = request.config.getoption("--skip-cleanup")
    if not skip_cleanup and not skip_setup:
        # Deployer containers run as root and write root-owned files into
        # this dir, so plain rmtree fails — force_rmtree falls back to sudo.
        log.info("Removing bridge state root: %s", p)
        force_rmtree(p)


@pytest.fixture(scope="session")
def bridge_state_dir(bridge_state_root: Path) -> Path:
    return bridge_state_root / "generated"


@pytest.fixture(scope="session")
def bridge_state_logs_dir(bridge_state_root: Path) -> Path:
    return bridge_state_root / "logs"
```

Replace with:

```python
@pytest.fixture(scope="session")
def bridge_state_root(request: pytest.FixtureRequest) -> Generator[Path, None, None]:
    """Kind umbrella root. SO emits a single extraMount (hostPath=this →
    containerPath=/mnt). All stack data volumes are subdirs of this root:
      bridge/generated/  — deployer output (program-ids.json, etc.)
      bridge/logs/       — deployer job logs
      minio/data/        — MinIO object store
      validator-gorchain/data/
      validator-solana/data/
      relayer/data/
      monitoring/prometheus/
      monitoring/grafana/

    Lifecycle is paired with the kind cluster: removed at session teardown
    unless --skip-cleanup or --skip-cluster-setup is set, so a kept cluster
    keeps its state and a fresh cluster always starts with fresh state."""
    p = BRIDGE_STATE_ROOT
    p.mkdir(parents=True, exist_ok=True)
    for subdir in [
        "bridge/generated",
        "bridge/logs",
        "minio/data",
        "validator-gorchain/data",
        "validator-solana/data",
        "relayer/data",
        "monitoring/prometheus",
        "monitoring/grafana",
    ]:
        (p / subdir).mkdir(parents=True, exist_ok=True)
    log.info("Bridge state root for this session: %s", p)
    yield p
    skip_setup = request.config.getoption("--skip-cluster-setup")
    skip_cleanup = request.config.getoption("--skip-cleanup")
    if not skip_cleanup and not skip_setup:
        # Deployer containers run as root and write root-owned files into
        # this dir, so plain rmtree fails — force_rmtree falls back to sudo.
        log.info("Removing bridge state root: %s", p)
        force_rmtree(p)


@pytest.fixture(scope="session")
def bridge_state_dir(bridge_state_root: Path) -> Path:
    return bridge_state_root / "bridge" / "generated"


@pytest.fixture(scope="session")
def bridge_state_logs_dir(bridge_state_root: Path) -> Path:
    return bridge_state_root / "bridge" / "logs"
```

- [ ] **Step 2: Verify the fixtures read back correctly**

```bash
grep -n "bridge/generated\|bridge/logs\|minio/data\|validator-gorchain\|validator-solana\|relayer/data\|monitoring" \
  tests/e2e/conftest.py | head -20
```

Expected: lines showing all 8 subdirs in the loop, plus `bridge_state_dir` and `bridge_state_logs_dir` returning `bridge/generated` and `bridge/logs`.

- [ ] **Step 3: Run the linter to catch any syntax errors**

```bash
cd tests/e2e && python -m ruff check conftest.py
```

Expected: no output (clean).

- [ ] **Step 4: Smoke-test the fixture by importing conftest**

```bash
cd tests/e2e && python -c "import conftest; print('OK')"
```

Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/conftest.py
git commit -m "feat(conftest): pre-create all stack data dirs under bridge_state_root

bridge_state_dir and bridge_state_logs_dir now return bridge/generated
and bridge/logs respectively, matching the new spec volume layout."
```

---

### Task 6: Update `specs/stack-specifications.md`

**Files:**
- Modify: `specs/stack-specifications.md:19`
- Modify: `specs/stack-specifications.md:422`
- Modify: `specs/stack-specifications.md:458`

- [ ] **Step 1: Update line 19** — the spec.yml field description

Find:
```
- `volumes:` — Named volumes → PVCs; path volumes → HostPath mounts
```

Replace with:
```
- `volumes:` — Named volumes with explicit host paths → HostPath PVs under `/srv/kind/hyperlane/`; empty value → dynamic PVC (avoid for data that must survive cluster recreation)
```

- [ ] **Step 2: Update line 422** — the deployment conventions section

Find:
```
- **`volumes:`** — Named volumes with explicit sizes. Data volumes → PVCs.
```

Replace with:
```
- **`volumes:`** — Data volumes map to explicit host paths under `/srv/kind/hyperlane/<stack>/` (see layout below). This makes data survive cluster recreation. Do not leave data volume values empty — that produces a dynamic PVC which is lost on cluster delete.
```

- [ ] **Step 3: Update line 458** — compose conventions section

Find:
```
- **Volumes**: Named volumes with `config` in the name → ConfigMaps in k8s. Other named volumes → PVCs.
```

Replace with:
```
- **Volumes**: Named volumes with `config` in the name → ConfigMaps in k8s. Other named volumes → host-path PVs (path set in the spec file under `volumes:`).
```

- [ ] **Step 4: Add a host-path layout section** after line 432 (the Volume Sizes table)

Find the end of the Volume Sizes table and add after it:

```markdown
### Host-Path Layout

All stacks share `kind-mount-root: /srv/kind/hyperlane`. Kind mounts this directory from the host into the cluster node; every data volume is a named subdir:

```
/srv/kind/hyperlane/
  bridge/
    generated/          ← svm-deployer output (program-ids.json, etc.)
    logs/               ← deployer job logs
  minio/
    data/               ← MinIO object store
  validator-gorchain/
    data/               ← gorchain validator state
  validator-solana/
    data/               ← solana validator state
  relayer/
    data/               ← relayer state
  monitoring/
    prometheus/         ← Prometheus TSDB
    grafana/            ← Grafana database
```

Tests use the same layout under `/tmp/hyperlane-bridge-e2e/`.
```

- [ ] **Step 5: Verify the doc changes look right**

```bash
grep -n "PVC\|host.path\|HostPath\|/srv/kind" specs/stack-specifications.md
```

Expected: no remaining references to "→ PVCs" for data volumes; all mentions of storage point to `/srv/kind/hyperlane`.

- [ ] **Step 6: Commit**

```bash
git add specs/stack-specifications.md
git commit -m "docs(specs): document host-path volume layout under /srv/kind/hyperlane"
```
