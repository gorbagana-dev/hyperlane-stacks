# Cross-Host Monitoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Switch Prometheus from in-cluster pod discovery to static cross-host scraping of validator/relayer `/metrics` over public DNS, and make the dashboards break data down per validator/relayer instance.

**Architecture:** Replace the `kubernetes_sd_configs` job in `prometheus.yml` with static HTTPS targets pointing at the validators'/relayer's existing Caddy hostnames; tag each target with a `hyperlane_instance` label. The committed config holds prod hostnames; the e2e harness rewrites them to the `.test` equivalents (already covered by the mkcert cert + `/etc/hosts`) and disables TLS verification for mkcert. Dashboards gain a `hyperlane_instance` template variable and group by it.

**Tech Stack:** Prometheus (static_configs), Grafana dashboards (JSON), laconic-so k8s-kind deploy, pytest e2e.

**Scope:** Facets 1 (cross-host scraping) + 2 (multiple validators/relayers). Metrics auth is a separate PR (see `docs/superpowers/specs/2026-05-30-cross-host-monitoring-design.md`).

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `stack_orchestrator/data/config/prometheus-config/prometheus.yml` | Prometheus scrape config | Replace `kubernetes-pods` job with static `validators` + `relayer` jobs |
| `tests/e2e/conftest.py` | E2E monitoring fixture | Patch the prometheus-config ConfigMap copy to use `.test` hostnames + skip TLS verify |
| `tests/e2e/test_07_monitoring.py` | E2E assertions | Assert validator/relayer targets are `up` and carry the `hyperlane_instance` label |
| `stack_orchestrator/data/config/grafana-dashboards-config/validator-dashboard.json` | Validator dashboard | Add `hyperlane_instance` variable; group/filter by it |
| `stack_orchestrator/data/config/grafana-dashboards-config/relayer-dashboard.json` | Relayer dashboard | Add `hyperlane_instance` variable; group/filter by it |
| `stack_orchestrator/data/stacks/hyperlane-monitoring/README.md` | Docs | Document static-target scraping |

---

## Task 1: Static cross-host scrape config

**Files:**
- Modify: `stack_orchestrator/data/config/prometheus-config/prometheus.yml`

- [ ] **Step 1: Replace the `kubernetes-pods` job with static targets**

Replace the entire block from the `  # Kubernetes pod discovery` comment through the end of the file (the `kubernetes-pods` job) with:

```yaml
  # Validator metrics — scraped cross-host over public DNS via each
  # validator's Caddy hostname. Static list; append one target per validator.
  # hyperlane_instance distinguishes validators (incl. multiple on one chain).
  - job_name: validators
    scheme: https
    metrics_path: /metrics
    # insecure_skip_verify is false in prod (Let's Encrypt certs are trusted);
    # the e2e harness flips it to true for mkcert certs.
    tls_config:
      insecure_skip_verify: false
    static_configs:
      - targets: ["validator-gorchain.bridge.gorbagana.wtf:443"]
        labels: { hyperlane_instance: gorchain-primary }
      - targets: ["validator-solana.bridge.gorbagana.wtf:443"]
        labels: { hyperlane_instance: solana-primary }

  # Relayer metrics — one relayer per bridge in v1.
  - job_name: relayer
    scheme: https
    metrics_path: /metrics
    tls_config:
      insecure_skip_verify: false
    static_configs:
      - targets: ["relayer.bridge.gorbagana.wtf:443"]
        labels: { hyperlane_instance: primary }
```

Leave the `global`, `rule_files`, `prometheus` (self), and `pushgateway` jobs unchanged.

- [ ] **Step 2: Validate the YAML parses**

Run: `python3 -c "import yaml; yaml.safe_load(open('stack_orchestrator/data/config/prometheus-config/prometheus.yml')); print('ok')"`
Expected: `ok`

- [ ] **Step 3: Confirm the old discovery job is gone**

Run: `grep -c kubernetes_sd_configs stack_orchestrator/data/config/prometheus-config/prometheus.yml`
Expected: `0`

- [ ] **Step 4: Commit**

