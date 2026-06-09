# Hyperlane SVM Bridge — Stack-Orchestrator Specifications

## How Stack-Orchestrator Works

Stack-orchestrator (`laconic-so`) deploys containerized applications via three backends: `compose` (Docker Compose), `k8s` (external Kubernetes), and `k8s-kind` (local Kind cluster). We target `k8s-kind`.

### Key Concepts

**stack.yml** defines a deployable unit:
- `repos:` — Git repositories cloned by `laconic-so setup-repositories` to `~/cerc/`. Used as build context for container images. Format: `host/org/repo@branch`. The `build.sh` scripts reference cloned source via `~/cerc/{repo-name}/`.
- `containers:` — Image names built by `laconic-so build-containers`. Each name maps to a `container-build/{name-with-slashes-replaced-by-hyphens}/build.sh` script. Also determines which images get loaded into Kind via `kind load docker-image`.
- `pods:` — List of pod names, each mapping to `compose/docker-compose-{pod-name}.yml`. All services in all pods become containers in ONE k8s Deployment (single Pod).
- `jobs:` — List of job names, each mapping to `compose-jobs/docker-compose-{job-name}.yml`. Jobs become k8s Jobs (restartPolicy: Never, backoffLimit: 0) instead of Deployments — appropriate for one-shot containers that should not restart on completion or failure.

**spec.yml** controls deployment configuration:
- `stack:` — Reference to the stack (path or name)
- `deploy-to:` — `k8s-kind`, `k8s`, or `compose`
- `config:` — Environment variables injected into all containers
- `volumes:` — Named volumes with explicit host paths → HostPath PVs under `/srv/kind/hyperlane/`; empty value → dynamic PVC (avoid for data that must survive cluster recreation)
- `configmaps:` — Directory → k8s ConfigMap, mounted as volume
- `network.http-proxy:` — Ingress routing rules
- `network.ports:` — NodePort mappings
- `security:` — privileged, capabilities, unlimited-memlock
- `resources:` — CPU/memory limits
- `image-pull-secret:` — Private registry auth

**deploy/commands.py** hooks:
- `init()` — Returns default spec.yml content (called during `deploy init`)
- `create()` — Post-create hook with access to `DeploymentContext` (called during `deploy create`)

### k8s-kind Deployment Flow

1. `laconic-so --stack <name> setup-repositories` — Clones repos listed in stack.yml to `~/cerc/`
2. `laconic-so --stack <name> build-containers` — Builds images listed in stack.yml `containers:` field
3. `laconic-so --stack <name> deploy init --output spec.yml` — Creates spec.yml template
4. `laconic-so deploy create --spec-file spec.yml --deployment-dir <dir>` — Creates deployment directory
5. `laconic-so deployment --dir <dir> start` — Creates Kind cluster, loads images, generates k8s manifests from compose files, deploys

### Critical Constraint

**All services across all pods in a stack become containers in a single k8s Pod.** Services needing localhost communication (e.g., validator + KMS proxy sidecar) MUST be in the same stack. Services communicating over the network SHOULD be separate stacks.

---

## Stack Decomposition

SO's constraint that **all services in a stack = one k8s Pod** means services needing independent restart, scaling, or lifecycle must be separate stacks. This drives the 8-stack decomposition below.

## Stack Inventory

| # | Stack | Type | Services | Purpose |
|---|-------|------|----------|---------|
| 1 | `hyperlane-svm-deployer` | One-time | deployer | Deploy core Hyperlane contracts on both chains |
| 2 | `hyperlane-svm-warp-deployer` | One-time | warp-deployer | Deploy warp route contracts (collateral + synthetic) |
| 3 | `hyperlane-validator` | Long-running | validator + kms-proxy | Checkpoint signing via Privy KMS proxy (one deployment per chain) |
| 4 | `hyperlane-relayer` | Long-running | relayer + igp-fee-claim | Message delivery + periodic IGP fee claiming |
| 5 | `hyperlane-minio` | Long-running | minio + minio-init | S3-compatible checkpoint storage |
| 6 | `hyperlane-gas-oracle` | Long-running | gas-oracle | Periodic IGP gas oracle updates via Privy |
| 7 | `hyperlane-monitoring` | Long-running | prometheus + pushgateway + grafana + balance-monitor | Metrics, alerting, dashboards |
| 8 | `hyperlane-warp-ui` | Long-running | warp-ui | Browser-based bridge UI |
| - | `ops/` | On-demand | kubectl Jobs | Kill switch, restore, teardown, ownership verify |

### stack.yml Summary

