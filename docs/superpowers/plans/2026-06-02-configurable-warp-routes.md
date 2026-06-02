# Configurable Warp Routes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make warp routes pure configuration — an operator declares a route's fields and it deploys, with multiple routes on the same chain pair — replacing the single hardcoded USDC route.

**Architecture:** The `hyperlane-svm-warp-deployer` stack runs once per route, parameterized by per-route config fields. `deploy.sh` builds the on-chain token-config generically (collateral/native/synthetic, explicit per-side type) with `jq` — no per-token template. Each route gets its own namespace and `/state/warp-routes/<route>/` subdir. The UI's route values come from config. Relayer, gas-oracle, validators, and storage are route-agnostic and untouched.

**Tech Stack:** laconic-so k8s-kind, POSIX shell + `jq`, hyperlane-sealevel-client, Next.js warp-UI (sentinel fill-at-startup), Python + pytest e2e.

**Spec:** `docs/superpowers/specs/2026-06-02-multiple-warp-routes-design.md`

**Config contract (per route, flat spec `config:` fields):**
`WARP_ROUTE_NAME`, `WARP_ORIGIN_CHAIN`, `WARP_ORIGIN_TYPE` (`collateral`|`native`), `WARP_ORIGIN_TOKEN` (mint; required for collateral), `WARP_REMOTE_CHAIN`, `WARP_REMOTE_TYPE` (`synthetic`|`collateral`), `WARP_TOKEN_SYMBOL`, `WARP_TOKEN_NAME`, `WARP_TOKEN_DECIMALS`, `WARP_TOKEN_METADATA_URI` (synthetic, non-testnet). Global `GORCHAIN_*` / `SOLANA_*` RPC/domain/testnet vars are reused.

---

## File structure

| File | Responsibility |
|---|---|
| `…/config/warp-deployer-scripts-config/deploy.sh` | Resolve per-side addresses; build token-config generically; per-route state + idempotency. |
| `…/config/warp-deployer-token-config/` | **Deleted** — no per-token template. |
| `…/compose-jobs/docker-compose-hyperlane-svm-warp-deployer.yml` | Pass the contract fields; drop the token-config volume. |
| `deployment/spec-warp-deployer.yml` | USDC route as contract fields + explicit `namespace:`. |
| `…/stacks/hyperlane-svm-warp-deployer/stack.yml` | (no change — already only `jobs:`) |
| `…/container-build/gorbagana-dev-hyperlane-warp-ui/{configs/warpRoutes.yaml,entrypoint.sh}` | Route values from config. |
| `…/compose/docker-compose-hyperlane-warp-ui.yml`, `deployment/spec-warp-ui.yml` | Per-route UI config. |
| `tests/e2e/lib/state_loader.py` (+ `test_state_loader_routes.py`) | Route discovery + per-route reads. |
| `tests/e2e/conftest.py`, `tests/e2e/fixtures/test-spec-warp-deployer-*.yml` | Drive a routes list; deploy each; resolve addresses. |
| `tests/e2e/test_02_warp_deployer.py` | Assert both route shapes deploy. |
| `specs/stack-specifications.md`, `docs/architecture-decisions.md`, `CLAUDE.md` | Docs + keep-in-sync table. |

---

## Phase A — Configurable warp-deployer

### Task A1: Rewrite deploy.sh to build the token-config generically

**Files:**
- Modify: `stack_orchestrator/data/config/warp-deployer-scripts-config/deploy.sh`

- [ ] **Step 1: Resolve per-side chain config + addresses from the contract fields**

Replace the `COLLATERAL_*`/`SYNTHETIC_*` extraction block (lines ~30-55) with origin/remote resolution driven by chain name. After the `program-ids.json` existence check, add:

```bash
# Resolve a chain's RPC URL and domain id from the global chain config by name,
# e.g. gorchain -> $GORCHAIN_RPC_URL / $GORCHAIN_DOMAIN_ID.
chain_var() {  # $1=chain  $2=suffix(RPC_URL|DOMAIN_ID)
  upper=$(echo "$1" | tr '[:lower:]' '[:upper:]')
  eval "printf '%s' \"\${${upper}_${2}:-}\""
}

for side_chain in "${WARP_ORIGIN_CHAIN}" "${WARP_REMOTE_CHAIN}"; do
  progs=$(jq -c --arg c "$side_chain" '.[$c] // {}' "${PROGRAM_IDS_FILE}")
  if [ "$progs" = "{}" ]; then
    echo "ERROR: program-ids.json missing data for ${side_chain} (route ${WARP_ROUTE_NAME})."
    exit 1
  fi
done
```

