# Config-driven warp routes: one deployment, checked-in route menu

**Date:** 2026-06-04
**Status:** Design (plan to follow).
**Scope:** The warp-deployer stack across all ops environments (`local`, `staging`, `prod`) and the e2e suite, plus one opt-in addition to stack-orchestrator's Job handling. No other stack changes.

## Problem

Each warp route is currently its own laconic-so deployment, with the route's full configuration embedded in a per-route spec and the route set driven by an ops loop:

- `ops/playbooks/deploy-all.yml:49-67` loops `warp_routes` and calls `stack_deploy` once per route, producing one deployment **and one namespace** per route (`stack_name: hyperlane-warp-{{ item }}` → `laconic-hyperlane-warp-<route>`, `spec_file: spec-warp-{{ item }}.yml`).
- The route's identity *and* the chain-level config live together in each per-route spec — e.g. `deployment/spec-warp-usdc.yml:17-46` carries `WARP_ROUTE_NAME`, the per-side `WARP_ORIGIN_*`/`WARP_REMOTE_*` fields, **and** `GORCHAIN_RPC_URL`, `*_DOMAIN_ID`, `*_CHAIN_ID`, `*_IS_TESTNET`.
- `deploy.sh` deploys exactly one route, reading those `WARP_*` env vars (`docker-compose-hyperlane-svm-warp-deployer.yml:9-21`).

The costs:

- **Adding a route means adding a whole deployment + spec**, and re-duplicating the identical chain-level block into it. With more routes this is sprawl: N specs, N deployments, N namespaces.
- **"Which routes are deployed" is implicit** in the existence of N spec files + the `warp_routes` list, rather than an explicit selection.

Validators already avoid this: the set is a checked-in config file consumed by the deployment layer (`ops/roles/common/tasks/load_validators.yml` reads `{{ deployment_root }}/bridges/{{ bridge_name }}/operator/validators.yaml`; the file is committed at `deployment/local/bridges/default/operator/validators.yaml`). Warp routes should follow the same "config drives the deployment" shape.

## What exists today (the mechanics we build on)

1. **`deploy.sh` is already structured as setup + a per-route body.** One-time setup (state/log dirs `:4-13`, core `program-ids.json` check `:26-30`, `chain_var` `:34-38`, deployer keypair + Solana CLI config `:66-80`, registry render `:104-107`) is cleanly separable from the per-route work (idempotency skip `:53-64`, token-config build `:109-136`, deploy `:144-155`, synthetic-mint resolve `:246-277`, write state `:287-309`, preflight `:319-331`). Per-route artifacts already go to a scoped dir `ROUTE_STATE_DIR=/state/warp-routes/<name>/` (`:8`), and the deployer already **skips a route whose `token-config.json` exists** unless `FORCE_REDEPLOY=true` (`:53-64`). The token-config it writes is already chain-keyed (`{warpRoute: {name, <chain>:{type,…}}}`) — the route data we are externalizing.

2. **The compose has one warp-deployer service** with a per-route env block (`:9-21`), a shared chain-config block (`:22-29`), and `bridge-state:/state` + `bridge-logs:/logs` volumes (`:32-36`).

3. **laconic-so will not re-run a completed Job.** `up()` calls `_create_jobs()` (`deploy_k8s.py:847`); on create it catches 409 and prints `"Job {job_name} already exists, skipping"` (`:869-873`) — Jobs are one-shot and SO does not recreate them on restart. The `up(force_recreate=…)` parameter exists but is an explicit no-op TODO (`:1081-1084`). Spec values are read with `self.cluster_info.spec.get(key, default)` (e.g. `:750`).

4. **Bridge state is already exported + committed.** `ops/playbooks/publish-bridge-state.yml` copies the deployer's `bridge/{{ bridge_name }}/generated/` into the on-host repo clone and commits + pushes it (`generated_rel` `:13`, copy `:36-39`). It reads each route's state generically by side `type` under `…/generated/warp-routes/<route>/` (`:93-107`) and is namespace-agnostic.

