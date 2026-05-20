# Bridge State Extract and Distribution — Implementation Plan (PR1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the kubectl-pipe pattern between deployer Jobs and consumer stacks with a disk-based artifact flow; drop the shared `laconic-hyperlane` namespace so each stack runs in its own k8s namespace. PR1 scope only; MinIO migration to `external-services:` + TLS + per-validator users is PR2.

**Architecture:** Deployer Jobs write JSON files to a host-path bind mount (Kind `extraMounts`); a new `BridgeStateLoader` populates each consumer stack's `{deploy_dir}/configmaps/` from those files; SO's existing `configmaps:` mechanism creates the k8s ConfigMaps from there; consumer pods mount them as normal volumes. Init containers that did `kubectl get configmap` from inside pods go away. Each stack runs in its own namespace via SO's default `laconic-{stack_name}` derivation.

**Tech Stack:** Python 3.10+ (pytest), bash, docker compose, laconic-so, kubectl/Kind, k8s ConfigMaps + Secrets.

**Reference design:** `docs/superpowers/specs/2026-05-20-bridge-state-extract-and-distribution-design.md`.

---

## Background for the implementer

- **stack-orchestrator (SO)** is a separate tool at `/home/dev/git_puller/repos/stack-orchestrator/`. Its source is required reading for: ConfigMap creation (`stack_orchestrator/deploy/k8s/cluster_info.py`, `deploy_k8s.py`), spec.yml schema (`stack_orchestrator/deploy/spec.py`).
- **Compose files** are the source of truth for what a stack runs. SO translates them to k8s Deployments + Services. Compose `volumes:` with `*-config` in the name become ConfigMap volumes in k8s, sourced from `{deploy_dir}/configmaps/<volume-name>/`.
- **Deployment lifecycle:** `laconic-so deployment init` (generates spec.yml) → `deploy create` (materializes deploy_dir with configmaps/ subdirs populated from `data/config/<name>/` source dirs) → `deployment start` (creates k8s objects). The `deploy create` step copies files from `stack_orchestrator/data/config/<volume-name>/` into `{deploy_dir}/configmaps/<volume-name>/`.
- **deploy/commands.py** in each stack defines `init()` (default spec content) and `create()` (post-create hooks — RBAC, Services).
- **E2E tests** run pytest against a local Kind cluster. They use checked-in test fixtures at `tests/e2e/fixtures/test-spec-*.yml` patched in-flight with placeholders (`REPLACE_AT_RUNTIME`, `REPLACE_NAMESPACE`, etc.).
- **Job vs Pod:** the two deployer stacks are Jobs (run-to-completion). All other stacks are Deployments.
- **Run e2e:** `cd tests/e2e && pytest -x -v -k <test-name>`. Each test file deploys one or more stacks. The user runs e2e themselves; this plan provides verification commands.

---

## File structure summary

**New files:**
- `tests/e2e/lib/state_loader.py` — `BridgeStateLoader` class encapsulating state-file-to-consumer mapping
- `docs/superpowers/plans/2026-05-20-bridge-state-extract-and-distribution.md` (this plan)

**Modified files (deployer side):**
- `stack_orchestrator/data/compose-jobs/docker-compose-hyperlane-svm-deployer.yml`
- `stack_orchestrator/data/compose-jobs/docker-compose-hyperlane-svm-warp-deployer.yml`
- `stack_orchestrator/data/config/deployer-scripts-config/deploy.sh`
- `stack_orchestrator/data/config/warp-deployer-scripts-config/deploy.sh`
- `stack_orchestrator/data/stacks/hyperlane-svm-warp-deployer/deploy/commands.py`

**Deleted files (deployer side):**
- `stack_orchestrator/data/stacks/hyperlane-svm-warp-deployer/deploy/rbac.yaml`

**Modified files (consumer side):**
- `stack_orchestrator/data/compose/docker-compose-hyperlane-validator.yml`
- `stack_orchestrator/data/compose/docker-compose-hyperlane-relayer.yml`

**Modified files (specs — 8 files):**
- `deployment/spec-deployer.yml`, `spec-warp-deployer.yml`, `spec-validator-gorchain.yml`, `spec-validator-solana.yml`, `spec-relayer.yml`, `spec-gas-oracle.yml`, `spec-minio.yml`, `spec-monitoring.yml`, `spec-warp-ui.yml`

**Modified files (test fixtures — same 8 names):**
- `tests/e2e/fixtures/test-spec-*.yml`

**Modified files (test code):**
- `tests/e2e/conftest.py`
- `tests/e2e/test_01_deployer.py`
- `tests/e2e/test_02_warp_deployer.py`

**Modified files (docs):**
- `CLAUDE.md`
- `docs/architecture-decisions.md`

---

## Sequencing rationale

Tasks are ordered so the e2e suite stays runnable for any given prefix where possible, but a clean refactor of this size cannot keep tests green between every commit. The natural gates are:

- After T1–T3: state-loader infrastructure exists but is unused. Tests unchanged, still green.
- After T4–T8: deployer Jobs write to disk AND state-loader can read it; but consumers still use the old kubectl-from-pod path AND old shared NS. Tests broken transiently because the deployer's k8s ConfigMaps are gone.
- After T9–T11: tests updated to check disk-files for deployer outputs. test_01 and test_02 should pass.
- After T12–T14: consumers mount state-loader-populated CMs; T01–T05 should pass.
- After T15–T17: per-stack NS; all tests should pass.
- After T18: MinIO interim FQDN; this is when the full e2e suite is expected to be green.
- T19–T20: docs.
- T21: full suite run.

Commits should land at the natural gates (after T3, T8, T11, T14, T17, T18, T20).

---

## Task 1: BridgeStateLoader module

**Goal:** Encapsulate the consumer→state-files mapping in one place so both pytest and ansible (PR3) can use the same contract.

**Files:**
- Create: `tests/e2e/lib/state_loader.py`

- [ ] **Step 1: Write `tests/e2e/lib/state_loader.py`**

