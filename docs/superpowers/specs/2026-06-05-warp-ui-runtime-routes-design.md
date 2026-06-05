# Warp UI Runtime Multi-Route Configuration

**Status:** design approved, awaiting implementation plan
**Date:** 2026-06-05

## Context

The warp-deployer is now config-driven: a single deployment deploys every route named in `WARP_ROUTES` (comma/space-separated), writing per-route artifacts under `/state/warp-routes/<name>/`. The warp UI has not kept up — it shows exactly one route, baked in at image-build time.

The UI is built from the upstream `hyperlane-warp-ui-template` (the "Warp UI v2" generation; package version 13.0.0, Next.js 15). That template already supports *multiple* routes natively: it assembles a `WarpCoreConfig` (`{ tokens, options }`) from several sources and renders a token/route selector over the merged `tokens` array. So the UI does not need new multi-route features — it needs to be *fed* the deployed routes.

The obstacle is timing. The template loads its routes (`src/consts/warpRoutes.{ts,yaml}`) and custom chains (`src/consts/chains.ts`) by `import`, so they are bundled into the JS at build time. But our route addresses do not exist until the warp-deployer runs. The current integration bridges that gap with a sentinel hack: the stacks repo overlays a single-route `configs/warpRoutes.yaml` full of `__SENTINEL__` placeholders, builds it into the bundle, then `sed`-replaces the placeholders in the compiled `/app/.next` output at container start (`entrypoint.sh`), with `fix-numeric-types.js` repairing numeric JSON afterwards.

That trick works for exactly one route of fixed shape. It cannot express a *variable number* of routes: filling scalar placeholders is possible, but adding or removing a whole token block from minified bundle output is not. The selected route set also differs per environment (production deploys `usdc`; the e2e suite deploys `usdc,sol`), so the UI must reflect a changing set.

A separate issue compounds this. The template falls back to the **full published Hyperlane registry** when no custom registry is configured, and the current `warpRouteWhitelist` is `null` (allow all). So the UI bundles and displays every published mainnet warp route and chain — real, but entirely unrelated to this deployment. The gorbagana bridge UI should show only gorbagana routes.

This design moves route and chain configuration from build-time bake to **runtime load**: the deployment generates the config from deployer output state, and the UI fetches it at startup. It also makes the UI gorbagana-only. The work spans two repositories — the fork (`gorbagana-dev/hyperlane-warp-ui-template`, branch `gorbagana`, sitting on upstream v2.0.0) and `hyperlane-stacks`.

## Goals

- One UI image serves any number of deployed routes; the route set is determined at deploy time, not build time.
- Route and chain configuration are generated from deployer output state and loaded by the UI at startup.
- The UI shows only gorbagana routes — no published-registry (mainnet) routes or chains.
- Gorbagana customizations that currently live as a build overlay in `hyperlane-stacks` move into the fork, where they belong.
- The brittle sentinel/`sed`/`fix-numeric-types` machinery is removed.
- The e2e suite deploys and exercises multiple routes (USDC collateral + native SOL) through the UI.

## Non-goals

- Building multi-route UI behaviour — the template's token selector already handles it.
- Any change to the warp-deployer or its `warp-routes-config` input menu — already multi-route.
- A hosted custom Hyperlane registry — rejected as heavy infrastructure for no added benefit over runtime file injection.
- Per-user runtime route injection via the browser modal (localStorage, per-browser) — unsuitable for a shared deployment.

## Architecture

### The config contract

The UI server holds two files under `/app/public`, generated at container start and fetched by the app during initialization:

- **`warpRoutes.yaml`** — a `WarpCoreConfig` (`{ tokens: [...], options: {} }`) containing every deployed route. Each route contributes two token entries, one per chain, in the shape the template already expects:
  - collateral side: `standard: SealevelHypCollateral`, `addressOrDenom: <warp program>`, `collateralAddressOrDenom: <SPL mint>`, `mailbox`, `connections: [ sealevel|<remote chain>|<remote warp program> ]`
  - synthetic side: `standard: SealevelHypSynthetic`, `addressOrDenom: <warp program>`, `collateralAddressOrDenom: <synthetic mint>`, `mailbox`, mirrored `connections`
  - native origin (e.g. the SOL route): `standard: SealevelHypNative`, `addressOrDenom: <warp program>`, no `collateralAddressOrDenom`, `mailbox`, `connections`
  - each entry also carries `chainName`, `name`, `symbol`, `decimals`
- **`chains.yaml`** — `ChainMetadata` for gorchain and solana: `protocol: sealevel`, `chainId`, `domainId`, `name`, `mailbox`, `rpcUrls`, `nativeToken`, `blocks`.