| # | Stack | repos: | containers: | pods: | jobs: |
|---|-------|--------|-------------|-------|-------|
| 1 | `hyperlane-svm-deployer` | `github.com/hyperlane-xyz/hyperlane-monorepo@16c056a` | `gorbagana-dev/hyperlane-svm-deployer` | — | `hyperlane-svm-deployer` |
| 2 | `hyperlane-svm-warp-deployer` | `github.com/hyperlane-xyz/hyperlane-monorepo@16c056a` | `gorbagana-dev/hyperlane-svm-deployer` | — | `hyperlane-svm-warp-deployer` |
| 3 | `hyperlane-validator` | `github.com/gorbagana-dev/hyperlane-stacks` | `gorbagana-dev/hyperlane-kms-proxy` | `hyperlane-validator` | — |
| 4 | `hyperlane-relayer` | `github.com/hyperlane-xyz/hyperlane-monorepo@16c056a` | `gorbagana-dev/hyperlane-svm-deployer` | `hyperlane-relayer` | — |
| 5 | `hyperlane-minio` | *(none)* | *(none)* | `hyperlane-minio` | — |
| 6 | `hyperlane-gas-oracle` | `github.com/gorbagana-dev/hyperlane-stacks` | `gorbagana-dev/hyperlane-gas-oracle` | `hyperlane-gas-oracle` | — |
| 7 | `hyperlane-monitoring` | *(none)* | *(none)* | `hyperlane-monitoring` | — |
| 8 | `hyperlane-warp-ui` | `github.com/hyperlane-xyz/hyperlane-warp-ui-template` | `gorbagana-dev/hyperlane-warp-ui` | `hyperlane-warp-ui` | — |

- Stacks 1 and 2 use `jobs:` (not `pods:`) because deployers are one-shot containers — k8s Jobs (restartPolicy: Never, backoffLimit: 0) prevent CrashLoopBackOff that occurs when Deployments restart completed containers. Their compose files live in `compose-jobs/` instead of `compose/`.
- Stacks 5 and 7 use only upstream images — no repos or containers needed.
- Stacks 1, 2, and 4 share the same repo/container (deployer image) and are independently buildable.
- Stack 7 balance-monitor will use a lightweight image (not the heavy deployer image).
- The deployer image is built from `@hyperlane-xyz/core@10.2.0` (commit `16c056a09af862b3ce9e14bd3b5b8034750af9d0`), not the older `agents-v2.0.0` tag.

---

## Stack 1: hyperlane-svm-deployer

### Purpose
One-time Job that deploys Hyperlane core contracts (Mailbox, IGP, ISM, Validator Announce, Merkle Tree Hook) on both Gorchain (domain 99999) and Solana (domain 99998).

### How It Works
1. Stack uses `jobs:` in stack.yml — runs as a k8s Job (restartPolicy: Never, backoffLimit: 0) instead of a Deployment, preventing CrashLoopBackOff after completion
2. Deploy script (`deploy.sh`) is mounted via ConfigMap volume at `/opt/scripts/` rather than baked into the Docker image — allows script updates without rebuilding the image
3. Deploys programs on both chains via `hyperlane-sealevel-client` (from `@hyperlane-xyz/core@10.2.0`)
4. Verifies on-chain program hashes via `solana-verify`
5. Transfers ownership: program authority → hardware wallet (uses `--skip-new-upgrade-authority-signer-check` flag required by Solana CLI 3.x), IGP → Privy oracle
6. Writes deployment artifacts as k8s ConfigMaps via kubectl (requires RBAC)
7. Discards hot deployer key

### Services
| Service | Image | Notes |
|---------|-------|-------|
| deployer | `gorbagana-dev/hyperlane-svm-deployer:local` | `restart: "no"`, mounts deploy script + config templates |

### ConfigMaps (input)
- `deployer-scripts-config` — deploy.sh entrypoint
- `deployer-gas-oracle-config` — initial gas oracle configs
- `deployer-multisig-config` — per-chain multisig ISM configs (`.json.tmpl` templates rendered via envsubst)
- `deployer-registry-config` — chain registry metadata.yaml template

### ConfigMaps (output, created by deployer via kubectl)
- `hyperlane-program-ids` — deployed program addresses per chain
- `hyperlane-agent-config` — agent-config.json for validators/relayer
- `hyperlane-gas-oracle-config` — gas oracle configs
- `hyperlane-multisig-config` — multisig ISM configs

### deploy/commands.py
`create()` applies RBAC (Role + RoleBinding) granting the default ServiceAccount permission to create/update ConfigMaps in the deployment namespace.

### Config (spec.yml)
`GORCHAIN_RPC_URL`, `SOLANA_RPC_URL`, `GORCHAIN_DOMAIN_ID` (99999), `SOLANA_DOMAIN_ID` (99998), `FORCE_REDEPLOY`

### Secrets (injected separately)
`DEPLOYER_KEYPAIR`, `HARDWARE_WALLET_PUBKEY`, `IGP_ORACLE_PUBKEY`

---

## Stack 2: hyperlane-svm-warp-deployer

