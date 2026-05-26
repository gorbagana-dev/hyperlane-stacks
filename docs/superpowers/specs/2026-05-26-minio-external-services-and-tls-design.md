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
                          │  AWS_ENDPOINT_URL_S3=http://hyperlane-minio:9000   (dev)
                          │  AWS_ENDPOINT_URL_S3=https://s3.bridge.gorbagana.wtf  (prod)
                          ▼
       ┌─────────────────────────────────────────────────┐
       │ Hostname resolution:                            │
       │  - dev:  Service "hyperlane-minio" in consumer  │
       │          NS, created by external-services       │
       │          (selector mode → minio pod IPs in the  │
       │          laconic-hyperlane-minio NS, port 9000) │
       │  - prod: cluster DNS forwards to upstream and   │
       │          resolves public hostname natively      │
       └─────────────────────────────────────────────────┘
                          │
                          │  dev:  direct to minio:9000
                          │  prod: Caddy ingress terminates TLS,
                          │        reverse_proxy minio:9000
                          ▼
                       MinIO  (S3 API, path-style)
```

| Env  | Endpoint URL                              | Path through Caddy?               |
|------|-------------------------------------------|-----------------------------------|
| dev  | `http://hyperlane-minio:9000`             | No — direct cross-NS via selector |
| prod | `https://s3.bridge.gorbagana.wtf`         | Yes — Caddy terminates TLS        |

## 4. Key decisions

### 4.1 Dev bypasses Caddy; prod goes through Caddy

In prod, Caddy fronts MinIO at `s3.bridge.gorbagana.wtf` with a
Let's-Encrypt-issued cert. In dev, the validator/relayer talk to MinIO
*directly* over plain HTTP, with no proxy in the path. The dev path
uses `external-services:` selector mode to resolve the in-cluster
Service name to the MinIO pod IPs in the minio namespace.

Caddy was originally in the dev path too, mirroring prod's topology
byte-for-byte. That broke for two compounding reasons (see §8 for the
full chain of investigation):

