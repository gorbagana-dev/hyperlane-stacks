# CLAUDE.md — hyperlane-stacks

## Project overview

Multi-component Hyperlane SVM bridge deployment system using laconic-so
(stack-orchestrator). 8 stacks: 2 deployer Jobs, 6 long-running Pods.
Two SVM chains: **Gorchain** (custom) and **Solana**.

## Repository layout

```
stack_orchestrator/data/
  stacks/              # Stack definitions (stack.yml per stack)
  compose/             # Compose files for Pods
  compose-jobs/        # Compose files for Jobs
  config/              # ConfigMap source dirs (scripts, templates, static config)
  container-build/     # Dockerfiles

deployment/            # Production deployment specs + ops playbooks
  spec-*.yml           # One per stack, user-facing config entrypoint
  ops/                 # Ansible playbooks + shell scripts

tests/e2e/             # Python + pytest E2E tests
  fixtures/            # Kind config, test specs, k8s manifests
  lib/                 # Shared test utilities (cluster, chain, deploy, keygen)

docs/                  # Architecture decisions, ops decisions, security, gaps; stack/e2e/ansible specs

hyperlane-gas-oracle/  # Node.js gas oracle service
hyperlane-kms-proxy/   # Go KMS proxy sidecar
```

## Keep in sync — CRITICAL

These groups of files must stay consistent. When changing one, update all:

### 1. Compose ↔ Deployment specs ↔ Test fixtures

| Compose file | Deployment spec | Test fixture |
|---|---|---|
| `compose-jobs/docker-compose-hyperlane-svm-deployer.yml` | `deployment/spec-deployer.yml` | `tests/e2e/fixtures/test-spec-deployer.yml` |
| `compose-jobs/docker-compose-hyperlane-svm-warp-deployer.yml` | `deployment/spec-warp-deployer.yml` (local: `deployment/local/spec-warp-deployer.yml`) | `tests/e2e/fixtures/test-spec-warp-deployer.yml` |
| `compose/docker-compose-hyperlane-validator.yml` | `deployment/spec-validator-{gorchain,solana}.yml` | — |
| `compose/docker-compose-hyperlane-relayer.yml` | `deployment/spec-relayer.yml` | — |
| `compose/docker-compose-hyperlane-gas-oracle.yml` | `deployment/spec-gas-oracle.yml` | — |
| `compose/docker-compose-hyperlane-monitoring.yml` | `deployment/spec-monitoring.yml` | — |
| `compose/docker-compose-hyperlane-warp-ui.yml` | `deployment/spec-warp-ui.yml` | — |
| `compose/docker-compose-hyperlane-minio.yml` | `deployment/spec-minio.yml` | — |

When you add/remove/rename an env var or configmap in a compose file:
- Update the corresponding `deployment/spec-*.yml`
- Update the corresponding `tests/e2e/fixtures/test-spec-*.yml` (if it exists)
- Update the test code in `tests/e2e/test_*.py` if affected
- Update the stack's entry in the `stack_env_vars` map in
  `ops/inventories/*/group_vars/all.yml` — the deploy-side ansible layer assembles
  each stack's `laconic-so` env from that map (see `ops/README.md`)

### 2. Config dirs ↔ Compose volumes ↔ Stack definitions

Each configmap referenced in a compose `volumes:` section must have:
- A source directory under `stack_orchestrator/data/config/`
- A matching entry in the stack's `stack.yml` (under `configmaps:` or volumes)
- A matching entry in each deployment spec and test fixture that uses it

### 3. Deploy scripts ↔ Compose env vars

The scripts in `config/*-scripts-config/` consume env vars injected by compose.
If a script references `${SOME_VAR}`, the compose file must pass it through
in its `environment:` block (and the spec must include it in `config:`).

### 4. Documentation

When making structural changes, update:
- `docs/stack-specifications.md` — detailed per-stack specs
- `docs/e2e-test-spec.md` — E2E test plan and infrastructure
- `docs/architecture-decisions.md` — if architectural patterns change

## Config patterns

### Environment variables

- **No nested defaults in k8s**: Compose `${VAR:-${OTHER}}` doesn't work in
  SO's k8s path. All vars must be set explicitly in spec `config:`.
- **Chain-specific vars** are canonical: `GORCHAIN_RPC_URL`, `SOLANA_RPC_URL`,
  `GORCHAIN_DOMAIN_ID`, `SOLANA_DOMAIN_ID`, `GORCHAIN_CHAIN_ID`, `SOLANA_CHAIN_ID`
- **Derived vars** (e.g. `COLLATERAL_CHAIN_RPC_URL`) use compose-level defaults:
  `${COLLATERAL_CHAIN_RPC_URL:-${SOLANA_RPC_URL}}` — users only set chain-specific vars