5. **The e2e mirrors the per-route model.** `tests/e2e/conftest.py` drives a `WARP_ROUTES` list (`~111`) where each entry has its own `deployment_id`/`namespace`, and `warp_deployment` (`:800-915`) loops them into separate deployments, creating a test SPL mint at runtime and patching it into each collateral route's spec.

## Decision

Model warp routes as a checked-in **menu** consumed by a **single** warp-deployer deployment. The spec selects which configured routes to deploy; `deploy.sh` loops over the selection; a small opt-in stack-orchestrator change makes the single Job idempotently re-runnable; per-route logs are written to the route's scoped state folder and ride the existing publish flow into git.

### Route menu (checked-in, route-data only)

Each route is one YAML file under `deployment/<env>/bridges/default/warp-routes/` — sitting beside the existing `operator/validators.yaml`, the same `bridges/<bridge_name>/` tree validators already use.

```yaml
# deployment/<env>/bridges/default/warp-routes/usdc.yml
name: USDC-solana-gorchain
origin:  { chain: solana,  type: collateral, token: "<collateral SPL mint>",
           name: "USD Coin", symbol: "USDC", decimals: 6 }
remote:  { chain: gorchain, type: synthetic,
           name: "USD Coin", symbol: "USDC", decimals: 6 }
metadataUri: "<optional synthetic metadata URI>"
```

**Why route-data only (chain config stays in the spec).** RPC URLs, domain IDs, chain IDs and testnet flags are environment-level and identical across every route on that env — keeping them in the single spec is the whole point of collapsing N specs into one. The route file carries only what differs per route: topology + token. This is the split the per-route specs already imply; we are just removing the duplication.

**Why per-env files.** `origin.token` (the collateral mint) is environment-specific — mainnet USDC `EPjFW…` in prod, a testnet mint in staging, and a mint **created at test time** locally. So each env has its own copy. The local file carries a `REPLACE_WITH_USDC_MINT_ADDRESS` placeholder that the e2e patches before deploy, exactly as `warp_deployment` already patches `WARP_ORIGIN_TOKEN` today (`conftest.py:858-865`). Native routes (e.g. `sol.yml`) omit `origin.token`.

**Why YAML in, JSON in the container.** The files are YAML for human review, but the populate step converts them to JSON so the container parses them with `jq` (already used throughout `deploy.sh`) and needs no new image dependency.

### Single deployment + `WARP_ROUTES` selection

One deployment (`namespace: laconic-hyperlane-warp-deployer`) mounts the **whole** menu as a `warp-routes-config` ConfigMap at `/config/warp-routes/`. The spec carries the shared chain config, the secrets, `recreate-jobs: true` (below), and:

```yaml
config:
  WARP_ROUTES: "usdc"      # space/comma-separated route file stems to deploy
```

**Why mount the full catalog and select with `WARP_ROUTES`** rather than copying only the selected files in. The menu is the catalog of *available* routes; `WARP_ROUTES` is the *active* subset, and it lives in the checked-in spec — so the selection is reviewable in the spec diff, and turning a route on/off is a one-line spec change, not a file add/remove. This is the literal reading of "the spec reflects which configured routes we want deployed."

### `deploy.sh` loops over the selection

The one-time setup runs once; the per-route body becomes a `deploy_route <cfg.json>` function; the script loops `for r in $WARP_ROUTES` and reads each route's fields from `/config/warp-routes/$r.json` (via `jq`) instead of from `WARP_*` env vars. Everything downstream (`build_side`, the deploy invocation, synthetic-mint resolution, state writes) is unchanged — it reads the now-per-iteration `WARP_*` shell vars. The per-route `token-config.json` skip stays, so the loop is idempotent route-by-route.

### Idempotency: a `recreate-jobs` spec key in stack-orchestrator

Idempotency has two independent levels, and only one is already solved:

- **Work level (solved):** `deploy.sh` skips routes whose `token-config.json` exists (`:53-64`). Re-running does only not-yet-deployed routes.
- **Job level (the gap):** a completed k8s Job will not re-run. With N deployments this never mattered — a new route was a new deployment, hence a new Job. With **one** deployment the single Job completes once and `_create_jobs` then prints `"already exists, skipping"` (`deploy_k8s.py:869-873`) on every later `deployment start`, so a newly-selected route would never deploy.

**Decision:** add an opt-in spec key `recreate-jobs: true`. When set, `_create_jobs` deletes the existing Job (cascading to its pods) and waits for it to be gone before creating a fresh one; when unset, today's skip-on-409 is preserved. The warp spec sets it, so each `deploy-all` re-runs the warp Job, and the per-route skip keeps that cheap (finished routes are no-ops; only a newly-selected route does on-chain work).

**Why a spec key, and why opt-in** — the alternatives and why they lose:

- *Ops-side delete (no SO change):* the playbook could `kubectl delete job` before `start`. It works, and SO's own comment even says "Delete the Job explicitly to re-run" — but the recreate decision then lives in ansible, invisible to anyone reading the spec or deploying SO directly. The behavior belongs in the orchestrator.
- *Always recreate Jobs (change the default):* simplest, but it changes behavior for **every** one-shot stack (e.g. the core `svm-deployer`), which would silently start re-running on each `deployment start`. Opt-in confines the change to stacks that ask for it.
- *CLI `--force-recreate` flag:* the `up()` plumbing already exists, but a flag is imperative and must be remembered on every run; a spec key is declarative and checked in, so `deploy-all` is idempotent with no operator memory. (We can still wire the flag later for ad-hoc use; it is not required here.)

**Why one looping Job rather than one Job per route.** Per-route Jobs would give additive idempotency for free (a new route is a new Job; finished ones are untouched), but laconic-so builds Jobs from a **static** compose file — `${VAR}` substitution can fill values into existing services but cannot vary the *number* of services. Getting N Jobs would mean **generating** the compose from the menu, a new pattern for this repo. The looping Job keeps the compose essentially static (one service) and pays for it with the `recreate-jobs` re-run, which is a contained, reusable SO capability rather than a build-time codegen step.

### Scoped artifacts and checked-in logs

Each route's artifacts already live in a scoped folder (`/state/warp-routes/<name>/`). The deployer additionally writes that route's deploy log to `/state/warp-routes/<name>/deploy.log`, **overwriting** the previous one. Because `publish-bridge-state.yml` already copies the whole `generated/` tree and commits it (`:36-39`), these per-route logs are exported and checked in with **no new publish path**.

**Why overwrite-latest, not timestamped.** Logs are committed to git on every deploy; timestamped files would grow the repo without bound and produce pure-noise diffs. Latest-only keeps one diffable log per route with bounded size.

**Why a redaction guard.** `deploy.sh` does not `set -x` and writes the keypair/RPC URL to files, not stdout — but `SOLANA_RPC_URL` is a secret (its Helius endpoint embeds an API key, per `CLAUDE.md`), and a CLI/tool could print it. Since the log is committed, the per-route `tee` is filtered (`sed "s#${SOLANA_RPC_URL}#<REDACTED>#g"`) as insurance against ever pushing the key into git.

### Ops

`deploy-all.yml` replaces the per-route loop (`:49-67`) with one warp-deployer deployment. A new `ops/roles/common/tasks/load_warp_routes.yml` — the warp analogue of `load_validators.yml` — reads the selected files from the env's menu, converts each to JSON, and writes them into the deployment's `warp-routes-config` ConfigMap **before** `deployment start`. `warp_routes` in `group_vars` stays as the selection list and feeds both the populate loop and the spec's `WARP_ROUTES`. `publish-bridge-state.yml` is unchanged (route state is read by path, namespace-agnostic), and now also commits each route's `deploy.log`.

### e2e

