# Warp-UI Route Config in the Deployer — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate the warp-UI `warpRoutes.yaml` (a Hyperlane `WarpCoreConfig`) inside the warp-deployer, so the type→standard/connections transform lives in one tested place and `publish-bridge-state` + `conftest` only distribute it.

**Architecture:** A new, parameterised `build-warp-ui-config.sh` (bash + jq) reads each selected route's already-written artifacts (`token-config.json`, `warp-deploy-outputs/program-ids.json`) plus the core `program-ids.json` mailboxes and emits a top-level `${STATE_DIR}/warpRoutes.yaml`. `deploy.sh` calls it after its route loop. The top-level file survives SO's single-level ConfigMap model and reaches the pod exactly as today; consumers stop building it.

**Tech Stack:** bash, jq, Ansible (publish playbook), Python/pytest (e2e + unit tests).

**Spec:** `docs/superpowers/specs/2026-06-08-warp-ui-config-in-deployer-design.md`

---

### Task 1: `build-warp-ui-config.sh` aggregation script (the transform)

**Files:**
- Create: `stack_orchestrator/data/config/warp-deployer-scripts-config/build-warp-ui-config.sh`
- Test: `tests/e2e/test_warp_ui_config_builder.py`

- [ ] **Step 1: Write the failing test**

Create `tests/e2e/test_warp_ui_config_builder.py`:

```python
"""Unit test for the warp-deployer's build-warp-ui-config.sh aggregation:
given per-route deployer artifacts, it emits a valid WarpCoreConfig covering
exactly the selected routes. No cluster required (pure bash + jq)."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILDER = (
    REPO_ROOT
    / "stack_orchestrator/data/config/warp-deployer-scripts-config/build-warp-ui-config.sh"
)


def _write(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))


@pytest.fixture
def fixture_state(tmp_path: Path) -> tuple[Path, Path]:
    state = tmp_path / "state"
    menus = tmp_path / "config" / "warp-routes"
    _write(state / "program-ids.json", {
        "solana": {"mailbox": "MAILBOX_SOL"},
        "gorchain": {"mailbox": "MAILBOX_GOR"},
    })
    # USDC: solana collateral <-> gorchain synthetic
    _write(menus / "usdc.json", {"name": "USDC-solana-gorchain"})
    _write(state / "warp-routes/USDC-solana-gorchain/token-config.json", {"warpRoute": {
        "name": "USDC-solana-gorchain",
        "solana": {"type": "collateral", "name": "USD Coin", "symbol": "USDC", "decimals": 6, "token": "MINT_USDC"},
        "gorchain": {"type": "synthetic", "name": "USD Coin", "symbol": "USDC", "decimals": 6, "mint": "MINT_GUSDC"},
    }})
    _write(state / "warp-routes/USDC-solana-gorchain/warp-deploy-outputs/program-ids.json", {
        "solana": {"base58": "WARP_USDC_SOL"},
        "gorchain": {"base58": "WARP_USDC_GOR"},
    })
    # SOL: solana native <-> gorchain synthetic
    _write(menus / "sol.json", {"name": "SOL-solana-gorchain"})
    _write(state / "warp-routes/SOL-solana-gorchain/token-config.json", {"warpRoute": {
        "name": "SOL-solana-gorchain",
        "solana": {"type": "native", "name": "Solana", "symbol": "SOL", "decimals": 9},
        "gorchain": {"type": "synthetic", "name": "Solana", "symbol": "SOL", "decimals": 9, "mint": "MINT_GSOL"},
    }})
    _write(state / "warp-routes/SOL-solana-gorchain/warp-deploy-outputs/program-ids.json", {
        "solana": {"base58": "WARP_SOL_SOL"},
        "gorchain": {"base58": "WARP_SOL_GOR"},
    })
    return state, menus


def _run(state: Path, menus: Path, routes: str) -> dict:
    env = {
        **os.environ,
        "STATE_DIR": str(state),
        "WARP_ROUTES_DIR": str(menus),
        "WARP_ROUTES": routes,
    }
    subprocess.run(["bash", str(BUILDER)], env=env, check=True, capture_output=True, text=True)
    return json.loads((state / "warpRoutes.yaml").read_text())


@pytest.mark.skipif(not shutil.which("jq"), reason="jq required")
def test_builds_all_routes(fixture_state):
    state, menus = fixture_state
    cfg = _run(state, menus, "usdc sol")

    assert cfg["options"] == {}
    assert len(cfg["tokens"]) == 4
    tokens = {(t["chainName"], t["symbol"]): t for t in cfg["tokens"]}

    assert tokens[("solana", "USDC")]["standard"] == "SealevelHypCollateral"
    assert tokens[("gorchain", "USDC")]["standard"] == "SealevelHypSynthetic"
    assert tokens[("solana", "SOL")]["standard"] == "SealevelHypNative"
    assert tokens[("gorchain", "SOL")]["standard"] == "SealevelHypSynthetic"

    assert tokens[("solana", "USDC")]["collateralAddressOrDenom"] == "MINT_USDC"
    assert tokens[("gorchain", "USDC")]["collateralAddressOrDenom"] == "MINT_GUSDC"
    assert "collateralAddressOrDenom" not in tokens[("solana", "SOL")]
    assert tokens[("gorchain", "SOL")]["collateralAddressOrDenom"] == "MINT_GSOL"

    # decimals must stay integers — the SDK token schema is strict z.number().int()
    assert tokens[("solana", "USDC")]["decimals"] == 6
    assert isinstance(tokens[("solana", "USDC")]["decimals"], int)
    assert tokens[("solana", "SOL")]["decimals"] == 9

    sol_usdc = tokens[("solana", "USDC")]
    assert sol_usdc["addressOrDenom"] == "WARP_USDC_SOL"
    assert sol_usdc["mailbox"] == "MAILBOX_SOL"
    assert sol_usdc["connections"] == [{"token": "sealevel|gorchain|WARP_USDC_GOR"}]
    assert tokens[("gorchain", "USDC")]["connections"] == [{"token": "sealevel|solana|WARP_USDC_SOL"}]


@pytest.mark.skipif(not shutil.which("jq"), reason="jq required")
def test_selects_only_named_routes(fixture_state):
    state, menus = fixture_state
    cfg = _run(state, menus, "usdc")
    assert len(cfg["tokens"]) == 2
    assert {t["chainName"] for t in cfg["tokens"]} == {"solana", "gorchain"}
    assert all(t["symbol"] == "USDC" for t in cfg["tokens"])


@pytest.mark.skipif(not shutil.which("jq"), reason="jq required")
def test_missing_artifact_fails_loud(fixture_state):
    state, menus = fixture_state
    shutil.rmtree(state / "warp-routes/SOL-solana-gorchain")
    with pytest.raises(subprocess.CalledProcessError):
        _run(state, menus, "usdc sol")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd tests/e2e && python -m pytest test_warp_ui_config_builder.py -v`
