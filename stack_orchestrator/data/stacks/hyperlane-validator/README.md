# hyperlane-validator

Runs a Hyperlane validator that signs merkle tree checkpoints for a single origin chain. This is a parameterized stack -- deployed once per chain with different spec files (`spec-validator-gorchain.yml`, `spec-validator-solana.yml`).

Each deployment runs two containers: the Hyperlane validator agent and a KMS proxy sidecar that bridges AWS KMS API calls to Privy wallet signing.

## Prerequisites

- A running `k8s-kind` cluster
- `laconic-so` (stack-orchestrator) installed
- `hyperlane-minio` stack deployed (validators write checkpoints to S3)
- `hyperlane-svm-deployer` stack deployed (provides `hyperlane-agent-config` ConfigMap)

## 1. Build container

```bash
laconic-so --stack hyperlane-validator build-containers
```

Builds `laconic/hyperlane-kms-proxy:local` -- the Privy-to-AWS-KMS bridge sidecar. The validator image (`gcr.io/abacus-labs-dev/hyperlane-agent:agents-v2.0.0`) is pulled from upstream.

## 2. Deploy for Gorchain

```bash
laconic-so --stack hyperlane-validator deploy init --output validator-gorchain-spec.yml
```

Edit `validator-gorchain-spec.yml` (see `deployment/spec-validator-gorchain.yml` for reference):

```yaml
stack: stack_orchestrator/data/stacks/hyperlane-validator
deploy-to: k8s-kind
config:
  ORIGIN_CHAIN_NAME: gorchain
  CHECKPOINT_BUCKET: hyperlane-validator-gorchain
configmaps:
  agent-config: ./configmaps/agent-config
secrets:
  hyperlane-validator-secrets:
    - PRIVY_APP_ID
    - PRIVY_APP_SECRET
    - PRIVY_WALLET_ID
    - AWS_ACCESS_KEY_ID
    - AWS_SECRET_ACCESS_KEY
```

```bash
laconic-so --stack hyperlane-validator deploy create --spec-file validator-gorchain-spec.yml --deployment-dir validator-gorchain-deployment
```

## 3. Deploy for Solana

Same process with `deployment/spec-validator-solana.yml` as reference. Key differences: `ORIGIN_CHAIN_NAME: solanatestnet`, `CHECKPOINT_BUCKET: hyperlane-validator-solana`.

```bash
laconic-so --stack hyperlane-validator deploy init --output validator-solana-spec.yml
# Edit spec, then:
laconic-so --stack hyperlane-validator deploy create --spec-file validator-solana-spec.yml --deployment-dir validator-solana-deployment
```

## 4. Create secrets

```bash
kubectl create secret generic hyperlane-validator-secrets \
  --from-literal=PRIVY_APP_ID='<app-id>' \
  --from-literal=PRIVY_APP_SECRET='<app-secret>' \
  --from-literal=PRIVY_WALLET_ID='<wallet-id>' \
  --from-literal=AWS_ACCESS_KEY_ID='<minio-access-key>' \
  --from-literal=AWS_SECRET_ACCESS_KEY='<minio-secret-key>'
```

| Secret key | Description |
|---|---|
| `PRIVY_APP_ID` | Privy application ID for KMS proxy |
| `PRIVY_APP_SECRET` | Privy application secret |
| `PRIVY_WALLET_ID` | Privy wallet ID used for validator signing |
| `AWS_ACCESS_KEY_ID` | MinIO access key for checkpoint storage |
| `AWS_SECRET_ACCESS_KEY` | MinIO secret key for checkpoint storage |

## 5. Start

```bash
laconic-so deployment --dir validator-gorchain-deployment start
laconic-so deployment --dir validator-solana-deployment start
```

## 6. Verify

```bash
# Check pods are running
kubectl get pods -l app=hyperlane-validator

# Check validator is signing checkpoints
kubectl logs -l app=hyperlane-validator -c validator --tail=50

# Check KMS proxy is healthy
kubectl logs -l app=hyperlane-validator -c kms-proxy --tail=20
```
