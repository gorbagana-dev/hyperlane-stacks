# Warp-UI Route Config in the Deployer — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate the warp-UI `warpRoutes.yaml` inside the warp-deployer (single tested transform), distribute it via the existing `state_distribute`/`populate` mechanism scoped to the existing `generated/warp-routes/` tree, and verify it through the existing two-route e2e.

**Architecture:** A new `build-warp-ui-config.sh` (bash + jq) reads each selected route's artifacts + core mailboxes and writes `${STATE_DIR}/warp-routes/warpRoutes.yaml`. `deploy.sh` calls it after its route loop. `state_distribute` gains an optional `generated_subdir` so the warp-UI play sources `generated/warp-routes/` — the ConfigMap then holds only `warpRoutes.yaml`. `publish-bridge-state` and `conftest` stop building the file.

**Tech Stack:** bash, jq, Ansible, Python/pytest (e2e).

**Spec:** `docs/superpowers/specs/2026-06-08-warp-ui-config-in-deployer-design.md`

---

### Task 1: `build-warp-ui-config.sh` aggregation script (the transform)

**Files:**
- Create: `stack_orchestrator/data/config/warp-deployer-scripts-config/build-warp-ui-config.sh`

- [ ] **Step 1: Write the script**

Create `stack_orchestrator/data/config/warp-deployer-scripts-config/build-warp-ui-config.sh`:

```bash
#!/bin/bash
# Build the warp-UI route config (warp-routes/warpRoutes.yaml) from the per-route artifacts
# the warp-deployer already wrote under ${STATE_DIR}/warp-routes/<name>/. Emits a Hyperlane
# WarpCoreConfig ({tokens, options}) covering exactly the routes named in WARP_ROUTES —
# one token entry per chain side. The warp-UI loads this file at runtime.
#
# Written into its own warp-ui/ subdir so state_distribute can scope the warp-ui ConfigMap
# to just this file. Single source of the type->standard / connections transform; ops
# (publish-bridge-state) and e2e (conftest) only distribute the result.
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
mkdir -p "${STATE_DIR}/warp-routes"
jq -n --argjson tokens "$tokens" '{tokens:$tokens, options:{}}' > "${STATE_DIR}/warp-routes/warpRoutes.yaml"
echo "Wrote ${STATE_DIR}/warp-routes/warpRoutes.yaml ($(jq '.tokens | length' "${STATE_DIR}/warp-routes/warpRoutes.yaml") token entries)"
```

- [ ] **Step 2: Syntax-check**

Run: `bash -n stack_orchestrator/data/config/warp-deployer-scripts-config/build-warp-ui-config.sh`
Expected: no output. If `shellcheck` is installed, run it too and address warnings.

- [ ] **Step 3: Dev smoke run against a temp fixture (not committed)**

Run (requires `jq`):

```bash
S=$(mktemp -d); C="$S/cfg"
mkdir -p "$S/warp-routes/USDC-solana-gorchain/warp-deploy-outputs" "$C"
echo '{"solana":{"mailbox":"MB_SOL"},"gorchain":{"mailbox":"MB_GOR"}}' > "$S/program-ids.json"
echo '{"name":"USDC-solana-gorchain"}' > "$C/usdc.json"
echo '{"warpRoute":{"name":"USDC-solana-gorchain","solana":{"type":"collateral","name":"USD Coin","symbol":"USDC","decimals":6,"token":"MINT_USDC"},"gorchain":{"type":"synthetic","name":"USD Coin","symbol":"USDC","decimals":6,"mint":"MINT_GUSDC"}}}' > "$S/warp-routes/USDC-solana-gorchain/token-config.json"
echo '{"solana":{"base58":"WARP_SOL"},"gorchain":{"base58":"WARP_GOR"}}' > "$S/warp-routes/USDC-solana-gorchain/warp-deploy-outputs/program-ids.json"
STATE_DIR="$S" WARP_ROUTES_DIR="$C" WARP_ROUTES="usdc" \
  bash stack_orchestrator/data/config/warp-deployer-scripts-config/build-warp-ui-config.sh
jq . "$S/warp-routes/warpRoutes.yaml"
```

Expected: 2 token entries; `solana/USDC` has `standard:"SealevelHypCollateral"`,
`collateralAddressOrDenom:"MINT_USDC"`, `decimals:6` (unquoted number), `mailbox:"MB_SOL"`,
`connections:[{"token":"sealevel|gorchain|WARP_GOR"}]`; `gorchain/USDC` is the synthetic
mirror. Then `rm -rf "$S"`.

- [ ] **Step 4: Commit**