```python
"""Loads deployer-generated state files into consumer stack deploy_dirs.

The deployer Jobs write JSON files (and one multi-file directory: registry/)
to STATE_OUTPUT_DIR. Before each consumer stack runs `deployment start`, the
loader copies the relevant subset of state files into
{deploy_dir}/configmaps/<cm-name>/ — which SO then turns into k8s ConfigMaps
that the consumer pod mounts as normal volumes.

Hardcoded mapping below is the source of truth for consumer↔state coupling.
If a state file the loader expects to copy is missing, populate() exits with
a clear error before the consumer Job/Pod is started.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterable
from pathlib import Path


# Maps a consumer stack name → list of (state file or dir, cm name) pairs.
# State paths are relative to STATE_OUTPUT_DIR. CM names match the
# `configmaps:` keys in each consumer's spec.yml. Multi-file CMs reference
# a directory (registry/, warp-deploy-outputs/) and the loader copies all
# files inside.
# Only stacks whose compose actually mounts a CM appear here. Stacks that
# consume deployer state via env-var injection (gas-oracle, warp-ui,
# monitoring) read individual values through BridgeStateLoader.read_json
# in conftest spec-patching — they don't need populate() to copy files.
CONSUMER_STATE_FILES: dict[str, list[tuple[str, str]]] = {
    "hyperlane-validator": [
        ("agent-config.json", "agent-config"),
    ],
    "hyperlane-relayer": [
        ("agent-config.json", "agent-config"),
    ],
    # Env-var consumers and stacks that don't read deployer state at all:
    "hyperlane-svm-deployer": [],
    "hyperlane-svm-warp-deployer": [],   # reads /state at runtime via mount
    "hyperlane-minio": [],
    "hyperlane-gas-oracle": [],          # env-var injection via read_json
    "hyperlane-monitoring": [],          # env-var injection via read_json
    "hyperlane-warp-ui": [],             # env-var injection via read_json
}


class BridgeStateLoader:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir

    def expected_files_for(self, stack_name: str) -> list[str]:
        return [src for src, _cm in CONSUMER_STATE_FILES.get(stack_name, [])]

    def assert_present(self, stack_name: str) -> None:
        missing = [
            src
            for src in self.expected_files_for(stack_name)
            if not (self.state_dir / src).exists()
        ]
        if missing:
            raise FileNotFoundError(
                f"BridgeStateLoader: missing state files for "
                f"{stack_name!r} under {self.state_dir}: {missing}"
            )

    def populate(self, stack_name: str, deploy_dir: Path) -> None:
        """Copy state files into {deploy_dir}/configmaps/<cm-name>/.

        Called before `laconic-so deployment start` for the given consumer.
        SO then creates one k8s ConfigMap per <cm-name>.
        """
        self.assert_present(stack_name)
        for src_rel, cm_name in CONSUMER_STATE_FILES.get(stack_name, []):
            src = self.state_dir / src_rel
            dst_dir = deploy_dir / "configmaps" / cm_name
            dst_dir.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                # Multi-file CM: copy each top-level file (SO doesn't
                # recurse subdirs into ConfigMaps; flat layout only).
                for f in src.iterdir():
                    if f.is_file():
                        shutil.copy2(f, dst_dir / f.name)
            else:
                shutil.copy2(src, dst_dir / src.name)

    def read_json(self, file_rel: str) -> dict:
        """Read a state JSON file. Used by conftest to patch test-spec
        env vars (REPLACE_AT_RUNTIME) for env-var consumers that don't
        mount a CM (gas-oracle, warp-ui, monitoring's balance-monitor).
        """
        path = self.state_dir / file_rel
        if not path.exists():
            raise FileNotFoundError(
                f"BridgeStateLoader: required state file {file_rel} not at {path}"
            )
        return json.loads(path.read_text())

    def read_program_ids(self, chain: str) -> dict:
        """Convenience: program-ids.json's `<chain>` key as a dict."""
        ids = self.read_json("program-ids.json")
        if chain not in ids:
            raise KeyError(
                f"program-ids.json missing chain {chain!r}; keys={list(ids)}"
            )
        return ids[chain]
```

- [ ] **Step 2: Smoke-test the module on a hand-crafted state dir**

Run:
```bash
cd tests/e2e
python3 -c "
from pathlib import Path
import tempfile, json, os
from lib.state_loader import BridgeStateLoader

with tempfile.TemporaryDirectory() as td:
    state = Path(td) / 'state'
    state.mkdir()
    (state / 'agent-config.json').write_text('{\"hello\":1}')
    (state / 'multisig-config.json').write_text('{}')
    (state / 'program-ids.json').write_text('{\"gorchain\":{},\"solana\":{}}')
    (state / 'registry').mkdir()
    (state / 'registry' / 'chains.yaml').write_text('chains: {}')

    loader = BridgeStateLoader(state)
    dd = Path(td) / 'dd'
    dd.mkdir()
    loader.populate('hyperlane-relayer', dd)

    assert (dd / 'configmaps' / 'agent-config' / 'agent-config.json').exists()
    assert (dd / 'configmaps' / 'multisig-config' / 'multisig-config.json').exists()
    assert json.loads((dd / 'configmaps' / 'agent-config' / 'agent-config.json').read_text())['hello'] == 1
    print('OK')
"
```

Expected: `OK`. If a file is missing or path resolution fails, the script will raise.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/lib/state_loader.py
git commit -m "tests: add BridgeStateLoader for state-file → ConfigMap distribution"
```

---

## Task 2: bridge_state_loader fixture and STATE_OUTPUT_DIR plumbing

**Goal:** Make a single `BridgeStateLoader` available to all fixtures, with `STATE_OUTPUT_DIR` set up as a per-session host-path that lives for the duration of pytest.

**Files:**
- Modify: `tests/e2e/conftest.py`

- [ ] **Step 1: Find the existing imports block at the top of conftest.py and add the import**

Add at the bottom of the existing `from lib.…` import block (currently around line 60):

```python
from lib.state_loader import BridgeStateLoader
```

- [ ] **Step 2: Add session-scoped fixture (place it near the other session fixtures, after the `SPEC_REPLACEMENTS` definition near line 110)**

```python
@pytest.fixture(scope="session")
def bridge_state_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Host-path bind-mounted into Kind via extraMounts; deployer Jobs
    write here, BridgeStateLoader reads here, consumer populate() copies
    from here. One per pytest session."""
    p = tmp_path_factory.mktemp("hyperlane-state")
    log.info("Bridge state dir for this session: %s", p)
    return p


@pytest.fixture(scope="session")
def bridge_state_logs_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Host-path for deployer Job logs (tee'd stdout/stderr); separate
    from state dir so logs survive even if state dir is cleared."""
    p = tmp_path_factory.mktemp("hyperlane-state-logs")
    log.info("Bridge state logs dir for this session: %s", p)
    return p


@pytest.fixture(scope="session")
def bridge_state_loader(bridge_state_dir: Path) -> BridgeStateLoader:
    return BridgeStateLoader(bridge_state_dir)
```

- [ ] **Step 3: Verify pytest collection still passes**

Run:
```bash
cd tests/e2e
pytest --collect-only -q 2>&1 | tail -10
```

Expected: no errors; same number of tests as before.

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/conftest.py
git commit -m "tests: add bridge_state_dir/loader/logs session fixtures"
```

---

## Task 3: Wire Kind extraMounts for state and logs

**Goal:** Make the deployer Jobs (which run in the Kind cluster) able to write to `bridge_state_dir` / `bridge_state_logs_dir` on the host. This needs an `extraMounts` entry in the Kind config so the host path is bind-mounted into Kind nodes; then the Job's compose-job volume mount points at the in-node path.

**Files:**
- Modify: `tests/e2e/lib/cluster.py` (Kind config generation)
- Modify: `tests/e2e/conftest.py` (pass the two host paths into the Kind config generator)

- [ ] **Step 1: Inspect current Kind config generation**

Run:
```bash
grep -n "extraMount\|kind_config\|create_kind_cluster" tests/e2e/lib/cluster.py | head -20
```

Read `create_kind_cluster` and locate where the Kind YAML config is built. Identify the existing `nodes:` block and any `extraMounts` already present.

- [ ] **Step 2: Modify `create_kind_cluster` signature to accept extra host-path mounts**

In `tests/e2e/lib/cluster.py`, change the `create_kind_cluster` function (and any callers) to accept an `extra_mounts: list[tuple[Path, str]] = None` parameter where each tuple is `(host_path, container_path)`. Append these to the generated `extraMounts:` block under each node:

```python
def create_kind_cluster(
    name: str = KIND_CLUSTER_NAME,
    *,
    extra_mounts: list[tuple[Path, str]] | None = None,
) -> None:
    ...
    extra_mounts = extra_mounts or []
    node_extra_mounts = [
        # ...existing entries...
    ]
    for host_path, container_path in extra_mounts:
        host_path.mkdir(parents=True, exist_ok=True)
        node_extra_mounts.append(
            {"hostPath": str(host_path), "containerPath": container_path}
        )
    ...
```

(Adapt to whatever serialization the existing code uses — yaml.safe_dump, f-string, or template.)

- [ ] **Step 3: Update the conftest cluster-setup fixture to pass the new mounts**

Find the fixture that calls `create_kind_cluster` (search `grep -n "create_kind_cluster" tests/e2e/conftest.py`). It is currently session-scoped. Make it depend on `bridge_state_dir` and `bridge_state_logs_dir` and pass them through:

```python
@pytest.fixture(scope="session", autouse=True)
def kind_cluster(bridge_state_dir: Path, bridge_state_logs_dir: Path) -> Generator[None, None, None]:
    create_kind_cluster(
        extra_mounts=[
            (bridge_state_dir, "/mnt/bridge-state"),
            (bridge_state_logs_dir, "/mnt/bridge-state-logs"),
        ],
    )
    yield
    destroy_kind_cluster()
```

