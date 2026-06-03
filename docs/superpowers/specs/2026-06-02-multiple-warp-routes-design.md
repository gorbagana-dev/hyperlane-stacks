# Configurable Warp Routes

_Design spec — 2026-06-02_

## Status

Make warp routes **configurable**: an operator declares a route's details and
that route is deployed, with multiple routes supported on the same chain pair.
No token pair is special-cased in code. The existing USDC route becomes one
ordinary configured route.

## Problem

A warp route bridges one asset across two chains. Each side has a token type —
`native`, `collateral`, or `synthetic`. The warp-deployer stack is hard-wired to
a single route:

- the token metadata (name, symbol, decimals) is baked into a fixed template;
- the deploy script assumes a collateral origin with an SPL mint;
- deployer state lives at a single top-level path with one idempotency check,
  so a second route can't be deployed alongside the first.

Adding a token pair therefore requires editing templates and scripts. The goal
is to make a route **pure configuration** — declared in a few fields, deployed
with no code or template changes — and to let several routes run side by side.

## Goals

1. A route is defined entirely by a small set of operator-supplied fields.
2. The deployer builds the on-chain token-config from those fields, supporting
   collateral, native, and synthetic token types generically.
3. Multiple routes deploy independently on the same chain pair.
4. The bridge UI shows the configured routes.
5. The e2e suite proves the above by deploying more than one route.

## Non-goals

- **New chain pairs.** Routes share the existing chains' core contracts,
  validators, relayer, gas-oracle, and storage. Spanning a new chain pair (new
  validators / relayer chains / gas-oracle domains) is out of scope.
- **The production orchestration that turns a route list into deployments.** The
  stack exposes a configuration contract (below); the automation that reads an
  operator's route list and deploys each route lives in the deployment
  automation layer, not in the stack. This spec defines the contract that layer
  drives.
- **Per-route pause / kill-switch / rate-limit controls.** Operational concerns,
  tracked separately.

## Route configuration model

An operator declares a route with a small block of human-known facts. Two
examples, showing the model generalizes across token types:

```yaml
# Collateral ↔ synthetic: a real SPL token on the origin, wrapped on the remote.
- name: USDC-solana-gorchain
  symbol: USDC
  display_name: USD Coin
  decimals: 6
  origin: { chain: solana,   type: collateral, token: "<USDC SPL mint>" }
  remote: { chain: gorchain, type: synthetic,  metadata_uri: "https://…/usdc.json" }
```

```yaml
# Native ↔ synthetic: a chain's native coin, wrapped on the remote.
- name: SOL-solana-gorchain
  symbol: SOL
  display_name: Solana
  decimals: 9
  origin: { chain: solana,   type: native }
  remote: { chain: gorchain, type: synthetic, metadata_uri: "https://…/sol.json" }
```

The operator supplies only what a human knows: the route name, the token's
`symbol` / `display_name` / `decimals`, each side's chain and **token type**, the
origin token mint (for a `collateral` side), and a metadata URI for the minted
(`synthetic`) side. Everything else — domain IDs, RPC URLs, and the ISM / IGP /
mailbox addresses for each side — is derived automatically from the core
deployment.

**Token type is declared explicitly per side** — `collateral` (locks an existing
SPL `token`), `native` (the chain's native coin), or `synthetic` (mints a wrapped
SPL with the given metadata) — mirroring the on-chain token-config. Declaring the
type, rather than inferring it from which fields are present, (a) makes a
forgotten field a fast, explicit error instead of a silently mis-typed
deployment, and (b) supports any combination the deployer allows (e.g. a side
that is collateral on both chains), rather than assuming the remote is always
synthetic.

### Stack configuration contract

The stack consumes the route as flat spec `config:` fields (one route per
deployer spec). This is the contract the deployment automation fills:

| Field | Meaning | Notes |
|---|---|---|
| `WARP_ROUTE_NAME` | Unique route identifier | e.g. `USDC-solana-gorchain` |
| `WARP_ORIGIN_CHAIN` | Origin chain | |
| `WARP_ORIGIN_TYPE` | `collateral` or `native` | |
| `WARP_ORIGIN_TOKEN` | Origin SPL mint | required when origin type is `collateral` |
| `WARP_REMOTE_CHAIN` | Remote chain | |
| `WARP_REMOTE_TYPE` | `synthetic` (or `collateral` for a both-sides-collateral route) | |
| `WARP_TOKEN_SYMBOL` | Token symbol | |
| `WARP_TOKEN_NAME` | Display name | defaults to symbol |
| `WARP_TOKEN_DECIMALS` | Decimals | |
| `WARP_TOKEN_METADATA_URI` | Metadata JSON for a `synthetic` side | required when that side's chain is non-testnet; skipped on testnet |