Expected: FAIL — `build-warp-ui-config.sh` does not exist, so `subprocess.run(..., check=True)` raises (the script path is missing / bash errors).

- [ ] **Step 3: Write the script**

Create `stack_orchestrator/data/config/warp-deployer-scripts-config/build-warp-ui-config.sh`:

```bash
#!/bin/bash
# Build the warp-UI route config (warpRoutes.yaml) from the per-route artifacts the
# warp-deployer already wrote under ${STATE_DIR}/warp-routes/<name>/. Emits a Hyperlane
# WarpCoreConfig ({tokens, options}) covering exactly the routes named in WARP_ROUTES —
# one token entry per chain side. The warp-UI loads this file at runtime.
#
# Single source of the type->standard / connections transform; ops (publish-bridge-state)
# and e2e (conftest) only distribute the result.
set -euo pipefail

STATE_DIR="${STATE_DIR:-${STATE_OUTPUT_DIR:-/state}}"
WARP_ROUTES_DIR="${WARP_ROUTES_DIR:-/config/warp-routes}"
PROGRAM_IDS_FILE="${PROGRAM_IDS_FILE:-${STATE_DIR}/program-ids.json}"
: "${WARP_ROUTES:?WARP_ROUTES must be set to a comma/space-separated list of route stems}"

warp_ui_standard() {  # $1=type -> Sealevel token standard
  case "$1" in
    collateral) echo "SealevelHypCollateral" ;;
    synthetic)  echo "SealevelHypSynthetic" ;;
    native)     echo "SealevelHypNative" ;;
    *) echo "ERROR: warp-UI config: unknown token type '$1'" >&2; exit 1 ;;
  esac
}

tokens="[]"
for route in $(echo "${WARP_ROUTES}" | tr ',' ' '); do
  cfg="${WARP_ROUTES_DIR}/${route}.json"
  [ -s "$cfg" ] || { echo "ERROR: warp-UI config: menu $cfg not found for route '${route}'" >&2; exit 1; }
  name=$(jq -r '.name' "$cfg")
  route_dir="${STATE_DIR}/warp-routes/${name}"
  tcfg="${route_dir}/token-config.json"
  wpids="${route_dir}/warp-deploy-outputs/program-ids.json"
  for f in "$tcfg" "$wpids"; do
    [ -s "$f" ] || { echo "ERROR: warp-UI config: missing ${f} for route '${name}'" >&2; exit 1; }
  done

  # The two chain sides (warpRoute object minus the "name" key).
  set -- $(jq -r '.warpRoute | keys[] | select(. != "name")' "$tcfg")
  side_a="$1"; side_b="$2"

  for pair in "${side_a} ${side_b}" "${side_b} ${side_a}"; do
    set -- $pair; self="$1"; other="$2"
    side=$(jq -c --arg c "$self" '.warpRoute[$c]' "$tcfg")
    standard=$(warp_ui_standard "$(printf '%s' "$side" | jq -r '.type')")
    self_prog=$(jq -r --arg c "$self" '.[$c].base58' "$wpids")
    other_prog=$(jq -r --arg c "$other" '.[$c].base58' "$wpids")
    mailbox=$(jq -r --arg c "$self" '.[$c].mailbox // ""' "${PROGRAM_IDS_FILE}")

    entry=$(printf '%s' "$side" | jq \
      --arg chainName "$self" --arg standard "$standard" \
      --arg addr "$self_prog" --arg mailbox "$mailbox" \
      --arg conn "sealevel|${other}|${other_prog}" \
      '{chainName:$chainName, standard:$standard, name:.name, symbol:.symbol,
        decimals:.decimals, addressOrDenom:$addr, mailbox:$mailbox,
        connections:[{token:$conn}]}
       + (if .type=="collateral" then {collateralAddressOrDenom:.token}
          elif .type=="synthetic" then {collateralAddressOrDenom:.mint}
          else {} end)')
    tokens=$(jq -c --argjson e "$entry" '. + [$e]' <<<"$tokens")
  done
done

# JSON is valid YAML and the warp-UI loader parses either (tryParseJsonOrYaml); emitting
# JSON keeps decimals numeric without needing a YAML emitter in the image.
jq -n --argjson tokens "$tokens" '{tokens:$tokens, options:{}}' > "${STATE_DIR}/warpRoutes.yaml"
echo "Wrote ${STATE_DIR}/warpRoutes.yaml ($(jq '.tokens | length' "${STATE_DIR}/warpRoutes.yaml") token entries)"
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd tests/e2e && python -m pytest test_warp_ui_config_builder.py -v`
Expected: PASS (3 tests). If `jq` is absent they SKIP — install jq and re-run; do not accept skipped as done.