The exact existing fixture body should be preserved; only the `extra_mounts` argument is added and the parameters are inserted into the signature.

- [ ] **Step 4: Manual verification — bring up a fresh Kind cluster and inspect the mount**

Run:
```bash
cd tests/e2e
pytest tests/e2e/conftest.py::dummy -x 2>&1 | grep -i "kind\|extraMount" | head -5 || true
# Then manually:
docker exec laconic-e2e-control-plane ls -la /mnt/bridge-state /mnt/bridge-state-logs
```

Expected: both directories exist inside the Kind node.

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/lib/cluster.py tests/e2e/conftest.py
git commit -m "tests: bind-mount bridge state and logs dirs into Kind via extraMounts"
```

---

## Task 4: Add /state and /logs volumes to deployer compose-job

**Goal:** Mount the in-node paths from Task 3 into the deployer Job's container at `/state` and `/logs`. The compose volume name uses `bind-mount` semantics: SO recognizes host-path mounts and converts them to k8s hostPath volumes when the source starts with `/`.

**Files:**
- Modify: `stack_orchestrator/data/compose-jobs/docker-compose-hyperlane-svm-deployer.yml`

- [ ] **Step 1: Add the two volume mounts**

Open the file; the existing `volumes:` block under the `deployer` service is:

```yaml
    volumes:
      - deployer-scripts-config:/opt/scripts:ro
      - deployer-gas-oracle-config:/config/gas-oracle:ro
      - deployer-multisig-config:/config/multisig:ro
      - deployer-registry-config:/config/registry:ro
```

Add two host-path entries below:

```yaml
    volumes:
      - deployer-scripts-config:/opt/scripts:ro
      - deployer-gas-oracle-config:/config/gas-oracle:ro
      - deployer-multisig-config:/config/multisig:ro
      - deployer-registry-config:/config/registry:ro
      - /mnt/bridge-state:/state
      - /mnt/bridge-state-logs:/logs
```

The values `/mnt/bridge-state` and `/mnt/bridge-state-logs` are the in-Kind-node paths set up by Task 3. SO's k8s path translates host-paths-starting-with-`/` into hostPath volumes.

- [ ] **Step 2: Commit**

```bash
git add stack_orchestrator/data/compose-jobs/docker-compose-hyperlane-svm-deployer.yml
git commit -m "deployer: mount /state and /logs hostPath volumes"
```

---

## Task 5: Deployer script writes state files (replaces kubectl create configmap)

**Goal:** Rewrite the "Write deployment artifacts" section of the deployer script to produce files under `/state/` instead of k8s ConfigMaps. Tee all output to `/logs/<job>-<timestamp>.log`. Add a preflight at exit.

**Files:**
- Modify: `stack_orchestrator/data/config/deployer-scripts-config/deploy.sh`

- [ ] **Step 1: Replace the idempotency check at the top of the script**

Find the existing block (around line 13–21):

```bash
if [ "${FORCE_REDEPLOY:-false}" != "true" ]; then
  EXISTING=$(kubectl get configmap hyperlane-program-ids \
    -o jsonpath='{.data.gorchain-program-ids\.json}' 2>/dev/null || echo "")
  if [ -n "$EXISTING" ] && [ "$EXISTING" != "{}" ]; then
    echo "Deployment artifacts already exist (hyperlane-program-ids ConfigMap has data)."
    echo "Set FORCE_REDEPLOY=true to override. Exiting."
    exit 0
  fi
fi
```

Replace with:

```bash
STATE_DIR="${STATE_OUTPUT_DIR:-/state}"
LOGS_DIR="${LOGS_OUTPUT_DIR:-/logs}"
mkdir -p "${STATE_DIR}" "${STATE_DIR}/registry" "${LOGS_DIR}"

if [ "${FORCE_REDEPLOY:-false}" != "true" ]; then
  if [ -s "${STATE_DIR}/program-ids.json" ] \
     && [ "$(cat "${STATE_DIR}/program-ids.json")" != "{}" ]; then
    echo "Deployment artifacts already exist (${STATE_DIR}/program-ids.json)."
    echo "Set FORCE_REDEPLOY=true to override. Exiting."
    exit 0
  fi
fi
```

- [ ] **Step 2: Add a log-tee just after the idempotency check**

Insert immediately after the block from Step 1:

```bash
# Tee all subsequent stdout+stderr to a timestamped log so it survives
# cluster tear-down.
LOG_FILE="${LOGS_DIR}/svm-deployer-$(date -u +%Y%m%dT%H%M%SZ).log"
exec > >(tee -a "${LOG_FILE}") 2>&1
echo "Logging to ${LOG_FILE}"
```

- [ ] **Step 3: Replace the "Write deployment artifacts to k8s ConfigMaps" section**

Find the entire block from `# Write deployment artifacts to k8s ConfigMaps` (around line 400) through the closing `for CM in hyperlane-program-ids ...` loop and its `kubectl label` body (around line 449).

Replace the entire section with:

```bash
# -------------------------------------------------------
# Write deployment artifacts to /state as JSON files
# -------------------------------------------------------
echo ""
echo "=== Writing deployment artifacts to ${STATE_DIR} ==="

# program-ids.json: merge per-chain program ID files into one map
python3 - <<PYEOF
import json, pathlib
out = {}
for chain, src in (("gorchain", "${GORCHAIN_PROGRAMS}"),
                   ("solana",   "${SOLANA_PROGRAMS}")):
    p = pathlib.Path(src)
    out[chain] = json.loads(p.read_text()) if p.exists() else {}
pathlib.Path("${STATE_DIR}/program-ids.json").write_text(
    json.dumps(out, indent=2, sort_keys=True) + "\n"
)
PYEOF

# agent-config.json: copy as-is
cp "${WORK_DIR}/agent-config.json" "${STATE_DIR}/agent-config.json"

# gas-oracle-config.json: copy from mount if present
if [ -f "${GAS_ORACLE_CONFIG}" ]; then
  cp "${GAS_ORACLE_CONFIG}" "${STATE_DIR}/gas-oracle-config.json"
else
  echo "WARNING: Gas oracle config not found at ${GAS_ORACLE_CONFIG}; not written"
fi

# multisig-config.json: merge the two rendered per-chain files
python3 - <<PYEOF
import json, pathlib
out = {}
for chain in ("gorchain", "solana"):
    p = pathlib.Path("${RENDERED_MULTISIG_DIR}") / f"{chain}-multisig.json"
    out[chain] = json.loads(p.read_text()) if p.exists() else {}
pathlib.Path("${STATE_DIR}/multisig-config.json").write_text(
    json.dumps(out, indent=2, sort_keys=True) + "\n"
)
PYEOF

# registry/: copy the rendered chain-metadata directory
if [ -d "${RENDERED_REGISTRY_DIR}/chains" ]; then
  rm -rf "${STATE_DIR}/registry"
  mkdir -p "${STATE_DIR}/registry"
  cp -a "${RENDERED_REGISTRY_DIR}/chains/." "${STATE_DIR}/registry/"
else
  echo "WARNING: Registry config not found at ${RENDERED_REGISTRY_DIR}/chains; not written"
fi
```

- [ ] **Step 4: Replace the closing summary echos and the cleanup section**

Find the end-of-script block:

```bash
# -------------------------------------------------------
# Clean up deployer keypair
# -------------------------------------------------------
rm -f "${DEPLOYER_KEY_FILE}"

echo ""
echo "=== Deployment complete ==="
echo "Artifacts written to ConfigMaps:"
echo "  - hyperlane-program-ids"
echo "  - hyperlane-agent-config"
echo "  - hyperlane-gas-oracle-config"
echo "  - hyperlane-multisig-config"
echo "  - hyperlane-registry"
```

Replace with:

```bash
# -------------------------------------------------------
# Preflight: verify all expected outputs are present
# -------------------------------------------------------
EXPECTED=(
  "${STATE_DIR}/program-ids.json"
  "${STATE_DIR}/agent-config.json"
  "${STATE_DIR}/gas-oracle-config.json"
  "${STATE_DIR}/multisig-config.json"
  "${STATE_DIR}/registry/chains.yaml"
)

MISSING=()
for f in "${EXPECTED[@]}"; do
  if [ ! -s "$f" ]; then
    MISSING+=("$f")
  fi
done

if [ "${#MISSING[@]}" -ne 0 ]; then
  echo ""
  echo "ERROR: deployer preflight failed — expected outputs missing or empty:"
  for f in "${MISSING[@]}"; do echo "  - $f"; done
  exit 1
fi

# -------------------------------------------------------
# Clean up deployer keypair
# -------------------------------------------------------
rm -f "${DEPLOYER_KEY_FILE}"

echo ""
echo "=== Deployment complete ==="
echo "Artifacts written to ${STATE_DIR}:"
for f in "${EXPECTED[@]}"; do
  echo "  - ${f#${STATE_DIR}/}"
done
```

The `registry/chains.yaml` entry in `EXPECTED` is the canary for the multi-file registry directory; if it's present, the directory was written.

- [ ] **Step 5: Bash syntax check**

Run:
```bash
bash -n stack_orchestrator/data/config/deployer-scripts-config/deploy.sh
```

Expected: exit 0, no output.

- [ ] **Step 6: Commit**

```bash
git add stack_orchestrator/data/config/deployer-scripts-config/deploy.sh
git commit -m "deployer: write state files to /state instead of k8s ConfigMaps"
```

---

## Task 6: Add /state and /logs volumes to warp-deployer compose-job

**Files:**
- Modify: `stack_orchestrator/data/compose-jobs/docker-compose-hyperlane-svm-warp-deployer.yml`

- [ ] **Step 1: Append host-path mounts**

Existing volume block under `warp-deployer`:

```yaml
    volumes:
      - warp-deployer-scripts-config:/opt/scripts:ro
      - warp-deployer-token-config:/config/token:ro
      - warp-deployer-registry-config:/config/registry:ro
```

Replace with:

```yaml
    volumes:
      - warp-deployer-scripts-config:/opt/scripts:ro
      - warp-deployer-token-config:/config/token:ro
      - warp-deployer-registry-config:/config/registry:ro
      - /mnt/bridge-state:/state
      - /mnt/bridge-state-logs:/logs
```

- [ ] **Step 2: Commit**

```bash
git add stack_orchestrator/data/compose-jobs/docker-compose-hyperlane-svm-warp-deployer.yml
git commit -m "warp-deployer: mount /state and /logs hostPath volumes"
```

---

## Task 7: Warp-deployer script reads/writes /state

**Files:**
- Modify: `stack_orchestrator/data/config/warp-deployer-scripts-config/deploy.sh`

- [ ] **Step 1: Add log-tee + STATE_DIR vars at the top of the script (just after the `set -euo pipefail` line)**

```bash
STATE_DIR="${STATE_OUTPUT_DIR:-/state}"
LOGS_DIR="${LOGS_OUTPUT_DIR:-/logs}"
mkdir -p "${STATE_DIR}" "${LOGS_DIR}"

LOG_FILE="${LOGS_DIR}/svm-warp-deployer-$(date -u +%Y%m%dT%H%M%SZ).log"
exec > >(tee -a "${LOG_FILE}") 2>&1
echo "Logging to ${LOG_FILE}"
```

- [ ] **Step 2: Replace the prerequisite check (kubectl-based) with a file-based read**

Find the existing block:

```bash
echo ""
echo "=== Checking core deployment artifacts ==="
COLLATERAL_PROGRAMS=$(kubectl get configmap hyperlane-program-ids \
  -o jsonpath="{.data.${COLLATERAL_CHAIN}-program-ids\.json}" 2>/dev/null || echo "")
SYNTHETIC_PROGRAMS=$(kubectl get configmap hyperlane-program-ids \
  -o jsonpath="{.data.${SYNTHETIC_CHAIN}-program-ids\.json}" 2>/dev/null || echo "")

if [ -z "$COLLATERAL_PROGRAMS" ] || [ "$COLLATERAL_PROGRAMS" = "{}" ]; then
  echo "ERROR: hyperlane-program-ids ConfigMap missing data for ${COLLATERAL_CHAIN}."
  echo "Run the hyperlane-svm-deployer stack first."
  exit 1
fi
if [ -z "$SYNTHETIC_PROGRAMS" ] || [ "$SYNTHETIC_PROGRAMS" = "{}" ]; then
  echo "ERROR: hyperlane-program-ids ConfigMap missing data for ${SYNTHETIC_CHAIN}."
  echo "Run the hyperlane-svm-deployer stack first."
  exit 1
fi
```

Replace with:

```bash
echo ""
echo "=== Checking core deployment artifacts ==="
PROGRAM_IDS_FILE="${STATE_DIR}/program-ids.json"
if [ ! -s "${PROGRAM_IDS_FILE}" ]; then
  echo "ERROR: ${PROGRAM_IDS_FILE} missing. Run the hyperlane-svm-deployer stack first."
  exit 1
fi

COLLATERAL_PROGRAMS=$(python3 -c "import json,sys;print(json.dumps(json.load(open('${PROGRAM_IDS_FILE}')).get('${COLLATERAL_CHAIN}', {})))")
SYNTHETIC_PROGRAMS=$(python3 -c "import json,sys;print(json.dumps(json.load(open('${PROGRAM_IDS_FILE}')).get('${SYNTHETIC_CHAIN}', {})))")

if [ "$COLLATERAL_PROGRAMS" = "{}" ]; then
  echo "ERROR: program-ids.json missing data for ${COLLATERAL_CHAIN}."
  exit 1
fi
if [ "$SYNTHETIC_PROGRAMS" = "{}" ]; then
  echo "ERROR: program-ids.json missing data for ${SYNTHETIC_CHAIN}."
  exit 1
fi
```

- [ ] **Step 3: Replace the "Write deployment artifacts" section**

Find the existing block (starting `echo "=== Writing warp route artifacts to Kubernetes ConfigMaps ==="`) through the end of the `for CM in hyperlane-token-config; do … done` and the trailing `if kubectl get configmap hyperlane-warp-deploy-outputs …` block.

Replace with:

```bash
echo ""
echo "=== Writing warp route artifacts to ${STATE_DIR} ==="

cp "${WORK_DIR}/output/token-config.json" "${STATE_DIR}/token-config.json"

if [ -d "${WARP_OUTPUT_DIR}" ]; then
  rm -rf "${STATE_DIR}/warp-deploy-outputs"
  mkdir -p "${STATE_DIR}/warp-deploy-outputs"
  cp -a "${WARP_OUTPUT_DIR}/." "${STATE_DIR}/warp-deploy-outputs/" 2>/dev/null || true
fi
```

- [ ] **Step 4: Replace the closing summary**

Find:

```bash
echo ""
echo "=== Warp route deployment complete ==="
echo "Collateral chain: ${COLLATERAL_CHAIN}"
echo "Synthetic chain: ${SYNTHETIC_CHAIN}"
echo ""
echo "Artifacts written to ConfigMaps:"
echo "  - hyperlane-token-config"
echo "  - hyperlane-warp-deploy-outputs (if deploy produced output files)"
```

Replace with:

```bash
# -------------------------------------------------------
# Preflight: verify expected outputs
# -------------------------------------------------------
if [ ! -s "${STATE_DIR}/token-config.json" ]; then
  echo "ERROR: warp-deployer preflight failed: ${STATE_DIR}/token-config.json missing or empty"
  exit 1
fi

echo ""
echo "=== Warp route deployment complete ==="
echo "Collateral chain: ${COLLATERAL_CHAIN}"
echo "Synthetic chain: ${SYNTHETIC_CHAIN}"
echo ""
echo "Artifacts written to ${STATE_DIR}:"
echo "  - token-config.json"
[ -d "${STATE_DIR}/warp-deploy-outputs" ] && echo "  - warp-deploy-outputs/"
```