- [ ] **Step 2: Add a `jq` builder for one side and assemble the token-config**

Replace the `envsubst < token-config.json.tmpl` render (lines ~115-123) with:

```bash
build_side() {  # $1=chain  $2=type  $3=token(optional)
  chain=$1; type=$2; token=${3:-}
  progs=$(jq -c --arg c "$chain" '.[$c]' "${PROGRAM_IDS_FILE}")
  ism=$(printf '%s' "$progs" | jq -r '.multisig_ism_message_id')
  igp=$(printf '%s' "$progs" | jq -r '.overhead_igp_account')
  jq -n \
    --arg type "$type" --arg name "${WARP_TOKEN_NAME}" --arg symbol "${WARP_TOKEN_SYMBOL}" \
    --argjson decimals "${WARP_TOKEN_DECIMALS}" --arg token "$token" \
    --arg uri "${WARP_TOKEN_METADATA_URI:-}" --arg ism "$ism" --arg igp "$igp" \
    '{type:$type, name:$name, symbol:$symbol, decimals:$decimals,
      interchainSecurityModule:$ism, interchainGasPaymaster:$igp}
     + (if $type=="collateral" then {token:$token} else {} end)
     + (if $type=="synthetic" and $uri!="" then {uri:$uri} else {} end)'
}

if [ "${WARP_ORIGIN_TYPE}" = "collateral" ] && [ -z "${WARP_ORIGIN_TOKEN:-}" ]; then
  echo "ERROR: WARP_ORIGIN_TYPE=collateral but WARP_ORIGIN_TOKEN is empty (route ${WARP_ROUTE_NAME})."
  exit 1
fi

mkdir -p "${WORK_DIR}"
jq -n \
  --arg oc "${WARP_ORIGIN_CHAIN}" --argjson o "$(build_side "${WARP_ORIGIN_CHAIN}" "${WARP_ORIGIN_TYPE}" "${WARP_ORIGIN_TOKEN:-}")" \
  --arg rc "${WARP_REMOTE_CHAIN}" --argjson r "$(build_side "${WARP_REMOTE_CHAIN}" "${WARP_REMOTE_TYPE}" "")" \
  '{($oc):$o, ($rc):$r}' > "${WORK_DIR}/token-config.json"
echo "Token config:"; cat "${WORK_DIR}/token-config.json"
```

- [ ] **Step 3: Point the CLI's solana config + registry at the origin chain's RPC**

The CLI config currently uses `${COLLATERAL_CHAIN_RPC_URL}`. Change the Solana CLI config and any RPC references to `$(chain_var "${WARP_ORIGIN_CHAIN}" RPC_URL)`. Keep the existing `--warp-route-name "${WARP_ROUTE_NAME}"`, `--token-config-file "${WORK_DIR}/token-config.json"`, registry render, and `warp-route deploy` invocation.

- [ ] **Step 4: Per-route state dir + idempotency + type-aware output**

Near the top, after `STATE_DIR` is set:

```bash
ROUTE_STATE_DIR="${STATE_DIR}/warp-routes/${WARP_ROUTE_NAME}"
mkdir -p "${ROUTE_STATE_DIR}"
```

Change the idempotency gate to check `${ROUTE_STATE_DIR}/token-config.json`; change the final artifact writes to `${ROUTE_STATE_DIR}/token-config.json` and `${ROUTE_STATE_DIR}/warp-deploy-outputs`; update the preflight + final echoes to `${ROUTE_STATE_DIR}`. Replace the hardcoded output `warpRoute` heredoc (lines ~233-252) with a copy of the rendered input config wrapped with the route name:

```bash
jq --arg name "${WARP_ROUTE_NAME}" '{warpRoute: ({name:$name} + .)}' \
  "${WORK_DIR}/token-config.json" > "${WORK_DIR}/output/token-config.json"
cp "${WORK_DIR}/output/token-config.json" "${ROUTE_STATE_DIR}/token-config.json"
```

(The upgrade-authority transfer loop reading `${WARP_OUTPUT_DIR}/program-ids.json` stays; it's already chain-name-driven.)

- [ ] **Step 5: Smoke-test the builder locally (no cluster)**

```bash
cd /home/dev/workspace/pranav/hyperlane-stacks
printf '{"solana":{"multisig_ism_message_id":"ISM","overhead_igp_account":"IGP","mailbox":"MB"},"gorchain":{"multisig_ism_message_id":"ISM2","overhead_igp_account":"IGP2","mailbox":"MB2"}}' > /tmp/program-ids.json
# Inline the build_side+assemble logic with: origin solana collateral, remote gorchain synthetic, decimals 6
```
Expected: valid JSON with `solana.type=collateral` (has `token`), `gorchain.type=synthetic` (has `uri` only if set), both carry ISM/IGP.

- [ ] **Step 6: Commit**

```bash
git add stack_orchestrator/data/config/warp-deployer-scripts-config/deploy.sh
git commit -m "feat(warp): build token-config generically from per-route config in deploy.sh"
```

---

### Task A2: Remove the per-token template + its ConfigMap

**Files:**
- Delete: `stack_orchestrator/data/config/warp-deployer-token-config/` (the `.tmpl`)
- Modify: `…/compose-jobs/docker-compose-hyperlane-svm-warp-deployer.yml`
- Modify: `stack_orchestrator/data/stacks/hyperlane-svm-warp-deployer/README.md`

- [ ] **Step 1: Delete the template dir**

```bash
git rm -r stack_orchestrator/data/config/warp-deployer-token-config
```

- [ ] **Step 2: Drop the `warp-deployer-token-config` volume from the compose file**

Remove the `- warp-deployer-token-config:/config/token:ro` mount and the `warp-deployer-token-config:` entry under `volumes:`. (deploy.sh no longer reads `/config/token`.)

- [ ] **Step 3: Update the stack README**

In `stacks/hyperlane-svm-warp-deployer/README.md`, remove the `warp-deployer-token-config` ConfigMap line (~49) and its description row (~69); replace with a note that the token-config is built from the route's `config:` fields by `deploy.sh`.

- [ ] **Step 4: Commit**

```bash
git add stack_orchestrator/data/config stack_orchestrator/data/compose-jobs/docker-compose-hyperlane-svm-warp-deployer.yml stack_orchestrator/data/stacks/hyperlane-svm-warp-deployer/README.md
git commit -m "feat(warp): drop per-token token-config template + its ConfigMap"
```

---

### Task A3: Pass the contract fields through compose

**Files:**
- Modify: `…/compose-jobs/docker-compose-hyperlane-svm-warp-deployer.yml`

- [ ] **Step 1: Replace the warp env block with the contract fields**

Replace the `WARP_TOKEN_MINT`/`COLLATERAL_*`/`SYNTHETIC_*` env lines with:

```yaml
      WARP_ROUTE_NAME: ${WARP_ROUTE_NAME}
      WARP_ORIGIN_CHAIN: ${WARP_ORIGIN_CHAIN}
      WARP_ORIGIN_TYPE: ${WARP_ORIGIN_TYPE}
      WARP_ORIGIN_TOKEN: ${WARP_ORIGIN_TOKEN:-}
      WARP_REMOTE_CHAIN: ${WARP_REMOTE_CHAIN}
      WARP_REMOTE_TYPE: ${WARP_REMOTE_TYPE}
      WARP_TOKEN_SYMBOL: ${WARP_TOKEN_SYMBOL}
      WARP_TOKEN_NAME: ${WARP_TOKEN_NAME}
      WARP_TOKEN_DECIMALS: ${WARP_TOKEN_DECIMALS}
      WARP_TOKEN_METADATA_URI: ${WARP_TOKEN_METADATA_URI:-}
```

Keep the global chain vars (`GORCHAIN_*`, `SOLANA_*`, `*_IS_TESTNET`), `FORCE_REDEPLOY`, and the registry metadata vars the registry template still needs.

- [ ] **Step 2: Commit**

```bash
git add stack_orchestrator/data/compose-jobs/docker-compose-hyperlane-svm-warp-deployer.yml
git commit -m "feat(warp): pass per-route contract fields to the warp-deployer container"
```

---

### Task A4: Express the USDC route as config + namespace in the prod spec

**Files:**
- Modify: `deployment/spec-warp-deployer.yml`

- [ ] **Step 1: Add namespace, swap to contract fields, drop the token-config ConfigMap**

Add `namespace: laconic-hyperlane-warp-usdc` after `deploy-to:`. Replace the route config block with:

```yaml
  WARP_ROUTE_NAME: "USDC-solana-gorchain"
  WARP_ORIGIN_CHAIN: solana
  WARP_ORIGIN_TYPE: collateral
  WARP_ORIGIN_TOKEN: "REPLACE_WITH_USDC_MINT_ADDRESS"
  WARP_REMOTE_CHAIN: gorchain
  WARP_REMOTE_TYPE: synthetic
  WARP_TOKEN_SYMBOL: "USDC"
  WARP_TOKEN_NAME: "USD Coin"
  WARP_TOKEN_DECIMALS: "6"
  WARP_TOKEN_METADATA_URI: "REPLACE_WITH_TOKEN_METADATA_URI"
```

Keep the global `GORCHAIN_*`/`SOLANA_*` vars and `FORCE_REDEPLOY`. Remove `warp-deployer-token-config:` from the `configmaps:` block.

- [ ] **Step 2: Commit**

```bash
git add deployment/spec-warp-deployer.yml
git commit -m "feat(warp): USDC route as config fields + explicit namespace"
```

---

### Task A5: Update the keep-in-sync table

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1:** In the compose↔spec↔fixture table, change the warp-deployer fixture cell to `tests/e2e/fixtures/test-spec-warp-deployer-{usdc,native}.yml` (added in Phase D). Commit:

```bash
git add CLAUDE.md
git commit -m "docs: update keep-in-sync table for per-route warp fixtures"
```

---

## Phase B — State loader (TDD)

### Task B1: Route discovery + per-route reads

**Files:**
- Modify: `tests/e2e/lib/state_loader.py`
- Test: `tests/e2e/lib/test_state_loader_routes.py` (new)

- [ ] **Step 1: Write failing tests**

```python
import json
from pathlib import Path
from tests.e2e.lib.state_loader import BridgeStateLoader

def _route(d: Path, name: str, programs: dict):
    r = d / "warp-routes" / name
    (r / "warp-deploy-outputs").mkdir(parents=True)
    (r / "token-config.json").write_text(json.dumps({"warpRoute": {"name": name}}))
    (r / "warp-deploy-outputs" / "program-ids.json").write_text(json.dumps(programs))

def test_discover_routes(tmp_path):
    _route(tmp_path, "USDC-solana-gorchain", {})
    _route(tmp_path, "SOL-solana-gorchain", {})
    assert sorted(BridgeStateLoader(tmp_path).discover_routes()) == ["SOL-solana-gorchain", "USDC-solana-gorchain"]

def test_discover_routes_empty(tmp_path):
    assert BridgeStateLoader(tmp_path).discover_routes() == []

def test_read_route_program_addresses(tmp_path):
    _route(tmp_path, "SOL-solana-gorchain", {"solana": {"base58": "SSS"}, "gorchain": {"base58": "GGG"}})
    assert BridgeStateLoader(tmp_path).read_route_program_addresses("SOL-solana-gorchain") == {"solana": "SSS", "gorchain": "GGG"}

def test_read_route_token_config(tmp_path):
    _route(tmp_path, "USDC-solana-gorchain", {})
    assert BridgeStateLoader(tmp_path).read_route_token_config("USDC-solana-gorchain")["warpRoute"]["name"] == "USDC-solana-gorchain"
```

- [ ] **Step 2: Run — expect failure**

Run: `cd /home/dev/workspace/pranav/hyperlane-stacks && python -m pytest tests/e2e/lib/test_state_loader_routes.py -v`
Expected: FAIL (`AttributeError: discover_routes`).

- [ ] **Step 3: Implement (append to `BridgeStateLoader`)**

```python
    def discover_routes(self) -> list[str]:
        base = self.state_dir / "warp-routes"
        if not base.is_dir():
            return []
        return [d.name for d in base.iterdir() if d.is_dir()]

    def read_route_token_config(self, route: str) -> dict:
        return self.read_json(str(Path("warp-routes") / route / "token-config.json"))

    def read_route_program_addresses(self, route: str) -> dict[str, str]:
        outputs = self.state_dir / "warp-routes" / route / "warp-deploy-outputs"
        if not outputs.is_dir():
            raise FileNotFoundError(f"{outputs} does not exist")
        programs: dict[str, str] = {}
        for f in outputs.iterdir():
            if f.is_file():
                for chain, entry in json.loads(f.read_text()).items():
                    if isinstance(entry, dict) and entry.get("base58"):
                        programs[chain] = entry["base58"]
        return programs
```

- [ ] **Step 4: Run — expect pass**

Run: `python -m pytest tests/e2e/lib/test_state_loader_routes.py -v` → PASS (4).

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/lib/state_loader.py tests/e2e/lib/test_state_loader_routes.py
git commit -m "feat(e2e): route discovery + per-route reads in BridgeStateLoader"
```

---

## Phase C — Warp-UI route values from config

> Minimal. The UI displays the baked USDC route with its values coming from config — which it already does via sentinels — so there is little to change here. Route *functionality* (including the native route) is proven by the CLI bridge test in Phase D, independent of the UI. Do **not** bake an unfilled route slot; `warpCoreConfig.ts` rejects empty addresses.

### Task C1: Keep the USDC route value-driven; confirm config flow

**Files:**
- Modify: `…/container-build/gorbagana-dev-hyperlane-warp-ui/{configs/warpRoutes.yaml,entrypoint.sh}`
- Modify: `…/compose/docker-compose-hyperlane-warp-ui.yml`, `deployment/spec-warp-ui.yml`

- [ ] **Step 1: Ensure every route value in `warpRoutes.yaml` is a sentinel**

Confirm the USDC entry's `symbol`/`name`/`decimals`/addresses/mailbox are all sentinels (they are today) so they come from config, not hardcoded literals. No structural change for the single route.

- [ ] **Step 2: Verify entrypoint seds all of them** (it does today: `__WARP_*__`, `__*_MAILBOX__`). No change unless a second slot is baked.

- [ ] **Step 3: Commit (only if anything changed)**

```bash
git add stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-warp-ui
git commit -m "chore(warp-ui): route values are fully config-driven"
```

---

## Phase D — E2E: routes deploy (collateral + native) and the native route bridges

### Task D1: Per-route warp-deployer fixtures

**Files:**
- Rename: `tests/e2e/fixtures/test-spec-warp-deployer.yml` → `test-spec-warp-deployer-usdc.yml`
- Create: `tests/e2e/fixtures/test-spec-warp-deployer-native.yml`

- [ ] **Step 1: USDC fixture** — `git mv` the existing fixture; add `namespace: laconic-warp-usdc-e2e`; swap to the contract fields (origin solana/collateral with `WARP_ORIGIN_TOKEN: "REPLACE_AT_RUNTIME"`, remote gorchain/synthetic, symbol USDC, decimals 6, `WARP_TOKEN_METADATA_URI: ""`, `*_IS_TESTNET: "true"`); drop the `warp-deployer-token-config` configmap.

- [ ] **Step 2: Native fixture** — copy the USDC one; change `namespace: laconic-warp-native-e2e`, `WARP_ROUTE_NAME: "SOL-solana-gorchain"`, `WARP_ORIGIN_TYPE: native`, remove `WARP_ORIGIN_TOKEN`, `WARP_TOKEN_SYMBOL: "SOL"`, `WARP_TOKEN_NAME: "Solana"`, `WARP_TOKEN_DECIMALS: "9"`. Keep origin solana / remote gorchain / synthetic.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/fixtures/test-spec-warp-deployer-usdc.yml tests/e2e/fixtures/test-spec-warp-deployer-native.yml
git commit -m "test(e2e): per-route warp-deployer fixtures (usdc collateral, native)"
```

### Task D2: Drive a routes list in conftest

**Files:**
- Modify: `tests/e2e/conftest.py`

- [ ] **Step 1: Define the routes list** (near the spec-path constants ~line 95):

```python
WARP_USDC_SPEC = E2E_DIR / "fixtures" / "test-spec-warp-deployer-usdc.yml"
WARP_NATIVE_SPEC = E2E_DIR / "fixtures" / "test-spec-warp-deployer-native.yml"

WARP_ROUTES = [
    {"name": "USDC-solana-gorchain", "spec": WARP_USDC_SPEC,
     "deployment_id": "warp-usdc", "namespace": "laconic-warp-usdc-e2e", "needs_spl_mint": True},
    {"name": "SOL-solana-gorchain", "spec": WARP_NATIVE_SPEC,
     "deployment_id": "warp-native", "namespace": "laconic-warp-native-e2e", "needs_spl_mint": False},
]
```

- [ ] **Step 2: Rework `warp_deployment`** (lines ~781-863) to loop `WARP_ROUTES`, deploy each, and yield `{"routes": {name: {deployment, namespace, origin_token}}}`. For `needs_spl_mint`, create+fund the SPL and patch `WARP_ORIGIN_TOKEN`; otherwise no mint. Make `_patch_warp_spec(spec_path, origin_token)` take the spec path and write a per-spec patched file. Wait for each route's Job (`{deployment_id}-job-hyperlane-svm-warp-deployer`).

> **Note (native deploy):** the native route deploys with no SPL mint. `--ata-payer-funding-amount` is a plain lamport transfer to a derived PDA (`warp_route.rs:233-250`) — harmless for native, no gating needed. Just capture the native Job log on first run.

- [ ] **Step 3: Update consumers** of the old `warp_deployment` shape (the warp-ui fixture, any test using `warp_deployment["token_mint"]`) to index by route: `warp_deployment["routes"]["USDC-solana-gorchain"][...]`. In `warp_ui_deployment`, resolve addresses via `bridge_state_loader.read_route_program_addresses("USDC-solana-gorchain")`. Also update the `bridge_setup` fixture to expose **per-route** data — for each route, its solana/gorchain warp programs (`read_route_program_addresses`) and synthetic mint, e.g. `bridge_setup["routes"][name] = {"warp_solana": …, "warp_gorchain": …, "synthetic_mint": …}` — and replace the old flat `_get_warp_program_addresses` helper + its callers (bridge setup, warp-ui fixture, test_08) with the per-route reads.

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/conftest.py
git commit -m "test(e2e): deploy a list of warp routes (collateral + native)"
```

### Task D3: Assertions

**Files:**
- Modify: `tests/e2e/test_02_warp_deployer.py`

- [ ] **Step 1: Replace single-route assertions** with:

```python
def test_both_routes_deployed(bridge_state_loader):
    routes = set(bridge_state_loader.discover_routes())
    assert {"USDC-solana-gorchain", "SOL-solana-gorchain"} <= routes

def test_usdc_route_collateral(bridge_state_loader):
    cfg = bridge_state_loader.read_route_token_config("USDC-solana-gorchain")["warpRoute"]
    assert cfg["solana"]["type"] == "collateral" and "token" in cfg["solana"]
    assert cfg["gorchain"]["type"] == "synthetic"

def test_native_route_native(bridge_state_loader):
    cfg = bridge_state_loader.read_route_token_config("SOL-solana-gorchain")["warpRoute"]
    assert cfg["solana"]["type"] == "native" and "token" not in cfg["solana"]
    assert cfg["gorchain"]["type"] == "synthetic"
```

- [ ] **Step 2: Run on the test machine**

Run: `python -m pytest tests/e2e/test_02_warp_deployer.py -v` → both routes deploy; native shows `type: native` with no token.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/test_02_warp_deployer.py
git commit -m "test(e2e): assert collateral and native routes both deploy"
```

### Task D4: Native-route bridge test (CLI — functional proof)

Proves a configured route actually moves tokens, end-to-end, without the UI.

**Files:**
- Modify: `tests/e2e/test_08_bridge.py`
- Modify: `tests/e2e/conftest.py` (only if `bridge_setup` per-route data from D2-step-3 is missing) and `tests/e2e/lib/chain.py` (SOL-balance helper, if absent)

- [ ] **Step 1: Ensure a native-balance helper exists**

If there's no `get_sol_balance(keypair_path, rpc)` (solana `getBalance` for the native coin) in `tests/e2e/lib/`, add one mirroring `get_spl_token_balance`. (SPL balance won't work for a `native` origin.)

