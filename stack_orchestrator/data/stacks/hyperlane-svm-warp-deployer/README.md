# hyperlane-svm-warp-deployer

Deploys warp route contracts (collateral lock + synthetic mint) across Gorchain and Solana SVM chains. A single deployment deploys the routes selected by the `WARP_ROUTES` env var, looping over them and writing each route's addresses to state files (`/state` host-path mount) consumed by the warp UI. Route definitions come from a checked-in menu, not the spec.

## Prerequisites

- A running `k8s-kind` cluster
- `laconic-so` (stack-orchestrator) installed
- `hyperlane-svm-deployer` stack deployed (needs core program IDs and agent config)

## 1. Setup repositories

```bash
laconic-so --stack hyperlane-svm-warp-deployer setup-repositories
```

Clones the deployer source — the gorbagana fork of Hyperlane at `v2.2.0-gorbagana.4` (in lockstep with the hyperlane-svm-deployer image it reuses) — to `~/cerc/hyperlane-monorepo`.

## 2. Build container

```bash
laconic-so --stack hyperlane-svm-warp-deployer build-containers
```

Reuses the `ghcr.io/gorbagana-dev/hyperlane-svm-deployer:latest` image (same as core deployer).

## 3. Create deployment

```bash
laconic-so --stack hyperlane-svm-warp-deployer deploy init --output warp-deployer-spec.yml
```

Edit `warp-deployer-spec.yml` (see `deployment/spec-warp-deployer.yml` for reference).
The spec selects routes via `WARP_ROUTES` and carries shared chain/control config;
per-route fields live in the route menu (see step 4), not the spec:

```yaml
stack: stack_orchestrator/data/stacks/hyperlane-svm-warp-deployer
deploy-to: k8s-kind
namespace: laconic-hyperlane-warp-deployer
config:
  WARP_ROUTES: "usdc"          # comma- or space-separated route stems from the menu
  GORCHAIN_RPC_URL: "https://gorchain-rpc.example.com"
  SOLANA_RPC_URL: "https://solana-rpc.example.com"
  GORCHAIN_DOMAIN_ID: "99999"
  SOLANA_DOMAIN_ID: "99998"
  GORCHAIN_CHAIN_ID: "99999"
  SOLANA_CHAIN_ID: "99998"
  GORCHAIN_IS_TESTNET: "false"
  SOLANA_IS_TESTNET: "false"
  FORCE_REDEPLOY: "false"
configmaps:
  warp-deployer-scripts-config: ./configmaps/warp-deployer-scripts-config
  warp-deployer-registry-config: ./configmaps/warp-deployer-registry-config
  warp-routes-config: ./configmaps/warp-routes-config
secrets:
  hyperlane-warp-deployer-secrets:
    - DEPLOYER_KEYPAIR
    - BRIDGE_OWNER_PUBKEY
```

Then create the deployment directory:

```bash
laconic-so --stack hyperlane-svm-warp-deployer deploy create --spec-file warp-deployer-spec.yml --deployment-dir warp-deployer-deployment
```

## 4. Populate config files

Edit the config templates in `warp-deployer-deployment/configmaps/`:

| ConfigMap directory | Contents |
|---|---|
| `warp-deployer-scripts-config/` | `deploy.sh` -- warp route deployment script |
| `warp-routes-config/` | `<stem>.json` -- one JSON file per selected route |

The `warp-routes-config` ConfigMap carries the selected routes, one `<stem>.json`
per stem in `WARP_ROUTES`. Like `agent-config`, it has no `data/config/` source
dir: the routes come from the checked-in menu under
`deployment/bridges/default/warp-routes/<stem>.yml`, rendered YAML→JSON into this
directory before start (the ops layer does this via
`ops/roles/common/tasks/load_warp_routes.yml`; e2e via conftest's
`_write_warp_menu`). Each menu file describes one route's name and origin/remote
sides (chain, type, token, name, symbol, decimals, metadataUri).

The token-config is built at runtime by `deploy.sh` from each route's menu JSON (no per-token template).

## 5. Create secrets

```bash
kubectl create secret generic hyperlane-warp-deployer-secrets \
  --from-literal=DEPLOYER_KEYPAIR='[<byte array>]' \
  --from-literal=BRIDGE_OWNER_PUBKEY='<pubkey>'
```

| Secret key | Description |
|---|---|
| `DEPLOYER_KEYPAIR` | JSON array of deployer secret key bytes (required) |
| `BRIDGE_OWNER_PUBKEY` | Pubkey to receive warp route upgrade authority (optional) |

## 6. Start deployment

```bash
laconic-so deployment --dir warp-deployer-deployment start
```

The pod runs `deploy.sh`, which loops over the routes in `WARP_ROUTES`. For each
route it reads `/config/warp-routes/<stem>.json` and:

1. Checks idempotency — skips the route if `token-config.json` already exists at `/state/warp-routes/<name>/` (override with `FORCE_REDEPLOY=true`)
2. Reads core program IDs from the `program-ids.json` state file (mounted at `/state/`)
3. Builds the token config generically from the route's menu JSON with `jq`, and renders the registry template via `envsubst`
4. Deploys the warp route programs for both sides via `hyperlane-sealevel-client`
5. Verifies deployed program hashes against local `.so` files
6. Transfers warp route program upgrade authority to `BRIDGE_OWNER_PUBKEY`
7. Writes artifacts to state files at `/state/warp-routes/<name>/`: `token-config.json`, `warp-deploy-outputs/`, and a scoped, RPC-redacted `deploy.log`

After all selected routes are processed it shreds and deletes the deployer keypair
from the pod. The container exits after completion (`restart: "no"`).

The compose service carries a `laconic.recreate-job: "true"` label, so
stack-orchestrator deletes and recreates the completed Job on `deployment start`.
Re-running the deployment is therefore idempotent: newly-selected routes deploy
while already-finished ones self-skip.

## 7. Verify

```bash
# Check pod completed successfully
kubectl get pods -l app=hyperlane-svm-warp-deployer

# Check warp route addresses in logs
kubectl logs -l app=hyperlane-svm-warp-deployer
```
