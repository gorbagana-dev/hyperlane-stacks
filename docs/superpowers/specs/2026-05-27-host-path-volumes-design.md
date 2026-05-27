# Host-Path Volumes Under `/srv/kind/hyperlane` — Design

> **For agentic workers:** Use `superpowers:writing-plans` to turn this into a task-by-task implementation plan.

**Goal:** Replace dynamic PVCs with host-path volumes for all hyperlane stacks, with all data living under a single `/srv/kind/hyperlane` tree on the host machine.

**Why:** Dynamic PVCs (StorageClass `standard`) live inside the Kind node container and are lost on cluster recreation. Host-path volumes survive cluster recreation and can be backed up at the OS level.

---

## Directory Layout

All stacks share one `kind-mount-root`. Kind mounts `/srv/kind/hyperlane` from the host into the cluster node once; every stack's PV is a path within that tree.

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

Tests use the same structure under `/tmp/hyperlane-bridge-e2e/`.

---

## Files Changed

### 1. Prod specs — `deployment/spec-*.yml` (7 files)

All specs get `kind-mount-root: /srv/kind/hyperlane`.

| Spec | Volume(s) | Host path |
|---|---|---|
| `spec-deployer.yml` | `bridge-state` | `/srv/kind/hyperlane/bridge/generated` |
| `spec-deployer.yml` | `bridge-logs` | `/srv/kind/hyperlane/bridge/logs` |
| `spec-warp-deployer.yml` | `bridge-state` | `/srv/kind/hyperlane/bridge/generated` |
| `spec-warp-deployer.yml` | `bridge-logs` | `/srv/kind/hyperlane/bridge/logs` |
| `spec-minio.yml` | `minio-data` | `/srv/kind/hyperlane/minio/data` |
| `spec-validator-gorchain.yml` | `validator-data` | `/srv/kind/hyperlane/validator-gorchain/data` |
| `spec-validator-solana.yml` | `validator-data` | `/srv/kind/hyperlane/validator-solana/data` |
| `spec-relayer.yml` | `relayer-data` | `/srv/kind/hyperlane/relayer/data` |
| `spec-monitoring.yml` | `prometheus-data` | `/srv/kind/hyperlane/monitoring/prometheus` |
| `spec-monitoring.yml` | `grafana-data` | `/srv/kind/hyperlane/monitoring/grafana` |

`spec-gas-oracle.yml` and `spec-warp-ui.yml` have no data volumes — only `kind-mount-root` changes.

### 2. Test fixtures — `tests/e2e/fixtures/test-spec-*.yml` (9 files)

`kind-mount-root` stays `/tmp/hyperlane-bridge-e2e` (already correct). Data volume paths added:

| Fixture | Volume(s) | Path |
|---|---|---|
| `test-spec-deployer.yml` | `bridge-state` | `REPLACE_KIND_MOUNT_ROOT/bridge/generated` |
| `test-spec-deployer.yml` | `bridge-logs` | `REPLACE_KIND_MOUNT_ROOT/bridge/logs` |
| `test-spec-warp-deployer.yml` | `bridge-state` | `REPLACE_KIND_MOUNT_ROOT/bridge/generated` |
| `test-spec-warp-deployer.yml` | `bridge-logs` | `REPLACE_KIND_MOUNT_ROOT/bridge/logs` |
| `test-spec-minio.yml` | `minio-data` | `/tmp/hyperlane-bridge-e2e/minio/data` |
| `test-spec-validator-gorchain.yml` | `validator-data` | `/tmp/hyperlane-bridge-e2e/validator-gorchain/data` |
| `test-spec-validator-solana.yml` | `validator-data` | `/tmp/hyperlane-bridge-e2e/validator-solana/data` |
| `test-spec-relayer.yml` | `relayer-data` | `/tmp/hyperlane-bridge-e2e/relayer/data` |
| `test-spec-monitoring.yml` | `prometheus-data` | `/tmp/hyperlane-bridge-e2e/monitoring/prometheus` |
| `test-spec-monitoring.yml` | `grafana-data` | `/tmp/hyperlane-bridge-e2e/monitoring/grafana` |

`test-spec-gas-oracle.yml`, `test-spec-warp-ui.yml` — no data volumes, no change needed.

### 3. `tests/e2e/conftest.py`

The `bridge_state_root` fixture currently pre-creates `generated/` and `logs/` directly under root. These move to `bridge/` and the other stack data dirs get pre-created too.

**`bridge_state_root` fixture** — change subdirs created:
```python
# Before
(p / "generated").mkdir(exist_ok=True)
(p / "logs").mkdir(exist_ok=True)

# After
(p / "bridge" / "generated").mkdir(parents=True, exist_ok=True)
(p / "bridge" / "logs").mkdir(parents=True, exist_ok=True)
(p / "minio" / "data").mkdir(parents=True, exist_ok=True)
(p / "validator-gorchain" / "data").mkdir(parents=True, exist_ok=True)
(p / "validator-solana" / "data").mkdir(parents=True, exist_ok=True)
(p / "relayer" / "data").mkdir(parents=True, exist_ok=True)
(p / "monitoring" / "prometheus").mkdir(parents=True, exist_ok=True)
(p / "monitoring" / "grafana").mkdir(parents=True, exist_ok=True)
```

**`bridge_state_dir` fixture** — path shifts one level:
```python
# Before
return bridge_state_root / "generated"
# After
return bridge_state_root / "bridge" / "generated"
```

**`bridge_state_logs_dir` fixture**:
```python
# Before
return bridge_state_root / "logs"
# After
return bridge_state_root / "bridge" / "logs"
```

The docstring on `bridge_state_root` references `generated/` and `logs/` subdirs directly — update to reflect the new layout.

### 4. `specs/stack-specifications.md`

Update the storage description (line ~19 and ~422) from "Named volumes → PVCs" to describe the host-path layout. Add a short section documenting the `/srv/kind/hyperlane` tree and explaining that this replaces dynamic PVCs.

---

## Directory Permissions

Containers writing to host-path volumes must be able to write to the pre-created directories. Two approaches depending on environment:

**Tests (`/tmp/hyperlane-bridge-e2e/`)** — `conftest.py` creates each dir then `chmod 0o777`. World-writable is acceptable under `/tmp/`.

**Production (`/srv/kind/hyperlane/`)** — Ansible must `chown` each dir to the container UID. Do **not** use 777 in prod.

| Directory | Container image | Runs as UID | Ansible `owner:` |
|---|---|---|---|
| `bridge/generated` | deployer job | root (0) | root (default) |
| `bridge/logs` | deployer job | root (0) | root (default) |
| `minio/data` | minio/minio | root (0) | root (default) |
| `validator-gorchain/data` | hyperlane validator | root (0) | root (default) |
| `validator-solana/data` | hyperlane validator | root (0) | root (default) |
| `relayer/data` | hyperlane relayer | root (0) | root (default) |
| `monitoring/prometheus` | prom/prometheus | 65534 (nobody) | **65534** |
| `monitoring/grafana` | grafana/grafana | 472 (grafana) | **472** |

---

## Out of Scope

- Production ops / Ansible playbooks for creating the `/srv/kind/hyperlane` tree on the server — handled separately.
- No compose file changes — host-path vs. PVC is a spec/deployment concern, not a compose concern.
- No SO changes — SO already supports host-path volumes via the `volumes:` spec key.