- [ ] **Step 2: Add a forward native transfer test**

Mirror `test_concurrent_forward_transfers`, but for the native route — `transfer-remote … "native" --program-id <native warp on solana>`, asserting the origin native balance drops and the remote synthetic balance rises:

```python
def test_native_forward_transfer(self, bridge_setup, bridge_state_loader):
    route = bridge_setup["routes"]["SOL-solana-gorchain"]
    user = bridge_setup["users"][0]
    solana_rpc = CHAINS["solana"]["rpc"]
    gorchain_rpc = CHAINS["gorchain"]["rpc"]
    gorchain_domain = str(CHAINS["gorchain"]["domain_id"])

    before_syn = get_spl_token_balance(route["synthetic_mint"], user["keypair_path"], gorchain_rpc)
    before_sol = get_sol_balance(user["keypair_path"], solana_rpc)

    res = _run_transfer_remote(
        "/tmp/key.json", str(NATIVE_AMOUNT), gorchain_domain, user["pubkey"],
        "native", "--program-id", route["warp_solana"],
        rpc=solana_rpc, keypair_path=user["keypair_path"],
    )
    assert res.returncode == 0, res.stdout + res.stderr

    # Poll for relayer delivery on the remote, like the USDC test does.
    after_syn = _wait_for_balance_increase(
        route["synthetic_mint"], user["keypair_path"], gorchain_rpc, before_syn,
    )
    assert after_syn > before_syn, "native route synthetic balance did not increase"
    assert get_sol_balance(user["keypair_path"], solana_rpc) < before_sol
```

