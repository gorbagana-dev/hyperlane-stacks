# Cross-Host Monitoring: Scraping + Multiple Validators/Relayers

_Design spec — 2026-05-30_

## Status

Covers two of the three monitoring follow-ups from
`docs/architecture-decisions.md` (§Monitoring): **cross-host scraping** and
**showing data for multiple validators/relayers**. The third — **metrics
authentication** — is a separate PR (see [Deferred: metrics auth](#deferred-metrics-auth)).

## Problem

In production each validator and the relayer run on **separate hosts**, each
behind its own Caddy + public DNS, so Prometheus must scrape their `/metrics`
over public DNS. The dashboards also assume one validator per chain: the `chain`
label alone cannot distinguish two validators on the same chain (the deployment
specs are already named `…-primary`, anticipating primary/secondary).

## Scope

**In scope:**
1. Cross-host scraping — Prometheus scrapes validator/relayer `/metrics` over
   public DNS.
2. Multiple validators/relayers — per-instance labels + dashboards that break
   data down per instance.

**Out of scope:** metrics authentication — see
`docs/superpowers/research/2026-06-01-metrics-auth-caddy-ingress-findings.md`.

## Design

### Cross-host scraping

The `validators`/`relayers` jobs in `prometheus.yml` use Prometheus' native
**`file_sd_configs`** (file-based service discovery), reading their target lists
from files. The target lists are **configured per-deployment in the spec** and
rendered into those files — the operator owns the targets in
`deployment/spec-monitoring.yml`, not in shared stack code.

```yaml
# spec-monitoring.yml config:
PROMETHEUS_VALIDATOR_TARGETS: "gorchain-primary=validator-gorchain.bridge.gorbagana.wtf:443,solana-primary=validator-solana.bridge.gorbagana.wtf:443"
PROMETHEUS_RELAYER_TARGETS: "primary=relayer.bridge.gorbagana.wtf:443"
PROMETHEUS_SCRAPE_SCHEME: "https"
```

Each entry is `instance_label=host:port` (consistent with the existing
`MONITORED_WALLETS_*` `label:addr:threshold` convention). Targets use the
validators'/relayer's **existing** Caddy hostnames — no rename. The
`hyperlane_instance` label is independent of hostname, so one validator per chain
needs no extra config.

#### Render location: prometheus container entrypoint

The prometheus container's entrypoint (`render-targets.sh`) renders the targets
on **each container start**:

- reads `PROMETHEUS_VALIDATOR_TARGETS` / `PROMETHEUS_RELAYER_TARGETS` and
  `PROMETHEUS_SCRAPE_SCHEME` from the environment (injected from spec `config:`
  via the compose `environment:` block);
- writes `validators.yml` / `relayer.yml` (file_sd format, each target carrying
  its `hyperlane_instance` label) under the writable data dir
  `/prometheus/targets/` (the ConfigMap mount at `/etc/prometheus` is read-only);
- renders the scrape scheme into a writable copy of `prometheus.yml`; then
- `exec`s prometheus pointing at the rendered config.

Because rendering happens at container start, **adding a validator is an env
change plus a restart** — no deploy hook and no `laconic-so` update command.
Compose maps `entrypoint` → k8s `command` (overriding the image entrypoint) and
`command` → k8s `args`, so the script receives the prometheus flags as `"$@"`
and `exec`s the real binary. The script is plain POSIX `sh` (the
`prom/prometheus` image is busybox-based).

This stack has **no `deploy/commands.py` hook** — the entrypoint does all the
rendering.

**Hostname convention (future).** A second validator on the same chain gets its
own hostname (e.g. `validator-gorchain-secondary`), keeping `validator-gorchain`
as the primary. No rename is required for the current single-primary-per-chain
setup.

### Multiple validators/relayers

Each target carries a `hyperlane_instance` label, giving a stable,
human-readable grouping key independent of hostname. Dashboard changes
(`validator-dashboard.json`, `relayer-dashboard.json`):

1. Add a `hyperlane_instance` template variable
   (`label_values(hyperlane_instance)`, multi-select, include-all) alongside the
   existing `chain` variable.
2. Group panel queries by the instance too, e.g. `max by(chain) (…)` →
   `max by(chain, hyperlane_instance) (…)`, with a
   `hyperlane_instance=~"${hyperlane_instance:regex}"` matcher.

So two validators on the same chain render as separate series instead of
colliding.

## Files touched

| File | Change |
|---|---|
| `data/config/prometheus-config/render-targets.sh` | New. Container entrypoint: render file_sd targets + scheme from env, then `exec` prometheus. |
| `data/config/prometheus-config/prometheus.yml` | `validators`/`relayers` jobs use `file_sd_configs` at `/prometheus/targets/{validators,relayer}.yml`. |
| `data/compose/docker-compose-hyperlane-monitoring.yml` | prometheus service: `entrypoint` runs the script; `command` keeps the flags; `environment:` passes the three vars from spec config. |
| `data/config/grafana-dashboards-config/validator-dashboard.json` | Add `hyperlane_instance` variable; group/select by instance. |
| `data/config/grafana-dashboards-config/relayer-dashboard.json` | Same. |
| `deployment/spec-monitoring.yml` | The three `config:` entries (prod hostnames + `https`). |
| `tests/e2e/fixtures/test-spec-monitoring.yml` | In-cluster `service:port` targets; `PROMETHEUS_SCRAPE_SCHEME: http`; `external-services:` to route Prometheus to the pods. |
| `tests/e2e/test_07_monitoring.py` | Cross-host scrape-up + instance-label assertions. |
| `data/stacks/hyperlane-monitoring/README.md` | Document the spec-driven targets + entrypoint render. |

Prod `spec-validator-*.yml` / `spec-relayer.yml` need no change — they already
publish `/metrics` via Caddy.

## E2E approach

Multi-*host* (separate machines) can't be replicated on one test box, but the
cross-host **code path** can. The test deployment routes Prometheus to the
validator/relayer pods **in-cluster** via the monitoring spec's
`external-services` (selector mode — the same pattern the validators use to reach
MinIO), scraping over plain HTTP (`PROMETHEUS_SCRAPE_SCHEME: http`). Prod's
public-DNS + HTTPS path is unchanged; only the targets and scheme differ, both
via spec config — no harness patching of `prometheus.yml`.

Assertions in `test_07_monitoring.py`:
- `up{job="validators"} == 1` for both instances, and `up{job="relayers"} == 1`
  — proves cross-host scraping reaches the targets and the `hyperlane_instance`
  labels are attached.
- An always-emitted agent metric (`hyperlane_block_height{agent="validator"}`)
  carries `hyperlane_instance` — proves the label propagates onto real agent
  metrics (what the dashboards group by).

## Rendered targets file format

`validators.yml` (Prometheus file_sd):

```yaml
- targets: ["validator-gorchain.bridge.gorbagana.wtf:443"]
  labels: { hyperlane_instance: gorchain-primary }
- targets: ["validator-solana.bridge.gorbagana.wtf:443"]
  labels: { hyperlane_instance: solana-primary }
```

## Error handling

- Malformed entry (no `=`, or no `:` in `host:port`): the entrypoint exits
  non-zero with a message naming the offending entry, so the container fails
  fast (visible in pod logs) rather than scraping a broken target set.
- Empty/unset target var: an empty list (`[]`) is written; the job has no targets
  and Prometheus stays healthy.

## Deferred: metrics auth

Basic auth on `/metrics` is a separate PR. The `laconicnetwork/caddy-ingress`
fork exposes no basic-auth annotation, and SO injects secrets as env vars only,
so the design space (auth sidecar vs. extending the fork vs. network ACL) is
worked out separately in
`docs/superpowers/research/2026-06-01-metrics-auth-caddy-ingress-findings.md`.
