# hyperlane-svm-deployer

Deploys Hyperlane core contracts (mailbox, IGP, multisig ISM, validator announce, merkle tree hook) on Gorchain and Solana SVM chains. Runs once, writes deployment artifacts to state files (`/state` host-path mount) consumed by downstream stacks (validators, relayer, warp-deployer).

## Prerequisites

- A running `k8s-kind` cluster
- `laconic-so` (stack-orchestrator) installed

## 1. Setup repositories (optional — for development only)

```bash
laconic-so --stack hyperlane-svm-deployer setup-repositories
```

Clones the gorbagana `hyperlane-monorepo` fork at `v2.2.0-gorbagana.3` to `~/cerc/hyperlane-monorepo` (the agents and scraper stay on `.1`; they don't build the on-chain programs). The `.so` programs are built with `cargo build-sbf --arch v0` (SBPFv0): Solana devnet only enables v0 for deployment (v1 and v3 are feature-gated off), and gorchain accepts v0 too, so one build deploys on both chains.

## 2. Build container (optional — for development only)

The stack uses the published image `ghcr.io/gorbagana-dev/hyperlane-svm-deployer:latest` by default. To build from source instead:

```bash
laconic-so --stack hyperlane-svm-deployer setup-repositories
laconic-so --stack hyperlane-svm-deployer build-containers
```

This builds `gorbagana-dev/hyperlane-svm-deployer:local` — a multi-stage image containing `hyperlane-sealevel-client`, compiled `.so` program artifacts, `solana-verify`, and `kubectl`.

## 3. Create deployment

```bash
laconic-so --stack hyperlane-svm-deployer deploy init --output deployer-spec.yml
```

Edit `deployer-spec.yml` (see `deployment/spec-deployer.yml` for reference):

```yaml
stack: stack_orchestrator/data/stacks/hyperlane-svm-deployer
deploy-to: k8s-kind
config:
  GORCHAIN_RPC_URL: "https://gorchain-rpc.example.com"
  SOLANA_RPC_URL: "https://solana-rpc.example.com"
  GORCHAIN_DOMAIN_ID: "99999"
  SOLANA_DOMAIN_ID: "99998"
  GORCHAIN_CHAIN_ID: "99999"
  SOLANA_CHAIN_ID: "99998"
  FORCE_REDEPLOY: "false"
secrets:
  hyperlane-deployer-secrets:
    - DEPLOYER_KEYPAIR
    - BRIDGE_OWNER_PUBKEY
    - IGP_ORACLE_PUBKEY
    - GORCHAIN_VALIDATOR_ADDRESS
    - SOLANA_VALIDATOR_ADDRESS
```

Then create the deployment directory:

```bash
laconic-so --stack hyperlane-svm-deployer deploy create --spec-file deployer-spec.yml --deployment-dir deployer-deployment
```

This applies RBAC (via `deploy/commands.py`) if needed by the deployment environment.

## 4. Create secrets

The `secrets:` section in the spec references a k8s Secret by name. SO mounts it as environment variables in the pod automatically, but **you must create the Secret yourself** before starting the deployment:

```bash
kubectl create secret generic hyperlane-deployer-secrets \
  --from-literal=DEPLOYER_KEYPAIR='[<byte array>]' \
  --from-literal=BRIDGE_OWNER_PUBKEY='<pubkey>' \
  --from-literal=IGP_ORACLE_PUBKEY='<pubkey>' \
  --from-literal=GORCHAIN_VALIDATOR_ADDRESS='<H160 address>' \
  --from-literal=SOLANA_VALIDATOR_ADDRESS='<H160 address>'
```

| Secret key | Description |
|---|---|
| `DEPLOYER_KEYPAIR` | JSON array of deployer secret key bytes (required) |
| `BRIDGE_OWNER_PUBKEY` | Pubkey to receive program upgrade authority (optional) |
| `IGP_ORACLE_PUBKEY` | Pubkey for IGP oracle account ownership (optional) |
| `GORCHAIN_VALIDATOR_ADDRESS` | H160 (Ethereum-format) address for Gorchain validator ISM (optional) |
| `SOLANA_VALIDATOR_ADDRESS` | H160 (Ethereum-format) address for Solana validator ISM (optional) |

## 5. Start deployment

```bash
laconic-so deployment --dir deployer-deployment start
```

> **Note:** The deploy script (`deploy.sh`) is ConfigMap-mounted via the
> `deployer-scripts-config` volume, not baked into the container image.
> Script changes do not require a container rebuild -- update the ConfigMap
> and restart the pod.

The pod runs `deploy.sh` which:

1. Checks idempotency — skips if `program-ids.json` file already exists at `/state/` (override with `FORCE_REDEPLOY=true`)
2. Deploys core contracts on both chains via `hyperlane-sealevel-client`
3. Verifies deployed program hashes against local `.so` files using `solana-verify`
4. Transfers program upgrade authority to `BRIDGE_OWNER_PUBKEY`
5. Transfers IGP and overhead IGP account ownership to `IGP_ORACLE_PUBKEY`
6. Configures 1-of-1 multisig ISM with validator addresses
7. Builds `agent-config.json` from deployed program IDs
8. Writes artifacts to state files at `/state/`: `program-ids.json`, `agent-config.json`, `gas-oracle-config.json`, `multisig-config.json`, `registry/metadata.yaml`
9. Shreds and deletes the deployer keypair from the pod

The container exits after completion (`restart: "no"`).

## 6. Verify

```bash
# Check pod completed successfully
kubectl get pods -l app=hyperlane-svm-deployer

# Check state files were written (dev: look in tmpdir; prod: in deployment/bridges/<bridge>/generated/)
ls -la /state/
cat /state/program-ids.json
cat /state/agent-config.json
```

## Outputs

These state files are written to `/state/` and consumed by downstream stacks (dev uses tmpdir bind-mount; prod uses ansible to populate ConfigMaps):

| State file | Consumed by |
|---|---|
| `program-ids.json` | warp-deployer, ops jobs |
| `agent-config.json` | validators, relayer |
| `gas-oracle-config.json` | gas-oracle |
| `multisig-config.json` | validators |
| `registry/metadata.yaml` | validators, relayer |
