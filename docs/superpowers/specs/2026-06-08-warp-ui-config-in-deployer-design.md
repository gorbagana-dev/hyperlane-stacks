# Generate the warp-UI route config in the warp-deployer

**Status:** design proposed, awaiting review
**Date:** 2026-06-08
**Refines:** [`2026-06-05-warp-ui-runtime-routes-design.md`](2026-06-05-warp-ui-runtime-routes-design.md)

## Problem

The warp UI loads its routes at runtime from `warpRoutes.yaml`, a Hyperlane
`WarpCoreConfig` derived from deployer output state. Today that file is *built* in two
places that each reimplement the same transform (per route: map each chain side's `type`
to a `SealevelHyp*` standard, attach `connections`, emit two token entries):

- `publish-bridge-state.yml` — Ansible/Jinja, for prod and local.
- `conftest._build_warp_ui_config` — Python, for e2e.

One contract, two implementations. They have already drifted (the `symbol`/`name`
fallbacks differ), and both put UI-schema knowledge in the ops/test layer while coupling
config regeneration to a `git commit && push`. `publish-bridge-state`'s real job is to
copy raw deployer output to git and patch scalar spec values — not to project that output
into a UI format.

A second, smaller issue rides along: today the warp-UI's ConfigMap is over-broad (see
"ConfigMap scoping").

The tempting fix — build `warpRoutes.yaml` in the warp-UI entrypoint, where `chains.yaml`
is already rendered — does not work. The reason shapes this design.

## Constraint: the per-route inputs never reach the pod

SO builds a ConfigMap from a single directory level
(`cluster_info.py:get_configmaps` → `for f in os.listdir(path): if os.path.isfile(f)`):
subdirectories are silently skipped, and ConfigMap keys cannot contain `/`.

The deployer's `generated/` tree (== its `/state`; `publish` copies it verbatim to git and
`state_distribute` copies it into a consumer's `configmaps/<name>/`):

```
generated/
  program-ids.json          core: { <chain>: { mailbox, igp, ism, … } }   [top-level → reaches pod]
  agent-config.json                                                        [top-level → reaches pod]
  gas-oracle-config.json                                                   [top-level → reaches pod]
  multisig-config.json                                                     [top-level → reaches pod]
  registry/metadata.yaml                                                   [subdir → dropped]
  warp-routes/                                                             [subdir → dropped]
    warpRoutes.yaml      ★ the UI's WarpCoreConfig (root of warp-routes/; see below)
    <route-name>/
      token-config.json   { warpRoute: { name,
                              <origin chain>: { type, symbol, decimals, token? },
                              <remote chain>: { type, symbol, decimals, mint?  } } }
      warp-deploy-outputs/
        program-ids.json  { <chain>: { base58: <warp program> } }
      deploy.log
```

Everything needed to build `warpRoutes.yaml` lives in the `warp-routes/` **subdir**
(dropped at configmap creation, and `token-config.json` basenames collide across routes)
plus the core `program-ids.json` mailboxes. So the entrypoint cannot see those inputs.
`chains.yaml` is buildable there only because *its* inputs are all scalar **pod env**
(chain IDs/names/mailbox/RPCs).

`warpRoutes.yaml` must therefore be pre-assembled by an actor that can see `warp-routes/`.
The first such actor is the **warp-deployer** — it *writes* that tree, before `publish`
copies it and before SO flattens it away.

## ConfigMap scoping (why source `generated/warp-routes/`)

`state_distribute` copies the *contents* of a source directory into the consumer's
`configmaps/<name>/`, and SO turns only the top-level files there into the ConfigMap (it
drops subdirectories — the behavior this whole design rests on). The role currently sources
the whole `generated/` directory, so a consumer's ConfigMap would hold **every** top-level
`generated/` file — for the warp-UI that means `agent-config.json`, `program-ids.json`,
`gas-oracle-config.json`, `multisig-config.json`, none of which the UI reads.

To keep the warp-UI ConfigMap to just `warpRoutes.yaml`, the deployer writes it at the
**root of the existing** `generated/warp-routes/` tree (beside the per-route `<name>/`
dirs), and the warp-UI play points `state_distribute` at `generated/warp-routes/`. SO then
takes the top-level `warpRoutes.yaml` into the ConfigMap and drops the per-route `<name>/`
subdirs — the ConfigMap ends up holding only `warpRoutes.yaml`. This reuses the existing
`warp-routes/` tree rather than adding a `generated/` subdir, and keeps the warp-UI on the
same distribution mechanism as the relayer/validators (`state_distribute`), just pointed at
a scoped source.

