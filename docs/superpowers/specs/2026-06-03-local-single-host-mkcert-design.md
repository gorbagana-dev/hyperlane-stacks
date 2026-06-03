# Local single-host: self-trusted certs (mkcert), no Cloudflare

**Date:** 2026-06-03
**Status:** Design — approved architecture, pending spec review
**Scope:** The `local` own-chains ops environment only. Staging and prod are untouched.

## Problem

The `local` environment currently mirrors prod/staging networking for **both** of its
topologies: Caddy + Cloudflare DNS + real Let's Encrypt, under an operator-supplied
public zone. That is the right model for the **multi-host** topology (Layer 2) — the
validator→MinIO leg crosses a host boundary, so the Rust S3 client must trust a real
cert, which forces public DNS + LE.

But for the **single-host** topology (Layer 1) it is overkill and it carries a real
risk:

- It requires a **Cloudflare API token and a public DNS zone** to bring the bridge up
  on one box — everything is local, yet we reach out to a DNS provider.
- The validator→MinIO call goes `pod → host public IP → back to Caddy` (NAT hairpin),
  which Docker/kind don't always loop cleanly. We flagged this as the single-host
  limitation and it is **unverified on a real VM**.

The e2e tests already solve exactly this for a single machine, with a pattern we can
lift directly.

## What e2e already does (the reference)

1. **mkcert, not ACME.** A multi-SAN mkcert cert is generated for the test hostnames and
   its root CA is trusted on the host. The cert is pre-seeded into Caddy as k8s Secrets;
   Caddy's `secret_store` driver finds them at startup and **skips ACME** for any
   hostname that already has a cert. (`tests/e2e/lib/cluster.py:103-205`,
   `tests/e2e/conftest.py:331-375`)

2. **SO restores the pre-seed for us — no SO change needed.**
   `install_ingress_for_kind()` runs a 3-phase Caddy bringup: apply config → **restore
   any certs found at `{kind_mount_root}/caddy-cert-backup/caddy-secrets.yaml` before the
   Caddy pod starts** → apply the Caddy Deployment. The `network.acme-email` stays in the
   specs for prod parity; ACME simply never fires when certs are present.
   (`../stack-orchestrator/.../deploy/k8s/helpers.py:464-546`)

   The certmagic storage key embeds the LE issuer
   (`certificates/acme-v02.api.letsencrypt.org-directory/{host}/{host}.crt`) even for
   mkcert certs, so the cluster's `acmeCA` default is left untouched — Caddy thinks LE is
   the issuer, finds a stored cert under that key, and uses it.

3. **The validator→MinIO leg bypasses Caddy entirely.**
   `AWS_ENDPOINT_URL_S3: http://hyperlane-minio:9000` via an `external-services:
   selector:` block — a headless Service in the validator's namespace with Endpoints
   discovered from the MinIO pods cross-namespace, plain HTTP, no TLS. This is the
   documented workaround for `aws-sdk-rust` ignoring `AWS_CA_BUNDLE`
   (awslabs/aws-sdk-rust#1362). (`tests/e2e/fixtures/test-spec-validator-gorchain.yml:16,52-72`;
   `docs/superpowers/specs/2026-05-26-minio-external-services-and-tls-design.md:278-292`)

4. **Prometheus scrapes in-cluster over HTTP.** `PROMETHEUS_SCRAPE_SCHEME: http` +
   targets like `validator-gorchain:9090` resolved via `external-services: selector:`
   blocks for the validators and relayer. (`tests/e2e/fixtures/test-spec-monitoring.yml:22-24,38-59`;
   `stack_orchestrator/data/config/prometheus-config/run.sh:43-49`)

## Decision

Single-host `local` adopts the e2e pattern; multi-host `local` keeps LE + Cloudflare.
Both are served by one committed spec tree (`deployment/local/`) — the choice is driven
by a **derived** topology, not by hand-editing or an operator flag.

### Topology derivation

In single-host, MinIO and the agents (relayer + validators) share a host, i.e. one kind
cluster — which is *exactly* the precondition for the in-cluster MinIO/scrape paths. In
multi-host they are on different hosts. That comparison is the signal:

```yaml
# inventories/local/group_vars/all.yml
topology:   "{{ 'single' if (groups['minio_hosts'][0] == groups['relayer_hosts'][0]) else 'multi' }}"
manage_dns: "{{ topology == 'multi' }}"
```

This keeps the shared `group_vars` working for both inventories (`hosts.yml`,
`hosts-multihost.yml`) with no per-inventory var and no `-e` flag — consistent with the
already-approved `dns_records` derivation.

### What actually diverges (small, and all in group_vars values)

The base specs stay in their current multi-host (LE) shape. Every difference is expressed
as a **token** rendered by the *existing* `spec_token_renders` machinery
(`ops/roles/stack_deploy/tasks/deploy.yml:34-41`). No new render task.

