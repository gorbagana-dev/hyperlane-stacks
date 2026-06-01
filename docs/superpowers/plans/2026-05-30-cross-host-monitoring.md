# Cross-Host Monitoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Scrape validator/relayer `/metrics` cross-host over public DNS, with the target list configured per-deployment in the spec, and make the dashboards break data down per validator/relayer instance.

**Architecture:** `prometheus.yml`'s `validators`/`relayers` jobs use `file_sd_configs`. The prometheus container's entrypoint (`run.sh`) renders the target files and scrape scheme from spec-provided env vars (`PROMETHEUS_VALIDATOR_TARGETS` / `PROMETHEUS_RELAYER_TARGETS` / `PROMETHEUS_SCRAPE_SCHEME`) on each start, then `exec`s prometheus — so adding a validator is an env change + restart. Prod scrapes the existing public Caddy hostnames over `https`; the e2e cluster scrapes the pods in-cluster via `external-services` over `http`. Dashboards gain a `hyperlane_instance` template variable and group by it.

**Tech Stack:** Prometheus (`file_sd_configs`), POSIX `sh` entrypoint, Grafana dashboards (JSON), laconic-so k8s-kind deploy, pytest e2e.

**Scope:** Facets 1 (cross-host scraping) + 2 (multiple validators/relayers). Metrics auth is a separate PR (see `docs/superpowers/research/2026-06-01-metrics-auth-caddy-ingress-findings.md`).

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `stack_orchestrator/data/config/prometheus-config/prometheus.yml` | Prometheus scrape config | `validators`/`relayers` jobs use `file_sd_configs` at `/prometheus/targets/*.yml` |
| `stack_orchestrator/data/config/prometheus-config/run.sh` | prometheus container entrypoint | New. Render file_sd targets + scheme from env, then `exec` prometheus |
| `stack_orchestrator/data/compose/docker-compose-hyperlane-monitoring.yml` | monitoring compose | prometheus `entrypoint` runs the script; `environment:` passes the three vars |
| `deployment/spec-monitoring.yml` | prod spec | The three `config:` entries (prod hostnames + `https`) |
| `tests/e2e/fixtures/test-spec-monitoring.yml` | e2e spec | In-cluster `service:port` targets + `PROMETHEUS_SCRAPE_SCHEME: http` + `external-services:` |
| `tests/e2e/test_07_monitoring.py` | E2E assertions | Assert validator/relayer targets `up` and carry `hyperlane_instance` |
| `stack_orchestrator/data/config/grafana-dashboards-config/validator-dashboard.json` | Validator dashboard | Add `hyperlane_instance` variable; group/filter by it |
| `stack_orchestrator/data/config/grafana-dashboards-config/relayer-dashboard.json` | Relayer dashboard | Add `hyperlane_instance` variable; group/filter by it |
| `stack_orchestrator/data/stacks/hyperlane-monitoring/README.md` | Docs | Document spec-driven cross-host scraping |

The monitoring stack has no `deploy/commands.py` hook — the entrypoint does the rendering.

---

## Task 1: Spec-driven cross-host scrape config + entrypoint render

**Files:**
- Modify: `stack_orchestrator/data/config/prometheus-config/prometheus.yml`
- Add: `stack_orchestrator/data/config/prometheus-config/run.sh`
- Modify: `stack_orchestrator/data/compose/docker-compose-hyperlane-monitoring.yml`
- Modify: `deployment/spec-monitoring.yml`

- [ ] **Step 1: Point the `validators`/`relayers` jobs at file_sd**

In `prometheus.yml`, replace the `kubernetes-pods` job with two `file_sd_configs` jobs (leave `global`, `rule_files`, and the `prometheus`/`pushgateway` self jobs unchanged):

```yaml
  - job_name: validators
    scheme: https
    metrics_path: /metrics
    file_sd_configs:
      - files: ["/prometheus/targets/validators.yml"]

  - job_name: relayers
    scheme: https
    metrics_path: /metrics
    file_sd_configs:
      - files: ["/prometheus/targets/relayer.yml"]
```

- [ ] **Step 2: Add the entrypoint render script**

Add `run.sh` (POSIX `sh` — the `prom/prometheus` image is busybox). It reads `PROMETHEUS_VALIDATOR_TARGETS` / `PROMETHEUS_RELAYER_TARGETS` (comma-separated `instance=host:port`) and writes file_sd YAML (each target tagged with its `hyperlane_instance` label) to `/prometheus/targets/` — `/etc/prometheus` is a read-only ConfigMap, so output goes to the writable data dir. It renders the scrape scheme from `PROMETHEUS_SCRAPE_SCHEME` (default `https`) into a writable copy of `prometheus.yml`, then `exec /bin/prometheus --config.file=/prometheus/prometheus.yml "$@"`. A malformed entry fails the container fast; an empty var writes `[]`.