```bash
git add stack_orchestrator/data/config/prometheus-config/prometheus.yml
git commit -m "feat(monitoring): scrape validators/relayer via static cross-host targets"
```

---

## Task 2: E2E harness — point Prometheus at `.test` hostnames

The committed `prometheus.yml` carries prod hostnames. The monitoring fixture copies configmaps into the deploy dir during `deploy_prepare`; patch that copy before `deploy_start` so the test scrapes the `.test` ingress hostnames (already in the mkcert cert + `/etc/hosts`) and skips TLS verification for mkcert.

**Files:**
- Modify: `tests/e2e/conftest.py`

- [ ] **Step 1: Add a prometheus.yml patch helper**

Add this module-level function in `conftest.py`, just after the monitoring URL constants (after the `PROMETHEUS_URL = ...` line, ~line 1377):

```python
def _patch_prometheus_targets_for_test(deploy_dir: Path) -> None:
    """Rewrite the prometheus-config ConfigMap in a prepared deploy dir to use
    the local `.test` ingress hostnames and skip TLS verification (mkcert).

    The committed prometheus.yml holds prod hostnames; in the single-host e2e
    cluster the validators/relayer are reachable via their Caddy `.test`
    hostnames (already in the mkcert SANs + /etc/hosts).
    """
    prom_yml = deploy_dir / "configmaps" / "prometheus-config" / "prometheus.yml"
    text = prom_yml.read_text()
    text = text.replace(".bridge.gorbagana.wtf", ".test")
    text = text.replace(
        "insecure_skip_verify: false", "insecure_skip_verify: true"
    )
    prom_yml.write_text(text)
    log.info("Patched prometheus.yml targets to .test hostnames")
```

- [ ] **Step 2: Call the helper in the monitoring fixture**

In the `monitoring_deployment` fixture, immediately after the `deploy_prepare(...)` call assigns `deploy_info` (after the line `deployment_id="monitoring",` / its closing `)`, ~line 1518) and before `bridge_state_loader.populate("hyperlane-monitoring", deploy_info.deploy_dir)` (~line 1520), insert:

```python
    _patch_prometheus_targets_for_test(deploy_info.deploy_dir)
```

- [ ] **Step 3: Verify the patch logic without a cluster**

Run:
```bash
python3 - <<'EOF'
import pathlib, yaml
src = pathlib.Path("stack_orchestrator/data/config/prometheus-config/prometheus.yml").read_text()
patched = src.replace(".bridge.gorbagana.wtf", ".test").replace("insecure_skip_verify: false", "insecure_skip_verify: true")
doc = yaml.safe_load(patched)
jobs = {j["job_name"]: j for j in doc["scrape_configs"]}
assert jobs["validators"]["static_configs"][0]["targets"] == ["validator-gorchain.test:443"], jobs["validators"]
assert jobs["validators"]["tls_config"]["insecure_skip_verify"] is True
assert jobs["relayer"]["static_configs"][0]["targets"] == ["relayer.test:443"]
print("ok")
EOF
```
Expected: `ok`

- [ ] **Step 4: Lint the changed file**

Run: `ruff check tests/e2e/conftest.py`
Expected: no errors on the added lines.

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/conftest.py
git commit -m "test(e2e): point Prometheus scrape targets at .test ingress hostnames"
```

---

## Task 3: E2E assertions for cross-host scraping

**Files:**
- Modify: `tests/e2e/test_07_monitoring.py`

- [ ] **Step 1: Add target-up + instance-label tests**

Append these methods to the `TestMonitoring` class in `tests/e2e/test_07_monitoring.py`:

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
        log.info("Validator scrape targets up: %s", instances)

    def test_relayer_target_up(self, monitoring_deployment: dict) -> None:
        """Relayer is scraped cross-host (up == 1)."""
        prom_url = monitoring_deployment["prometheus_url"]

        results = _prometheus_query(prom_url, 'up{job="relayer"}')
        assert len(results) > 0, "No up series for job=relayer"
        assert results[0]["value"][1] == "1", f"relayer target down: {results}"
        log.info("Relayer scrape target up")

    def test_agent_metrics_have_instance_label(self, monitoring_deployment: dict) -> None:
        """Agent metrics carry the hyperlane_instance label from the scrape target."""
        prom_url = monitoring_deployment["prometheus_url"]

        results = _prometheus_query(
            prom_url, 'hyperlane_latest_checkpoint{agent="validator"}',
        )
        assert len(results) > 0, "No validator checkpoint metrics scraped"
        labels = {r["metric"].get("hyperlane_instance") for r in results}
        assert labels & {"gorchain-primary", "solana-primary"}, (
            f"hyperlane_instance label missing on agent metrics: {labels}"
        )
        log.info("Agent metrics carry hyperlane_instance: %s", labels)
```

