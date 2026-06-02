# Multiple Warp Route Deployments

_Design spec — 2026-06-02_

## Status

The bridge deploys exactly one warp route today (USDC, Solana ↔ gorchain).
This spec generalizes the warp-deployer and warp-UI to support **multiple warp
routes on the same chain pair**, and adds a second route: **GOR** (gorchain's
native token, bridged to a synthetic on Solana).

It supersedes the "Known Limitations / single warp route" note in
`specs/stack-specifications.md` (Stack 2). That note came from a quick
investigation; the scope it describes is verified and **corrected** below
against the code.

## Problem

A warp route is one bridged asset: a token that is *locked* (collateral) or
*native* on its origin chain and *minted* (synthetic) on the remote chain. The
bridge is structurally single-route:

- `spec-warp-deployer.yml` exposes scalar `WARP_TOKEN_MINT`, `WARP_ROUTE_NAME`,
  `WARP_TOKEN_METADATA_URI`, and one `COLLATERAL_CHAIN`/`SYNTHETIC_CHAIN` pair
  (`deployment/spec-warp-deployer.yml:17-20`).
- `token-config.json.tmpl` hardcodes `USDC`/`USDC`/`decimals: 6` and a
  `collateral`+`synthetic` shape with an SPL `token` mint on both sides
  (`stack_orchestrator/data/config/warp-deployer-token-config/token-config.json.tmpl`).
- The deploy script writes a single `/state/token-config.json` and
  `/state/warp-deploy-outputs/`, and its idempotency gate skips when that one
  file is populated (`.../warp-deployer-scripts-config/deploy.sh:60-70, 260`).
- The deploy script's output builder hardcodes a `collateral`+`synthetic`
  `warpRoute` object (`deploy.sh:233-252`) — it cannot represent a `native`
  side.

## What was verified (and corrected)

Each claim in the `stack-specifications.md` note was checked against code.

**Confirmed:** the scalar spec vars, the hardcoded USDC template, and the flat
single-file state + single idempotency gate (citations above).

**Corrected — the blast radius is smaller than the note states:**

- The note lists **relayer and gas-oracle** as single-route consumers that need
  per-route ConfigMaps. They are **route-agnostic and need no changes**:
  - The relayer delivers every message between the two mailboxes; its compose
    has no warp/route/recipient filtering — only gas-payment enforcement and
    signer keys (`docker-compose-hyperlane-relayer.yml`). A new route on the
    same mailboxes is relayed automatically.
  - The gas-oracle sets IGP gas-oracle configs **per domain**, not per route;
    `spec-gas-oracle.yml` carries no warp variables. The same IGP serves all
    routes on that domain.
  - Validators (per-chain checkpoint signing over the mailbox merkle tree) and
    MinIO are likewise per-chain, not per-route.

- The note treats **warp-UI** as structurally single-route. It is not: the UI
  assembles its token list from `registry + warpRoutes.ts + warpRoutes.yaml +
  store overrides`, flattens, dedupes, and drops unconnected tokens
  (`hyperlane-warp-ui-template/src/features/warpCore/warpCoreConfig.ts:62-67`).
  The `tokens:` list already supports N routes. The *only* single-route limit
  is that the baked `warpRoutes.yaml` and its startup sentinels are fixed at
  **image build time**, so adding a route requires a UI image rebuild.

## Route model

Both routes are on the **Solana ↔ gorchain** pair and share the same core
contracts, validators, relayer, gas-oracle, and MinIO.

| Route name | gorchain side | Solana side | Token types |
|---|---|---|---|
| `USDC-solana-gorchain` (existing) | `gUSDC` — `synthetic` | `sUSDC` — `collateral` (real SPL) | collateral + synthetic |
| `GOR-gorchain-solana` (new) | `gGOR` — **`native`** (decimals 9) | `sGOR` — `synthetic` (decimals 9) | **native + synthetic** |