Both chains and routes are generated, not committed in the fork, because chain identifiers (domain/chain IDs) differ between environments (local testnet vs production) and mailbox/RPC values are only known at deploy time.

### Data sources

The generator builds the contract from deployer output state that already exists per route:

- `warp-routes/<name>/token-config.json` — per-chain side `type` (collateral/synthetic/native), origin token, synthetic mint.
- `warp-routes/<name>/warp-deploy-outputs/program-ids.json` — warp program addresses (base58) per chain.
- Core deployer `program-ids.json` per chain — mailbox addresses.

`tests/e2e/lib/state_loader.py` already exposes the multi-route readers (`discover_routes`, `read_route_token_config`, `read_route_program_addresses`).

### Distinct from the deployer menu

The deployer's `warp-routes-config` configmap (input *menu*: `usdc.json`, `sol.json`) is a different artifact from the UI's generated `WarpCoreConfig` (deployer *output*, with real addresses). To avoid collision, the UI's configmap is named **`warp-ui-config`**.

### Loading flow

The template's initialization is async end-to-end: `assembleWarpCoreConfig()` and `assembleChainMetadata()` are async, called inside async `initWarpContext()`, and `WarpContextInitGate` already blocks rendering behind a spinner until they resolve. Adding an `await fetch(...)` of the runtime files is therefore additive. `/app/public` is served by the Next.js standalone server and is already written to at container start, so a file produced by the entrypoint is fetchable at e.g. `/warpRoutes.yaml`.

## Repository changes

### Fork — `hyperlane-warp-ui-template` (branch `gorbagana`)