**Value tokens** (computed per topology in `group_vars`):

| token | single | multi |
|---|---|---|
| `__S3_ENDPOINT__` | `http://hyperlane-minio:9000` | `https://s3.{{ dns_zone }}` |
| `__PROM_SCRAPE_SCHEME__` | `http` | `https` |
| `__PROM_VALIDATOR_TARGETS__` | `gorchain-primary=validator-gorchain:9090,solana-primary=validator-solana:9090` | `gorchain-primary=validator-gorchain.{{ zone }}:443,solana-primary=validator-solana.{{ zone }}:443` |
| `__PROM_RELAYER_TARGETS__` | `primary=relayer:9091` | `primary=relayer.{{ zone }}:443` |

**Structural-block tokens** — `ansible.builtin.replace` substitutes a comment marker
with a multi-line YAML block. The committed base spec carries the marker as a comment
(valid YAML, yamllint-clean); single-host renders it to the block, multi-host renders it
to empty:

- `# __SINGLE_HOST_MINIO_XS__` — in both validator specs. Single → the
  `external-services: hyperlane-minio:` selector block (namespace
  `laconic-hyperlane-minio`, port 9000). Multi → empty.
- `# __SINGLE_HOST_PROM_XS__` — in the monitoring spec. Single → the
  `external-services:` block with `validator-gorchain`, `validator-solana`, `relayer`
  selectors. Multi → empty.

`spec_token_renders` always includes every marker key; only the value differs by
topology. Because the markers are plain comments until rendered, the committed specs
remain valid YAML and parse identically in both topologies before render.

### Specs touched (spec-level edits)

- `spec-validator-gorchain.yml`, `spec-validator-solana.yml`, and `spec-relayer.yml`:
  `AWS_ENDPOINT_URL_S3: "__S3_ENDPOINT__"` (was `https://s3.__DNS_ZONE__`); append
  `# __SINGLE_HOST_MINIO_XS__` at column 0 after the `network:` block. The relayer reads
  validator checkpoints from MinIO over the same anonymous `aws-sdk-rust` S3 client as the
  validators, so it needs the same in-cluster treatment. The validators' own
  `network.http-proxy` route and chain RPC access (via `gorchain_rpc_url` /
  `solana_rpc_url` domains, out-of-band) are unchanged — both topologies keep them;
  mkcert covers the validator and relayer hostnames in single-host.
- `spec-monitoring.yml`: `PROMETHEUS_VALIDATOR_TARGETS: "__PROM_VALIDATOR_TARGETS__"`,
  `PROMETHEUS_RELAYER_TARGETS: "__PROM_RELAYER_TARGETS__"`, add
  `PROMETHEUS_SCRAPE_SCHEME: "__PROM_SCRAPE_SCHEME__"` to `config:`, append
  `# __SINGLE_HOST_PROM_XS__` at column 0.
- All other specs (`spec-minio.yml`, gas-oracle, warp-ui, deployer,
  warp-deployer): unchanged. MinIO keeps its `s3.{{ zone }}` / `minio-console.{{ zone }}`
  Caddy routes in both topologies — in single-host the validators and relayer no longer
  use the `s3` route, but it stays available for operator browser/CLI access via mkcert.

## New role: `local_tls` (single-host only)

Provisions self-trusted TLS and local name resolution so the operator needs **no DNS
provider and no public zone** for single-host. Runs only when `topology == 'single'`,
**entirely on the single host** (privileged) — it never mutates the controller or the
operator's workstation:

1. Install `mkcert`.
2. Ensure the mkcert root CA exists (`mkcert -CAROOT`) and trust it in the **host's**
   system store (`update-ca-certificates`), so host-side `curl` and the role's own
   self-check trust the certs.
3. Generate one multi-SAN leaf cert covering all bridge hostnames. The hostname list is
   **derived** from the same sources the specs use:
   `[ key ~ '.' ~ dns_zone for key in dns_record_map ]` (→ `s3`, `minio-console`,
   `grafana`, `prometheus`, `warp-ui`, `relayer`) plus each validator's hostname from
   `bridges/default/operator/validators.yaml`.
4. Render `{{ kind_mount_root }}/caddy-cert-backup/caddy-secrets.yaml` — three k8s
   Secrets per hostname (`.crt`, `.key`, `.json`) in namespace `caddy-system`, named and
   annotated to match Caddy's certmagic storage key. This replicates
   `write_caddy_cert_backup()` (`tests/e2e/lib/cluster.py:103-152`) exactly; the
   implementation reads that function as the source of truth for the name/annotation
   format and the empty-`.json` metadata object.