Existing global chain config (`GORCHAIN_*`, `SOLANA_*` RPC / domain / testnet
flags) is reused; the route fields only name *which* chain is origin vs remote.

### On-the-fly spec generation (no committed derived files)

The operator edits a **short route block**; they never hand-write a full deployer
spec. The deployment automation expands each route block into a complete deployer
spec **at deploy time** and deploys it — the expanded spec is plumbing, not a
committed artifact. This keeps a single source of truth (the route list), avoids
derived files drifting out of sync, and adds no "regenerate and commit" step.
The stack's only obligation is to accept the contract fields above; how the spec
is produced (templating in production automation, parameterization in the e2e
harness, or a hand-edited example) is the caller's concern.

## Deployer: generic token-config builder

The deployer stack runs once per route. The deploy script builds the
hyperlane-sealevel-client token-config from the contract fields — there is **no
per-token template**:

1. Resolve each side's domain ID, RPC URL, and ISM / IGP / mailbox addresses
   from the global chain config and the core deployment's program IDs.
2. Construct the token-config (keyed by chain name) with `jq`, using each side's
   declared `type`: `collateral` carries the `token` mint; `native` carries
   neither mint nor metadata; `synthetic` carries `name` / `symbol` / `decimals`
   and, when present, `uri`.
3. Invoke `hyperlane-sealevel-client warp-route deploy` with the rendered config.

Rendered token-config for a collateral route:

```json
{
  "solana":   { "type": "collateral", "token": "<mint>", "interchainSecurityModule": "…", "interchainGasPaymaster": "…" },
  "gorchain": { "type": "synthetic", "name": "USD Coin", "symbol": "USDC", "decimals": 6, "uri": "…", "interchainSecurityModule": "…", "interchainGasPaymaster": "…" }
}
```

For a native route the origin side is `{ "type": "native", "decimals": 9, … }`
with no `token`; the remote side is unchanged. `jq` includes `token` / `uri`
only when supplied, so each token type renders correctly from the same builder.

## Per-route deployment

Each route is an independent deployment of the one warp-deployer stack,
parameterized by its route config. Independence is required for correctness:

- **Separate namespace per route.** The deployer creates namespace-scoped
  resources (the Job and its config maps). Two deployments of one stack cannot
  share a namespace, so each route's spec sets an explicit `namespace:`
  (e.g. `laconic-hyperlane-warp-<route>`).
- **Per-route state + idempotency.** Each route writes to its own state subdir
  (below) and its "already deployed?" check reads that subdir, so deploying one
  route never sees another as done.

Independent deployments also isolate failures (one route failing to deploy does
not block another) and allow per-route redeploy. The route config travels as
flat fields rather than a list inside a single deployment, matching how the
deployment tooling resolves configuration.

## State layout

Each route owns a subdirectory; the shared core deployment output
(`program-ids.json`) is unchanged and read-only:

```
/state/
  program-ids.json                      ← core deployment (shared input)
  warp-routes/
    <route-name>/
      token-config.json                 ← this route's config + addresses
      warp-deploy-outputs/              ← deployed warp program IDs per chain
```

The idempotency gate checks `warp-routes/<route-name>/token-config.json`.

## Bridge UI

The UI displays the configured routes. Each route's values — token metadata and
the deployed program / mint addresses — are supplied as configuration and filled
into the UI's route entries when the container starts. A route's *values* are
therefore pure configuration.

The UI's **route set is fixed when its image is built**: the number of route
entries is compiled into the app, and startup only fills their values. Adding a
*new* route to the UI therefore requires adding an entry and rebuilding the UI
image. This is an accepted operational step (the route set changes rarely and the
image is already built as part of deployment) and is documented as a limitation.
A future enhancement can drop the rebuild: the UI already merges runtime route
configs (its "add warp config" path), so loading an operator-provided routes file
at startup would reduce a route change to a config update plus restart. Out of
scope here.

## Unchanged by design

