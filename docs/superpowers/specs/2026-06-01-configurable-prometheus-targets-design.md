# Configurable Prometheus Scrape Targets — Design

**Date:** 2026-06-01
**Status:** Approved (design)

## Problem

The validator and relayer scrape targets are hardcoded inside the stack's
source config at `stack_orchestrator/data/config/prometheus-config/prometheus.yml`
(the `validators` and `relayer` jobs list `host:port` + `hyperlane_instance`
inline). To add or remove a validator an operator must edit that file — shared
stack code that is identical for every deployment — rather than the
per-deployment `deployment/spec-monitoring.yml` they are meant to own.

Two requirements from review:

1. **Configurable from spec** — the target list lives in `spec-monitoring.yml`,
   not baked into `prometheus.yml`.
2. **Incrementally addable to a live deployment** — adding a validator to an
   already-running monitoring stack works on redeploy/update without disrupting
   the targets already being scraped.

## Approach

Use Prometheus' native **`file_sd_configs`** (file-based service discovery).
The `validators`/`relayer` jobs in `prometheus.yml` stop listing targets inline
and instead point at a targets file that Prometheus *watches*. When that file
changes, Prometheus reloads the target set on its own — no process restart, no
scrape gap on existing targets. This is the idiomatic mechanism for changing a
live server's target set, and it directly satisfies requirement 2.

The targets file is **generated from the spec** at deploy time, so the spec
stays the single source of truth (requirement 1). The operator never hand-edits
the generated file.

### Config format

Two new `config:` entries in `spec-monitoring.yml`, comma-separated
`instance_label=host:port` entries — consistent with the existing
`MONITORED_WALLETS_*` (`label:addr:threshold`) convention already used in this
spec:

```yaml
  PROMETHEUS_VALIDATOR_TARGETS: "gorchain-primary=validator-gorchain.bridge.gorbagana.wtf:443,solana-primary=validator-solana.bridge.gorbagana.wtf:443"
  PROMETHEUS_RELAYER_TARGETS: "primary=relayer.bridge.gorbagana.wtf:443"
```

Adding a validator = append one `instance=host:port` entry.

### Render location: host-side `commands.py create()` hook

The render runs in the stack's `deploy/commands.py` `create()` hook, which:

- runs **after** SO copies the configmap source dirs into the deploy dir
  (`deployment_create.py`: `_write_deployment_files` then
  `call_stack_deploy_create`), so it can write into the already-copied
  `configmaps/prometheus-config/` dir; and
- can read spec config via `context.spec.get("config", {})` (same `Spec` object
  the current hook uses for `get_namespace()`).

It parses the two config vars and writes `validators.yml` and `relayer.yml`
(Prometheus file_sd format, each target carrying its `hyperlane_instance` label)
into `configmaps/prometheus-config/`. SO then creates these as part of the
existing `prometheus-config` ConfigMap at `deploy start`, mounted alongside
`prometheus.yml` at `/etc/prometheus/`.

On redeploy/update the ConfigMap content changes; k8s propagates the updated
file into the running Prometheus pod and file_sd reloads it — no pod restart.

**Rejected alternative — init container in the pod.** An init container renders
once at pod start from env vars, so changing targets would require a pod restart
— defeating file_sd's hot reload. Out.

### Dead-code removal

The current `commands.py create()` applies `deploy/rbac.yaml` (a ClusterRole
granting pod/endpoint list/watch) that existed solely for the
`kubernetes_sd_configs` pod discovery removed in the cross-host scraping change.
Verified unused: `prometheus.yml` has no `kubernetes_sd`/relabel references, and
nothing else in the repo consumes the `prometheus-pod-discovery` role. So:

- **Rewrite** `commands.py create()` from "apply RBAC" to "render targets".
- **Delete** `deploy/rbac.yaml`.

Related but out of scope: the `prometheus.io/scrape: "true"` annotations on the
validator/relayer specs and test fixtures were also only consumed by the old
pod discovery. They are inert annotations on other stacks now; left for a
separate cleanup sweep to keep this change focused.