```bash
git add stack_orchestrator/data/config/warp-deployer-scripts-config/build-warp-ui-config.sh
git commit -m "feat(warp-deployer): build warp-routes/warpRoutes.yaml from per-route artifacts"
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
`STATE_OUTPUT_DIR` are already container env; `WARP_ROUTES_DIR` defaults to
`/config/warp-routes`.)

- [ ] **Step 2: Syntax-check + verify wiring**

Run:
```bash
bash -n stack_orchestrator/data/config/warp-deployer-scripts-config/deploy.sh
grep -n "build-warp-ui-config.sh" stack_orchestrator/data/config/warp-deployer-scripts-config/deploy.sh
```
Expected: no syntax output; one grep match in the post-loop section. (deploy.sh can't run
outside the container — it needs `hyperlane-sealevel-client`; the full path is exercised in
Task 6.)

- [ ] **Step 3: Commit**

```bash
git add stack_orchestrator/data/config/warp-deployer-scripts-config/deploy.sh
git commit -m "feat(warp-deployer): emit warp-routes/warpRoutes.yaml after deploying routes"
```

---

### Task 3: Scope `state_distribute` to an optional subdir + point the warp-UI play at it

**Files:**
- Modify: `ops/roles/state_distribute/tasks/main.yml:5-7`
- Modify: `ops/playbooks/deploy-all.yml` (warp-UI play vars)

- [ ] **Step 1: Add the optional `generated_subdir`**

In `ops/roles/state_distribute/tasks/main.yml`, replace:

```yaml
- name: Resolve the generated dir in the on-host clone
  ansible.builtin.set_fact:
    _generated_src: "{{ deployment_root }}/bridges/{{ bridge_name }}/generated/"
```

with:

```yaml
# generated_subdir scopes the source to a single consumer's slice (e.g. warp-ui),
# so only that slice's files become the consumer's ConfigMap. Default: whole generated/.
# The trailing slash makes copy take the dir's *contents*, so files land at the
# ConfigMap root (SO drops nested subdirs).
- name: Resolve the generated dir in the on-host clone (optionally a scoped subdir)
  ansible.builtin.set_fact:
    _generated_src: "{{ deployment_root }}/bridges/{{ bridge_name }}/generated/{{ generated_subdir + '/' if generated_subdir | default('') else '' }}"
```

- [ ] **Step 2: Point the warp-UI play at the `warp-ui` subdir**

In `ops/playbooks/deploy-all.yml`, the `Warp UI` play currently has (from the
runtime-routes branch):

```yaml
  vars:
    stack_name: hyperlane-warp-ui
    configmap_names: "{{ stacks['hyperlane-warp-ui'].configmaps }}"
    # state_distribute runs as the pre-start hook (after deploy create makes the dir,
    # before start mounts the configmaps) — as a pre-role it would create deploy_dir
    # first and make `deploy create` fail "already exists". deploy_dir is set here
    # (play-scoped) so both the hook and stack_deploy resolve the same dir.
    deploy_dir: "{{ ansible_env.HOME }}/deployments/hyperlane-warp-ui"
    stack_pre_start_tasks: "{{ playbook_dir }}/../roles/state_distribute/tasks/main.yml"
```

Add one line to its `vars:` (after `deploy_dir`):

```yaml
    # Source only generated/warp-routes/ so the ConfigMap holds just warpRoutes.yaml.
    generated_subdir: warp-routes
```

- [ ] **Step 3: Lint**

Run: `cd ops && ansible-lint roles/state_distribute/tasks/main.yml playbooks/deploy-all.yml`
Expected: no new errors.

- [ ] **Step 4: Verify the default is unchanged for other callers**

Run: `grep -n "generated_subdir" ops/playbooks/deploy-all.yml`
Expected: exactly one match (the warp-UI play). The relayer/validator plays do not set it,
so `_generated_src` stays `…/generated/` for them.

- [ ] **Step 5: Commit**

```bash
git add ops/roles/state_distribute/tasks/main.yml ops/playbooks/deploy-all.yml
git commit -m "feat(ops): scope warp-ui state_distribute to generated/warp-routes"
```

---

### Task 4: Remove the warpRoutes build from `publish-bridge-state.yml`

**Files:**
- Modify: `ops/playbooks/publish-bridge-state.yml`

- [ ] **Step 1: Delete the warpRoutes-building tasks**

Remove the contiguous block of tasks from (and including) `Read the warp-deployer spec (for WARP_ROUTES)` through (and including) `Write warpRoutes.yaml into the generated state`:

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

Leave intact above: `Parse program-ids.json` (sets `_pids`). Leave intact below: the
`Patch core deployment-derived config keys into committed specs` task (it patches
`spec-warp-ui.yml`'s `GORCHAIN_MAILBOX`/`SOLANA_MAILBOX` from `_pids` for `chains.yaml`) and
the `Stage the generated paths and patched specs` git-add (still adds `generated/`, now
containing `warp-routes/warpRoutes.yaml`). After the edit, the task after `Parse program-ids.json`
is `Patch core deployment-derived config keys into committed specs`.

- [ ] **Step 2: Update the explanatory comment**

The comment directly above `Parse program-ids.json` currently begins
`# The warp-UI needs a WarpCoreConfig (warpRoutes.yaml) covering ALL routes …`. Replace it
with:

```yaml
    # warpRoutes.yaml is built by the warp-deployer (build-warp-ui-config.sh) under
    # generated/warp-routes/; publish only copies it. Here we patch the core scalar values
    # (IGP/mailbox) into the committed specs from program-ids.json.
```

- [ ] **Step 3: Lint + verify**

Run:
```bash
cd ops && ansible-lint playbooks/publish-bridge-state.yml
grep -nE "warpRoutes|_warp_tokens|_warp_stems|WARP_ROUTES" playbooks/publish-bridge-state.yml
grep -n "Patch core deployment-derived config keys" playbooks/publish-bridge-state.yml
```
Expected: no new lint errors; first grep returns nothing; second returns one match.

- [ ] **Step 4: Commit**

```bash
git add ops/playbooks/publish-bridge-state.yml
git commit -m "refactor(ops): drop warpRoutes.yaml build from publish (deployer owns it)"
```

---

### Task 5: Distribute the deployer-built file in e2e (conftest + state_loader)

**Files:**
- Modify: `tests/e2e/lib/state_loader.py:20-38` (`CONSUMER_STATE_FILES`)
- Modify: `tests/e2e/conftest.py` (delete `_build_warp_ui_config`; simplify `warp_ui_deployment`)

- [ ] **Step 1: Add the scoped warp-ui ConfigMap entry**

In `tests/e2e/lib/state_loader.py`, find:

```python
    "hyperlane-warp-ui": [],             # env-var injection via read_json
```

Replace with:

```python
    # warp-ui mounts the deployer-built warpRoutes.yaml as the warp-ui-config CM:
    "hyperlane-warp-ui": [
        ("warp-routes/warpRoutes.yaml", "warp-ui-config"),
    ],
```

Also update the module comment above the dict (lines ~20-23) that lists `warp-ui` among
env-var-only consumers — remove `warp-ui` so it reads `(gas-oracle, monitoring)`.

- [ ] **Step 2: Delete `_build_warp_ui_config` and simplify `warp_ui_deployment`**

In `tests/e2e/conftest.py`:

(a) Delete the entire `_build_warp_ui_config` function (its `def`, the `_STANDARD`/`_MAILBOX`
locals, the loop, and the trailing blank line).

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
    # warpRoutes.yaml is built by the warp-deployer (under warp-routes/); populate copies it
    # into the warp-ui-config ConfigMap dir.
    bridge_state_loader.populate("hyperlane-warp-ui", deploy_info.deploy_dir)
```

(`gorchain_mailbox`/`solana_mailbox` are still used above for the spec mailbox patch —
leave those.)

- [ ] **Step 3: Verify + lint**

Run:
```bash
cd tests/e2e
grep -n "_build_warp_ui_config" conftest.py            # expect: no matches
python -m pytest --collect-only -q >/dev/null          # expect: collects, no ImportError
ruff check conftest.py lib/state_loader.py
```
Expected: no matches, clean collection, ruff passes.

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/lib/state_loader.py tests/e2e/conftest.py
git commit -m "test(e2e): distribute deployer-built warpRoutes.yaml; drop conftest builder"
```

---

### Task 6: Assert the artifact in e2e + docs sync + full e2e gate

**Files:**
- Modify: `tests/e2e/test_02_warp_deployer.py` (artifact assertion)
- Modify: `specs/stack-specifications.md`, `specs/e2e-test-spec.md`

- [ ] **Step 1: Assert the deployer produced a valid warpRoutes.yaml**

Append to `tests/e2e/test_02_warp_deployer.py` (uses the existing `warp_deployment` and
`bridge_state_loader` fixtures; `read_json` reads the JSON-content file from the state dir):

