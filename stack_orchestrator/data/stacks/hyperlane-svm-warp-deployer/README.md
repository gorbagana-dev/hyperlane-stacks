# hyperlane-svm-warp-deployer

Deploys warp route contracts (collateral lock + synthetic mint) for a specific token pair across Gorchain and Solana SVM chains. Runs once per token pair, writes warp route addresses to k8s ConfigMaps consumed by the warp UI.

## Prerequisites

- A running `k8s-kind` cluster
- `laconic-so` (stack-orchestrator) installed
- `hyperlane-svm-deployer` stack deployed (needs core program IDs and agent config)

## 1. Setup repositories

```bash
laconic-so --stack hyperlane-svm-warp-deployer setup-repositories
```

Clones `hyperlane-monorepo` at `@hyperlane-xyz/core@10.2.0` (`16c056a09`) to `~/cerc/hyperlane-monorepo`.

## 2. Build container

```bash
laconic-so --stack hyperlane-svm-warp-deployer build-containers
```

Reuses the `ghcr.io/gorbagana-dev/hyperlane-svm-deployer:latest` image (same as core deployer).

## 3. Create deployment

```bash
laconic-so --stack hyperlane-svm-warp-deployer deploy init --output warp-deployer-spec.yml
```

Edit `warp-deployer-spec.yml` (see `deployment/spec-warp-deployer.yml` for reference):

```yaml
stack: stack_orchestrator/data/stacks/hyperlane-svm-warp-deployer
deploy-to: k8s-kind
config:
  WARP_TOKEN_MINT: "REPLACE_WITH_TOKEN_MINT_ADDRESS"
  COLLATERAL_CHAIN: gorchain
  SYNTHETIC_CHAIN: solana
  COLLATERAL_CHAIN_RPC_URL: "https://gorchain-rpc.example.com"
  SYNTHETIC_CHAIN_RPC_URL: "https://solana-rpc.example.com"
  COLLATERAL_DOMAIN_ID: "99999"
  SYNTHETIC_DOMAIN_ID: "99998"
  FORCE_REDEPLOY: "false"
configmaps:
  warp-deployer-scripts-config: ./configmaps/warp-deployer-scripts-config
  warp-deployer-token-config: ./configmaps/warp-deployer-token-config
secrets:
  hyperlane-warp-deployer-secrets:
    - DEPLOYER_KEYPAIR
    - HARDWARE_WALLET_PUBKEY
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
| `warp-deployer-token-config/` | Token metadata (name, symbol, decimals, mint address) |

## 5. Create secrets

```bash
kubectl create secret generic hyperlane-warp-deployer-secrets \
  --from-literal=DEPLOYER_KEYPAIR='[<byte array>]' \
  --from-literal=HARDWARE_WALLET_PUBKEY='<pubkey>'
```

| Secret key | Description |
|---|---|
| `DEPLOYER_KEYPAIR` | JSON array of deployer secret key bytes (required) |
| `HARDWARE_WALLET_PUBKEY` | Pubkey to receive warp route upgrade authority (optional) |

## 6. Start deployment

```bash
laconic-so deployment --dir warp-deployer-deployment start
```

The pod runs `deploy.sh` which:

1. Checks idempotency — skips if `hyperlane-token-config` ConfigMap already exists (override with `FORCE_REDEPLOY=true`)
2. Reads core program IDs from the `hyperlane-program-ids` ConfigMap
3. Renders token config and registry templates via `envsubst`
4. Deploys warp route programs (collateral + synthetic) via `hyperlane-sealevel-client`
5. Verifies deployed program hashes against local `.so` files
6. Transfers warp route program upgrade authority to `HARDWARE_WALLET_PUBKEY`
7. Writes artifacts to k8s ConfigMaps: `hyperlane-token-config`, `hyperlane-warp-deploy-outputs`
8. Shreds and deletes the deployer keypair from the pod

The container exits after completion (`restart: "no"`).

## 7. Verify

```bash
# Check pod completed successfully
kubectl get pods -l app=hyperlane-svm-warp-deployer

# Check warp route addresses in logs
kubectl logs -l app=hyperlane-svm-warp-deployer
```
