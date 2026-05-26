# MinIO external-services and TLS via Caddy

**Date:** 2026-05-26
**Status:** Approved — PR1 of a two-PR MinIO migration. PR2 (per-validator
users + bucket-prefix policies) follows once this lands.

---

## 1. Goal

Cut MinIO consumers (both validators, relayer) over from the hardcoded
cross-namespace FQDN to the `external-services:` mechanism, and route the
data path through Caddy with TLS in both dev and prod.

This is the work originally described as "PR2" in the 2026-05-20
bridge-state-extract design, adapted to the Caddy-ingress / cluster-management
architecture that landed in PR #15.

## 2. Background

### What's in place

PR #12 (bridge-state-extract) introduced per-stack namespaces and added a
temporary env var to the validator and relayer compose files:

```
AWS_ENDPOINT_URL_S3=http://hyperlane-minio.laconic-hyperlane-minio.svc.cluster.local:9000
```

This kept S3 traffic working across the new per-stack namespaces but only
on a single-host Kind cluster — the FQDN is unreachable when validators
and MinIO sit on different hosts, which is exactly the topology prod uses.

PR #15 (cluster-management + http-proxy) moved all TLS handling to Caddy
via the `http-proxy:` spec key and explicitly removed cert-manager. The
2026-05-20 design's plan for "cert-manager + self-signed ClusterIssuer
for MinIO TLS in dev" no longer fits the architecture; this design
replaces it.

### What we verified empirically

On 2026-05-26 we ran a two-container compose (Caddy v2 + MinIO) and
exercised a signed S3 v4 conversation through Caddy with `mc`: bucket
create, object PUT, list, GET. All succeeded. A Host-header rewrite
would have produced `SignatureDoesNotMatch` on the first signed PUT — it
did not, so Caddy v2's `reverse_proxy` directive (which is the primitive
`caddyserver/ingress` emits for every `http-proxy:` route) preserves the
client's Host header upstream by default. No Caddy or SO changes are
needed to make MinIO behind Caddy work.

We also confirmed the agent image already forces path-style addressing
via `stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-agent/s3-path-style.patch`,
which sets `force_path_style(true)` and resolves the endpoint from the
`AWS_ENDPOINT_URL_S3` env var. No fork changes needed for this PR.

## 3. Architecture

```
       validator-gorchain / validator-solana / relayer pod
                          │
                          │  AWS_ENDPOINT_URL_S3=http://hyperlane-minio:80         (dev)
                          │  AWS_ENDPOINT_URL_S3=https://s3.bridge.gorbagana.wtf:443  (prod)
                          ▼
       ┌─────────────────────────────────────────────────┐
       │ Hostname resolution:                            │
       │  - dev:  Service "hyperlane-minio" in consumer  │
       │          NS, created by external-services entry │
       │          (ip: REPLACE_HOST_IP, port: 80)        │
       │  - prod: cluster DNS forwards to upstream and   │
       │          resolves public hostname natively      │
       └─────────────────────────────────────────────────┘
                          │
                          ▼
              Caddy ingress (terminates TLS in prod; passes HTTP in dev)
                          │  host-name matches an http-proxy entry
                          ▼  on the minio stack
              reverse_proxy minio:9000   (preserves Host)
                          │
                          ▼
                       MinIO  (S3 API, path-style)
```

The Host header reaching Caddy in each environment matches a `host-name:`
configured on the minio stack:

| Env  | Endpoint URL                              | Caddy host-name on minio stack    |
|------|-------------------------------------------|-----------------------------------|
| dev  | `http://hyperlane-minio:80`               | `hyperlane-minio`                 |
| prod | `https://s3.bridge.gorbagana.wtf:443`     | `s3.bridge.gorbagana.wtf`         |

## 4. Key decisions

### 4.1 Caddy fronts MinIO in both dev and prod

Single TLS story across the bridge. Validators and relayer reach MinIO
only through Caddy; nothing dials port 9000 directly.

### 4.2 `external-services:` in dev only, public DNS in prod