- [ ] **Step 5: Bash syntax check**

Run:
```bash
bash -n stack_orchestrator/data/config/warp-deployer-scripts-config/deploy.sh
```

Expected: exit 0.

- [ ] **Step 6: Commit**

```bash
git add stack_orchestrator/data/config/warp-deployer-scripts-config/deploy.sh
git commit -m "warp-deployer: read program-ids and write outputs via /state"
```

---

## Task 8: Drop warp-deployer RBAC

**Goal:** Warp-deployer no longer talks to k8s — no kubectl-from-pod, no kubectl create. Drop the RBAC apply.

**Files:**
- Delete: `stack_orchestrator/data/stacks/hyperlane-svm-warp-deployer/deploy/rbac.yaml`
- Modify: `stack_orchestrator/data/stacks/hyperlane-svm-warp-deployer/deploy/commands.py`

Also: the core deployer's `deploy/commands.py` (if it applies RBAC) must be handled. Check:

```bash
cat stack_orchestrator/data/stacks/hyperlane-svm-deployer/deploy/commands.py
ls stack_orchestrator/data/stacks/hyperlane-svm-deployer/deploy/
```

If a `rbac.yaml` exists, delete it; if `commands.py::create()` applies RBAC, gut it the same way as warp-deployer below.

- [ ] **Step 1: Replace warp-deployer commands.py**

Open `stack_orchestrator/data/stacks/hyperlane-svm-warp-deployer/deploy/commands.py`. Replace its entire contents with:

```python
from stack_orchestrator.deploy.deployment_context import DeploymentContext


def init(deploy_command_context):
    """Return default spec content for this stack."""
    return {}


def create(context: DeploymentContext, extra_args):
    """No post-create hooks. Warp-deployer reads/writes via /state mount."""
    pass
```

- [ ] **Step 2: Delete the RBAC manifest**

```bash
rm stack_orchestrator/data/stacks/hyperlane-svm-warp-deployer/deploy/rbac.yaml
```

- [ ] **Step 3: Apply the same treatment to the core deployer if it has RBAC**

Run:
```bash
ls stack_orchestrator/data/stacks/hyperlane-svm-deployer/deploy/
cat stack_orchestrator/data/stacks/hyperlane-svm-deployer/deploy/commands.py 2>/dev/null
```

If `rbac.yaml` exists and `commands.py::create()` applies it, gut `create()` to a no-op (as in Step 1) and `rm rbac.yaml`.

- [ ] **Step 4: Commit**

```bash
git add stack_orchestrator/data/stacks/hyperlane-svm-warp-deployer/ stack_orchestrator/data/stacks/hyperlane-svm-deployer/ 2>/dev/null || true
git commit -m "deployer/warp-deployer: drop RBAC (no more kubectl-from-pod)"
```

---

## Task 9: Update test_01_deployer to verify state files instead of CMs

**Files:**
- Modify: `tests/e2e/test_01_deployer.py`

- [ ] **Step 1: Add the bridge_state_loader fixture parameter to the test class methods that currently check CMs**

The tests `test_program_ids_configmap`, `test_agent_config_configmap`, `test_gas_oracle_configmap`, `test_multisig_configmap`, `test_registry_configmap` each currently call `wait_for_configmap(ns, "hyperlane-…", CONFIGMAP_TIMEOUT)` then `get_configmap_json(...)`.

Replace these with reads from the loader. Concretely, change each `test_…_configmap` method's signature and body. Example for `test_program_ids_configmap`:

```python
def test_program_ids_configmap(
    self,
    deployer_deployment: DeploymentInfo,
    bridge_state_loader: BridgeStateLoader,
) -> None:
    """Validate program-ids state file has correct structure and valid pubkeys."""
    for chain in CHAINS:
        program_ids = bridge_state_loader.read_program_ids(chain)
        for field in CORE_PROGRAM_ID_FIELDS:
            value = program_ids.get(field)
            assert value, f"{chain} program-ids missing '{field}'"
            assert is_base58_pubkey(value), (
                f"{chain} program-ids.{field} is not a valid base58 pubkey: {value}"
            )
```

For `test_agent_config_configmap`:

```python
def test_agent_config_configmap(
    self,
    deployer_deployment: DeploymentInfo,
    bridge_state_loader: BridgeStateLoader,
) -> None:
    """Validate agent-config structure, field values, and cross-references."""
    agent_config = bridge_state_loader.read_json("agent-config.json")
    chains = agent_config.get("chains")
    assert isinstance(chains, dict), "agent-config missing 'chains' object"
    # (rest of method body unchanged, operates on `chains` dict)
```

For `test_gas_oracle_configmap`:

```python
def test_gas_oracle_configmap(
    self,
    deployer_deployment: DeploymentInfo,
    bridge_state_loader: BridgeStateLoader,
) -> None:
    configs = bridge_state_loader.read_json("gas-oracle-config.json")
    # (rest of method body unchanged)
```

For `test_multisig_configmap`: Use `bridge_state_loader.read_json("multisig-config.json")`; the file shape is `{chain: <per-chain-config>}`.

For `test_registry_configmap` (if present): read individual files from the `registry/` subdir under `bridge_state_loader.state_dir`.

- [ ] **Step 2: Remove now-unused imports**

Remove `wait_for_configmap`, `get_configmap_json` from `from lib.common import …` if no other test in the file uses them.

Add: `from lib.state_loader import BridgeStateLoader` at the top.

- [ ] **Step 3: Verify pytest still collects the file**

Run:
```bash
cd tests/e2e
pytest --collect-only tests/e2e/test_01_deployer.py -q 2>&1 | tail -10
```

Expected: no collection errors.

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/test_01_deployer.py
git commit -m "tests: verify deployer outputs from state files instead of CMs"
```

---

## Task 10: Update test_02_warp_deployer to read state files

**Files:**
- Modify: `tests/e2e/test_02_warp_deployer.py`

- [ ] **Step 1: Update `test_warp_token_configmap`**

Replace its body that uses `wait_for_configmap` + `get_configmap_json` with:

```python
def test_warp_token_configmap(
    self,
    warp_deployment: dict,
    bridge_state_loader: BridgeStateLoader,
) -> None:
    """Validate warp token-config state file has correct structure and references."""
    token_mint = warp_deployment["token_mint"]
    parsed = bridge_state_loader.read_json("token-config.json")
    warp_route = parsed.get("warpRoute")
    # (rest unchanged)
```

- [ ] **Step 2: Update `test_warp_deploy_outputs`**

```python
def test_warp_deploy_outputs(
    self,
    warp_deployment: dict,
    bridge_state_loader: BridgeStateLoader,
) -> None:
    """Validate warp-deploy-outputs directory contains program IDs."""
    outputs_dir = bridge_state_loader.state_dir / "warp-deploy-outputs"
    assert outputs_dir.is_dir(), f"warp-deploy-outputs dir missing at {outputs_dir}"
    files = list(outputs_dir.iterdir())
    assert files, "warp-deploy-outputs directory is empty"
    import json
    for f in files:
        if f.suffix == ".json":
            parsed = json.loads(f.read_text())
            assert isinstance(parsed, dict), f"warp output {f.name!r} is not a JSON object"
```

- [ ] **Step 3: Adjust imports**

Add: `from lib.state_loader import BridgeStateLoader`.
Remove unused: `wait_for_configmap`, `get_configmap_json`, `get_configmap_data` if no other test in the file references them.

- [ ] **Step 4: Verify collection**

```bash
cd tests/e2e
pytest --collect-only tests/e2e/test_02_warp_deployer.py -q 2>&1 | tail -10
```

Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/test_02_warp_deployer.py
git commit -m "tests: verify warp-deployer outputs from state files"
```