5. Add **host** `/etc/hosts` entries mapping every derived hostname to `127.0.0.1` (kind
   maps Caddy's ingress to the host's loopback), using `ansible.builtin.blockinfile` with
   a marker so re-runs are idempotent and the block is removable. This serves host-side
   tools and any browsing done on the host itself.
6. Copy the mkcert `rootCA.pem` to a known, documented host path (e.g.
   `{{ credentials_dir }}/local-rootCA.pem`) so the operator can `scp` it down for
   workstation browser trust.

**Browsing is operator-driven and documented in the runbook, not automated.** No
in-cluster component resolves `*.{{ zone }}` in single-host (MinIO and the scrape targets
are in-cluster), so the only machine that needs hostname resolution + CA trust beyond the
host is wherever the operator points a browser. The documented flow:
`ssh -L 443:localhost:443 <host>`, add `127.0.0.1 <hostnames>` to the **workstation's**
`/etc/hosts`, and trust the fetched `rootCA.pem` there. The role deliberately does not
reach into the operator's workstation.

### Playbook wiring

`playbooks/setup-all.yml` imports a new `playbooks/local-tls.yml` after
`configure-dns.yml`. `configure-dns.yml` already no-ops when `manage_dns` is false
(single-host); `local-tls.yml` is a single play targeting the single host, gated on
`topology == 'single'`, so multi-host skips the role and single-host skips Cloudflare.
The two are mutually exclusive by construction.

### Secrets

`required_operator_secrets` drops `cloudflare_api_token` for single-host (it is only
needed when `manage_dns`). Make the list topology-conditional in `group_vars`:
single-host requires `privy_*` + `ghcr_pat` only. `secrets.example.yml` keeps the
Cloudflare entry but the runbook notes it is multi-host-only.

## What this removes / simplifies

- **The NAT-hairpin limitation is eliminated for single-host** — the validator→MinIO
  call is pod-to-pod in-cluster, never leaving the box.
- **The "Fallback — no public DNS (local ACME)" section** in `runbooks/local.md` is
  deleted. It required an unbuilt caddy-ingress enhancement (non-LE issuer + per-host
  HTTP-only site) to work around the Rust S3 trust problem. mkcert + in-cluster MinIO is
  strictly better and needs no SO change.

## Files changed

- `deployment/local/spec-validator-gorchain.yml`, `spec-validator-solana.yml`,
  `spec-relayer.yml`, `spec-monitoring.yml` — tokens + comment markers (above).
- `ops/inventories/local/group_vars/all.yml` — `topology`, `manage_dns`,
  topology-conditional `spec_token_renders` values and `required_operator_secrets`.
- `ops/roles/local_tls/` — new role (tasks + a Jinja template for `caddy-secrets.yaml`).
- `ops/playbooks/local-tls.yml` — new; imported by `setup-all.yml`.
- `ops/runbooks/local.md` — single-host now needs no DNS provider; remove the local-ACME
  fallback; note the hairpin limitation is gone for single-host; document the browse-via-
  tunnel flow (SSH `-L`, workstation `/etc/hosts` → `127.0.0.1`, fetch + trust the
  published `rootCA.pem`).
- `ops/tests/test_local_env.yml` — assert both topologies' rendered token values (stub
  `topology=single` and `topology=multi`); assert `cloudflare_api_token` is absent from
  `required_operator_secrets` under single and present under multi.
- `docs/superpowers/specs/2026-06-01-deploy-side-ansible-design.md` — update the
  own-chains section: single-host = mkcert/in-cluster, multi-host = LE/Cloudflare.

## Keep-in-sync

The new tokens (`__S3_ENDPOINT__`, `__PROM_*__`) and the comment markers are local-only
and rendered before `deploy create`; SO never sees them. The CLAUDE.md
compose↔spec↔fixture table is unaffected (no env-var add/remove at the compose layer —
`PROMETHEUS_SCRAPE_SCHEME` already exists in the monitoring stack and the e2e fixture).

## Testing / verification

- `ansible-lint` (production profile), `yamllint`, `ansible-playbook --syntax-check` on
  both inventories.
- `ops/tests/test_local_env.yml` extended for both topologies.
- Render dry-run: confirm a single-host render of the validator + monitoring specs
  produces valid YAML with the `external-services` blocks and `http://hyperlane-minio:9000`,
  and a multi-host render produces the LE shape with the markers gone.
- Real-VM single-host run (deferred, same follow-up bucket as the multi-host run):
  mkcert pre-seed → Caddy serves trusted certs with no ACME calls; validator writes
  checkpoints to MinIO in-cluster; Prometheus scrapes validators/relayer in-cluster.

## Out of scope

- Multi-host networking (unchanged: LE + Cloudflare).
- Any SO change — the pre-seed restore already exists.
- Workstation-side trust/resolution and the SSH tunnel — operator-driven, documented in
  the runbook. The role stays host-only.