- [ ] **Step 3: Wire the entrypoint + env in compose**

In the `prometheus` service: set `entrypoint: ["/bin/sh", "/etc/prometheus/run.sh"]`, keep the prometheus flags in `command:` (minus `--config.file`, which the script supplies), and add an `environment:` block passing `PROMETHEUS_VALIDATOR_TARGETS`, `PROMETHEUS_RELAYER_TARGETS`, `PROMETHEUS_SCRAPE_SCHEME` from spec config. (Compose `entrypoint` → k8s `command`, `command` → k8s `args`.)

- [ ] **Step 4: Add the spec config entries**

In `deployment/spec-monitoring.yml` `config:`, add `PROMETHEUS_VALIDATOR_TARGETS` / `PROMETHEUS_RELAYER_TARGETS` (prod Caddy hostnames `:443`) and `PROMETHEUS_SCRAPE_SCHEME: "https"`.

- [ ] **Step 5: Validate**

```bash
python3 -c "import yaml; yaml.safe_load(open('stack_orchestrator/data/config/prometheus-config/prometheus.yml')); print('ok')"
sh -n stack_orchestrator/data/config/prometheus-config/run.sh && echo "script ok"
```

- [ ] **Step 6: Commit**

```bash
git add stack_orchestrator/data/config/prometheus-config/ \
        stack_orchestrator/data/compose/docker-compose-hyperlane-monitoring.yml \
        deployment/spec-monitoring.yml
git commit -m "feat(monitoring): render cross-host scrape targets from spec on container start"
```

---

## Task 2: E2E spec — scrape the pods in-cluster

The prod spec scrapes public hostnames over https. In the single-host e2e cluster the validators/relayer are reached in-cluster, so the test spec overrides the targets and scheme; no harness patching of `prometheus.yml` is needed (the scheme is env-driven).

**Files:**
- Modify: `tests/e2e/fixtures/test-spec-monitoring.yml`

- [ ] **Step 1: In-cluster targets + http scheme**

Set `PROMETHEUS_VALIDATOR_TARGETS` / `PROMETHEUS_RELAYER_TARGETS` to in-cluster `service:port` names (e.g. `gorchain-primary=validator-gorchain:9090`) and `PROMETHEUS_SCRAPE_SCHEME: "http"`.

- [ ] **Step 2: Declare external-services**

Add `external-services:` entries (selector mode — the same pattern the validators use to reach MinIO) so SO creates headless Services routing Prometheus to the validator/relayer pods by selector + namespace.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/fixtures/test-spec-monitoring.yml
git commit -m "test(e2e): scrape validator/relayer in-cluster via external-services"
```

---

## Task 3: E2E assertions for cross-host scraping

**Files:**
- Modify: `tests/e2e/test_07_monitoring.py`

- [ ] **Step 1: Add target-up + instance-label tests**

Append to `TestMonitoring`:

```python
    def test_validator_targets_up(self, monitoring_deployment: dict) -> None:
        """Both validators are scraped cross-host (up == 1 per instance)."""
        prom_url = monitoring_deployment["prometheus_url"]
        results = _prometheus_query(prom_url, 'up{job="validators"}')
        instances = {
            r["metric"].get("hyperlane_instance"): r["value"][1] for r in results
        }
        assert instances.get("gorchain-primary") == "1", f"gorchain validator down: {instances}"
        assert instances.get("solana-primary") == "1", f"solana validator down: {instances}"

    def test_relayer_target_up(self, monitoring_deployment: dict) -> None:
        """Relayer is scraped cross-host (up == 1)."""
        prom_url = monitoring_deployment["prometheus_url"]
        results = _prometheus_query(prom_url, 'up{job="relayers"}')
        assert len(results) > 0, "No up series for job=relayers"
        assert results[0]["value"][1] == "1", f"relayer target down: {results}"

    def test_agent_metrics_have_instance_label(self, monitoring_deployment: dict) -> None:
        """Agent metrics carry the hyperlane_instance label from the scrape target."""
        prom_url = monitoring_deployment["prometheus_url"]
        # hyperlane_block_height is always emitted by a running validator;
        # checkpoint metrics only appear after bridge messages are processed.
        results = _prometheus_query(prom_url, 'hyperlane_block_height{agent="validator"}')
        assert len(results) > 0, "No validator metrics scraped"
        labels = {r["metric"].get("hyperlane_instance") for r in results}
        assert labels & {"gorchain-primary", "solana-primary"}, (
            f"hyperlane_instance label missing on agent metrics: {labels}"
        )
```

- [ ] **Step 2: Run against a kept cluster**

`xvfb-run pytest -v tests/e2e/test_07_monitoring.py -k "targets_up or relayer_target_up or instance_label" --skip-cleanup` → 3 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/test_07_monitoring.py
git commit -m "test(e2e): assert cross-host validator/relayer scrape targets are up"
```

---