### Purpose
One-time Job that deploys the warp route contracts for the routes selected by `WARP_ROUTES`. A route is a set of fields where each side has an explicit token type: an origin side (`native` or `collateral`) and a remote side (`synthetic` or `collateral`). Route definitions are a checked-in menu; a single deployment can deploy several of them.

### How It Works
1. Same `jobs:` pattern as core deployer — runs as a k8s Job with ConfigMap-mounted deploy script at `/opt/scripts/`
2. Reads `hyperlane-program-ids` ConfigMap (created by Stack 1) for mailbox addresses
3. `deploy.sh` loops over the comma- or space-separated `WARP_ROUTES` selection. For each route it reads `/config/warp-routes/<stem>.json` (from the `warp-routes-config` ConfigMap) and builds the on-chain token-config generically with `jq` from that route's fields (origin/remote chain, type, token, and metadata) — there is no per-token template
4. Deploys the warp route programs for both sides and writes each route's addresses to state under `/state/warp-routes/<name>/`

### Warp route token model

A `collateral-and-synthetic` route spans two chains, and each chain has two
distinct on-chain objects: the Hyperlane **token-router program** and the SPL
**mint** it operates on. That 2×2 is the whole model — the four warp variables
the downstream warp-UI consumes are one per cell:

| | Token-router program | SPL mint |
|---|---|---|
| **Collateral side** (Solana) | `WARP_COLLATERAL_ADDRESS` | `WARP_TOKEN_MINT` |
| **Synthetic side** (gorchain) | `WARP_SYNTHETIC_ADDRESS` | `WARP_SYNTHETIC_MINT` |

- **`WARP_COLLATERAL_ADDRESS`** — the warp program on Solana that escrows real
  USDC. Created by the warp deploy; read from
  `warp-deploy-outputs/program-ids.json` (`<collateral-chain>.base58`).
- **`WARP_SYNTHETIC_ADDRESS`** — the warp program on gorchain that mints/burns
  gUSDC. Created by the warp deploy; `<synthetic-chain>.base58` from the same file.