The relayer, gas-oracle, validators, and storage are **not modified**. They
operate below the warp-route abstraction — the relayer delivers every message
between the two mailboxes, the gas-oracle sets gas-oracle configs per domain, and
validators sign the mailbox checkpoints — so additional routes on the same chain
pair are handled without change. This is the main reason the work is contained.

## E2E approach

The e2e suite drives a small list of routes through the configurable path,
deploying each via the deployer stack directly (the same CLI the production
automation uses). It deploys **two differently-shaped routes** — a collateral
route and a native route — to prove the generic builder, and asserts:

- both routes deploy: each `warp-routes/<route>/token-config.json` and
  `warp-deploy-outputs/` is populated, with the collateral route showing a
  `collateral` origin and the native route a `native` origin (no mint);
- per-route idempotency is independent (deploying one does not skip the other);
- the UI renders both routes' token pairs.

The native route runs on the test chains with the testnet flag set, so the
minted token's metadata-URI validation is skipped and no hosted metadata JSON is
needed for tests.

## Files touched

| File | Change |
|---|---|
| `stack_orchestrator/data/config/warp-deployer-scripts-config/deploy.sh` | Build token-config generically from contract fields (per-side declared type); per-route state subdir + idempotency. |
| `stack_orchestrator/data/config/warp-deployer-token-config/` | Remove the hardcoded token-config template — no longer needed. |
| `stack_orchestrator/data/compose-jobs/docker-compose-hyperlane-svm-warp-deployer.yml` | Pass the route contract fields through to the container. |
| `deployment/spec-warp-deployer.yml` | Express the route as contract `config:` fields (USDC as the worked example); add explicit `namespace:`. |
| `stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-warp-ui/configs/warpRoutes.yaml` | Route entries filled from config; one entry per configured route. |
| `stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-warp-ui/entrypoint.sh` | Fill each route entry's values from config at startup. |
| `stack_orchestrator/data/compose/docker-compose-hyperlane-warp-ui.yml`, `deployment/spec-warp-ui.yml` | Per-route UI config fields. |
| `tests/e2e/lib/state_loader.py` | Discover routes under `warp-routes/`; read per-route token-config and program addresses. |
| `tests/e2e/conftest.py`, `tests/e2e/fixtures/*` | Drive a routes list; deploy each route; resolve per-route addresses. |
| `tests/e2e/test_02_warp_deployer.py`, `test_10_warp_ui.py` | Assert multiple, differently-shaped routes deploy and render. |
| `specs/stack-specifications.md`, `docs/architecture-decisions.md` | Document configurable routes. |

## Error handling

- A route missing a required field, or naming a chain absent from the core
  program IDs, fails the deploy fast with a message naming the route — no partial
  or mis-shaped deployment.
- An unknown `type`, or a `collateral` side without a `token` mint, fails fast.
  Because the type is declared, a missing mint is a clear error rather than a
  silently mis-typed (native) deployment.
- A minted (synthetic) token on a non-testnet remote chain without a metadata URI
  fails fast (the client requires it); on a testnet remote the URI is optional.

## Decision log

1. **Generic `jq` token-config builder, not per-token templates** — one builder
   handles native / collateral / synthetic; adding a token needs no template.
2. **Token type declared explicitly per side** — mirrors the on-chain
   token-config and supports any valid combination; a forgotten field becomes a
   fast error instead of a silently mis-typed deployment, which matters for
   on-chain deploys.
3. **Routes are configuration, not code** — adding a route means supplying config
   fields; no per-token template or script change. laconic-so generates the
   deployment artifacts from a per-route spec.
4. **Independent deployment per route (own namespace + state)** — required for
   namespace-scoped resource isolation and per-route idempotency; also isolates
   failures and enables per-route redeploy.
5. **UI route set fixed at image build (values configurable)** — keeps the UI in
   step with the configured routes using the existing fill-at-startup mechanism;
   runtime-loaded routes (no rebuild) are a documented future step.
6. **Relayer / gas-oracle / validators / storage untouched** — route-agnostic by
   construction; changing them would be unfounded scope.

## Known limitations

- **Adding a route to the UI requires a UI image rebuild** (the route set is
  compiled into the image). Path: load routes at startup from a mounted config
  via the UI's existing runtime-override hook (config update + restart instead of
  rebuild) — out of scope here.
- **Same chain pair only.** New chain pairs need new agents/infrastructure and
  are out of scope.
- **Production route orchestration is external.** The stack exposes the
  configuration contract; the automation that expands an operator's route list
  into per-route deployments is a separate layer that consumes this contract.