Trade-off: `state_distribute`'s copy is recursive, so on the ops path it duplicates the
per-route `<name>/` subdirs into the warp-UI deploy dir on disk. SO ignores them, so it is a
small, harmless local copy — not a ConfigMap or correctness concern. (e2e's `populate` is
file-based and copies only `warpRoutes.yaml`, so it has no such dup.)

## Goals

- The route transform lives in **one** place — the warp-deployer (`deploy.sh`), in
  shell/jq next to the data — exercised identically by prod, local, and e2e.
- `publish-bridge-state` and `conftest` return to **distribution only**: they carry the
  deployer-produced `warpRoutes.yaml`, they do not build it.
- The warp-UI ConfigMap carries only `warpRoutes.yaml`.
- Adding a route post-deployment is a warp-deployer re-run (it already loops `WARP_ROUTES`
  and is idempotent), not an ops-playbook concern.

## Non-goals

- Hot route addition without a UI restart. Add-a-route stays
  re-run-deployer → republish → restart warp-UI (one `deploy-all` pass).
- Changing `chains.yaml` provenance — it stays entrypoint-rendered from pod env (it
  carries the secret Helius RPC and must never be committed).
- Changing the warp-deployer's route menu, idempotency, or per-route artifacts.
- Per-route UI config fragment files — the aggregation reads each route's existing
  `token-config.json` + `warp-deploy-outputs/program-ids.json` directly (YAGNI).
- Reworking `state_distribute` for the other consumers; the only change is an optional,
  defaulted `generated_subdir` it already ignores unless asked.

## Design

### Producer — aggregation step in `deploy.sh`

`deploy.sh` ends with a loop over `WARP_ROUTES` (`for route in …; do deploy_route …`).
Add one step **after** the loop that writes `${STATE_DIR}/warp-routes/warpRoutes.yaml`. For
each stem in `WARP_ROUTES`:

1. Resolve the route name the same way `deploy_route` does: `jq -r .name /config/warp-routes/<stem>.json`.
2. Read `warp-routes/<name>/token-config.json` → the two chain sides (`warpRoute` minus `name`).
3. Read `warp-routes/<name>/warp-deploy-outputs/program-ids.json` → `<chain>.base58` per side.
4. Read core `program-ids.json` → `<chain>.mailbox` per side.
5. Emit two token entries (one per side):
   - `chainName` = chain key; `standard` = `{collateral:SealevelHypCollateral,
     synthetic:SealevelHypSynthetic, native:SealevelHypNative}[side.type]`
   - `name`, `symbol`, `decimals` (a JSON **number**) from the side; `addressOrDenom`
     = that side's warp-program base58; `mailbox` from core `program-ids.json`
   - `collateralAddressOrDenom` = `side.token` (collateral) / `side.mint` (synthetic);
     omitted for native
   - `connections: [ { token: "sealevel|<other chain>|<other warp program>" } ]`

Write `{ tokens: [<all>], options: {} }` to `${STATE_DIR}/warp-routes/warpRoutes.yaml`.

Properties:
- Runs **unconditionally** — it reads each route's persistent artifacts, not in-loop
  variables, so it produces a correct file even when every route self-skips.
- Iterates `WARP_ROUTES` (not a `warp-routes/*` glob), so the aggregate is exactly the
  selected set — a route added to / removed from `WARP_ROUTES` is added to / removed from
  the UI on the next run.
- A selected route missing its artifacts is a hard error (fail loud), matching the
  existing publish/e2e contract.

### Contract (token schema)

Validated against `@hyperlane-xyz/sdk` `WarpCoreConfigSchema`: required `chainName`
(lowercase), `standard` (the three `SealevelHyp*` values are valid enum members), `name`
(min 1), `symbol` (min 1), `decimals` (**strict** `z.number().int()`, < 256),
`addressOrDenom`; optional `connections` (items `{ token: string }`) and
`collateralAddressOrDenom`. `chainName` must match a `chains.yaml` key — the entrypoint
emits `gorchain`/`solana`, matching the `token-config.json` chain keys.

`decimals` being strict-number is the load-bearing detail: the runtime loader *throws* on
a string, so the emitter must keep it numeric. `token-config.json` already stores it
numeric (written via `jq --argjson`), so the aggregation must read and re-emit it without
stringifying.

### Consumers — reduced to distribution