---

## Task 11: Update conftest helpers that read deployer-output CMs

**Goal:** Several fixtures in conftest.py currently call `get_configmap_json(namespace, "hyperlane-program-ids", …)` etc. to grab values for patching `REPLACE_AT_RUNTIME` in test specs (relayer, gas-oracle, warp-ui, monitoring). Switch them to use `BridgeStateLoader.read_*`.

**Files:**
- Modify: `tests/e2e/conftest.py`

- [ ] **Step 1: Find every call site**

Run:
```bash
grep -n "hyperlane-program-ids\|hyperlane-agent-config\|hyperlane-gas-oracle-config\|hyperlane-multisig-config\|hyperlane-token-config\|hyperlane-warp-deploy-outputs\|hyperlane-registry" tests/e2e/conftest.py
```

For each match, classify: is it (a) a read used to patch a REPLACE_AT_RUNTIME placeholder, or (b) something else?

- [ ] **Step 2: For each (a) call site, swap the source**

Example: the relayer fixture currently does:

```python
for chain in ("gorchain", "solana"):
    program_ids = get_configmap_json(namespace, "hyperlane-program-ids", f"{chain}-program-ids.json")
    if chain == "gorchain":
        gorchain_igp_program_id = program_ids["igp_program_id"]
        ...
```

Replace with:

```python
for chain in ("gorchain", "solana"):
    program_ids = bridge_state_loader.read_program_ids(chain)
    if chain == "gorchain":
        gorchain_igp_program_id = program_ids["igp_program_id"]
        ...
```

(`bridge_state_loader` is a session fixture from Task 2; add it to the fixture's parameter list.)

Similarly for gas-oracle, warp-ui, monitoring fixtures: each one needs `bridge_state_loader` added to its parameters, and each `get_configmap_json` call swapped for the corresponding `bridge_state_loader.read_json(...)` or `.read_program_ids(...)`.

- [ ] **Step 3: Remove now-unused imports if applicable**

After all swaps, check whether `get_configmap_json` / `get_configmap_data` / `wait_for_configmap` are still imported in conftest.py and used elsewhere. If they're only used for these patching loops, remove them from `from lib.common import …`. Keep them if any are still referenced.

- [ ] **Step 4: Verify pytest collection**

```bash
cd tests/e2e
pytest --collect-only -q 2>&1 | tail -10
```

Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/conftest.py
git commit -m "tests: read deployer outputs from state files via BridgeStateLoader"
```

---

## Task 12: Switch validator compose to plain CM mount

**Files:**
- Modify: `stack_orchestrator/data/compose/docker-compose-hyperlane-validator.yml`

- [ ] **Step 1: Remove the `agent-config-init` service**

Delete the entire `agent-config-init:` block (the one that uses `alpine/kubectl` to do `kubectl get configmap`).

- [ ] **Step 2: Replace the agent-config volume definition**

Find the `volumes:` block at the bottom:

```yaml
volumes:
  # Agent config volume — PVC shared between init container (writer) and
  # validator (reader). The init container fetches the real agent-config
  # from the ConfigMap created by the deployer job.
  agent-config:
  # Data volume — PVC for validator checkpoint DB
  validator-data:
```

Replace with:

```yaml
volumes:
  # agent-config: ConfigMap volume (sourced from BridgeStateLoader →
  # {deploy_dir}/configmaps/agent-config/ → k8s ConfigMap)
  agent-config:
  # Data volume — PVC for validator checkpoint DB
  validator-data:
```

(The volume name stays the same. SO recognizes it as a ConfigMap because the spec's `configmaps:` block (Task 14) maps it.)

- [ ] **Step 3: Update validator service's CONFIG_FILES**

The validator service mounts `agent-config:/config:ro`. The deployer's agent-config.json was previously written into `/config/agent-config.json` by the init container; now SO mounts the CM directly. The mount root contains `agent-config.json` (the CM's only key). Verify the `CONFIG_FILES` env still points to `/config/agent-config.json` (it does — no change needed). The env line should remain:

```yaml
      CONFIG_FILES: /config/agent-config.json
```

- [ ] **Step 4: Commit**

```bash
git add stack_orchestrator/data/compose/docker-compose-hyperlane-validator.yml
git commit -m "validator: mount agent-config CM directly, drop init container"
```

---

## Task 13: Switch relayer compose to plain CM mounts

**Files:**
- Modify: `stack_orchestrator/data/compose/docker-compose-hyperlane-relayer.yml`

- [ ] **Step 1: Remove the `agent-config-init` service**

Delete the entire `agent-config-init:` block.

- [ ] **Step 2: Leave the relayer service's volume mounts unchanged except for the dropped init container's writes**

The relayer service currently mounts:

```yaml
    volumes:
      - agent-config:/config:ro
      - relayer-data:/data
```

No changes here — `agent-config` is now backed by the SO-created ConfigMap directly (sourced from state files in T15). The relayer doesn't need to mount `multisig-config` at runtime; if a future change makes the relayer consume it, that's a separate task that also adds the CM to its spec.

- [ ] **Step 3: Update the volume definitions block at the bottom**

```yaml
volumes:
  # agent-config: ConfigMap from BridgeStateLoader (populated at deploy-create)
  agent-config:
  relayer-data:
  igp-fee-claim-scripts-config:
```

- [ ] **Step 4: Commit**

```bash
git add stack_orchestrator/data/compose/docker-compose-hyperlane-relayer.yml
git commit -m "relayer: mount agent-config CM directly, drop init container"
```

---

## Task 14: Add state ConfigMaps to consumer specs

**Goal:** Each consumer stack that mounts a state-derived CM lists it in its `spec.yml` `configmaps:` block, so SO creates the CM in the stack's namespace from `{deploy_dir}/configmaps/<cm-name>/`.

**Files:**
- Modify: `deployment/spec-validator-gorchain.yml`
- Modify: `deployment/spec-validator-solana.yml`
- Modify: `deployment/spec-relayer.yml`
- Modify: `tests/e2e/fixtures/test-spec-validator-gorchain.yml`
- Modify: `tests/e2e/fixtures/test-spec-validator-solana.yml`
- Modify: `tests/e2e/fixtures/test-spec-relayer.yml`

For PR1 scope, only validator and relayer have new CM mounts. (gas-oracle, warp-ui, monitoring use env-var injection populated by the loader in conftest/ansible; their compose doesn't mount the deployer's state CMs.)

- [ ] **Step 1: validator-gorchain spec**

In `deployment/spec-validator-gorchain.yml`, locate any existing `configmaps:` block. If absent, add at top-level (sibling of `config:`):

```yaml
configmaps:
  agent-config: ./configmaps/agent-config
```

If a `configmaps:` block already exists, append the entry.

- [ ] **Step 2: validator-solana spec**

Same edit in `deployment/spec-validator-solana.yml`:

```yaml
configmaps:
  agent-config: ./configmaps/agent-config
```

- [ ] **Step 3: relayer spec**

In `deployment/spec-relayer.yml`, the existing `configmaps:` block is:

```yaml
configmaps:
  igp-fee-claim-scripts-config: ./configmaps/igp-fee-claim-scripts-config
```

Add agent-config:

```yaml
configmaps:
  igp-fee-claim-scripts-config: ./configmaps/igp-fee-claim-scripts-config
  agent-config: ./configmaps/agent-config
```

- [ ] **Step 4: Apply the same three edits to the test fixtures**

- `tests/e2e/fixtures/test-spec-validator-gorchain.yml` → add `configmaps: { agent-config: ./configmaps/agent-config }`
- `tests/e2e/fixtures/test-spec-validator-solana.yml` → same
- `tests/e2e/fixtures/test-spec-relayer.yml` → append `agent-config: ./configmaps/agent-config` to the existing `configmaps:` block

- [ ] **Step 5: Commit**

```bash
git add deployment/spec-validator-gorchain.yml deployment/spec-validator-solana.yml deployment/spec-relayer.yml tests/e2e/fixtures/test-spec-validator-gorchain.yml tests/e2e/fixtures/test-spec-validator-solana.yml tests/e2e/fixtures/test-spec-relayer.yml
git commit -m "specs: declare agent-config CM for validator and relayer stacks"
```

---

## Task 15: Wire bridge_state_loader.populate() into deploy fixtures

**Goal:** Before each consumer test runs `deployment start`, the loader must have copied the state files into the consumer's `{deploy_dir}/configmaps/`. The natural hook is the fixture that calls `deploy_prepare` then `deploy_start`.

**Files:**
- Modify: `tests/e2e/conftest.py`

- [ ] **Step 1: Find every fixture that calls `deploy_start` for a consumer**

Run:
```bash
grep -n "deploy_prepare\|deploy_start" tests/e2e/conftest.py | head -30
```

Expect fixtures for: `validator_gorchain_deployment`, `validator_solana_deployment`, `relayer_deployment`, `gas_oracle_deployment`, `warp_ui_deployment`, `monitoring_deployment`.

- [ ] **Step 2: For each consumer fixture, call `bridge_state_loader.populate(...)` between `deploy_prepare` and `deploy_start`**

Pattern:

```python
deploy_info = deploy_prepare(
    "hyperlane-validator", patched_path,
    namespace=E2E_NAMESPACE,
    spec_replacements=SPEC_REPLACEMENTS,
    cluster_id="validator-gorchain",
)

# Populate state CMs into the deploy dir before deployment start
bridge_state_loader.populate("hyperlane-validator", deploy_info.deploy_dir)

deploy_start(deploy_info.deploy_dir)
```

Apply this to all six consumer fixtures listed above. Add `bridge_state_loader` to each fixture's parameter list.

For fixtures that consume the state files via env-var injection only (e.g., gas-oracle, warp-ui), `populate()` is still safe to call — `CONSUMER_STATE_FILES["hyperlane-gas-oracle"]` lists what to copy. If `populate()` finds no entries for a stack, it's a no-op.

- [ ] **Step 3: Verify pytest collection**

```bash
cd tests/e2e
pytest --collect-only -q 2>&1 | tail -10
```

Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/conftest.py
git commit -m "tests: populate state CMs into consumer deploy_dirs via loader"
```

---

## Task 16: Drop `namespace:` from all 8 deployment specs

**Files:**
- Modify: `deployment/spec-deployer.yml`, `spec-warp-deployer.yml`, `spec-validator-gorchain.yml`, `spec-validator-solana.yml`, `spec-relayer.yml`, `spec-gas-oracle.yml`, `spec-minio.yml`, `spec-monitoring.yml`, `spec-warp-ui.yml`

- [ ] **Step 1: Remove the line from each file**

For each file, delete the line matching `^namespace: laconic-hyperlane$`:

```bash
for f in deployment/spec-deployer.yml deployment/spec-warp-deployer.yml deployment/spec-validator-gorchain.yml deployment/spec-validator-solana.yml deployment/spec-relayer.yml deployment/spec-gas-oracle.yml deployment/spec-minio.yml deployment/spec-monitoring.yml deployment/spec-warp-ui.yml; do
  if grep -q "^namespace: laconic-hyperlane$" "$f"; then
    grep -v "^namespace: laconic-hyperlane$" "$f" > "$f.tmp" && mv "$f.tmp" "$f"
    echo "Updated $f"
  else
    echo "Skipped $f (no match)"
  fi
done
```

- [ ] **Step 2: Verify**

```bash
grep "^namespace:" deployment/spec-*.yml
```

Expected: no output (no remaining `namespace:` keys in prod specs).

- [ ] **Step 3: Commit**

```bash
git add deployment/spec-*.yml
git commit -m "specs: drop shared laconic-hyperlane namespace; each stack uses own NS"
```

---

## Task 17: Drop `namespace: REPLACE_NAMESPACE` from test fixtures + conftest patching

**Files:**
- Modify: all `tests/e2e/fixtures/test-spec-*.yml`
- Modify: `tests/e2e/conftest.py`

- [ ] **Step 1: Remove the line from each fixture**

```bash
for f in tests/e2e/fixtures/test-spec-*.yml; do
  if grep -q "^namespace: REPLACE_NAMESPACE$" "$f"; then
    grep -v "^namespace: REPLACE_NAMESPACE$" "$f" > "$f.tmp" && mv "$f.tmp" "$f"
    echo "Updated $f"
  fi
done
```

- [ ] **Step 2: Remove REPLACE_NAMESPACE from SPEC_REPLACEMENTS**

In `tests/e2e/conftest.py`, find:

```python
SPEC_REPLACEMENTS = {
    "REPLACE_NAMESPACE": E2E_NAMESPACE,
    "REPLACE_KIND_CLUSTER": KIND_CLUSTER_NAME,
}
```

Drop the `REPLACE_NAMESPACE` entry:

```python
SPEC_REPLACEMENTS = {
    "REPLACE_KIND_CLUSTER": KIND_CLUSTER_NAME,
}
```

- [ ] **Step 3: Update `E2E_NAMESPACE` usage**

Grep for `E2E_NAMESPACE` to see where it's still used:

```bash
grep -n "E2E_NAMESPACE" tests/e2e/lib/ tests/e2e/conftest.py tests/e2e/test_*.py 2>/dev/null
```

For each `deploy_prepare(..., namespace=E2E_NAMESPACE, ...)` call: drop the `namespace=` kwarg entirely. SO will derive each stack's namespace from `laconic-{stack_name}`.

For test code that asserts pods/CMs in a specific namespace: the namespace is now `f"laconic-{stack_name}"`. The `DeploymentInfo` returned by `deploy_prepare` already exposes `.namespace`; pass it through where needed instead of `E2E_NAMESPACE`.

- [ ] **Step 4: Update `deploy_prepare` signature if it expects `namespace=`**

Run:
```bash
grep -n "def deploy_prepare" tests/e2e/lib/deploy.py
```

If `deploy_prepare` accepts `namespace=` as a parameter that's then injected into the spec, remove the parameter and the injection (since each stack's spec now drives its own NS). If keeping the parameter for back-compat, default it to `None` and skip injection when `None`.

