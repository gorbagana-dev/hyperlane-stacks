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

The tempting fix — build `warpRoutes.yaml` in the warp-UI entrypoint, where `chains.yaml`
is already rendered — does not work. The reason shapes this design.

## Constraint: the per-route inputs never reach the pod

SO builds a ConfigMap from a single directory level
(`cluster_info.py:get_configmaps` → `for f in os.listdir(path): if os.path.isfile(f)`):
subdirectories are silently skipped, and ConfigMap keys cannot contain `/`.

The deployer's `generated/` tree (== its `/state`; `publish` copies it verbatim to git and
`state_distribute` copies it into `configmaps/warp-ui-config/`):

```
generated/
  program-ids.json          core: { <chain>: { mailbox, igp, ism, … } }   [top-level → reaches pod]
  agent-config.json                                                        [top-level → reaches pod]
  gas-oracle-config.json                                                   [top-level → reaches pod]
  multisig-config.json                                                     [top-level → reaches pod]
  warpRoutes.yaml      ★ the UI's WarpCoreConfig                           [top-level → reaches pod]
  registry/metadata.yaml                                                   [subdir → dropped]
  warp-routes/                                                             [subdir → dropped]
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

`warpRoutes.yaml` must therefore be pre-assembled into one **top-level** file by an actor
that can see `warp-routes/`. The first such actor is the **warp-deployer** — it *writes*
that tree, before `publish` copies it and before SO flattens it away.

## Goals

- The route transform lives in **one** place — the warp-deployer (`deploy.sh`), in
  shell/jq next to the data — exercised identically by prod, local, and e2e.
- `publish-bridge-state` and `conftest` return to **distribution only**: they carry the
  deployer-produced `warpRoutes.yaml`, they do not build it.
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

## Design

### Producer — aggregation step in `deploy.sh`

`deploy.sh` ends with a loop over `WARP_ROUTES` (`for route in …; do deploy_route …`).
Add one step **after** the loop that writes `${STATE_DIR}/warpRoutes.yaml`. For each stem
in `WARP_ROUTES`:

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

Write `{ tokens: [<all>], options: {} }` to `${STATE_DIR}/warpRoutes.yaml`.

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

- **`publish-bridge-state.yml`** — delete the `warpRoutes.yaml`-building block
  (`_warp_stems`, the per-route slurps, the `_warp_tokens` accumulator, "Write
  warpRoutes.yaml into the generated state"). Keep: copy `generated/` (now already
  containing `warpRoutes.yaml`), patch `GORCHAIN_MAILBOX`/`SOLANA_MAILBOX` into
  `spec-warp-ui.yml` (the entrypoint needs them as env for `chains.yaml`), the existing
  core IGP/mailbox spec patches, and git add/commit/push.
- **`conftest.py`** — delete `_build_warp_ui_config`. Add
  `("warpRoutes.yaml", "warp-ui-config")` to
  `CONSUMER_STATE_FILES["hyperlane-warp-ui"]` so the existing
  `BridgeStateLoader.populate` copies the deployer-produced file into the configmap dir —
  removing the bespoke generate-and-write entirely.
- **`entrypoint.sh`** — unchanged (renders `chains.yaml` from env; copies `warpRoutes.yaml`
  from `/config/warp-ui-config/` to `/app/public`).
- **`state_distribute`, SO, specs, compose** — unchanged. `warpRoutes.yaml` stays a
  top-level file in `generated/`, surviving the flat-configmap model exactly as now.

### Add-a-route flow

1. Add the stem to `WARP_ROUTES` in `spec-warp-deployer.yml` (menu file checked in, or add one).
2. Re-run `deploy-all` (or the warp-deployer → publish → warp-ui plays): the deployer
   deploys only the new route (others self-skip), the aggregation rebuilds the full
   `warpRoutes.yaml`, publish pushes the updated `generated/`, the warp-UI play redeploys
   and the entrypoint re-installs the file. No transform runs outside the deployer; nothing
   is hand-edited.

## Migration

A deployment whose routes predate this change has the per-route artifacts but a
publish-built `warpRoutes.yaml`. Because the aggregation reads persistent artifacts rather
than depending on the in-loop deploy, the next deployer run rebuilds the file without
redeploying routes — no `FORCE_REDEPLOY` needed.

## Testing

- **Deployer:** with two route dirs present (USDC collateral/synthetic, native SOL), run
  the aggregation and assert the emitted `warpRoutes.yaml` parses under
  `WarpCoreConfigSchema`, has four token entries, correct standards, integer `decimals`,
  and mirrored `connections`.
- **e2e:** consumer behaviour is unchanged — `test_10_warp_ui` still asserts the served
  `warpRoutes.yaml` carries both routes; `test_12_warp_ui_bridge` still bridges USDC and
  native SOL. The only difference is the file is deployer-produced, so conftest no longer
  builds it.

## Risks to verify during implementation

- **jq numeric fidelity** — `decimals` must stay an unquoted YAML number end to end.
- **Route-name resolution parity** — the aggregation must resolve stem → name via
  `jq -r .name` on the menu, exactly as `deploy_route`, or the dir lookup misses.
- **e2e ordering** — conftest reads `state_dir/warpRoutes.yaml` only after the
  warp-deployer fixture has produced it (the existing fixture dependency already enforces
  this).
