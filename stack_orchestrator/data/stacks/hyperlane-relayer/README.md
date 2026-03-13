# hyperlane-relayer

Relays cross-chain messages between Gorchain and Solana. Reads validator checkpoints from MinIO, submits delivery transactions on destination chains. Includes an `igp-fee-claim` sidecar that periodically claims accumulated IGP fees.

## Prerequisites

- A running `k8s-kind` cluster
- `laconic-so` (stack-orchestrator) installed
- `hyperlane-minio` stack deployed (relayer reads validator checkpoints from S3)
- `hyperlane-svm-deployer` stack deployed (provides `hyperlane-agent-config` ConfigMap)

## 1. Create deployment

```bash
laconic-so --stack hyperlane-relayer deploy init --output relayer-spec.yml
```

Edit `relayer-spec.yml` (see `deployment/spec-relayer.yml` for reference):

```yaml
stack: stack_orchestrator/data/stacks/hyperlane-relayer
deploy-to: k8s-kind
config:
  GORCHAIN_RPC_URL: "https://gorchain-rpc.example.com"
  SOLANA_RPC_URL: "https://solana-rpc.example.com"
  GORCHAIN_IGP_PROGRAM_ID: "<program-id>"
  SOLANA_IGP_PROGRAM_ID: "<program-id>"
  GORCHAIN_IGP_ACCOUNT: "<igp-account-address>"
  SOLANA_IGP_ACCOUNT: "<igp-account-address>"
configmaps:
  igp-fee-claim-scripts-config: ./configmaps/igp-fee-claim-scripts-config
secrets:
  hyperlane-relayer-secrets:
    - HYP_CHAINS_GORCHAIN_SIGNER_KEY
    - HYP_CHAINS_SOLANA_SIGNER_KEY
    - AWS_ACCESS_KEY_ID
    - AWS_SECRET_ACCESS_KEY
    - RELAYER_KEYPAIR_JSON
```

```bash
laconic-so --stack hyperlane-relayer deploy create --spec-file relayer-spec.yml --deployment-dir relayer-deployment
```

## 2. Create secrets

```bash
# Generate relayer chain signer keys (ed25519 seed as 32-byte hex).
# These are HOT keys used for message delivery transactions.
# For SVM chains, derive the Solana address and fund it with SOL:
#   solana-keygen new -o relayer-signer.json
#   solana-keygen pubkey relayer-signer.json  # fund this address on both chains
#   python3 -c "import json; print('0x' + bytes(json.load(open('relayer-signer.json'))[:32]).hex())"
#   # use the hex output as signer key

kubectl create secret generic hyperlane-relayer-secrets \
  --from-literal=HYP_CHAINS_GORCHAIN_SIGNER_KEY='0x<hex-key>' \
  --from-literal=HYP_CHAINS_SOLANA_SIGNER_KEY='0x<hex-key>' \
  --from-literal=AWS_ACCESS_KEY_ID='<minio-access-key>' \
  --from-literal=AWS_SECRET_ACCESS_KEY='<minio-secret-key>' \
  --from-literal=RELAYER_KEYPAIR_JSON='[<byte array>]'
```

| Secret key | Description |
|---|---|
| `HYP_CHAINS_GORCHAIN_SIGNER_KEY` | Hex private key for signing Gorchain delivery transactions |
| `HYP_CHAINS_SOLANA_SIGNER_KEY` | Hex private key for signing Solana delivery transactions |
| `AWS_ACCESS_KEY_ID` | MinIO access key for reading validator checkpoints |
| `AWS_SECRET_ACCESS_KEY` | MinIO secret key |
| `RELAYER_KEYPAIR_JSON` | Solana keypair JSON (byte array) for IGP fee claims |

## 3. Start

```bash
laconic-so deployment --dir relayer-deployment start
```

## 4. Verify

```bash
# Check pods are running
kubectl get pods -l app=hyperlane-relayer

# Check relayer is processing messages
kubectl logs -l app=hyperlane-relayer -c relayer --tail=50

# Check IGP fee claim sidecar
kubectl logs -l app=hyperlane-relayer -c igp-fee-claim --tail=20
```
