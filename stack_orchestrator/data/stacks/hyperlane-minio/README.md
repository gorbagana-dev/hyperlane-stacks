# hyperlane-minio

S3-compatible object storage for Hyperlane validator checkpoints. Runs MinIO server and an init job that creates per-chain buckets.

## 1. Create deployment

```bash
laconic-so --stack hyperlane-minio deploy init --output minio-spec.yml
```

Edit `minio-spec.yml` (see `deployment/spec-minio.yml` for reference):

```yaml
stack: stack_orchestrator/data/stacks/hyperlane-minio
deploy-to: k8s-kind
network:
  ports:
    minio:
      - "9000"
      - "9001"
volumes:
  minio-data: 10Gi
secrets:
  hyperlane-minio-secrets:
    - MINIO_ROOT_USER
    - MINIO_ROOT_PASSWORD
```

```bash
laconic-so --stack hyperlane-minio deploy create --spec-file minio-spec.yml --deployment-dir minio-deployment
```

## 2. Create secrets

```bash
kubectl create secret generic hyperlane-minio-secrets \
  --from-literal=MINIO_ROOT_USER='minioadmin' \
  --from-literal=MINIO_ROOT_PASSWORD='<strong-password>'
```

| Secret key | Description |
|---|---|
| `MINIO_ROOT_USER` | MinIO root username |
| `MINIO_ROOT_PASSWORD` | MinIO root password |

## 3. Start

```bash
laconic-so deployment --dir minio-deployment start
```

The `minio-init` sidecar creates buckets `hyperlane-validator-gorchain` and `hyperlane-validator-solana` on first start.

## 4. Verify

```bash
# Check pods are running
kubectl get pods -l app=hyperlane-minio

# Check MinIO console is accessible
kubectl port-forward svc/hyperlane-minio 9001:9001
# Open http://localhost:9001
```
