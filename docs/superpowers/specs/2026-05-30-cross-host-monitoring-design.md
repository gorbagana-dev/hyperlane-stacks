# Cross-Host Monitoring: Scraping + Multiple Validators/Relayers

_Design spec — 2026-05-30_

## Status

Proposed. Covers two of the three monitoring follow-ups from
`docs/architecture-decisions.md` (§Monitoring): **cross-host scraping** and
**showing data for multiple validators/relayers**. The third follow-up —
**metrics authentication** — is explicitly **deferred to a separate PR**
(see [Deferred: metrics auth](#deferred-metrics-auth)).

## Problem

Today Prometheus discovers validator/relayer pods with `kubernetes_sd_configs`
(pod role) and scrapes them inside a single Kind cluster on one host
(`stack_orchestrator/data/config/prometheus-config/prometheus.yml`). In
production each validator and the relayer run on **separate hosts**, each
behind its own Caddy + public DNS, so in-cluster pod discovery cannot reach
them. The dashboards also assume one validator per chain — the `chain` label
alone cannot distinguish two validators on the same chain (the deployment
specs are already named `…-primary`, anticipating primary/secondary).

## Scope

**In scope (this PR):**
1. Cross-host scraping — Prometheus scrapes validator/relayer `/metrics` over
   public DNS instead of in-cluster pod discovery.
2. Multiple validators/relayers — per-instance labels + dashboards that break
   data down per instance.

**Out of scope (next PR):** metrics authentication. It is the only facet that
depends on the `laconicnetwork/caddy-ingress` fork's capabilities and on a
mechanism SO does not currently provide; neither of the two facets above
depends on it.

## Current state (verified)

| Fact | Source |
|---|---|
| Prometheus uses `kubernetes_sd_configs` (in-cluster, single host) | `prometheus-config/prometheus.yml:23-54` |
| Validator exposes `/metrics` on `:9090`, published at `validator-gorchain.bridge.<zone>` | `spec-validator-gorchain.yml` (`HYP_METRICSPORT: 9090`, `http-proxy`) |
| Relayer exposes `/metrics` on `:9091`, published at `relayer.bridge.<zone>` | `spec-relayer.yml` (`--metricsPort 9091`, `http-proxy`) |
| Dashboards already have a `chain` template variable, group `by(chain)` | `grafana-dashboards-config/validator-dashboard.json` |
| Agent metrics carry `agent` + `chain` labels (e.g. `hyperlane_latest_checkpoint{agent="validator",chain="gorchain"}`) | dashboard panel exprs |
| `test_07_monitoring.py` asserts Prometheus self / Pushgateway / balance / Grafana — **not** validator/relayer scraping | `tests/e2e/test_07_monitoring.py` |
| Ops/ansible layer is archived (`deployment/ops` → `deployment/ops-archive`) | repo tree, PR #22 |

## Design

### Facet 1 — Cross-host scraping

Replace the `kubernetes-pods` (`kubernetes_sd_configs`) job with **static
HTTPS targets** pointing at the validator/relayer Caddy hostnames. This is the
"static, hardcoded target list; append one entry per new validator" model from
the architecture decision.

```yaml
scrape_configs:
  - job_name: prometheus
    static_configs:
      - targets: ["localhost:9090"]

  - job_name: pushgateway
    honor_labels: true
    static_configs:
      - targets: ["localhost:9091"]

  - job_name: validators
    scheme: https
    metrics_path: /metrics
    static_configs:
      - targets: ["validator-gorchain.bridge.gorbagana.wtf:443"]
        labels: { hyperlane_instance: gorchain-primary }
      - targets: ["validator-solana.bridge.gorbagana.wtf:443"]
        labels: { hyperlane_instance: solana-primary }

  - job_name: relayer
    scheme: https
    metrics_path: /metrics
    static_configs:
      - targets: ["relayer.bridge.gorbagana.wtf:443"]
        labels: { hyperlane_instance: primary }
```

Targets use the validators' and relayer's **existing** Caddy hostnames — no
rename. The `hyperlane_instance` label is independent of the hostname, so one
validator per chain needs no spec change at all.

- **No container changes.** Prometheus reads the verbatim ConfigMap as today;
  only the file's contents change.
- **No dependency on the archived ansible layer.** The static list lives in the
  committed `prometheus.yml` (prod hostnames). Adding a validator = appending
  one target entry, matching the GitOps add-validator flow.
- **Prod TLS** is publicly-trusted (Let's Encrypt via Caddy), so no
  `tls_config` is needed in prod.
- **Hostname convention (future).** A second validator on the same chain needs
  its own hostname; adopt an instance suffix then (`validator-gorchain-secondary`,
  …), keeping the existing `validator-gorchain` as the primary. No rename is
  required for the current single-primary-per-chain setup.

The monitoring stack's pod-discovery RBAC
(`hyperlane-monitoring/deploy/rbac.yaml`, applied by `deploy/commands.py`)
becomes unused once `kubernetes_sd_configs` is removed. Leave it in place for
this PR (removing it is unrelated cleanup); note it as dead for a later sweep.

### Facet 2 — Multiple validators/relayers

Each static target carries a `hyperlane_instance` label (above). Prometheus
also auto-adds an `instance` label (the target address), but `hyperlane_instance`
gives a stable, human-readable grouping key independent of hostname.

Dashboard changes (`validator-dashboard.json`, `relayer-dashboard.json`):
1. Add a `hyperlane_instance` template variable
   (`label_values(hyperlane_instance)`, multi-select, include-all) alongside the
   existing `chain` variable.
2. Update panel queries to group by the instance as well, e.g.
   `max by(chain) (…)` → `max by(chain, hyperlane_instance) (…)`, and add a
   `hyperlane_instance=~"${hyperlane_instance:regex}"` matcher.
3. Update legends to include the instance so two validators on the same chain
   render as separate series instead of colliding.

The `hyperlane-overview.json` dashboard is reviewed for the same treatment
where it shows per-validator/relayer data.

## Files touched

| File | Change |
|---|---|
| `stack_orchestrator/data/config/prometheus-config/prometheus.yml` | Replace `kubernetes-pods` job with static `validators` + `relayer` jobs |
| `stack_orchestrator/data/config/grafana-dashboards-config/validator-dashboard.json` | Add `hyperlane_instance` variable; group/legend by instance |
| `stack_orchestrator/data/config/grafana-dashboards-config/relayer-dashboard.json` | Same |
| `stack_orchestrator/data/config/grafana-dashboards-config/hyperlane-overview.json` | Per-instance breakdown where applicable |
| `tests/e2e/` (conftest + fixtures) | Validator/relayer test ingress + cert SANs; `prometheus.yml` hostname substitution; new scrape-up assertions |
| `stack_orchestrator/data/stacks/hyperlane-monitoring/README.md` | Document static-target scraping model |

Prod `deployment/spec-validator-*.yml` / `spec-relayer.yml` need **no change** —
they already publish `/metrics` via Caddy.

## Testing strategy

True multi-*host* (separate machines) cannot be replicated on one test box, but
the cross-host **code path** can: point Prometheus at the validator/relayer
Caddy ingress hostnames (e.g. `validator-gorchain.test/metrics`) — exactly what
prod does — instead of in-cluster pod IPs.

E2E harness additions (`tests/e2e/`):
- Expose validator + relayer **ingress hostnames** and add them to the mkcert
  **cert SANs**, so Prometheus can scrape them through Caddy.
- The harness substitutes the prod hostnames in `prometheus.yml` with the
  `.test` equivalents (matches the existing `REPLACE_AT_RUNTIME` /
  `REPLACE_HOST_IP` placeholder-substitution pattern) and adds
  `tls_config: { insecure_skip_verify: true }` for the mkcert certs in test.
- New assertions in `test_07_monitoring.py`: `up{job="validators"} == 1` and
  `up{job="relayer"} == 1`, plus presence of an agent metric
  (e.g. `hyperlane_latest_checkpoint`) carrying the `hyperlane_instance` label.
- Multiple-instance is demonstrated by adding a second target label entry
  (may point at the same pod in test) and asserting two series.

Existing `test_07` assertions are unaffected (none currently test
validator/relayer scraping), so this does not break current functionality.

## Deferred: metrics auth

The third facet — basic auth on `/metrics` — is a separate PR because:
- SO's `http-proxy` block cannot express `basic_auth` (only `path` +
  `proxy-to`), and SO injects secrets as **env vars only** (no file mounts),
  so Prometheus cannot use `basic_auth.password_file`.
- Whether auth can be applied at the ingress depends on the **custom
  `laconicnetwork/caddy-ingress`** fork (not upstream `caddyserver/ingress`,
  which has no basic_auth annotation). That fork must be investigated first.
- Likely landing spots: an auth reverse-proxy **sidecar** on the
  validator/relayer pods (controller-agnostic, matches the existing
  kms-proxy/igp-fee-claim sidecar pattern), or a **network/IP ACL** restricting
  `/metrics` to the monitoring host.

When auth lands, the scrape jobs above gain a `basic_auth` block whose password
must reach the Prometheus container — at which point the env-only secret
constraint forces a render step (e.g. `prometheus.yml.tmpl` substituted at
container start). That is the auth PR's problem, not this one.

## Implementation order

1. Static-target `prometheus.yml` (Facet 1).
2. E2E harness: validator/relayer ingress + cert SANs + hostname substitution;
   new scrape-up assertions.
3. Dashboard instance variable + groupings (Facet 2).
4. README update.