## Task 4: Dashboards — per-instance breakdown

Each target carries a `hyperlane_instance` label, so every scraped metric gains it. Add a `hyperlane_instance` template variable to both dashboards and fold it into the groupings/selectors so multiple validators/relayers (including two on the same chain) render as separate series.

The instance variable JSON is identical for both:

```json
{
  "current": { "text": "All", "value": ["$__all"] },
  "definition": "label_values(hyperlane_instance)",
  "description": "Validator/relayer instance (e.g. gorchain-primary).",
  "includeAll": true,
  "label": "Instance",
  "multi": true,
  "name": "hyperlane_instance",
  "options": [],
  "query": {
    "qryType": 1,
    "query": "label_values(hyperlane_instance)",
    "refId": "PrometheusVariableQueryEditor-VariableQuery"
  },
  "refresh": 1,
  "regex": "",
  "type": "query"
}
```

### Validator dashboard

- [ ] **Step 1: Add the instance variable** to `templating.list` (alongside `chain`).
- [ ] **Step 2: Add the instance matcher to every selector**
  - Find: `chain=~"${chain:regex}"` → Replace: `chain=~"${chain:regex}", hyperlane_instance=~"${hyperlane_instance:regex}"`
- [ ] **Step 3: Add the instance to every grouping** (`pod` is not a label under cross-host scraping, so those panels move to chain+instance):
  - `by(chain)` → `by(chain, hyperlane_instance)`
  - `by (chain)` → `by (chain, hyperlane_instance)`
  - `by (pod)` → `by (chain, hyperlane_instance)`

### Relayer dashboard

- [ ] **Step 4: Add the instance variable** (the `templating.list` is `[]` → one-element array).
- [ ] **Step 5: Apply the relayer query replacements** (panels use `origin`/`remote`, no `chain` selector):
  - `sum by (origin,remote)(round(increase(hyperlane_messages_processed_count[5m])))` → `sum by (origin,remote,hyperlane_instance)(round(increase(hyperlane_messages_processed_count{hyperlane_instance=~"${hyperlane_instance:regex}"}[5m])))`
  - `sum by (remote, queue_name)(` → `sum by (remote, queue_name, hyperlane_instance)(`
  - `hyperlane_submitter_queue_length{queue_name="prepare_queue"}` → `hyperlane_submitter_queue_length{queue_name="prepare_queue", hyperlane_instance=~"${hyperlane_instance:regex}"}`
  - `sum by(remote, queue_name) (hyperlane_submitter_queue_length{queue_name="submit_queue"})` → `sum by(remote, queue_name, hyperlane_instance) (hyperlane_submitter_queue_length{queue_name="submit_queue", hyperlane_instance=~"${hyperlane_instance:regex}"})`
  - `sum by(remote, queue_name) (avg_over_time(hyperlane_submitter_queue_length{queue_name="confirm_queue"}[20m]))` → `sum by(remote, queue_name, hyperlane_instance) (avg_over_time(hyperlane_submitter_queue_length{queue_name="confirm_queue", hyperlane_instance=~"${hyperlane_instance:regex}"}[20m]))`

- [ ] **Step 6: Validate both dashboards** (`json.load` parses; validator vars `["chain","hyperlane_instance"]`; relayer vars `["hyperlane_instance"]`; no `by (pod)` left).

- [ ] **Step 7: Commit**

```bash
git add stack_orchestrator/data/config/grafana-dashboards-config/validator-dashboard.json \
        stack_orchestrator/data/config/grafana-dashboards-config/relayer-dashboard.json
git commit -m "feat(monitoring): break validator/relayer dashboards down by instance"
```

---

## Task 5: Update monitoring README

- [ ] **Step 1** Replace the pod-discovery description with the spec-driven, entrypoint-rendered cross-host model (targets from `PROMETHEUS_*_TARGETS`, rendered to file_sd on container start; `hyperlane_instance` per target; scheme from `PROMETHEUS_SCRAPE_SCHEME`).
- [ ] **Step 2** Commit: `docs(monitoring): document static cross-host scraping`.

---

## Final verification

- [ ] **Run the monitoring suite end-to-end**

`xvfb-run pytest -v tests/e2e/test_07_monitoring.py --skip-cleanup` → all pass, including the three cross-host assertions. Confirm in Grafana that the validator/relayer dashboards show an "Instance" variable and per-instance series.

---

## Notes / follow-ups

- `hyperlane-overview.json` aggregates across instances (e.g. `hyperlane_wallet_balance_sol` from Pushgateway has no `hyperlane_instance` label). Left as-is; revisit if it needs per-instance breakdown.
- The `prometheus.io/scrape` annotations on the validator/relayer specs are unused; a separate cleanup sweep can drop them.
- Metrics auth (basic auth on `/metrics`) is the next PR — see `docs/superpowers/research/2026-06-01-metrics-auth-caddy-ingress-findings.md`.
