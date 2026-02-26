# hyperlane-kms-proxy

A lightweight AWS KMS API proxy that forwards signing requests to [Privy](https://docs.privy.io/) server wallets. This allows the Hyperlane validator to use its native `type: "aws"` signer configuration with Privy-managed secp256k1 keys.

## Architecture

```
Validator        AWS KMS API       KMS Proxy       Privy API        Privy
(unmodified) ──────────────────> (port 9999) ──────────────────> (TEE)
type: "aws"                      sidecar
```

The proxy implements three AWS KMS endpoints:

| Endpoint | X-Amz-Target | Behavior |
|----------|-------------|----------|
| **Sign** | `TrentService.Sign` | Forwards digest to Privy `secp256k1_sign`, returns DER-encoded ECDSA signature |
| **GetPublicKey** | `TrentService.GetPublicKey` | Returns cached secp256k1 public key as DER SubjectPublicKeyInfo |
| **DescribeKey** | `TrentService.DescribeKey` | Returns static key metadata (`ECC_SECG_P256K1`, `SIGN_VERIFY`) |

On startup, the proxy recovers the full public key by signing a known hash and using ECDSA public key recovery (ecrecover).

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PRIVY_APP_ID` | Yes | | Privy application ID |
| `PRIVY_APP_SECRET` | Yes | | Privy application secret |
| `PRIVY_WALLET_ID` | Yes | | Privy server wallet ID (secp256k1/EVM wallet) |
| `LISTEN_ADDR` | No | `:9999` | Address to listen on |
| `PRIVY_API_URL` | No | `https://api.privy.io` | Privy API base URL |

## Validator Configuration

Configure the Hyperlane validator to use the proxy as its KMS endpoint:

```bash
# Route KMS traffic to the proxy sidecar
AWS_ENDPOINT_URL_KMS=http://localhost:9999

# Route S3 traffic to MinIO (unrelated to KMS proxy)
AWS_ENDPOINT_URL_S3=http://minio:9000
AWS_ACCESS_KEY_ID=<minio-access-key>
AWS_SECRET_ACCESS_KEY=<minio-secret-key>
```

Validator signer config:

```json
{
  "type": "aws",
  "id": "<privy-wallet-id>",
  "region": "us-east-1"
}
```

The `id` field is passed as the `KeyId` in KMS requests. The proxy ignores it for routing (there is only one wallet), but it is returned in responses for SDK compatibility.

## Build

```bash
# Local build
go build -o kms-proxy .

# Docker
docker build -t hyperlane-kms-proxy .
```

## Health Check

```
GET /health → 200 "ok"
```