- [ ] **Step 5: Lint the script**

Run: `bash -n stack_orchestrator/data/config/warp-deployer-scripts-config/build-warp-ui-config.sh`
Expected: no output (syntax OK). If `shellcheck` is installed, also run it and address warnings.

- [ ] **Step 6: Commit**

```bash
git add stack_orchestrator/data/config/warp-deployer-scripts-config/build-warp-ui-config.sh tests/e2e/test_warp_ui_config_builder.py
git commit -m "feat(warp-deployer): build warpRoutes.yaml from per-route artifacts"
```

---

### Task 2: Call the builder from `deploy.sh`

**Files:**
- Modify: `stack_orchestrator/data/config/warp-deployer-scripts-config/deploy.sh:363`

- [ ] **Step 1: Add the invocation after the route loop**

In `deploy.sh`, the route loop ends with:

```bash
echo "=== All selected warp routes processed ==="
```

Immediately after that line (before the `# Clean up deployer keypair` block) insert:

```bash

# Build the warp-UI route config from the per-route artifacts written above (single
# source of the WarpCoreConfig transform; publish/conftest only distribute the result).
echo ""
echo "=== Building warp-UI route config ==="
STATE_DIR="${STATE_DIR}" bash /opt/scripts/build-warp-ui-config.sh
```

(`/opt/scripts` is where `warp-deployer-scripts-config` mounts — see
`compose-jobs/docker-compose-hyperlane-svm-warp-deployer.yml`. `WARP_ROUTES` and
`STATE_OUTPUT_DIR` are already container env, and `WARP_ROUTES_DIR` defaults to
`/config/warp-routes`, so no other vars need passing.)