- [ ] **Step 2: Run the new tests against a kept cluster**

Run: `xvfb-run pytest -v tests/e2e/test_07_monitoring.py -k "targets_up or relayer_target_up or instance_label" --skip-cleanup`
Expected: 3 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/test_07_monitoring.py
git commit -m "test(e2e): assert cross-host validator/relayer scrape targets are up"
```

---

## Task 4: Dashboards — per-instance breakdown

Each static target carries a `hyperlane_instance` label, so every scraped metric
gains it. Add a `hyperlane_instance` template variable to both dashboards and fold
it into the groupings/selectors so multiple validators/relayers (including two on
the same chain) render as separate series.

The two dashboards have **different structures** (verified), so the edits differ:
- `validator-dashboard.json`: 1 variable (`chain`); panels group `by(chain)` /
  `by (chain)` (and two by `(pod)`) with `chain=~"${chain:regex}"` selectors.
- `relayer-dashboard.json`: **no** variable; panels group by `origin`/`remote`
  with **no** `chain` selector.

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

- [ ] **Step 1: Add the instance variable**

In `validator-dashboard.json`, the `templating.list` array has one object (the
`chain` variable). Append the instance variable object above to that array.

- [ ] **Step 2: Add the instance matcher to every selector**

All 8 validator panels include `chain=~"${chain:regex}"`. Apply this exact
replacement across the whole file:

- Find:    `chain=~"${chain:regex}"`
- Replace: `chain=~"${chain:regex}", hyperlane_instance=~"${hyperlane_instance:regex}"`

- [ ] **Step 3: Add the instance to every grouping**

Apply these three exact replacements across the whole file (covers the
`by(chain)`, `by (chain)`, and the two `by (pod)` panels — `pod` is not a label
under static scraping, so those move to chain+instance):

- Find: `by(chain)`  → Replace: `by(chain, hyperlane_instance)`
- Find: `by (chain)` → Replace: `by (chain, hyperlane_instance)`
- Find: `by (pod)`   → Replace: `by (chain, hyperlane_instance)`

### Relayer dashboard

- [ ] **Step 4: Add the instance variable**

In `relayer-dashboard.json`, the `templating.list` array is **empty** (`[]`).
Set it to a one-element array containing the instance variable object above.

- [ ] **Step 5: Apply the four exact panel-query replacements**

The relayer panels use `origin`/`remote` labels and have no `chain` selector, so
edit each query explicitly. Apply these four exact replacements:

- Find:    `sum by (origin,remote)(round(increase(hyperlane_messages_processed_count[5m])))`
- Replace: `sum by (origin,remote,hyperlane_instance)(round(increase(hyperlane_messages_processed_count{hyperlane_instance=~"${hyperlane_instance:regex}"}[5m])))`

- Find:    `sum by (remote, queue_name)(`
- Replace: `sum by (remote, queue_name, hyperlane_instance)(`

- Find:    `hyperlane_submitter_queue_length{queue_name="prepare_queue"}`
- Replace: `hyperlane_submitter_queue_length{queue_name="prepare_queue", hyperlane_instance=~"${hyperlane_instance:regex}"}`

- Find:    `sum by(remote, queue_name) (hyperlane_submitter_queue_length{queue_name="submit_queue"})`
- Replace: `sum by(remote, queue_name, hyperlane_instance) (hyperlane_submitter_queue_length{queue_name="submit_queue", hyperlane_instance=~"${hyperlane_instance:regex}"})`

- Find:    `sum by(remote, queue_name) (avg_over_time(hyperlane_submitter_queue_length{queue_name="confirm_queue"}[20m]))`
- Replace: `sum by(remote, queue_name, hyperlane_instance) (avg_over_time(hyperlane_submitter_queue_length{queue_name="confirm_queue", hyperlane_instance=~"${hyperlane_instance:regex}"}[20m]))`

- [ ] **Step 6: Validate both dashboards**

Run:
```bash
python3 - <<'EOF'
import json
# Validator
d = json.load(open("stack_orchestrator/data/config/grafana-dashboards-config/validator-dashboard.json"))
assert [v["name"] for v in d["templating"]["list"]] == ["chain", "hyperlane_instance"]
blob = json.dumps(d)
assert 'hyperlane_instance=~"${hyperlane_instance:regex}"' in blob
assert "by(chain, hyperlane_instance)" in blob and "by (chain, hyperlane_instance)" in blob
assert '"by (pod)"' not in blob and "by (pod)" not in blob
print("validator ok")
# Relayer
r = json.load(open("stack_orchestrator/data/config/grafana-dashboards-config/relayer-dashboard.json"))
assert [v["name"] for v in r["templating"]["list"]] == ["hyperlane_instance"]
rblob = json.dumps(r)
assert rblob.count('hyperlane_instance=~"${hyperlane_instance:regex}"') == 4
assert "origin,remote,hyperlane_instance" in rblob
print("relayer ok")
EOF
```
Expected: `validator ok` / `relayer ok`

- [ ] **Step 7: Commit**

```bash
git add stack_orchestrator/data/config/grafana-dashboards-config/validator-dashboard.json \
        stack_orchestrator/data/config/grafana-dashboards-config/relayer-dashboard.json
git commit -m "feat(monitoring): break validator/relayer dashboards down by instance"
```

---

## Task 5: Update monitoring README

**Files:**
- Modify: `stack_orchestrator/data/stacks/hyperlane-monitoring/README.md`

- [ ] **Step 1: Replace the pod-discovery description**

Find the bullet describing Kubernetes pod discovery:

> 1. **Validator/relayer metrics**: Prometheus discovers pods with `prometheus.io/scrape: "true"` annotation via `kubernetes_sd_configs` and scrapes their `/metrics` endpoints directly

Replace it with:

```markdown
1. **Validator/relayer metrics**: Prometheus scrapes each validator/relayer
   `/metrics` endpoint over its public Caddy hostname (static targets in
   `prometheus.yml`, `job_name: validators` / `relayer`). Each target carries a
   `hyperlane_instance` label so multiple validators (including two on the same
   chain) appear as distinct series. Add a validator by appending one target
   entry. (Cross-host scraping replaced the former in-cluster pod discovery;
   the pod-discovery RBAC in `deploy/rbac.yaml` is now unused.)
```

- [ ] **Step 2: Commit**

```bash
git add stack_orchestrator/data/stacks/hyperlane-monitoring/README.md
git commit -m "docs(monitoring): document static cross-host scraping"
```

---

## Final verification

- [ ] **Run the monitoring suite end-to-end**

Run: `xvfb-run pytest -v tests/e2e/test_07_monitoring.py --skip-cleanup`
Expected: all tests pass, including the three new cross-host assertions. Confirm in Grafana that the validator/relayer dashboards show an "Instance" variable and per-instance series.

---

## Notes / follow-ups

- The pod-discovery RBAC (`hyperlane-monitoring/deploy/rbac.yaml`, applied by `deploy/commands.py`) is now unused. Leaving it is harmless; removing it is unrelated cleanup for a later sweep.
- `hyperlane-overview.json` has no template variables and aggregates across instances (e.g. `hyperlane_wallet_balance_sol` from Pushgateway has no `hyperlane_instance` label). Left as-is; revisit if it needs per-instance breakdown.
- Metrics auth (Caddy basic_auth on `/metrics`) is the next PR — see the design spec.