Add `NATIVE_AMOUNT` (lamports) next to the existing `FORWARD_*` constants. Reuse the existing relayer-delivery wait pattern (extract a `_wait_for_balance_increase` helper from the USDC test if one isn't already factored out).

- [ ] **Step 3: Run on the test machine**

Run: `python -m pytest tests/e2e/test_08_bridge.py -v` → the native route transfer succeeds; remote synthetic balance increases; origin native balance decreases.

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/test_08_bridge.py tests/e2e/conftest.py tests/e2e/lib
git commit -m "test(e2e): native-route cross-chain transfer (functional proof)"
```

---

## Phase E — Docs

### Task E1: Update stack docs

**Files:**
- Modify: `specs/stack-specifications.md`, `docs/architecture-decisions.md`

- [ ] **Step 1:** Replace Stack 2's single-route description with the configurable model (route = config fields; per-route specs/namespaces/state; generic `deploy.sh`; relayer/gas-oracle/validators/storage unchanged). In `architecture-decisions.md`, update the warp-deployer row to "deploys one configurable route per deployment."

- [ ] **Step 2: Commit**

```bash
git add specs/stack-specifications.md docs/architecture-decisions.md
git commit -m "docs: document configurable warp routes in stack specs"
```

---

## Self-review notes

- **Spec coverage:** configurable deployer (A1–A4), generic builder/no template (A1–A2), explicit per-side type (A1), per-route state+namespace (A1, A4, D1), state discovery (B1), UI values from config (C1), e2e routes deploy (D1–D3) + native route bridges via CLI (D4), docs (A5, A2 README, E1). Relayer/gas-oracle/validators/storage untouched — no tasks, by design.
- **Resolved:** native ATA funding is harmless (D2 note); UI is minimal/baked (Phase C) with route functionality proven by the CLI bridge test (D4) — no UI app change.
- **Type consistency:** contract fields (`WARP_ORIGIN_*`/`WARP_REMOTE_*`/`WARP_TOKEN_*`) identical across A1/A3/A4/D1; loader methods `discover_routes`/`read_route_token_config`/`read_route_program_addresses` identical across B1/D2/D3.