- **`WARP_TOKEN_MINT`** — the SPL mint of the **collateral** token = the real
  Solana USDC mint. It **pre-exists** (Circle's mint); the operator supplies it
  as the deployer input `WARP_TOKEN_MINT`, and the deployer echoes it back out as
  `token-config.json` → `warpRoute.tokenMint`. Named from the deployer's "token
  to bridge" perspective; conceptually it is the collateral-side mint.
- **`WARP_SYNTHETIC_MINT`** — the SPL mint of the **synthetic** token = the gUSDC
  mint on gorchain. It is **created** by the warp deploy (that is what "synthetic"
  means — the wrapped token is minted into existence). Its address is the PDA
  `find_program_address(["hyperlane_token","-","mint"], WARP_SYNTHETIC_ADDRESS)`,
  so it is fully determined by the synthetic program.

Pre-exists vs minted is exactly what makes this route `collateral-and-synthetic`
rather than `collateral-collateral`; it holds only because gorchain has no native
canonical USDC.

**Why the synthetic mint must be emitted, not left blank.** Although the mint is a
deterministic PDA, the warp-UI's Hyperlane SDK (`@hyperlane-xyz/sdk`) does **not**
auto-derive it: `Token.getHypAdapter`'s `SealevelHypSynthetic` branch asserts
`collateralAddressOrDenom` is present and feeds it to the adapter as the mint used
for balance/ATA lookups — an empty value throws and the gorchain side of the UI
fails to construct. So the deployer resolves it explicitly: after deploy it runs
`hyperlane-sealevel-client token query --program-id <synthetic-program> synthetic`,
parses the `Mint / Mint Authority:` line, and writes it to `token-config.json` →
`warpRoute.synthetic.mint`. The deployer then aggregates all route configs into
`/state/warp-routes/warpRoutes.yaml` (a Hyperlane `WarpCoreConfig`), which the ops
layer distributes into the `warp-ui-config` ConfigMap for the warp-UI to serve
directly. (The e2e suite reads this file from state via `bridge_state_loader` and
asserts it in `test_02_warp_deployer.py`, and `test_10_warp_ui.py` verifies it is
served by the warp-UI container.)

### Services
| Service | Image | Notes |
|---------|-------|-------|
| warp-deployer | `gorbagana-dev/hyperlane-svm-deployer:local` | Same deployer image, different entrypoint script |

### Dependencies
- Requires Stack 1 (core deployer) to have run first

### Config (spec.yml)
The spec selects routes and carries shared chain/control config; the per-route
fields live in the menu, not the spec.

- **`WARP_ROUTES`** — comma- or space-separated list of route stems to deploy (e.g. `"usdc"`
  in prod, `"usdc sol"` locally/e2e). Each stem must have a menu file (below) and
  must match the `warp_routes` selection in
  `ops/inventories/{prod,local}/group_vars/all.yml`.
- Shared chain config (`GORCHAIN_RPC_URL`, domain/chain IDs, `*_IS_TESTNET`) and
  `FORCE_REDEPLOY`. The Solana origin RPC (`SOLANA_RPC_URL`) is a secret.
- `configmaps:` includes `warp-routes-config`, the runtime-populated ConfigMap
  that carries the selected routes (see below).

#### Route menu
Each route is defined in a checked-in per-env menu file. The deployment menus —
`deployment/bridges/default/warp-routes/<stem>.yml` (prod) /
`deployment/local/bridges/default/warp-routes/<stem>.yml` (local) — ship the
operator routes; the e2e suite owns a parallel menu under
`tests/e2e/fixtures/warp-routes/<stem>.yml`. A menu file describes one route: its
`name`, the origin and remote sides (`chain`/`type`/`token`/`name`/`symbol`/
`decimals`, each side may label or scale the asset independently), and the
synthetic-side `metadataUri`. Deployment menus ship `usdc`; the e2e menu adds
`sol` (a native-route test vehicle).

#### warp-routes-config ConfigMap
The selected routes are carried into the deployer as the `warp-routes-config`
ConfigMap (mounted at `/config/warp-routes/`), one `<stem>.json` per selected
route. Like `agent-config`, it is runtime-populated and has no `data/config/`
source dir: the ops layer renders the deployment menu YAML→JSON
(`ops/roles/common/tasks/load_warp_routes.yml`), and e2e renders its own fixtures
menu the same way in conftest's `_write_warp_menu`. `deploy.sh` reads `/config/warp-routes/<stem>.json`
for each selected route.

### Secrets (injected separately)
`DEPLOYER_KEYPAIR`, `HARDWARE_WALLET_PUBKEY`

### Multiple routes

A single deployment deploys all routes named in `WARP_ROUTES`. To add a route,
check a menu file into `bridges/default/warp-routes/`, add its stem to the
spec's `WARP_ROUTES`, and re-run the deployment (ops derives its render list from
`WARP_ROUTES`). Each route keeps its state under `/state/warp-routes/<name>/`
(where `<name>` is the menu file's `name:` field), so routes do not collide and
the deploy script's idempotency check is scoped per route: an already-deployed
route self-skips (its `token-config.json` exists) unless `FORCE_REDEPLOY=true`.

Each route also gets a scoped, RPC-redacted `deploy.log` under
`/state/warp-routes/<name>/`, which rides the existing `publish-bridge-state.yml`
flow into git alongside the other bridge state.

After all selected routes are deployed, `deploy.sh` invokes
`build-warp-ui-config.sh`, which aggregates every route's `token-config.json` into
a single Hyperlane `WarpCoreConfig` and writes it to
`/state/warp-routes/warpRoutes.yaml`. The ops layer distributes this file into the
`warp-ui-config` ConfigMap (via `state_distribute`) so the warp-UI container can
serve it directly at startup — no separate build step is needed.

#### Idempotent re-runs
The warp-deployer compose service carries a `laconic.recreate-job: "true"`
label. On `deployment start`, stack-orchestrator deletes and recreates the
completed Job rather than treating it as already-applied, so re-running the
deployment picks up newly-selected routes while finished routes skip via the
per-route idempotency check.

The relayer, gas-oracle, validators, and storage are route-agnostic — they
operate at the mailbox/domain level — so adding a route needs no per-route
changes to those stacks.

---

## Stack 3: hyperlane-validator

### Purpose
Runs a Hyperlane validator for a single chain. Signs checkpoints using secp256k1/ECDSA via a Privy KMS proxy sidecar. **One deployment is created per chain** — a Gorchain validator deployment and a Solana validator deployment both use this same stack definition with different spec.yml configs.

### How It Works
1. Validator container runs unmodified hyperlane-agent with `type: "aws"` signer config
2. KMS proxy sidecar intercepts AWS KMS API calls on localhost:9999, forwards to Privy
3. Validator writes checkpoints to MinIO (separate stack) via S3 API
4. Both containers share a Pod (required for localhost:9999 communication)

### Compose: `docker-compose-hyperlane-validator.yml`

Single parameterized compose file. All chain-specific values come from env vars (set via spec.yml config per deployment).

| Service | Image | Notes |
|---------|-------|-------|
| agent-config-init | `alpine/kubectl:1.35.3` | Init container — fetches `hyperlane-agent-config` ConfigMap to shared PVC |
| validator | `ghcr.io/gorbagana-dev/hyperlane-agent:latest` | CLI args from env vars, metrics on :9090 |
| kms-proxy | `ghcr.io/gorbagana-dev/hyperlane-kms-proxy:latest` | Port 9999, proxies KMS Sign/GetPublicKey/DescribeKey to Privy |

Validator command uses env vars for all chain-specific args:
- `--origin-chain-name ${ORIGIN_CHAIN_NAME}`
- `--checkpointSyncer.bucket ${CHECKPOINT_BUCKET}`
- `--validator.id ${PRIVY_WALLET_ID}`

MinIO endpoint uses the static hostname `hyperlane-minio` — a k8s Service with this name is created by `deploy/commands.py` in stacks that need MinIO access (see Cross-Stack Communication).

### Config (spec.yml)
- `ORIGIN_CHAIN_NAME` — chain name (e.g., `gorchain` or `solana`), set per deployment
- `CHECKPOINT_BUCKET` — S3 bucket name, set per deployment
- `PRIVY_WALLET_ID` — Privy wallet ID (varies per chain deployment)

### Secrets (injected separately)
- `PRIVY_APP_ID`, `PRIVY_APP_SECRET` — Privy API credentials for KMS proxy
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` — MinIO credentials for checkpoint storage
- `HYP_DEFAULTSIGNER_KEY` — ed25519 hex key for on-chain announce tx (hot key, separate from KMS validator key)

### Compose Environment (hardcoded in compose, not in spec)
- `AWS_ENDPOINT_URL_KMS=http://localhost:9999` — routes to sidecar
- `AWS_ENDPOINT_URL_S3=http://hyperlane-minio:9000` — routes to MinIO k8s Service
- `CONFIG_FILES=/config/agent-config.json`

### ConfigMaps
- `agent-config` — agent-config.json (created by deployer, or template)

### Per-Chain Deployment
Two separate spec.yml files deploy this stack for each chain:
- `spec-validator-gorchain.yml` — sets `ORIGIN_CHAIN_NAME=gorchain`, `PRIVY_WALLET_ID=<gorchain-wallet>`, `CHECKPOINT_BUCKET=hyperlane-validator-gorchain`
- `spec-validator-solana.yml` — sets `ORIGIN_CHAIN_NAME=solana`, `PRIVY_WALLET_ID=<solana-wallet>`, `CHECKPOINT_BUCKET=hyperlane-validator-solana`

Each deployment gets its own PVC for validator data and its own k8s Deployment.

### Cross-Stack Dependencies
- MinIO stack must be running (S3 for checkpoints)
- Deployer stack must have run (agent-config.json)

---

## Stack 4: hyperlane-relayer

### Purpose
Delivers cross-chain messages between Gorchain and Solana. Includes an IGP fee claim sidecar.

### Compose: `docker-compose-hyperlane-relayer.yml`

| Service | Image | Notes |
|---------|-------|-------|
| agent-config-init | `alpine/kubectl:1.35.3` | Init container — fetches `hyperlane-agent-config` ConfigMap to shared PVC |
| relayer | `ghcr.io/gorbagana-dev/hyperlane-agent:latest` | `relayer` subcommand, gas enforcement `none`, metrics on :9091 |
| igp-fee-claim | `ghcr.io/gorbagana-dev/hyperlane-svm-deployer:latest` | Runs `claim-fees.sh` from ConfigMap, loops every 6h |

### IGP Fee Claim Sidecar
Script at `stack_orchestrator/data/config/igp-fee-claim-scripts-config/claim-fees.sh`. Mounted as ConfigMap volume. Claims accumulated IGP fees on both chains using the relayer key for tx fees (permissionless operation).

### Config (spec.yml)
- `GORCHAIN_RPC_URL`, `SOLANA_RPC_URL`
- `GORCHAIN_IGP_PROGRAM_ID`, `SOLANA_IGP_PROGRAM_ID` — for igp-fee-claim sidecar
- `GORCHAIN_IGP_ACCOUNT`, `SOLANA_IGP_ACCOUNT` — IGP account addresses for fee claims

### Secrets (injected separately)
- `HYP_CHAINS_GORCHAIN_SIGNER_KEY` — hex ed25519 key for Gorchain delivery txs
- `HYP_CHAINS_SOLANA_SIGNER_KEY` — hex ed25519 key for Solana delivery txs
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` — MinIO credentials for reading checkpoints
- `RELAYER_KEYPAIR_JSON` — Solana keypair JSON (byte array) for igp-fee-claim

### Compose Environment (hardcoded in compose)
- `HYP_GASPAYMENTENFORCEMENT='[{"type": "none"}]'` — disabled (Sealevel returns hardcoded zeros)
- `AWS_ENDPOINT_URL_S3=http://hyperlane-minio:9000`

---

## Stack 5: hyperlane-minio

### Purpose
S3-compatible storage for validator checkpoints. Replaces shared PVCs (RWX) with S3 API access (RWO-friendly).

### Services
| Service | Image | Notes |
|---------|-------|-------|
| minio | `minio/minio:RELEASE.2025-09-07T16-13-09Z` | `server /data`, ports 9000 (S3) + 9001 (console) |
| minio-init | `minio/mc:RELEASE.2025-08-13T08-35-41Z` | `restart: "no"`, creates validator buckets then exits |

### Buckets Created
- `hyperlane-validator-gorchain`
- `hyperlane-validator-solana`

---

## Stack 6: hyperlane-gas-oracle

### Purpose
Periodically fetches token prices and updates IGP gas oracle configurations on both chains.

### How It Works
1. Fetches sGOR and SOL prices from CoinGecko (or configurable price endpoint)
2. Converts sGOR price to gGOR (Gorchain native token) via configurable multiplier
3. Computes exchange rates using `@hyperlane-xyz/sdk` `getLocalStorageGasOracleConfig()` (1e19 Sealevel scale, with margin)
4. Builds `SetGasOracleConfigs` instructions using SDK Borsh serialization
5. Signs via Privy server wallet (production) or local keypair (testing)
6. Runs in a loop (`RUN_LOOP=true`) with configurable interval (default 15 min)

### Services
| Service | Image | Notes |
|---------|-------|-------|
| gas-oracle | `ghcr.io/gorbagana-dev/hyperlane-gas-oracle:latest` | TypeScript, loop mode via `RUN_LOOP=true` |

### Config (spec.yml)
`GORCHAIN_RPC_URL`, `SOLANA_RPC_URL`, `GORCHAIN_IGP_PROGRAM_ID`, `SOLANA_IGP_PROGRAM_ID`, `GORCHAIN_DOMAIN_ID`, `SOLANA_DOMAIN_ID`, `GAS_ORACLE_INTERVAL_MS` (default 900000), `GAS_PRICE` (default "0.000000001"), `GAS_OVERHEAD` (default 200000), `EXCHANGE_RATE_MARGIN_PCT` (default 10), `MIN_USD_COST` (default "0.50"), `GORCHAIN_NATIVE_TOKEN_MULTIPLIER` (default 100), `SIGNER_MODE` ("privy" or "keypair")

### Secrets (injected separately)
Privy mode: `PRIVY_APP_ID`, `PRIVY_APP_SECRET`, `PRIVY_ORACLE_WALLET_ID`
Keypair mode: `ORACLE_KEYPAIR`

---

## Stack 7: hyperlane-monitoring

### Purpose
Prometheus metrics collection, alerting, Grafana dashboards, and wallet balance monitoring.

### Compose: `docker-compose-hyperlane-monitoring.yml`

| Service | Image | Notes |
|---------|-------|-------|
| prometheus | `prom/prometheus:latest` | 30d retention, k8s service discovery |
| pushgateway | `prom/pushgateway:latest` | Receives pushed metrics from balance monitor |
| grafana | `grafana/grafana:latest` | Auto-provisioned datasource + dashboards |
| balance-monitor | TBD (lightweight image with solana-cli + curl) | Runs `check-balance.sh` from ConfigMap, pushes to pushgateway at localhost:9091 |

### Balance Monitor
Script at `stack_orchestrator/data/config/balance-monitor-scripts-config/check-balance.sh`. Mounted as ConfigMap volume. Checks wallet balances on both chains and pushes metrics to pushgateway.

### ConfigMaps
- `prometheus-config` — prometheus.yml (with `kubernetes_sd_configs` for cross-pod scraping), alerts.yml
- `grafana-datasources-config` — Prometheus datasource
- `grafana-dashboard-config` — Dashboard provisioning config
- `grafana-dashboards` — hyperlane-overview.json

### deploy/commands.py
`create()` applies RBAC (ClusterRole + ClusterRoleBinding) for Prometheus k8s service discovery.

### Config (spec.yml)
`GORCHAIN_RPC_URL`, `SOLANA_RPC_URL`, `MONITORED_WALLETS_GORCHAIN`, `MONITORED_WALLETS_SOLANA`, `BALANCE_THRESHOLD_SOL`, `BALANCE_CHECK_INTERVAL`

### Secrets (injected separately)
`GRAFANA_ADMIN_PASSWORD`

### Prometheus Scrape Targets
Uses `kubernetes_sd_configs` with pod annotation relabeling (`prometheus.io/scrape: "true"`) to discover validators/relayer in separate pods. Validator/relayer compose files should include prometheus annotations.

---

## Stack 8: hyperlane-warp-ui

### Purpose
Browser-based bridge UI (Next.js) for cross-chain token transfers.

### Services
| Service | Image | Notes |
|---------|-------|-------|
| warp-ui | `gorbagana-dev/hyperlane-warp-ui:local` | Port 3000, sentinel placeholder substitution at startup |

### Ingress
HTTP proxy routes host → warp-ui:3000 via nginx ingress controller with automatic ACME TLS.

---

## Ops Directory (Not a Stack)

On-demand k8s Jobs applied manually with `kubectl`. These are standalone Job manifests rather than SO-managed stacks.

| Job | Purpose | Signing |
|-----|---------|---------|
| kill-switch-job.yaml | Scale agents to 0, reconfigure ISM to null | Unsigned tx output → Ledger |
| restore-job.yaml | Restore ISM validators, scale agents up | Unsigned tx output → Ledger |
| teardown-job.yaml | Close programs, recover rent (DRY_RUN default) | Unsigned tx output → Ledger |
| verify-ownership-job.yaml | Read-only ownership verification | None |

---

## Container Builds

See `docs/architecture-decisions.md` § "Build Strategy" for full image build details (Dockerfile stages, version pinning, sentinel patterns) and § "Version Pinning" for the complete image registry table.

All container build artifacts (Dockerfile, entrypoint scripts, config files) live in `stack_orchestrator/data/container-build/{name}/`. There are no image source directories at the repo root — everything is consolidated into the SO directory structure.

Summary of custom images and their SO build pipeline:

| Container Name | Build Dir | repos: (cloned to ~/cerc/) | Source |
|---------------|-----------|---------------------------|--------|
| `gorbagana-dev/hyperlane-svm-deployer` | `gorbagana-dev-hyperlane-svm-deployer` | `github.com/hyperlane-xyz/hyperlane-monorepo@16c056a` | Multi-stage Rust build of `hyperlane-sealevel-client` + `.so` programs + `solana-verify`. Solana CLI 3.0.14. |
| `gorbagana-dev/hyperlane-kms-proxy` | `gorbagana-dev-hyperlane-kms-proxy` | `github.com/gorbagana-dev/hyperlane-stacks` | Go service, source at `hyperlane-kms-proxy/` |
| `gorbagana-dev/hyperlane-gas-oracle` | `gorbagana-dev-hyperlane-gas-oracle` | `github.com/gorbagana-dev/hyperlane-stacks` | TypeScript, source at `hyperlane-gas-oracle/`, uses `@hyperlane-xyz/sdk` |
| `gorbagana-dev/hyperlane-warp-ui` | `gorbagana-dev-hyperlane-warp-ui` | `github.com/hyperlane-xyz/hyperlane-warp-ui-template` | Next.js with sentinel placeholders, runtime sed substitution |

Each `build.sh` sources `build-base.sh` and runs `docker build` using the SO-cloned repo in `~/cerc/` as build context. Build scripts must NOT use relative paths back to the repo tree — components may move to different repos.

### Build Dir Contents

Each container-build dir is self-contained with its Dockerfile and any supporting files (entrypoint scripts, config templates):

```
container-build/gorbagana-dev-hyperlane-svm-deployer/
  build.sh          # runs: docker build -f Dockerfile -t ...:local ~/cerc/hyperlane-monorepo
  Dockerfile        # COPY from ~/cerc/hyperlane-monorepo (no internal git clone)
  entrypoint.sh     # copied into image at build time

container-build/gorbagana-dev-hyperlane-warp-ui/
  build.sh          # runs: docker build -f Dockerfile -t ...:local ~/cerc/hyperlane-warp-ui-template
  Dockerfile        # COPY from ~/cerc/hyperlane-warp-ui-template (no internal git clone)
  entrypoint.sh     # sentinel substitution + Next.js start
  configs/          # chains.yaml, warpRoutes.yaml, .env.sentinel — placeholder configs

container-build/gorbagana-dev-hyperlane-kms-proxy/
  build.sh          # runs: docker build -t ...:local ~/cerc/hyperlane-stacks/hyperlane-kms-proxy

container-build/gorbagana-dev-hyperlane-gas-oracle/
  build.sh          # runs: docker build -t ...:local ~/cerc/hyperlane-stacks/hyperlane-gas-oracle
```

Dockerfiles for deployer and warp-ui use `~/cerc/` as build context (COPY from the SO-cloned repo), not internal `git clone`. The Dockerfile and entrypoint.sh are passed via `-f` flag from the build dir.

---

## Deployment Order

```
1. hyperlane-minio                    (no dependencies)
2. hyperlane-svm-deployer             (creates ConfigMaps consumed by all agent stacks)
3. hyperlane-svm-warp-deployer        (needs program-ids from step 2)
4. hyperlane-validator (gorchain)  }
   hyperlane-validator (solana)    }  parallel, need agent-config from step 2
   hyperlane-relayer               }    + MinIO from step 1
5. hyperlane-gas-oracle               (needs IGP program IDs from step 2)
6. hyperlane-monitoring               (can run anytime, discovers pods dynamically)
7. hyperlane-warp-ui                  (needs warp route addresses from step 3)
```

Steps 4's two validator deployments use the same `hyperlane-validator` stack with different spec.yml files.

---

## spec.yml Conventions

- **`stack:`** — Full relative path: `stack_orchestrator/data/stacks/hyperlane-*`
- **`deploy-to:`** — `k8s-kind` for all stacks
- **`config:`** — Non-secret environment variables only. Secrets (keys, passwords) are NOT included in spec files — they are injected separately (e.g., via k8s Secrets or at deploy time).
- **`configmaps:`** — Relative to deployment dir: `./configmaps/{name}`. Operator populates these directories after `deploy create`.
- **`volumes:`** — Data volumes map to explicit host paths under `/srv/kind/hyperlane/<stack>/` (see layout below). This makes data survive cluster recreation. Do not leave data volume values empty — that produces a dynamic PVC which is lost on cluster delete.
- **`network.ports:`** — Exposed service ports (NodePort mappings)
- **`network.http-proxy:`** — Ingress routing (warp-ui only)

### Volume Sizes

| Volume | Size | Used By |
|--------|------|---------|
| validator data | 5Gi | Stack 3 (per chain) |
| relayer data | 5Gi | Stack 4 |
| minio data | 10Gi | Stack 5 |
| prometheus data | 10Gi | Stack 7 |
| grafana data | 2Gi | Stack 7 |

### Host-Path Layout

All stacks share `kind-mount-root: /srv/kind/hyperlane`. Kind mounts this directory from the host into the cluster node; every data volume is a named subdir:

```
/srv/kind/hyperlane/
  bridge/
    generated/          ← svm-deployer output (program-ids.json, etc.)
    logs/               ← deployer job logs
  minio/
    data/               ← MinIO object store
  validator-gorchain/
    data/               ← gorchain validator state
  validator-solana/
    data/               ← solana validator state
  relayer/
    data/               ← relayer state
  monitoring/
    prometheus/         ← Prometheus TSDB
    grafana/            ← Grafana database
```

Tests use the same layout under `/tmp/hyperlane-bridge-e2e/`.

### Spec Files

| Spec File | Stack |
|-----------|-------|
| `spec-deployer.yml` | `hyperlane-svm-deployer` |
| `spec-warp-deployer.yml` (local: `local/spec-warp-deployer.yml`) | `hyperlane-svm-warp-deployer` |
| `spec-validator-gorchain.yml` | `hyperlane-validator` (gorchain deployment) |
| `spec-validator-solana.yml` | `hyperlane-validator` (solana deployment) |
| `spec-relayer.yml` | `hyperlane-relayer` |
| `spec-minio.yml` | `hyperlane-minio` |
| `spec-gas-oracle.yml` | `hyperlane-gas-oracle` |
| `spec-monitoring.yml` | `hyperlane-monitoring` |
| `spec-warp-ui.yml` | `hyperlane-warp-ui` |

Both validator spec files reference the same stack (`stack_orchestrator/data/stacks/hyperlane-validator`) with different config values for `ORIGIN_CHAIN_NAME`, `CHECKPOINT_BUCKET`, `PRIVY_WALLET_ID`, etc.

## Compose File Conventions

- **Directory layout**: Long-running services use `compose/docker-compose-{pod-name}.yml` (referenced by `pods:` in stack.yml). One-shot deployers use `compose-jobs/docker-compose-{job-name}.yml` (referenced by `jobs:` in stack.yml).
- **Image tags**: Use `gorbagana-dev/name:local` (what `build-containers` produces). Add comment noting future published version: `# TODO: use ghcr.io/gorbagana-dev/name:tag once CI publish workflows are set up`
- **Inline scripts**: No multi-line inline scripts in compose files. Extract to shell scripts in `stack_orchestrator/data/config/{name}-scripts-config/` dirs. Mount as ConfigMap volumes at `/opt/scripts/`. Reference via `command: ["/bin/bash", "/opt/scripts/script.sh"]`.
- **Environment variables**: All deployment-specific values come from env vars (set via spec.yml config). Compose files use `${VAR}` syntax.
- **Volumes**: Named volumes with `config` in the name → ConfigMaps in k8s. Other named volumes → host-path PVs (path set in the spec file under `volumes:`).

## Cross-Stack Communication

All stacks deploy to the same k8s-kind cluster.

| From | To | Mechanism |
|------|----|-----------|
| Validators → MinIO | `http://hyperlane-minio:9000` | k8s Service created by `deploy/commands.py` |
| Relayer → MinIO | `http://hyperlane-minio:9000` | Same k8s Service |
| Validator → KMS proxy | `localhost:9999` | Same pod (must be same stack) |
| Balance monitor → Pushgateway | `localhost:9091` | Same pod (monitoring stack) |
| Prometheus → Agents | k8s service discovery | `kubernetes_sd_configs` with annotations |
| Deployer → k8s API | kubectl in-pod | RBAC grants ConfigMap create/update |
| Grafana → Prometheus | `localhost:9090` | Same pod (monitoring stack) |

### MinIO Service Discovery

SO-generated service names follow the pattern `{deployment-id}-service`, so compose hostnames don't resolve across stacks. To allow static `hyperlane-minio:9000` references in compose files, the `deploy/commands.py` hook in stacks that access MinIO (validator, relayer) creates a k8s Service named `hyperlane-minio` pointing to the MinIO deployment's pod. This decouples compose files from deployment IDs.