- `aws-sdk-rust` does not honor `AWS_CA_BUNDLE` (awslabs/aws-sdk-rust#1362),
  so distributing the mkcert root to consumer pods has no effect — they
  would refuse the mkcert-signed Caddy cert.
- Caddy v2 auto-enables HTTPS for any hostname-based site and 308-redirects
  HTTP to HTTPS. AWS SDK clients do not follow redirects on signed S3
  requests, so a fallback "use plain HTTP through Caddy in dev" path
  also fails.

Solving either of those would require a fork patch (custom HTTP
connector for cert handling, or an SO change to inject a Caddy
annotation that disables auto-HTTPS). Both add ongoing maintenance
cost for parity on a leg that doesn't carry S3-specific behavior — Caddy
is just a reverse proxy, and we already exercise the Caddy code path
under TLS via browser-facing tests (warp-ui, monitoring, validator
dashboard).

The pragmatic line: dev validates the routing topology and S3 logic
without TLS; prod validates the TLS-through-Caddy path on the
hostname where it actually matters.

### 4.2 `external-services:` in dev only, public DNS in prod

The validator/relayer pods need to resolve the hostname in the endpoint
URL. In dev, the hostname is the in-cluster Service name `hyperlane-minio`
and must be created via an `external-services:` entry in each consumer's
namespace. The entry uses **selector mode** with `namespace: laconic-hyperlane-minio`
and a label selector matching the MinIO pod (`app.kubernetes.io/stack:
hyperlane-minio`, the standard label SO applies to every pod in a stack).
SO populates a headless Service plus Endpoints at deploy time by
discovering the matching pod IPs in the target namespace, so the
consumer's DNS resolution lands directly on MinIO on port 9000.

In prod the hostname is a real DNS name (`s3.bridge.gorbagana.wtf`);
cluster DNS forwards unknown names to upstream resolvers by default and
resolves it without any explicit Service object.

The dev fixture's `external-services:` block carries an inline comment
explaining the asymmetry, so anyone reading the prod spec doesn't wonder
where the matching entry went.

### 4.3 Dev URL is HTTP on the MinIO API port; prod URL is HTTPS

- Dev: `AWS_ENDPOINT_URL_S3=http://hyperlane-minio:9000` — direct to MinIO,
  no TLS.
- Prod: `AWS_ENDPOINT_URL_S3=https://s3.bridge.gorbagana.wtf` — through
  Caddy, TLS terminated by Caddy with an LE cert that the SDK's built-in
  `webpki-roots` trusts without configuration.

Both URLs are spec-driven via each spec's `config:` block.

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
    https://s3.bridge.gorbagana.wtf`.
  - Do not add an `external-services:` block (public DNS handles it).

### Test fixtures

- `tests/e2e/fixtures/test-spec-minio.yml`
  - `http-proxy:` block contains the existing `host-name:
    minio-console.test` → `minio:9001` only. No `hyperlane-minio`
    host-name (consumers reach MinIO directly in dev, not through Caddy).
- `tests/e2e/fixtures/test-spec-validator-gorchain.yml`,
  `test-spec-validator-solana.yml`, `test-spec-relayer.yml`
  - Add `external-services:` entry:
    ```yaml
    hyperlane-minio:
      selector:
        app.kubernetes.io/stack: hyperlane-minio
      namespace: laconic-hyperlane-minio
      port: 9000
    ```
    with comment explaining "dev only — prod uses public DNS to resolve
    `s3.bridge.gorbagana.wtf`".
  - Add `config:` entry `AWS_ENDPOINT_URL_S3: http://hyperlane-minio:9000`.

### Test code

- `tests/e2e/lib/cluster.py`
  - Remove `hyperlane-minio` from `TEST_HOSTNAMES` — no longer reached
    via Caddy in dev, so mkcert SAN coverage is moot.

## 6. Verification

PR1 is green when:

- `test_03_minio.py`, `test_04_validator.py`, `test_05_relayer.py`,
  `test_08_bridge.py` all pass.
- `test_relayer_checkpoint_syncer_connected` (which already asserts no
  `ConnectError` / `NoSuchBucket` / `InvalidAccessKeyId` /
  `SignatureDoesNotMatch` in relayer logs) confirms the dev cross-NS
  selector path resolves correctly and signed S3 traffic round-trips.
- The cross-stack ClusterIP Service is no longer present in the
  `laconic-hyperlane-minio` namespace.
- The old cross-namespace FQDN literal does not appear in any compose
  file.
- Each consumer pod's `AWS_ENDPOINT_URL_S3` env is
  `http://hyperlane-minio:9000`.

## 7. Out of scope (deferred to PR2)

- Per-validator MinIO users and bucket-prefix policies.
- `deployment/bridges/<name>/operator/minio-users.yaml` schema.
- Extended MinIO init Job for `mc admin policy/user/attach`.
- Per-validator MinIO credential values (will use the
  `secrets.<name>.keys` mechanism from PR #15).

## 8. Alternatives considered

This design went through three iterations as load-bearing assumptions
about the SDK and Caddy were verified empirically. The path matters
because each rejected option is the obvious first idea, and recording
why it failed prevents the next person from re-walking the trail.

### Iteration 1 — HTTPS in dev via mkcert root + `AWS_CA_BUNDLE`

The original draft distributed the mkcert root CA via a
`minio-ca-config` ConfigMap and set `AWS_CA_BUNDLE` on consumer pods.
Investigation against our pinned SDK versions (aws-config 1.1.7,
aws-sdk-s3 1.65.0, aws-smithy-runtime 1.8.1) found `AWS_CA_BUNDLE` is
**not honored at all** by aws-sdk-rust — awslabs/aws-sdk-rust#1362 is
the open, unimplemented feature request. Setting the env var would
silently do nothing. Implemented (Tasks 1, 2, 4, 5) then rolled back in
Task R.

### Iteration 2 — HTTP through Caddy in dev (port 80 reverse_proxy)

After dropping `AWS_CA_BUNDLE`, the proposed fallback was plain HTTP
between consumer and Caddy: validator dials `http://hyperlane-minio:80`,
Caddy reverse_proxies to `minio:9000`. Empirical test confirmed Caddy v2
auto-enables HTTPS for hostname-based sites and returns
`308 Permanent Redirect` from port 80 to port 443. AWS SDK clients do
not follow redirects on signed S3 requests; the validator would receive
a 308 and abort.

Suppressing the redirect requires either an SO change to emit a
`caddy.ingress.kubernetes.io/...` annotation that disables auto-HTTPS
on this specific Ingress, or upstream caddyserver/ingress support for
such an annotation (existence and stability not verified). Either path
adds ingress-controller-specific surface to maintain.

### Iteration 3 — Dev bypasses Caddy via selector-mode external-services (this design)

The path actually taken. `external-services:` selector mode creates a
headless k8s Service in the consumer's namespace with Endpoints pointing
at the MinIO pod IPs (discovered cross-NS by SO at deploy time). The
consumer dials `http://hyperlane-minio:9000` and lands directly on
MinIO. No Caddy in the dev S3 data path; no TLS; no SDK env-var
support needed. Prod stays Caddy-fronted via public DNS where the LE
cert is trusted by the SDK's built-in webpki-roots.

The dev/prod topology asymmetry is the residual cost; the test signal
on the Caddy-fronts-S3 path is exercised in prod only. Browser-facing
tests (warp-ui, monitoring, validator dashboards) continue to exercise
Caddy under TLS in dev, so Caddy is not unverified — only the
S3-specific traffic shape is.

### Fork-patch aws-sdk-rust to honor `AWS_CA_BUNDLE`

Would give dev byte-for-byte HTTPS parity with prod. Rejected because
it adds a third long-lived patch (alongside `s3-path-style.patch` and
`kms-endpoint.patch`) we'd carry until upstream merges #1362 or we drop
the fork.

### `rustls-native-certs` + init container installing mkcert root

Would also work — switch the SDK from `webpki-roots` to system-cert
mode and bake the mkcert root into the container's trust store at
deploy time. Two moving parts (fork patch + init container) instead of
one selector entry. Rejected for the same maintenance reason.

### `external-services:` in prod too (ExternalName → public DNS)

Symmetric spec shape between environments. Rejected because the prod
entry would be a no-op CNAME (cluster DNS already resolves public
names) and would mislead readers into thinking the cross-NS Service was
load-bearing.

### MinIO terminates its own TLS, no Caddy in front

Eliminates the Host-preservation concern outright. Rejected because
MinIO would then need its own cert lifecycle (LE in prod, mkcert in
dev), diverging from the single Caddy-managed TLS story every other
stack uses in prod.

### cert-manager + self-signed ClusterIssuer (the original 2026-05-20 plan)

Rejected because PR #15 explicitly removed cert-manager from the
architecture. Re-adding it just for MinIO would contradict the "Caddy
serves TLS autonomously" decision.