```python
def test_warp_deployer_builds_warp_ui_config(warp_deployment, bridge_state_loader):
    """The warp-deployer emits a WarpCoreConfig covering every deployed route."""
    cfg = bridge_state_loader.read_json("warp-routes/warpRoutes.yaml")

    assert cfg["options"] == {}
    # Two deployed routes (USDC, SOL) x two chain sides = four token entries.
    assert len(cfg["tokens"]) == 4
    standards = {t["standard"] for t in cfg["tokens"]}
    assert "SealevelHypCollateral" in standards   # USDC solana side
    assert "SealevelHypNative" in standards        # SOL solana side
    assert "SealevelHypSynthetic" in standards     # gorchain sides
    for t in cfg["tokens"]:
        assert isinstance(t["decimals"], int), f"decimals must be int: {t}"
        assert t["addressOrDenom"]
        assert t["connections"][0]["token"].startswith("sealevel|")
```

- [ ] **Step 2: Run the new assertion (requires a deployed warp route)**

Run: `cd tests/e2e && python -m pytest test_02_warp_deployer.py::test_warp_deployer_builds_warp_ui_config -v`
Expected: PASS when run against a cluster that has completed the warp-deployer Job (use the
`--skip-*` reuse flags against an existing deployment). If no cluster/state is available,
this is covered by the full gate in Step 4.

- [ ] **Step 3: Update the docs**

In `specs/stack-specifications.md` (search `grep -n "warpRoutes\|warp-ui\|publish-bridge-state" specs/stack-specifications.md`): add `warp-routes/warpRoutes.yaml` to the warp-deployer's emitted outputs, and change any warp-ui text that says the route config is built by `publish-bridge-state`/conftest to say it is produced by the warp-deployer and distributed via the `warp-ui-config` ConfigMap.

In `specs/e2e-test-spec.md` (search `grep -n "warpRoutes\|warp_ui\|warp-ui" specs/e2e-test-spec.md`): note that `warpRoutes.yaml` is deployer-produced and asserted by `test_02_warp_deployer.py` (artifact) and `test_10_warp_ui.py` (served), and that conftest no longer builds it.

- [ ] **Step 4: Full e2e gate (requires a cluster + chains)**

The warp-deployer Job picks up the new `build-warp-ui-config.sh` from the
`warp-deployer-scripts-config` ConfigMap — no image rebuild needed; the warp-UI image is
unchanged.

```bash
cd tests/e2e
python -m pytest test_02_warp_deployer.py test_10_warp_ui.py test_12_warp_ui_bridge.py -v
```

Expected: PASS. `test_02` confirms the deployer-built artifact; `test_10` confirms the
served `warpRoutes.yaml` carries both routes; `test_12` bridges USDC and native SOL through
the UI — proving the deployer-built config drives the UI end to end.

- [ ] **Step 5: Commit the docs**

```bash
git add tests/e2e/test_02_warp_deployer.py specs/stack-specifications.md specs/e2e-test-spec.md
git commit -m "test(e2e): assert deployer-built warpRoutes.yaml; docs sync"
```

---

## Self-Review

**1. Spec coverage:**
- "Producer — aggregation step" → Tasks 1 (script, writes `warp-routes/warpRoutes.yaml`) + 2 (wiring). ✓
- "ConfigMap scoping (dedicated warp-ui/ subdir)" → Task 1 writes the subdir; Task 3 scopes `state_distribute` via `generated_subdir`. ✓
- "Contract (token schema)" → Task 1 dev smoke run + Task 6 e2e assertion check standards, integer decimals, connections, collateral/synthetic mint, native-has-none. ✓
- "Consumers — distribution only": publish → Task 4; conftest/state_loader → Task 5; entrypoint/specs/compose unchanged (no task — correct). ✓
- "Add-a-route flow" → script iterates `WARP_ROUTES` (Task 1) + unconditional post-loop call (Task 2). ✓
- "Migration" → script reads persistent artifacts; no separate task. ✓
- "Testing (e2e artifact, no unit tests)" → Task 6. ✓
- "Risks: jq numeric / route-name parity / scoped trailing slash / e2e ordering" → Task 1 keeps `.decimals` numeric and resolves stem→name via `jq -r .name`; Task 3 uses a trailing slash; Task 6 runs deployer before reading. ✓

**2. Placeholder scan:** No TBD/TODO; every code step has complete code; commands have expected results.

**3. Type/name consistency:** `build-warp-ui-config.sh` path, env vars (`STATE_DIR`, `WARP_ROUTES`, `WARP_ROUTES_DIR`, `PROGRAM_IDS_FILE`), the output path `warp-routes/warpRoutes.yaml`, the `generated_subdir: warp-routes` var, the `("warp-routes/warpRoutes.yaml", "warp-ui-config")` tuple, and the emitted field names are identical across the script, `deploy.sh`, `state_distribute`, the warp-UI play, `state_loader`, and the e2e assertion. ✓