**Direction is derived from code, not assumed.** `gGOR` is gorchain's native
token: `spec-gas-oracle.yml:47-49` ("Gorchain's native token (gGOR) … 1 gGOR =
100 sGOR") and the registry metadata
(`warp-deployer-registry-config/metadata.yaml.tmpl:9-11`, gorchain `nativeToken`
= GOR, decimals 9). So the gorchain side is `type: native`; the Solana side is a
`synthetic` SPL (`sGOR`). The sealevel client supports all three token types —
`native` → `hyperlane_sealevel_token_native`, `collateral` →
`hyperlane_sealevel_token_collateral`, `synthetic` → `hyperlane_sealevel_token`
(`hyperlane-monorepo/rust/sealevel/client/src/warp_route.rs:201-203`).

## Scope

**In scope:** N warp routes on one chain pair — per-route warp-deployer
deployments, per-route state, a deploy script that honors any per-side token
type, and a warp-UI that renders all routes. Adds the GOR route as the second
instance.

**Out of scope:**
- New chain pairs (would pull in new validators/relayer chains/MinIO buckets/IGP
  domains). Same-pair only.
- Per-route pause / kill-switch / rate-limit ops flows — these belong to the ops
  layer and `docs/production-readiness-gaps.md` §5. Tracked, not built here.
- Fully dynamic route count in the UI (arbitrary N without an image rebuild).
  See [Known limitations](#known-limitations).

## Design

### Repository boundary

All changes are in **`hyperlane-stacks`**. The warp-UI customization
(`warpRoutes.yaml`, `chains.yaml`, `entrypoint.sh`, `fix-numeric-types.js`)
lives in this repo's `container-build/` dir and is `COPY`'d over the template at
docker build (`Dockerfile: COPY configs/warpRoutes.yaml src/consts/warpRoutes.yaml`),
so `hyperlane-warp-ui-template` is untouched. The token types are already
supported by the sealevel client, so `hyperlane-monorepo` is untouched.

### Warp-deployer: one deployment per route

Mirror the per-chain validator pattern (two specs, one stack). Each route is a
separate deployment of the `hyperlane-svm-warp-deployer` stack:

- `deployment/spec-warp-deployer-usdc.yml` (renamed from `spec-warp-deployer.yml`)
- `deployment/spec-warp-deployer-gor.yml` (new)

Each spec sets its own `WARP_ROUTE_NAME`, collateral/synthetic chain + domain +
RPC, metadata, and references its own token-config ConfigMap. Like the two
validators, both deployments resolve to the same SO-derived namespace
(`laconic-hyperlane-svm-warp-deployer`) but get distinct deployment-ids, so
their Jobs and resources do not collide.

**Why per-route specs over a single list-shaped `warp-routes:` block:** matches
the established validator per-instance convention; isolates failures (one route
failing to deploy doesn't block the other); gives each route an independent
idempotency gate and redeploy; and avoids encoding a list inside a single env
var, which SO's k8s config path handles poorly.

### Per-route token-config

Replace the single hardcoded template with **one token-config template per
route**, each a ConfigMap source dir under `stack_orchestrator/data/config/`:

- `warp-deployer-token-config-usdc/token-config.json.tmpl` — USDC: collateral on
  Solana, synthetic on gorchain (today's content).
- `warp-deployer-token-config-gor/token-config.json.tmpl` — GOR: `native` on
  gorchain (no `token` mint, no `uri`), `synthetic` on Solana.

Each template carries the route's real metadata (per-side `type`, `decimals`,
`symbol`, optional `token`/`uri`); only the deploy-time addresses
(`${COLLATERAL_ISM}`, `${…_IGP}`, `${…_MAILBOX}`) stay as `envsubst`
placeholders. The deploy script stays generic — it renders whichever template is
mounted. The existing post-render `jq` step that strips empty `uri` fields
already lets a side omit `uri` (needed for `native`).

### Generalize the deploy script's output builder

`deploy.sh:233-252` hardcodes the output `warpRoute` object as
`collateral`+`synthetic` with a `tokenMint`. This is the one place the script
itself assumes a token shape. Generalize it so the emitted
`token-config.json` reflects the route's actual per-side types (derive from the
rendered input token-config rather than re-templating collateral/synthetic).
Everything else in the script (program-id lookup, CLI invocation, upgrade
authority transfer) is already chain-name-driven and route-agnostic.

### State layout

Each route writes to its own subdirectory; the shared core deployer output
(`/state/program-ids.json`) is unchanged (read-only input):

```
/state/
  program-ids.json                         ← core deployer (shared, unchanged)
  warp-routes/
    USDC-solana-gorchain/
      token-config.json
      warp-deploy-outputs/
    GOR-gorchain-solana/
      token-config.json
      warp-deploy-outputs/
```

The idempotency gate checks the route's own
`warp-routes/<WARP_ROUTE_NAME>/token-config.json`, so deploying the GOR route
does not see the USDC route as "already done."

This is a breaking change to the on-disk layout. There is no production
multi-route deployment yet, so no migration is required; the single existing
USDC route simply moves under `warp-routes/USDC-solana-gorchain/`.

### State aggregation (tests + future ops)

`tests/e2e/lib/state_loader.py` and `tests/e2e/conftest.py` read a fixed
`token-config.json` and a `{chain: address}` program map
(`_get_warp_program_addresses`, conftest:1601). Two routes on the same chains
collide in that map. Change the loader to **discover all
`warp-routes/<route>/` subdirs** and key program addresses by
`(route, chain)`, so each route's collateral/synthetic addresses are
distinguishable.

### Warp-UI: render all routes

The UI's `tokens:` list already supports N routes; the work is to add the
second route and feed it real addresses:

- `container-build/.../configs/warpRoutes.yaml` — add the GOR route's **two
  connected token entries** (gorchain `native`, Solana `synthetic`), each with a
  fresh sentinel set (e.g. `__GGOR_NATIVE_ADDRESS__`,
  `__SGOR_SYNTHETIC_ADDRESS__`, `__SGOR_SYNTHETIC_MINT__`, plus
  symbol/decimals). The two entries cross-reference via `connections` so neither
  is dropped by `filterUnconnectedToken`.
- `container-build/.../entrypoint.sh` — add matching `sed` lines for the new
  sentinels.
- `docker-compose-hyperlane-warp-ui.yml` + `deployment/spec-warp-ui.yml` — add
  the new route's env vars (addresses, symbol, decimals).
- Address values come from the GOR route's state subdir, filled the same way the
  USDC route's are (conftest patching in e2e; operator/state in prod).

Because the route set is baked at build time, **adding the GOR route requires a
warp-UI image rebuild.** The warp-deployer side needs no rebuild — it is driven
by per-route specs and ConfigMaps at deploy time.

### Unchanged by design

Relayer, gas-oracle, validators, and MinIO are **not modified** — each operates
at the mailbox / domain / chain level, below the warp-route abstraction (see
[What was verified](#what-was-verified-and-corrected)). This is the main reason
the change is contained.

## Files touched

| File | Change |
|---|---|
| `deployment/spec-warp-deployer.yml` → `spec-warp-deployer-usdc.yml` | Rename; reference per-route token-config CM. |
| `deployment/spec-warp-deployer-gor.yml` | New. GOR route: native gorchain + synthetic Solana, own `WARP_ROUTE_NAME`. |
| `stack_orchestrator/data/config/warp-deployer-token-config-usdc/token-config.json.tmpl` | USDC template (moved from `warp-deployer-token-config/`). |
| `stack_orchestrator/data/config/warp-deployer-token-config-gor/token-config.json.tmpl` | New. `native`+`synthetic` template. |
| `stack_orchestrator/data/config/warp-deployer-scripts-config/deploy.sh` | Per-route state subdir + idempotency; generalize output `token-config.json` to honor per-side type. |
| `stack_orchestrator/data/compose-jobs/docker-compose-hyperlane-svm-warp-deployer.yml` | Reference per-route token-config CM volume. |
| `container-build/gorbagana-dev-hyperlane-warp-ui/configs/warpRoutes.yaml` | Add GOR route's two token entries + sentinels. |
| `container-build/gorbagana-dev-hyperlane-warp-ui/entrypoint.sh` | Add `sed` lines for GOR sentinels. |
| `stack_orchestrator/data/compose/docker-compose-hyperlane-warp-ui.yml` | GOR route env vars. |
| `deployment/spec-warp-ui.yml` | GOR route env vars. |
| `tests/e2e/lib/state_loader.py` | Discover `warp-routes/<route>/` subdirs; key programs by `(route, chain)`. |
| `tests/e2e/conftest.py` | Per-route warp-deployer deployment; per-route address resolution + warp-UI patching. |
| `tests/e2e/test_02_warp_deployer.py` | Assert both routes deploy (per-route state, `native`+`synthetic` program-ids). |
| `tests/e2e/test_10_warp_ui.py` | Assert both routes' tokens render. |
| `tests/e2e/fixtures/test-spec-warp-deployer-*.yml`, `test-spec-warp-ui.yml` | Per-route fixtures + UI env. |
| `specs/stack-specifications.md` | Replace the single-route limitation note with multi-route reality. |
| `docs/architecture-decisions.md` | Update the warp-deployer row (multi-route). |
| `docs/production-readiness-gaps.md` | Fill §5.4 (multiple warp routes). |

## E2E approach

Add the GOR native route as a second warp-deployer deployment alongside USDC.
Assert:

- Both routes deploy: each `warp-routes/<route>/token-config.json` and
  `warp-deploy-outputs/program-ids.json` are populated, with the GOR route's
  gorchain side deployed as the `native` program and its Solana side as
  `synthetic`.
- The per-route idempotency gate is independent (deploying GOR does not skip on
  USDC's state, and vice versa).
- Warp-UI renders both routes' token pairs (the assembled `tokens[]` contains
  all four entries, correctly connected).

A bridge transfer over the GOR route is a nice-to-have but depends on a funded
native balance; checkpoint/relay correctness is already covered by the existing
USDC transfer test and is route-independent at the agent layer.

## Decision log

1. **Per-route specs, not a `warp-routes:` list** — matches the validator
   per-instance pattern, isolates failures, independent idempotency/redeploy,
   avoids list-in-env in SO's k8s path.
2. **Per-route token-config templates, not richer env vars** — `native` and
   `collateral` differ structurally (mint present/absent, metadata fields), and
   `envsubst` has no conditionals; a full per-route template is clearer than
   branching one template across token types.
3. **State under `warp-routes/<route>/`** — gives each route an isolated
   idempotency target and lets the loader enumerate routes without a manifest.
4. **Relayer/gas-oracle/validator/MinIO untouched** — verified route-agnostic
   in code; touching them would be unfounded scope.
5. **Accept build-time route count for the UI** — the `tokens[]` list already
   supports N; a runtime/registry loader would be a larger UI fork change with
   no payoff for a known, rarely-changing route set (YAGNI).

## Known limitations

- **UI route count is fixed at image build.** Adding a route requires rebuilding
  the warp-ui image. The fully-dynamic path is to have the UI load warp routes
  from a runtime registry or mounted config instead of the build-time
  `warpRoutes.yaml` — deferred until there's a real need for operator-added
  routes without a rebuild.
- **No per-route ops controls.** Independent pause / kill-switch / rate limits
  per route are an ops-layer concern (`production-readiness-gaps.md` §5), not
  part of this deployment-plumbing change.