- [ ] **Step 5: Verify collection + grep for stragglers**

```bash
cd tests/e2e
pytest --collect-only -q 2>&1 | tail -10
grep -rn "REPLACE_NAMESPACE\|E2E_NAMESPACE\s*=\|E2E_NAMESPACE\)" tests/e2e/ | head
```

Expected: no collection errors. Any remaining `E2E_NAMESPACE` references must be reviewed and resolved.

- [ ] **Step 6: Commit**

```bash
git add tests/e2e/conftest.py tests/e2e/fixtures/ tests/e2e/lib/deploy.py 2>/dev/null
git commit -m "tests: per-stack namespaces; drop shared E2E_NAMESPACE pattern"
```

---

## Task 18: Switch validator + relayer to MinIO cross-NS FQDN

**Files:**
- Modify: `stack_orchestrator/data/compose/docker-compose-hyperlane-validator.yml`
- Modify: `stack_orchestrator/data/compose/docker-compose-hyperlane-relayer.yml`

- [ ] **Step 1: Validator compose env update**

Find:

```yaml
      # MinIO for checkpoint storage (cross-stack, k8s service DNS)
      AWS_ENDPOINT_URL_S3: "http://hyperlane-minio:9000"
```

Replace with:

```yaml
      # MinIO for checkpoint storage (cross-namespace via FQDN; PR2 replaces with external-services)
      AWS_ENDPOINT_URL_S3: "http://hyperlane-minio.laconic-hyperlane-minio.svc.cluster.local:9000"
```

- [ ] **Step 2: Relayer compose env update**

Same change in the relayer compose:

```yaml
      AWS_ENDPOINT_URL_S3: "http://hyperlane-minio.laconic-hyperlane-minio.svc.cluster.local:9000"
```