- **`build-warp-ui-config.sh`** — new sibling script under
  `warp-deployer-scripts-config/`, called by `deploy.sh` after the route loop. Holds the
  whole transform above. Parameterised by env (`STATE_DIR`, `WARP_ROUTES`,
  `WARP_ROUTES_DIR`, `PROGRAM_IDS_FILE`) so it is self-contained.
- **`state_distribute`** — gains an optional, defaulted `generated_subdir`: the source dir
  becomes `…/generated/<generated_subdir>/` (default `…/generated/`). No behaviour change
  for existing callers (relayer/validators omit it).
- **`deploy-all.yml` warp-UI play** — keep the existing `state_distribute` pre-start hook
  (from the runtime-routes branch); add `generated_subdir: warp-routes` so the hook sources
  `generated/warp-routes/` and the ConfigMap holds only `warpRoutes.yaml`.
- **`publish-bridge-state.yml`** — delete the `warpRoutes.yaml`-building block
  (`_warp_stems`, the per-route slurps, the `_warp_tokens` accumulator, "Write
  warpRoutes.yaml into the generated state"). Keep: copy `generated/` (now containing
  `warp-routes/warpRoutes.yaml`), patch `GORCHAIN_MAILBOX`/`SOLANA_MAILBOX` into
  `spec-warp-ui.yml` (the entrypoint needs them as env for `chains.yaml`), the existing
  core IGP/mailbox spec patches, and git add/commit/push.
- **`conftest.py` + `state_loader.py`** — delete `_build_warp_ui_config`. Add
  `("warp-routes/warpRoutes.yaml", "warp-ui-config")` to
  `CONSUMER_STATE_FILES["hyperlane-warp-ui"]` so the existing `BridgeStateLoader.populate`
  copies the deployer-produced file into the ConfigMap dir — same scoped result as ops.
- **`entrypoint.sh`, specs, compose** — unchanged. `warpRoutes.yaml` arrives in the
  `warp-ui-config` ConfigMap at the same mount path as today.

### Add-a-route flow

1. Add the stem to `WARP_ROUTES` in `spec-warp-deployer.yml` (menu file checked in, or add one).
2. Re-run `deploy-all` (or the warp-deployer → publish → warp-ui plays): the deployer
   deploys only the new route (others self-skip), the aggregation rebuilds the full
   `warp-routes/warpRoutes.yaml`, publish pushes the updated `generated/`, the warp-UI play
   redeploys and the entrypoint re-installs the file. No transform runs outside the
   deployer; nothing is hand-edited.

## Migration

A deployment whose routes predate this change has the per-route artifacts but a
publish-built `warpRoutes.yaml` at the `generated/` root. Because the aggregation reads
persistent artifacts rather than depending on the in-loop deploy, the next deployer run
rebuilds it under `generated/warp-routes/` without redeploying routes (no `FORCE_REDEPLOY`).
The stale root-level `warpRoutes.yaml`, if present from a prior deploy, is unused once the
warp-UI ConfigMap sources `generated/warp-routes/`; publish's `git add generated/` will carry
the new subdir, and the old file can be deleted in the same cleanup that drops committed
state.

## Testing

No bespoke unit tests. The e2e already deploys both routes (USDC collateral/synthetic +
native SOL), so it validates the deployer-built config directly:

- **`test_02_warp_deployer.py`** — assert the warp-deployer produced
  `<state>/warp-routes/warpRoutes.yaml`, that it parses as a `WarpCoreConfig`, has four token
  entries, the expected `SealevelHyp*` standards, and integer `decimals`.
- **`test_10_warp_ui.py::test_warp_ui_serves_runtime_config`** — unchanged; already asserts
  the *served* `warpRoutes.yaml` carries both routes.
- **`test_12_warp_ui_bridge.py`** — unchanged; already bridges USDC and native SOL through
  the UI.

## Risks to verify during implementation

- **jq numeric fidelity** — `decimals` must stay an unquoted JSON/YAML number end to end.
- **Route-name resolution parity** — the aggregation must resolve stem → name via
  `jq -r .name` on the menu, exactly as `deploy_route`, or the dir lookup misses.
- **Scoped-source trailing slash** — `state_distribute` must copy the *contents* of
  `generated/warp-routes/` (trailing slash) so `warpRoutes.yaml` lands at the ConfigMap root,
  not under a `warp-ui/` subdir (which SO would drop).
- **e2e ordering** — `test_02` reads `<state>/warp-routes/warpRoutes.yaml` only after the
  warp-deployer fixture has produced it (the existing fixture dependency enforces this).