- Deployment specs should use chain-specific vars, not derived vars
- **`SOLANA_RPC_URL` is a secret** (the Helius URL embeds an API key): it lives
  under each spec's `secrets:` (`{ env: SOLANA_RPC_URL }`), not `config:`. SO
  injects it as a pod env var either way; only its provenance differs.
- **Generated state is secret-free**: `agent-config.json` and the published
  `registry/metadata.yaml` carry the placeholder `http://rpc-placeholder.invalid`
  instead of real RPC URLs. Agents get real URLs via
  `HYP_CHAINS_<CHAIN>_CUSTOMRPCURLS` env overrides — gorchain from compose
  `${GORCHAIN_RPC_URL:-placeholder}`, solana via each spec's `secrets:` key
  `{ env: SOLANA_RPC_URL }` (never via compose: a missing var renders as empty
  string, which both crashes the agent's URL parser and shadows the envFrom
  secret). `publish-bridge-state.yml` secret-scans `generated/` before staging.
- **Domain/chain IDs are committed per-env spec literals** under `config:`.
  SO writes `config:` values verbatim — it does not expand `${VAR}` in `config:`.

### Templates (envsubst)

- Template files use `.tmpl` extension and `${VAR}` placeholder syntax
- Deploy scripts render them at runtime via `envsubst`
- Examples: `metadata.yaml.tmpl`
- ConfigMaps flatten directories; scripts reconstruct needed dir structures at runtime
  (e.g. `mkdir -p /tmp/registry/chains && envsubst < .tmpl > .../chains/metadata.yaml`)

### Secrets

- Created by operator via `kubectl create secret generic`
- Referenced in spec files under `secrets:`, injected as env vars
- Never committed to the repo

## Deployment order

1. `hyperlane-minio` (no deps)
2. `hyperlane-svm-deployer` (Job — writes state files to `/state` host-path)
3. `hyperlane-svm-warp-deployer` (Job — deploys the routes selected by `WARP_ROUTES` from the checked-in menu; reads `program-ids.json` from `/state`, writes per-route `token-config.json`)
4. `hyperlane-validator` × 2 (gorchain + solana, mounts agent-config CM populated by bridge_state_loader)
5. `hyperlane-relayer` (mounts agent-config CM, needs MinIO via cross-namespace FQDN)
6. `hyperlane-gas-oracle` (env vars populated from state files via conftest)
7. `hyperlane-monitoring` (anytime)
8. `hyperlane-warp-ui` (env vars populated from state files via conftest)

State flow: deployer Jobs write JSON files to `/state` (host-path via Kind `extraMounts`).
Before each consumer's `deployment start`, `BridgeStateLoader.populate(stack, deploy_dir)`
copies the relevant state files into `{deploy_dir}/configmaps/<cm-name>/`. SO then creates
those as k8s ConfigMaps in each consumer's own namespace. See
`docs/superpowers/specs/2026-05-20-bridge-state-extract-and-distribution-design.md`.

## E2E tests

- Python 3.10+, pytest, ruff linter (see `pyproject.toml`)
- Deployer Jobs use `_wait_for_job_complete()`, not pod-phase waiting
- Test chains run on host: gorchain (:8899), solana (:18899)
- In-cluster DNS: `gorchain-rpc:8899`, `solana-rpc:18899`
- Test specs use `REPLACE_AT_RUNTIME` placeholders patched by test code
- SPL token lifecycle: create-token → create-account → mint

## Stack orchestrator conventions

- Image names: `gorbagana-dev/hyperlane-*` (registry: `ghcr.io/gorbagana-dev/`)
- `deploy/commands.py` in stack dirs: post-create hooks (RBAC, services)
- `stack.yml` `jobs:` for one-shot deployers, `pods:` for long-running services
- **Check SO source code** (`../stack-orchestrator/`) for deployment lifecycle questions.
  Don't guess how laconic-so works — read `stack_orchestrator/deploy/` source.
- **ConfigMap lifecycle** (3 places must agree):
  1. **Compose file** — RO named volumes with "config" in the name become ConfigMaps.
     `deploy init` auto-discovers these and writes `configmaps:` to the spec.
  2. **Spec file** — `configmaps:` maps volume names to paths (`./configmaps/{name}`).
     `deploy create` reads spec keys, finds source dirs via
     `resolve_config_dir(stack, key)` → `data/config/{key}/`, copies to
     `{deploy_dir}/configmaps/{key}/`.
  3. **`data/config/{volume-name}/`** — source directory for stack-internal configmaps.
     For deployer-output configmaps (e.g. `agent-config`), `BridgeStateLoader.populate()`
     fills `{deploy_dir}/configmaps/{name}/` from state files instead — no source dir
     under `data/config/`.
  At `deploy start`, SO reads files from `{deploy_dir}/configmaps/{name}/`,
  creates k8s ConfigMap objects in the stack's namespace, and mounts them into pods/jobs.
  SO's ConfigMap creation is now idempotent (patches on 409 instead of failing).