The two per-route warp deployments collapse into one: create the test SPL mint, write the local menu files (usdc patched with the mint, sol) into the deployment's ConfigMap, deploy once with `WARP_ROUTES="usdc sol"`, wait for the single Job, capture its log once. The per-route state layout is unchanged, so `bridge_setup`, the bridge tests, and the warp-UI fixture keep reading per-route data as they do today.

## Per-environment specifics

| Env | Menu dir (`deployment_subdir`) | `origin.token` (usdc) | `WARP_ROUTES` |
|---|---|---|---|
| local | `deployment/local/bridges/default/warp-routes/` | placeholder, patched by e2e | `usdc sol` |
| staging | `deployment/staging/bridges/default/warp-routes/` | testnet USDC mint | `usdc` |
| prod | `deployment/bridges/default/warp-routes/` | mainnet USDC mint (`EPjFW…`) | `usdc` |

## Files changed

- `../stack-orchestrator/stack_orchestrator/deploy/k8s/deploy_k8s.py` — `recreate-jobs` spec key in `_create_jobs`; a `_delete_job_and_wait` helper.
- `stack_orchestrator/data/config/warp-deployer-scripts-config/deploy.sh` — setup/loop split; `deploy_route` reads per-route JSON; per-route scoped `deploy.log` with redaction.
- `stack_orchestrator/data/compose-jobs/docker-compose-hyperlane-svm-warp-deployer.yml` — drop the per-route env block, add `WARP_ROUTES`, mount `warp-routes-config`.
- `deployment/{,(staging|local)/}bridges/default/warp-routes/usdc.yml` (+ `local/.../sol.yml`) — the menu.
- `deployment/{,(staging|local)/}spec-warp-deployer.yml` — single spec per env (`spec-warp-usdc.yml` is renamed/removed; `local` already uses this name).
- `ops/playbooks/deploy-all.yml`, `ops/roles/common/tasks/load_warp_routes.yml`, `ops/inventories/*/group_vars/all.yml` — single deployment + menu population.
- `tests/e2e/conftest.py`, `tests/e2e/fixtures/test-spec-warp-deployer.yml` (replacing the two per-route fixtures).
- Docs: `CLAUDE.md` keep-in-sync table, `specs/stack-specifications.md`, the warp-deployer `README.md`.

## Keep-in-sync

The compose↔spec↔fixture trio in `CLAUDE.md` updates: one `spec-warp-deployer.yml` and one `test-spec-warp-deployer.yml`. The route menu (`bridges/default/warp-routes/`) and the new `warp-routes-config` ConfigMap are added to the warp-deployer's config-dir mapping. `WARP_ROUTES` replaces the per-route `WARP_*` keys in compose, spec, and fixture together.

## Testing / verification

- `bash -n deploy.sh`; `ruff` on conftest; `ansible-lint` (production profile) + `--syntax-check` on the warp playbook and `load_warp_routes.yml`.
- SO change: unit test with a mocked batch API (delete called, then create) where feasible; otherwise verified by the redeploy below.
- Integration (test machine): `pytest -v --skip-cleanup test_02_warp_deployer.py test_08_bridge.py` — both routes deploy via the single Job; forward+reverse bridges pass. Re-run `deploy.sh` (or re-`start`) and confirm the Job recreates and already-deployed routes skip.
- Confirm on the deploy host that `publish-bridge-state.yml` commits each route's `deploy.log` and that the committed log contains no Helius URL.

## Out of scope / limitations

- **Bridge UI multi-route** — the UI still shows the first route; runtime multi-route loading is separate.
- **Partial-failure orphaning** — the skip marker is `token-config.json`; a route interrupted after on-chain deploy but before that file is written is re-deployed on the next run, orphaning the earlier programs.
- **`FORCE_REDEPLOY` granularity** — it remains a single switch across the selected routes.
- **No other stack's Job behavior changes** — `recreate-jobs` is opt-in; the default skip-on-409 is preserved everywhere it is not set.
