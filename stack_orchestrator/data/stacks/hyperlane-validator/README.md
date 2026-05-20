# hyperlane-validator

Runs a Hyperlane validator that signs merkle tree checkpoints for a single origin chain. This is a parameterized stack -- deployed once per chain with different spec files (`spec-validator-gorchain.yml`, `spec-validator-solana.yml`).

Each deployment runs two containers: the Hyperlane validator agent and a KMS proxy sidecar that bridges AWS KMS API calls to Privy wallet signing.

## Prerequisites

- A running `k8s-kind` cluster
- `laconic-so` (stack-orchestrator) installed
- `hyperlane-minio` stack deployed (validators write checkpoints to S3)
- `hyperlane-svm-deployer` stack deployed (provides state files; consumer stack will populate `agent-config` ConfigMap before starting)

## 1. Build container

```bash
laconic-so --stack hyperlane-validator build-containers
```

Builds `gorbagana-dev/hyperlane-kms-proxy:local` (Privy-to-AWS-KMS bridge sidecar) and `gorbagana-dev/hyperlane-agent:local` (patched Hyperlane agent with `AWS_ENDPOINT_URL_KMS` support for the KMS proxy).

## 2. Deploy for Gorchain

```bash
laconic-so --stack hyperlane-validator deploy init --output validator-gorchain-spec.yml
```

Edit `validator-gorchain-spec.yml` (see `deployment/spec-validator-gorchain.yml` for reference):

```yaml
stack: stack_orchestrator/data/stacks/hyperlane-validator
deploy-to: k8s-kind
namespace: laconic-hyperlane-validator-gorchain
config:
  ORIGIN_CHAIN_NAME: gorchain
  CHECKPOINT_BUCKET: hyperlane-validator-gorchain
  PRIVY_WALLET_ID: "<wallet-id>"
configmaps:
  agent-config: ./configmaps/agent-config
secrets:
  hyperlane-validator-secrets:
    - PRIVY_APP_ID
    - PRIVY_APP_SECRET
    - AWS_ACCESS_KEY_ID
    - AWS_SECRET_ACCESS_KEY
    - HYP_DEFAULTSIGNER_KEY
```

```bash
laconic-so --stack hyperlane-validator deploy create --spec-file validator-gorchain-spec.yml --deployment-dir validator-gorchain-deployment
```

## 3. Deploy for Solana

Same process with `deployment/spec-validator-solana.yml` as reference. Key differences: `ORIGIN_CHAIN_NAME: solana`, `CHECKPOINT_BUCKET: hyperlane-validator-solana`.

```bash
laconic-so --stack hyperlane-validator deploy init --output validator-solana-spec.yml
# Edit spec, then:
laconic-so --stack hyperlane-validator deploy create --spec-file validator-solana-spec.yml --deployment-dir validator-solana-deployment
```

## 4. Populate agent-config ConfigMap

Before starting the deployment, the `agent-config` ConfigMap must be populated from the deployer's state files. In dev, this happens via `BridgeStateLoader.populate()` in the test fixture. In production, use ansible to read `/state/agent-config.json` from the deployer's generated artifacts directory and populate the ConfigMap:

```bash
# Dev: handled by test framework
# Prod: ansible reads deployment/bridges/<bridge>/generated/agent-config.json
#       and creates the ConfigMap in the validator's namespace
```

## 5. Create secrets

```bash
# Generate a chain signer key (ed25519 seed as 32-byte hex).
# This is a HOT key used only for the on-chain announce transaction.
# It is separate from the KMS-backed validator checkpoint signing key.
# For SVM chains, derive the Solana address and fund it with SOL:
#   solana-keygen new -o chain-signer.json
#   solana-keygen pubkey chain-signer.json  # fund this address
#   python3 -c "import json; print('0x' + bytes(json.load(open('chain-signer.json'))[:32]).hex())"
#   # use the hex output as HYP_DEFAULTSIGNER_KEY

kubectl create secret generic hyperlane-validator-secrets \
  --from-literal=PRIVY_APP_ID='<app-id>' \
  --from-literal=PRIVY_APP_SECRET='<app-secret>' \
  --from-literal=AWS_ACCESS_KEY_ID='<minio-access-key>' \
  --from-literal=AWS_SECRET_ACCESS_KEY='<minio-secret-key>' \
  --from-literal=HYP_DEFAULTSIGNER_KEY='0x<32-byte-hex-chain-signer-key>'
```

| Secret key | Description |
|---|---|
| `PRIVY_APP_ID` | Privy application ID for KMS proxy |
| `PRIVY_APP_SECRET` | Privy application secret |
| `AWS_ACCESS_KEY_ID` | MinIO access key for checkpoint storage |
| `AWS_SECRET_ACCESS_KEY` | MinIO secret key for checkpoint storage |
| `HYP_DEFAULTSIGNER_KEY` | Hot ed25519 hex key for announce tx (fund the derived Solana address) |

## 6. Start

```bash
laconic-so deployment --dir validator-gorchain-deployment start
laconic-so deployment --dir validator-solana-deployment start
```

## 7. Verify

```bash
# Check pods are running
kubectl get pods -l app=hyperlane-validator

# Check validator is signing checkpoints
kubectl logs -l app=hyperlane-validator -c validator --tail=50

# Check KMS proxy is healthy
kubectl logs -l app=hyperlane-validator -c kms-proxy --tail=20

# Verify agent-config ConfigMap was populated from deployer state
kubectl get configmap -A | grep agent-config
```