- **Runtime loader.** New `src/utils/runtimeConfig.ts` fetches, parses, and validates `/warpRoutes.yaml` and `/chains.yaml` (reusing the template's YAML/JSON parse and the `WarpCoreConfigSchema` / `ChainMetadataSchema` validators). Merge calls are added to `assembleWarpCoreConfig()` (in `src/features/warpCore/warpCoreConfig.ts`) and `assembleChainMetadata()` (in `src/features/chains/metadata.ts`). The merge is additive and falls back to the bundled (empty) config when a file is absent or invalid.
- **Gorbagana-only.** `src/consts/warpRouteWhitelist.ts` is set to `[]` (an empty array filters out all published-registry routes, leaving only the injected ones; `null` would allow them all). The mainnet defaults in `src/consts/config.ts` (`defaultOriginToken`, `defaultDestinationToken`, `featuredTokens`) are replaced with gorbagana tokens.
- **Absorb the overlay.** The stacks build currently overlays the fork at image-build time via two directories — `configs/` (build overlays: `chains.yaml`, `warpRoutes.yaml`, `.env.sentinel`) and `patches/` (source patches: `warpRouteWhitelist.ts`, `SolanaWalletContext.tsx`). This work eliminates that overlay entirely; every piece lives natively in the fork:
  - `warpRoutes.yaml` / `chains.yaml` — no longer overlaid; generated at deploy time and loaded at runtime (above).
  - `warpRouteWhitelist.ts` — committed in the fork as `[]` (above).
  - `.env.sentinel` `NEXT_PUBLIC_WALLET_CONNECT_ID` — a real build-time value committed in the fork, no sentinel.
  - `SolanaWalletContext.tsx` — committed in the fork. This file is not a pure move: it currently embeds a `__SOLANA_RPC_URL__` sentinel for the wallet `ConnectionProvider` endpoint (a deliberate fix pinning a fixed solana RPC so wallet autoConnect doesn't fail on the reverse bridge). With `sed` gone, the in-fork version sources that RPC **at runtime from the loaded chain metadata** (e.g. `multiProvider.getRpcUrl('solana')`) instead of a sentinel. The endpoint only needs *a valid* solana RPC, not the origin chain's, so reading it from loaded chains is safe.
  - Any branding / `config.ts` defaults.

  Note: the fork's own `patches/` directory (pnpm `patchedDependencies`, referenced by `pnpm-lock.yaml`) is unrelated upstream dependency patching and stays as-is.

### hyperlane-stacks — container build

The sentinel machinery and the source overlay are both removed:

- Delete `fix-numeric-types.js` and the entire `configs/` overlay (`warpRoutes.yaml`, `chains.yaml`, `.env.sentinel`) and source `patches/` overlay (`warpRouteWhitelist.ts`, `SolanaWalletContext.tsx`) — all now live in the fork.
- `build.sh` drops its overlay-copy and patch-apply/restore logic.
- `Dockerfile` stops COPYing `configs/*` over `src/consts/` and the source patches; it just builds the fork. `stack.yml`'s repo pin points at the fork at tag `v2.0.0-gorbagana.1`.
- `entrypoint.sh` no longer `sed`s the compiled bundle. It copies the mounted `warp-ui-config` files (`warpRoutes.yaml`, `chains.yaml`) into `/app/public`, then starts the server.

Both files are produced by the deployment layer (ops and conftest), not the entrypoint: `chains.yaml` is generated from the spec's chain values (RPC URLs, domain/chain IDs, names, native token) and the core deployer's mailboxes. The only remaining build-time UI env is `NEXT_PUBLIC_WALLET_CONNECT_ID`, a constant project ID baked normally by Next.js — its sentinel is removed.

### hyperlane-stacks — wiring

- **compose** (`docker-compose-hyperlane-warp-ui.yml`): remove the per-route scalar env (`WARP_COLLATERAL_ADDRESS`, `WARP_SYNTHETIC_ADDRESS`, `WARP_TOKEN_MINT`, `WARP_SYNTHETIC_MINT`, `WARP_TOKEN_*`, `WARP_SYNTHETIC_*`) and the chain env (chain values now feed `chains.yaml` generation); keep `NEXT_PUBLIC_WALLET_CONNECT_ID`; add the `warp-ui-config` configmap mount.
- **specs** (`deployment/spec-warp-ui.yml` and `deployment/local/spec-warp-ui.yml`): remove the scalar `WARP_*` keys; add the `warp-ui-config` configmap; keep the chain and mailbox config (the ops layer reads them to generate `chains.yaml`).
- **ops** (`ops/playbooks/publish-bridge-state.yml`): replace the first-route resolution (`WARP_ROUTES.split()[0]` and the scalar spec patch) with a loop over all selected routes that builds the `WarpCoreConfig` and `chains.yaml` and writes them into generated state. Add the `state_distribute` role and `configmap_names: ["warp-ui-config"]` to the warp-ui play in `ops/playbooks/deploy-all.yml` (currently absent). The `state_distribute` role itself is generic and needs no change.

### hyperlane-stacks — e2e

- `tests/e2e/conftest.py` (`warp_ui_deployment`): stop patching first-route scalars into the spec; instead generate the multi-route `WarpCoreConfig` + `chains.yaml` into the deployment's `warp-ui-config` configmap dir, from the same per-route state the bridge tests use.
- `tests/e2e/test_10_warp_ui.py`: drop the route-specific sentinel assertions; assert the served runtime config and that both deployed routes are present.
- `tests/e2e/test_12_warp_ui_bridge.py`: exercise route selection across both routes (USDC and native SOL).
- `tests/e2e/fixtures/test-spec-warp-ui.yml`: remove the route placeholders; keep mailbox/chain placeholders.

## Error handling

- **UI loader:** a missing or invalid runtime file is logged and skipped; assembly continues with the bundled config. If nothing loads, the existing `WarpContextInitGate` surfaces the template's "no routes" state rather than rendering a broken form.
- **Generation (ops + e2e):** a missing route artifact is a hard error. Generation must fail loudly rather than ship an empty or partial UI config.

## Testing

- **Fork, standalone:** run the UI with a hand-authored two-route sample config and confirm runtime load, gorbagana-only filtering, and that the **native SOL route** both renders and transfers. This validates `SealevelHypNative` support before any stacks wiring.
- **Stacks e2e:** the updated `test_10_warp_ui.py` and `test_12_warp_ui_bridge.py` run against a real two-route deployment (USDC + SOL), reusing the existing deployment via the `--skip-*` flags.

## Risks to verify during implementation

- **`SealevelHypNative` support.** Confirm the exact `TokenStandard` value and required fields, and that the v2 transfer flow handles a native sealevel origin. Verify standalone before wiring.
- **Runtime-written `/app/public`.** Confirm the standalone server serves files written at container start (low risk — the current entrypoint already writes there).
- **Publish path.** Pushing `ghcr.io/gorbagana-dev/hyperlane-warp-ui` at the new tag needs registry credentials; confirm the publish mechanism (the local `build.sh` produces a `:local` tag; production pulls the published tag).
- **Wallet RPC timing.** `SolanaWalletContext` may mount before chain metadata is loaded; its runtime solana-RPC lookup needs a sane fallback (or to render behind the init gate) so wallet autoConnect still works on first paint.

## Sequencing

1. Lock the config contract (this document).
2. Fork changes — runtime loader, gorbagana-only config, absorbed overlay — testable standalone.
3. Publish the fork image at `v2.0.0-gorbagana.1`.
4. Stacks — container-build rework, compose/spec wiring, ops multi-route generation.
5. E2e — multi-route fixtures and tests.