## Components changed

| File | Change |
|---|---|
| `data/config/prometheus-config/prometheus.yml` | `validators`/`relayer` jobs: replace inline `static_configs` with `file_sd_configs` pointing at `/etc/prometheus/validators.yml` and `/etc/prometheus/relayer.yml`. Scheme/TLS/metrics_path stay. |
| `data/stacks/hyperlane-monitoring/deploy/commands.py` | Rewrite `create()`: parse `PROMETHEUS_VALIDATOR_TARGETS`/`PROMETHEUS_RELAYER_TARGETS` from spec config, write file_sd YAML into `configmaps/prometheus-config/`. |
| `data/stacks/hyperlane-monitoring/deploy/rbac.yaml` | Delete (dead pod-discovery RBAC). |
| `deployment/spec-monitoring.yml` | Add the two `config:` entries with prod hostnames. |
| `tests/e2e/fixtures/test-spec-monitoring.yml` | Point the two target vars at in-cluster `service:port` names and declare `external-services:` (selector mode, same pattern the validators use for MinIO) so SO routes Prometheus to the validator/relayer pods directly. |
| `tests/e2e/conftest.py` | Simplify `_patch_prometheus_targets_for_test` to flip the scrape jobs `https`→`http` (in-cluster targets are plain HTTP). Remove the CoreDNS `.test`→Caddy machinery and the mkcert `ca_file` injection — no longer needed once scraping is in-cluster. |
| `data/stacks/hyperlane-monitoring/README.md` | Document the spec-driven target config + file_sd. |

## Data flow

```
spec-monitoring.yml config:                       (operator edits this)
  PROMETHEUS_VALIDATOR_TARGETS = "inst=host:port,..."
        │  deploy create
        ▼
commands.py create() hook  ──renders──▶  configmaps/prometheus-config/validators.yml
        │  deploy start                                    relayer.yml
        ▼
k8s ConfigMap prometheus-config  ──mounted──▶  /etc/prometheus/validators.yml
        │                                                   /etc/prometheus/relayer.yml
        ▼
prometheus.yml  file_sd_configs: files: ['/etc/prometheus/validators.yml']
        │  Prometheus watches the file
        ▼
target set reloaded live (no restart) on every ConfigMap change
```

## Rendered targets file format

`validators.yml` (Prometheus file_sd):

```yaml
- targets: ["validator-gorchain.bridge.gorbagana.wtf:443"]
  labels: { hyperlane_instance: gorchain-primary }
- targets: ["validator-solana.bridge.gorbagana.wtf:443"]
  labels: { hyperlane_instance: solana-primary }
```

## Error handling

- Malformed entry (no `=`, or no `:` in `host:port`): the hook raises with a
  clear message naming the offending entry, failing the deploy fast rather than
  shipping a broken target file.
- Empty/unset target var: the hook writes an empty list (`[]`); the job has no
  targets and Prometheus stays healthy. (Matches Prometheus tolerating an empty
  file_sd file.)

## Testing

Existing `test_07_monitoring.py` cross-host assertions
(`test_validator_targets_up`, `test_relayer_target_up`,
`test_agent_metrics_have_instance_label`) already verify the targets are scraped
with the right `hyperlane_instance` labels. They continue to pass unchanged —
they assert on the *result* (targets `up`, labels present), which is identical
whether the targets came from inline `static_configs` or rendered file_sd, and
whether Prometheus reaches them over the public https hostname (prod) or the
in-cluster external-service over http (e2e).

No new behavior needs a new test: the render is exercised end-to-end by the
existing target-up assertions, and an empty/malformed config is an operator
input error surfaced at deploy time.

## Out of scope

- Auth on `/metrics` (separate future PR, depends on the caddy-ingress fork).
- Removing the now-inert `prometheus.io/scrape` annotations (separate sweep).