- [ ] **Step 3: Commit**

```bash
git add stack_orchestrator/data/compose/docker-compose-hyperlane-validator.yml stack_orchestrator/data/compose/docker-compose-hyperlane-relayer.yml
git commit -m "validator/relayer: reach MinIO via cross-namespace FQDN"
```

---

## Task 19: Update CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Replace the "Cross-stack dependencies" memory note**

Find:

```markdown
### Cross-stack dependencies (same namespace required)
- 17 dependencies across stacks: ConfigMaps, Services, RBAC
- All stacks share namespace `laconic-{cluster-id}`
- Namespace separation not feasible without SO changes
```

Replace with:

```markdown
### Per-stack namespaces
- Each stack runs in its own namespace (`laconic-{stack_name}`). SO enforces this via the `laconic.com/deployment-dir` annotation; the old shared-namespace pattern was a workaround for SO limitations that no longer exist.
- Cross-stack data flows through committed state files in `deployment/bridges/<bridge>/generated/`, not through in-cluster ConfigMaps. The deployer Jobs write files via a Kind `extraMounts` host-path bind; consumers mount those files as plain ConfigMaps via SO's `configmaps:` block.
- Cross-stack Services (MinIO) are reached via FQDN in PR1; PR2 declares them via `external-services:` in each consumer spec.
```

- [ ] **Step 2: Update the "ConfigMap lifecycle (SO k8s)" note**

Find:

```markdown
### ConfigMap lifecycle (SO k8s)
- SO creates ConfigMaps named `{cluster-id}-{volume-name}` from files in `{deploy_dir}/configmaps/{name}/`
- Deployer jobs create ConfigMaps with bare names (`hyperlane-agent-config`) via kubectl
- These are two separate mechanisms — no built-in way for a pod to mount a pre-existing ConfigMap by name
- SO does NOT support init containers (as of current code)
- Warp deployer reads deployer ConfigMaps at runtime via `kubectl get configmap` (RBAC-based)
```

Replace with:

```markdown
### ConfigMap lifecycle (SO k8s)
- SO creates ConfigMaps from files in `{deploy_dir}/configmaps/{name}/` (sourced from `data/config/{name}/` or copied in by the `bridge_state_loader` for state-derived CMs).
- Deployer Jobs no longer create ConfigMaps directly — they write JSON files to `/state` (host-path via Kind `extraMounts`).
- SO still does not support init containers; the design avoids them by populating CMs at deploy-create time instead of pod-start time.
- Warp-deployer reads `program-ids.json` from `/state` (not from k8s); RBAC for kubectl-from-pod is gone.
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: CLAUDE.md reflects per-stack NS and state-file flow"
```

---

## Task 20: Update architecture-decisions.md

**Files:**
- Modify: `docs/architecture-decisions.md`

- [ ] **Step 1: Add a new section superseding the single-namespace decision**

Find the original section on namespace pattern (search for "namespace" or "single namespace" in the file). Add a new section just before or after it titled:

```markdown
## Bridge State Distribution (supersedes "Single Namespace")

**Date:** 2026-05-20 (PR1)
**Status:** Active. Replaces the pre-2026 decision to put all stacks in `laconic-hyperlane`.

**Decision:** Deployer Jobs produce state files on disk; consumers receive those files as plain k8s ConfigMaps populated at `deployment start` time. Each stack runs in its own namespace.

**Why this changed:**
- SO's k8s path now enforces per-deployment namespace ownership (`laconic.com/deployment-dir` annotation), making the shared-NS pattern fail on the second `deployment start`.
- The original kubectl-from-pod pattern coupled consumers to the deployer's k8s cluster at runtime, blocking multi-machine deployments where validators run on separate hosts.
- ConfigMap idempotency and source-path resolution have improved in SO; the workarounds the shared-NS pattern was built around are gone.

**Components:**
- State files: `deployment/bridges/<bridge>/generated/` (deployer output, committed), `operator/` (operator-supplied identity/policy), `logs/` (deployer Job stdout/stderr, audit trail).
- Distribution: `BridgeStateLoader` (in `tests/e2e/lib/state_loader.py`) for dev; ansible (PR3) for prod. Both copy state files into each consumer's `{deploy_dir}/configmaps/` before `deployment start`.
- Namespaces: per-stack, derived as `laconic-{stack_name}` (or overridden in spec for multi-instance deployments like multiple validators per chain).

**Alternatives considered:** Pattern B (central state cluster, HTTP fetch at runtime), Pattern C (artifacts in MinIO). See `docs/superpowers/specs/2026-05-20-bridge-state-extract-and-distribution-design.md`.

**Per-validator MinIO** (future direction): documented in the design doc, deferred. v1 prod uses one shared MinIO with per-validator users and bucket-prefix policies.
```

- [ ] **Step 2: Update or remove any stale subsection on the old single-NS pattern**

Read the rest of the file. If there's an explicit "Single Namespace" decision section, replace its body with a one-liner pointer to the new section above ("Superseded by Bridge State Distribution, 2026-05-20").

- [ ] **Step 3: Commit**

```bash
git add docs/architecture-decisions.md
git commit -m "docs: architecture-decisions records the bridge-state-distribution refactor"
```

---

## Task 21: Full e2e run + cleanup

- [ ] **Step 1: Inventory remaining loose ends**

Run:
```bash
grep -rn "hyperlane-program-ids\|hyperlane-agent-config\|hyperlane-gas-oracle-config\|hyperlane-multisig-config\|hyperlane-token-config\|hyperlane-warp-deploy-outputs\|hyperlane-registry\|REPLACE_NAMESPACE\|E2E_NAMESPACE" tests/ stack_orchestrator/ deployment/ docs/ 2>/dev/null | grep -v "/.git/" | grep -v "docs/superpowers/"
```

Each remaining match must be either:
- a state file name still used internally (the deployer-output filenames live on, just without the `hyperlane-` prefix);
- a docs reference describing the old pattern (likely fine);
- or an actual bug — investigate and fix.

- [ ] **Step 2: Run the e2e suite end-to-end (user executes)**

Owner: user. Command:

```bash
cd tests/e2e
pytest -x -v 2>&1 | tee /tmp/e2e-run.log
```

Expected: all 11 test files green. If a test fails, capture which one and which assertion; this plan does not predict failure modes but the most likely classes are:
- A consumer reaching MinIO that the cross-NS FQDN didn't reach (check MinIO's `hyperlane-minio` Service exists in `laconic-hyperlane-minio` NS).
- A test asserting on a CM that's no longer created (these were updated in T9–T11; any leftover assertion in another test file needs the same treatment).
- A pod stuck in `Init` because the dropped `agent-config-init` container is still referenced in a `depends_on`.
- A namespace lookup that hardcoded `laconic-hyperlane` (grep for any remaining occurrence).

- [ ] **Step 3: Commit any small fixups discovered during e2e**

If anything needs fixing, each fix is its own commit using the same patterns above. No omnibus "fixes" commit — one issue per commit.

---

## Plan Self-Review Summary

- **Spec coverage:** all PR1-scope items from the design doc map to tasks:
  - Deployer extract → T4–T8
  - Consumer plain CM mounts → T12–T15
  - Per-stack namespaces → T16–T17
  - bridge_state_loader fixture → T1–T3, T11, T15
  - MinIO interim FQDN → T18
  - Docs → T19–T20
  - E2e green → T21
- **PR2 and PR3+ scope** explicitly NOT in this plan: MinIO `external-services:` migration, cert-manager TLS, per-validator MinIO users, ansible — these are deferred per the spec.
- **Placeholders:** none. Every step shows the actual code/command.
- **Type consistency:** `BridgeStateLoader.populate(stack_name, deploy_dir)`, `.read_json(file_rel)`, `.read_program_ids(chain)`, `.expected_files_for(stack_name)`, `.assert_present(stack_name)` — used consistently across T1, T9, T10, T11, T15.