- [ ] **Step 2: Syntax-check deploy.sh**

Run: `bash -n stack_orchestrator/data/config/warp-deployer-scripts-config/deploy.sh`
Expected: no output.

- [ ] **Step 3: Verify the call is wired**

Run: `grep -n "build-warp-ui-config.sh" stack_orchestrator/data/config/warp-deployer-scripts-config/deploy.sh`
Expected: one match in the post-loop section. (End-to-end behaviour is exercised in Task 5; deploy.sh can't run outside the container because it needs `hyperlane-sealevel-client`.)

- [ ] **Step 4: Commit**

```bash
git add stack_orchestrator/data/config/warp-deployer-scripts-config/deploy.sh
git commit -m "feat(warp-deployer): emit warpRoutes.yaml after deploying routes"
```

---

### Task 3: Remove the warpRoutes build from `publish-bridge-state.yml`

**Files:**
- Modify: `ops/playbooks/publish-bridge-state.yml` (delete the warpRoutes-building block; keep `Parse program-ids.json`, the core spec patches, and the git add)

- [ ] **Step 1: Delete the warpRoutes-building tasks**

Remove the contiguous block of tasks from (and including) `Read the warp-deployer spec (for WARP_ROUTES)` through (and including) `Write warpRoutes.yaml into the generated state`. Concretely, delete these task `- name:` blocks and their bodies:

- `Read the warp-deployer spec (for WARP_ROUTES)`
- `Resolve all warp-route stems from WARP_ROUTES`
- `Slurp each route's menu file`
- `Init route items accumulator`
- `Build route items list (route name)`
- `Slurp token-config.json for each route`
- `Slurp warp-deploy-outputs/program-ids.json for each route`
- `Init warp tokens accumulator`
- `Accumulate warp token entries for all routes`
- `Write warpRoutes.yaml into the generated state`

Leave intact, immediately above the deletion: `Parse program-ids.json` (sets `_pids`).
Leave intact, immediately below the deletion: the `Patch core deployment-derived config keys into committed specs` task (it already patches `spec-warp-ui.yml`'s `GORCHAIN_MAILBOX`/`SOLANA_MAILBOX` from `_pids`, which the entrypoint needs for `chains.yaml`) and the `Stage the generated paths and patched specs` git-add (which still adds `generated/`, now containing the deployer-written `warpRoutes.yaml`).

The result: after `Parse program-ids.json`, the next task is `Patch core deployment-derived config keys into committed specs`.

- [ ] **Step 2: Update the explanatory comment**

The comment directly above `Parse program-ids.json` currently begins:

```yaml
    # The warp-UI needs a WarpCoreConfig (warpRoutes.yaml) covering ALL routes in
    # the spec's WARP_ROUTES. Each route's per-route state lives under
    # warp-routes/<name>/, where <name> comes from that route's menu file.
```

Replace that comment with:

```yaml
    # warpRoutes.yaml is built by the warp-deployer (build-warp-ui-config.sh) and lands
    # in generated/; publish only copies it. Here we just patch the core scalar values
    # (IGP/mailbox) into the committed specs from program-ids.json.
```

- [ ] **Step 3: Lint the playbook**

Run: `cd ops && ansible-lint playbooks/publish-bridge-state.yml`
Expected: no new errors (same baseline as before the change).

- [ ] **Step 4: Verify the deletion and the kept pieces**

Run:
```bash
grep -nE "warpRoutes.yaml|_warp_tokens|WARP_ROUTES|_warp_stems" ops/playbooks/publish-bridge-state.yml
grep -n "Patch core deployment-derived config keys" ops/playbooks/publish-bridge-state.yml
```
Expected: first grep returns nothing (the build block is gone); second returns one match (the core patch task remains).

- [ ] **Step 5: Commit**

```bash
git add ops/playbooks/publish-bridge-state.yml
git commit -m "refactor(ops): drop warpRoutes.yaml build from publish (deployer owns it)"
```

---

### Task 4: Distribute the deployer-built file in e2e (conftest + state_loader)

**Files:**
- Modify: `tests/e2e/lib/state_loader.py:20-38` (`CONSUMER_STATE_FILES`)
- Modify: `tests/e2e/conftest.py` (delete `_build_warp_ui_config`; simplify `warp_ui_deployment`)
- Test: `tests/e2e/test_state_loader_warp_ui.py`

- [ ] **Step 1: Write the failing test for the populate contract**

Create `tests/e2e/test_state_loader_warp_ui.py`:

```python
"""BridgeStateLoader must distribute the deployer-built warpRoutes.yaml into the
warp-ui-config ConfigMap dir (top-level file -> survives SO's flat ConfigMap)."""
from pathlib import Path

import pytest

from lib.state_loader import BridgeStateLoader


def test_populate_copies_warp_routes(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "warpRoutes.yaml").write_text("tokens: []\noptions: {}\n")

    deploy_dir = tmp_path / "deploy"
    BridgeStateLoader(state).populate("hyperlane-warp-ui", deploy_dir)

    out = deploy_dir / "configmaps" / "warp-ui-config" / "warpRoutes.yaml"
    assert out.is_file()
    assert "tokens" in out.read_text()


def test_populate_missing_warp_routes_fails_loud(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()  # no warpRoutes.yaml written
    with pytest.raises(FileNotFoundError):
        BridgeStateLoader(state).populate("hyperlane-warp-ui", tmp_path / "deploy")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd tests/e2e && python -m pytest test_state_loader_warp_ui.py -v`
Expected: FAIL — `CONSUMER_STATE_FILES["hyperlane-warp-ui"]` is `[]`, so `populate` copies nothing and `out` is missing (first test), and `assert_present` does not check for the file (second test does not raise).

- [ ] **Step 3: Add the warp-ui ConfigMap entry**

In `tests/e2e/lib/state_loader.py`, change the warp-ui entry. Find:

```python
    # Env-var consumers and stacks that don't read deployer state at all:
    "hyperlane-svm-deployer": [],
    "hyperlane-svm-warp-deployer": [],   # reads /state at runtime via mount
    "hyperlane-minio": [],
    "hyperlane-gas-oracle": [],          # env-var injection via read_json
    "hyperlane-monitoring": [],          # env-var injection via read_json
    "hyperlane-warp-ui": [],             # env-var injection via read_json
```

Replace the `hyperlane-warp-ui` line with:

```python
    # warp-ui mounts the deployer-built warpRoutes.yaml as the warp-ui-config CM:
    "hyperlane-warp-ui": [
        ("warpRoutes.yaml", "warp-ui-config"),
    ],
```

Also update the module docstring comment at the top of the dict (lines ~20-23) that lists `warp-ui` among env-var-only consumers — remove `warp-ui` from that parenthetical so it reads `(gas-oracle, monitoring)`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd tests/e2e && python -m pytest test_state_loader_warp_ui.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Delete `_build_warp_ui_config` and simplify `warp_ui_deployment`**

In `tests/e2e/conftest.py`:

(a) Delete the entire `_build_warp_ui_config` function (the `def _build_warp_ui_config(...)` block, including its `_STANDARD`/`_MAILBOX` locals and the trailing blank line).

(b) In `warp_ui_deployment`, delete the `import yaml` line added at the top of that fixture.

(c) In `warp_ui_deployment`, replace this block:

```python
    log.info("Generating warp-ui-config configmap (warpRoutes.yaml)...")
    warp_ui_cfg = _build_warp_ui_config(bridge_state_loader, gorchain_mailbox, solana_mailbox)
    cmdir = deploy_info.deploy_dir / "configmaps" / "warp-ui-config"
    cmdir.mkdir(parents=True, exist_ok=True)
    (cmdir / "warpRoutes.yaml").write_text(yaml.safe_dump(warp_ui_cfg))

    bridge_state_loader.populate("hyperlane-warp-ui", deploy_info.deploy_dir)
```

with just:

```python
    # warpRoutes.yaml is built by the warp-deployer; populate copies it into the
    # warp-ui-config ConfigMap dir.
    bridge_state_loader.populate("hyperlane-warp-ui", deploy_info.deploy_dir)
```

(`gorchain_mailbox`/`solana_mailbox` are still used above for the spec mailbox patch — leave those.)

- [ ] **Step 6: Verify no dangling references and lint**

Run:
```bash
cd tests/e2e
grep -n "_build_warp_ui_config" conftest.py            # expect: no matches
python -m pytest --collect-only -q >/dev/null          # expect: collects with no ImportError
ruff check conftest.py lib/state_loader.py test_state_loader_warp_ui.py
```
Expected: no `_build_warp_ui_config` matches, clean collection, ruff passes.

- [ ] **Step 7: Commit**

```bash
git add tests/e2e/lib/state_loader.py tests/e2e/conftest.py tests/e2e/test_state_loader_warp_ui.py
git commit -m "test(e2e): distribute deployer-built warpRoutes.yaml; drop conftest builder"
```

---

### Task 5: Docs sync + full e2e verification

**Files:**
- Modify: `specs/stack-specifications.md` (warp-deployer outputs / warp-ui inputs)
- Modify: `specs/e2e-test-spec.md` (warp-ui config provenance)

- [ ] **Step 1: Update the stack specs**

In `specs/stack-specifications.md`, locate the warp-deployer outputs description and add `warpRoutes.yaml` to its emitted state (built by `build-warp-ui-config.sh` from the per-route artifacts). In the warp-ui section, change any text that says the route config is built by `publish-bridge-state`/conftest to state it is produced by the warp-deployer and distributed via the `warp-ui-config` ConfigMap. (Search: `grep -n "warpRoutes\|warp-ui\|publish-bridge-state" specs/stack-specifications.md` to find the lines.)

- [ ] **Step 2: Update the e2e spec**

In `specs/e2e-test-spec.md`, find the warp-UI test description (search `grep -n "warpRoutes\|warp_ui\|warp-ui" specs/e2e-test-spec.md`) and note that `warpRoutes.yaml` is now deployer-produced (`build-warp-ui-config.sh`) and asserted via the served file in `test_10_warp_ui.py`; conftest no longer builds it.

- [ ] **Step 3: Run the new unit tests together**

Run: `cd tests/e2e && python -m pytest test_warp_ui_config_builder.py test_state_loader_warp_ui.py -v`
Expected: PASS (5 tests total).

- [ ] **Step 4: Full e2e gate (requires a cluster)**

Run the warp-deployer → warp-UI slice end to end (the warp-deployer Job picks up the new `build-warp-ui-config.sh` from the `warp-deployer-scripts-config` ConfigMap — no image rebuild needed; the warp-UI image is unchanged):

```bash
cd tests/e2e
python -m pytest test_02_warp_deployer.py test_10_warp_ui.py test_12_warp_ui_bridge.py -v
```

Expected: PASS. Specifically, `test_10_warp_ui.py::TestWarpUI::test_warp_ui_serves_runtime_config` confirms the served `warpRoutes.yaml` carries both routes (`SealevelHypCollateral`, `SealevelHypNative`, ≥4 `chainName`), and `test_12_warp_ui_bridge.py` bridges both USDC and native SOL — proving the deployer-built config drives the UI.

- [ ] **Step 5: Commit the docs**

```bash
git add specs/stack-specifications.md specs/e2e-test-spec.md
git commit -m "docs: warp-UI route config is produced by the warp-deployer"
```

---

## Self-Review

**1. Spec coverage:**
- "Producer — aggregation step in deploy.sh" → Tasks 1 (script) + 2 (wiring). ✓
- "Contract (token schema)" → Task 1 test asserts standards, integer decimals, connections, collateral/synthetic mint fields, native has none. ✓
- "Consumers — reduced to distribution": publish → Task 3; conftest/state_loader → Task 4; entrypoint/state_distribute/specs/compose unchanged (no task — correct, they don't change). ✓
- "Add-a-route flow" → covered by the script iterating `WARP_ROUTES` (Task 1 `test_selects_only_named_routes`) and the unconditional post-loop call (Task 2). ✓
- "Migration" → the script reads persistent artifacts, not in-loop state (Task 1 design); no separate task needed. ✓
- "Testing" (deployer unit + e2e) → Tasks 1 and 5. ✓
- "Risks: jq numeric fidelity / route-name parity / e2e ordering" → Task 1 asserts integer decimals; the script resolves stem→name via `jq -r .name` exactly as `deploy_route`; Task 5 runs the deployer before the UI. ✓

**2. Placeholder scan:** No TBD/TODO; every code step has complete code; every command has an expected result. ✓

**3. Type/name consistency:** `build-warp-ui-config.sh` path, env var names (`STATE_DIR`, `WARP_ROUTES`, `WARP_ROUTES_DIR`, `PROGRAM_IDS_FILE`), the `("warpRoutes.yaml", "warp-ui-config")` tuple, and the emitted field names (`chainName`, `standard`, `addressOrDenom`, `collateralAddressOrDenom`, `connections[].token`) are identical across the script, the deploy.sh call, state_loader, and the tests. ✓