The validator/relayer pods need to resolve the hostname in the endpoint
URL. In dev, the hostname is the in-cluster Service name `hyperlane-minio`
and must be created via an `external-services:` entry in each consumer's
namespace, pointing at the Kind gateway IP (mode `ip: REPLACE_HOST_IP,
port: 80` — the same pattern the chain RPCs already use). In prod the
hostname is a real DNS name (`s3.bridge.gorbagana.wtf`); cluster DNS
forwards unknown names to upstream resolvers by default and resolves it
without any explicit Service object.

The dev fixture's `external-services:` block carries an inline comment
explaining the asymmetry, so anyone reading the prod spec doesn't wonder
where the matching entry went.

### 4.3 HTTP in dev, HTTPS in prod — TLS only on the prod leg

We considered terminating TLS at Caddy in dev as well (matching prod's
HTTPS path byte-for-byte), so the dev fixture would distribute the
mkcert root CA into each consumer pod via a `minio-ca-config` ConfigMap
and set `AWS_CA_BUNDLE` on the validator/relayer. Investigation against
the pinned SDK versions (aws-config 1.1.7, aws-sdk-s3 1.65.0,
aws-smithy-runtime 1.8.1) found that **`AWS_CA_BUNDLE` is not honored
by aws-sdk-rust** — open feature request awslabs/aws-sdk-rust#1362,
no implementation in the SDK source. Setting the env var in dev would
silently do nothing; the mkcert handshake would fail; validators would
never reach MinIO.

To make HTTPS work in dev we would need either (a) a third fork patch
that wires `AWS_CA_BUNDLE` into a custom HTTP connector, or (b) an init
container that installs the mkcert root into the system trust store
*plus* a fork patch to switch aws-sdk-rust from `webpki-roots` to
`rustls-native-certs`. Both add ongoing fork-maintenance burden in
exchange for parity on a TLS leg that's already exercised by other
stacks' Caddy-fronted tests (warp-ui, monitoring, validator HTTP).

Instead this PR uses **HTTP between consumers and Caddy in dev**.
Caddy is configured to listen on port 80 for the in-cluster S3 hostname;
the validator/relayer hit `http://hyperlane-minio:80`. Production still
uses `https://s3.bridge.gorbagana.wtf:443` — Let's Encrypt certs are in
the SDK's built-in `webpki-roots` so prod needs no CA configuration at
all. The dev/prod difference is exactly the URL scheme; both still
exercise the Caddy reverse-proxy code path.

### 4.4 The cross-stack ClusterIP Service is removed

`stack_orchestrator/data/stacks/hyperlane-minio/deploy/commands.py`
currently creates a `hyperlane-minio` Service in the minio namespace as
a stable short-name target — a workaround for cross-namespace reachability
that was needed before consumers had their own `external-services:`
entries. With this PR, each consumer creates the name in its own namespace,
so the cross-stack Service is no longer needed and is deleted.

### 4.5 Endpoint URL is spec-driven, not compose-hardcoded

The current compose files set a literal `AWS_ENDPOINT_URL_S3=...`. After
this PR, both the validator and relayer compose pass it through as
`AWS_ENDPOINT_URL_S3: ${AWS_ENDPOINT_URL_S3}`, sourced from each spec's
`config:` block. This is what lets dev and prod use different URLs from
the same compose file.

## 5. Files affected

### Stack definitions and compose

- `stack_orchestrator/data/compose/docker-compose-hyperlane-validator.yml`
  - Replace literal `AWS_ENDPOINT_URL_S3` with spec-driven passthrough.
- `stack_orchestrator/data/compose/docker-compose-hyperlane-relayer.yml`
  - Same change as validator.
- `stack_orchestrator/data/stacks/hyperlane-minio/deploy/commands.py`
  - Remove the cross-stack ClusterIP Service creation.

### Production specs

- `deployment/spec-minio.yml`
  - Extend `http-proxy:` with a second entry: `host-name:
    s3.bridge.gorbagana.wtf` → `minio:9000`.
- `deployment/spec-validator-gorchain.yml`, `spec-validator-solana.yml`,
  `spec-relayer.yml`
  - Add `config:` entry `AWS_ENDPOINT_URL_S3:
    https://s3.bridge.gorbagana.wtf:443`.
  - Do not add an `external-services:` block (public DNS handles it).

### Test fixtures