- **Warp routes are config-driven**: a single warp-deployer deployment deploys
  the routes named in the comma- or space-separated `WARP_ROUTES` spec var. Route
  definitions are a checked-in menu under `bridges/default/warp-routes/<stem>.yml`
  (prod) / `local/bridges/default/warp-routes/<stem>.yml` (local), one route per
  file. The selected routes are carried in the `warp-routes-config` ConfigMap
  (mounted at `/config/warp-routes/`) as `<stem>.json` — runtime-populated like
  `agent-config` (no `data/config/` source dir): ops renders the deployment menu
  YAML→JSON (`ops/roles/common/tasks/load_warp_routes.yml`); e2e renders its own
  menu under `tests/e2e/fixtures/warp-routes/<stem>.yml` via conftest's
  `_write_warp_menu`. `deploy.sh` loops `WARP_ROUTES`, deploys each route, and
  writes a scoped per-route `deploy.log` under `/state/warp-routes/<name>/`;
  already-deployed routes self-skip unless `FORCE_REDEPLOY=true`. The warp-deployer
  compose service carries a `laconic.recreate-job: "true"` label so SO
  deletes+recreates the completed Job on `deployment start`, making re-runs
  idempotent (newly-selected routes deploy, finished ones skip). The spec's
  `WARP_ROUTES` is the single source of truth for route selection — ops derives
  the list from it (no separate `warp_routes` ansible var).
  The relayer's `HYP_WHITELIST` is built by `warp-deployer-scripts-config/build-relayer-whitelist.sh`
  from the deployed routes' program addresses (written to `relayer-whitelist.json` in state,
  like `warpRoutes.yaml`), injected by conftest (e2e) and `publish-bridge-state.yml` (prod);
  an empty `[]` would relay everything, so the default is a deny-all sentinel.
- **cluster-id lifecycle**: `deploy create` generates `laconic-{id}` in
  `deployment.yml`. `deploy start` uses it as kube context `kind-{cluster-id}`.
  Patch `deployment.yml` (not the spec) after create.
- **`--skip-cluster-management`**: Now the default. SO does not create or
  destroy Kind clusters on `start`/`stop` unless `--perform-cluster-management`
  is passed explicitly.
- **`image-pull-secret:`** (formerly `registry-credentials:`): Spec key for
  private registry auth. The old name is silently ignored by current SO.
- **Namespace derivation**: SO derives the k8s namespace from the stack name
  (`laconic-{stack_name}`), not the cluster-id. Specs with an explicit
  `namespace:` key override this.
- **`external-services:`**: Declares external endpoints. Three modes:
  `host:` (ExternalName/DNS), `ip:` (headless Service + static IP Endpoints),
  `selector:` (headless Service + pod IP discovery). Test specs use `ip:` mode
  with `REPLACE_HOST_IP` placeholder for the Kind gateway.
- **`image-overrides:`**: Override container images at the spec level.
  Keys are compose service names, values are full image refs. CLI `--image`
  flags take precedence. All prod specs have commented examples.
- **Ops commands**: `deployment update` is now `deployment update-envs`.
  `deployment prepare` combines init + create. `deployment restart --image`
  swaps a container image without full stop/start.

## Git workflow

- Never amend pushed commits — create new commits instead
- Separate commits by concern (spec updates, stack fixes, tests, docs)

## Issue tracking (pebbles)

This repo tracks work as **pebbles** via the `pb` CLI (github.com/Martian-Engineering/pebbles).
Use it as part of the normal workflow — file pebbles for follow-ups, deferred fixes,
and findings instead of leaving them only in prose or memory.

- **Data:** `.pebbles/events.jsonl` (append-only source of truth, committed) +
  `.pebbles/pebbles.db` (SQLite cache, gitignored). Issue id prefix is **`hyp-`**.
- **Read before starting work:** `pb list`, `pb show <id>`, `pb dep tree <id>`
  (use `NO_COLOR=1` for clean output). Check for an existing pebble before filing a new one.
- **File / update:** `pb create --title … --type task|bug|epic --priority P0-P4 --description …`;
  `pb update <id> --status in_progress|closed`; `pb dep add <child> <parent> --type parent-child`.
  Keep descriptions self-contained (file:line evidence, fix direction) — they are the context
  a future agent picks the work up from.
- **Commit** `.pebbles/` changes by concern like any other change (pebbles.db stays ignored).
  Don't `pb sync` (it makes its own commits); stage `.pebbles/` into a normal commit instead.