- `tests/e2e/fixtures/test-spec-minio.yml`
  - Add `http-proxy:` block with `host-name: hyperlane-minio` →
    `minio:9000` and `host-name: minio-console.test` → `minio:9001`.
- `tests/e2e/fixtures/test-spec-validator-gorchain.yml`,
  `test-spec-validator-solana.yml`, `test-spec-relayer.yml`
  - Add `external-services:` entry `hyperlane-minio: { ip:
    REPLACE_HOST_IP, port: 80 }` with comment: "dev only — prod uses
    public DNS to resolve `s3.bridge.gorbagana.wtf`".
  - Add `config:` entry `AWS_ENDPOINT_URL_S3: http://hyperlane-minio:80`.

### Test code

- `tests/e2e/lib/cluster.py`
  - `hyperlane-minio` may stay in `TEST_HOSTNAMES` (mkcert SAN — unused
    in dev now but cheap to keep for future HTTPS-in-dev work).

## 6. Verification

PR1 is green when:

- `test_03_minio.py`, `test_04_validator.py`, `test_05_relayer.py`,
  `test_08_bridge.py` all pass.
- `test_relayer_checkpoint_syncer_connected` (which already asserts no
  `ConnectError` / `NoSuchBucket` / `InvalidAccessKeyId` /
  `SignatureDoesNotMatch` in relayer logs) confirms the Caddy-fronted
  S3 path works end-to-end in dev (HTTP) — and by extension the prod
  HTTPS path, which differs only in URL scheme and the (default,
  webpki-roots-backed) cert verification.
- The cross-stack ClusterIP Service is no longer present in the
  `laconic-hyperlane-minio` namespace.
- The current FQDN `AWS_ENDPOINT_URL_S3` literal does not appear in any
  compose file.

## 7. Out of scope (deferred to PR2)

- Per-validator MinIO users and bucket-prefix policies.
- `deployment/bridges/<name>/operator/minio-users.yaml` schema.
- Extended MinIO init Job for `mc admin policy/user/attach`.
- Per-validator MinIO credential values (will use the
  `secrets.<name>.keys` mechanism from PR #15).

## 8. Alternatives considered

### HTTPS between validator and Caddy in dev via `AWS_CA_BUNDLE`

The original draft of this spec proposed distributing the mkcert root
CA via a `minio-ca-config` ConfigMap and setting `AWS_CA_BUNDLE` on the
consumer pods so the SDK would trust the mkcert-signed Caddy cert.
Investigation against our pinned SDK versions (aws-config 1.1.7,
aws-sdk-rust 1.65.0) found `AWS_CA_BUNDLE` is **not honored at all** —
awslabs/aws-sdk-rust#1362 is the open, unimplemented request for it.
Setting the env var would silently do nothing. The plumbing was
implemented and then rolled back; the current §4.3 documents the HTTP
fallback we landed on instead.

### Fork-patch aws-sdk-rust to honor `AWS_CA_BUNDLE`

Would give dev byte-for-byte HTTPS parity with prod. Rejected because it
adds a third long-lived patch (alongside `s3-path-style.patch` and
`kms-endpoint.patch`) that we'd carry until either upstream merges
#1362 or we drop the fork. The dev/prod URL-scheme split is a much
smaller maintenance cost.

### `rustls-native-certs` + init container that installs mkcert root

Would also work — switch the SDK from `webpki-roots` to system-cert
mode and bake the mkcert root into the container's trust store at
deploy time. Two moving parts (fork patch + init container) instead of
one URL scheme. Rejected for the same maintenance reason.

### `external-services:` in prod too (ExternalName → public DNS)

Symmetric spec shape between environments. Rejected because the prod
entry would be a no-op CNAME (cluster DNS already resolves public
names) and would mislead readers into thinking the cross-NS Service was
load-bearing.

### MinIO terminates its own TLS, no Caddy in front

Eliminates the Host-preservation concern outright. Rejected because
MinIO would then need its own cert lifecycle (LE in prod, mkcert in
dev), diverging from the single Caddy-managed TLS story every other
stack uses.

### cert-manager + self-signed ClusterIssuer (the original 2026-05-20 plan)

Rejected because PR #15 explicitly removed cert-manager from the
architecture. Re-adding it just for MinIO would contradict the "Caddy
serves TLS autonomously" decision.
