"""E2E tests for the hyperlane-monitoring stack.

Verifies Prometheus scraping, Grafana provisioning, and balance monitor
operation: the underfunded watch fires a Slack alert (captured by a host-side
mock endpoint) while funded watches stay quiet.

Grafana and Prometheus are accessed via TLS ingress (grafana.test,
prometheus.test) using mkcert-trusted certificates (matches the prod Caddy
+ ACME flow with the cert source swapped).
"""

from __future__ import annotations

import json
import logging
import subprocess
from urllib.parse import quote

import pytest

log = logging.getLogger(__name__)

# Grafana test credentials (must match the secret created by the fixture)
GRAFANA_ADMIN_PASSWORD = "testadmin"


def _prometheus_query(prometheus_url: str, query: str) -> list[dict]:
    """Run a PromQL instant query via ingress and return the result vector."""
    result = subprocess.run(
        [
            "curl", "-s",
            f"{prometheus_url}/api/v1/query?query={quote(query)}",
        ],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        log.warning("PromQL query failed: %s", result.stderr)
        return []
    data = json.loads(result.stdout)
    if data.get("status") != "success":
        log.warning("PromQL query status: %s", data.get("status"))
        return []
    return data.get("data", {}).get("result", [])


def _grafana_api(
    grafana_url: str, path: str,
) -> tuple[int, dict | list | str]:
    """Call a Grafana API endpoint via ingress with Basic auth."""
    result = subprocess.run(
        [
            "curl", "-s",
            "-u", f"admin:{GRAFANA_ADMIN_PASSWORD}",
            f"{grafana_url}{path}",
        ],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        return result.returncode, result.stderr
    try:
        return 0, json.loads(result.stdout)
    except json.JSONDecodeError:
        return 0, result.stdout


def _get_container_logs(
    namespace: str, pod_name: str, container: str,
) -> str:
    """Get logs from a specific container."""
    result = subprocess.run(
        [
            "kubectl", "-n", namespace, "logs", pod_name,
            "-c", container,
        ],
        capture_output=True, text=True, timeout=30,
    )
    return result.stdout


@pytest.mark.slow
class TestMonitoring:
    """Monitoring stack tests.

    The monitoring_deployment fixture deploys Prometheus, Grafana, and a
    balance monitor. Tests verify each component is operational and that the
    balance monitor alerts Slack on the underfunded watch.
    """

    def test_prometheus_healthy(self, monitoring_deployment: dict) -> None:
        """Verify Prometheus health endpoint returns successfully."""
        prom_url = monitoring_deployment["prometheus_url"]

        result = subprocess.run(
            ["curl", "-s", f"{prom_url}/-/healthy"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, (
            f"Prometheus health check failed: {result.stderr}"
        )
        assert "Healthy" in result.stdout, (
            f"Unexpected Prometheus health response: {result.stdout}"
        )
        log.info("Prometheus is healthy")

    def test_prometheus_self_scrape(self, monitoring_deployment: dict) -> None:
        """Verify Prometheus is scraping itself (job='prometheus', up=1)."""
        prom_url = monitoring_deployment["prometheus_url"]

        results = _prometheus_query(prom_url, 'up{job="prometheus"}')
        assert len(results) > 0, "No results for up{job='prometheus'}"

        value = results[0]["value"][1]
        assert value == "1", (
            f"Prometheus self-scrape target is down (value={value})"
        )
        log.info("Prometheus self-scrape target is up")

    def test_grafana_healthy(self, monitoring_deployment: dict) -> None:
        """Verify Grafana health endpoint returns successfully."""
        grafana_url = monitoring_deployment["grafana_url"]

        result = subprocess.run(
            ["curl", "-s", f"{grafana_url}/api/health"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, (
            f"Grafana health check failed: {result.stderr}"
        )
        body = json.loads(result.stdout)
        assert body.get("database") == "ok", (
            f"Grafana database not ok: {body}"
        )
        log.info("Grafana is healthy")

    def test_grafana_login(self, monitoring_deployment: dict) -> None:
        """Verify Grafana login with injected admin password."""
        grafana_url = monitoring_deployment["grafana_url"]

        status, body = _grafana_api(grafana_url, "/api/org")
        assert status == 0, f"Grafana API call failed: {body}"
        assert isinstance(body, dict), f"Unexpected response type: {type(body)}"
        assert "id" in body, f"Grafana org response missing 'id': {body}"
        log.info("Grafana login successful (org id=%s)", body.get("id"))

    def test_grafana_datasource_configured(self, monitoring_deployment: dict) -> None:
        """Verify Grafana has a Prometheus datasource provisioned."""
        grafana_url = monitoring_deployment["grafana_url"]

        status, body = _grafana_api(grafana_url, "/api/datasources")
        assert status == 0, f"Grafana datasources API failed: {body}"
        assert isinstance(body, list), f"Expected list, got: {type(body)}"

        prometheus_ds = [ds for ds in body if ds.get("type") == "prometheus"]
        assert len(prometheus_ds) > 0, (
            f"No Prometheus datasource found. Datasources: {body}"
        )
        log.info(
            "Grafana has %d Prometheus datasource(s)", len(prometheus_ds),
        )

    def test_grafana_dashboards_provisioned(self, monitoring_deployment: dict) -> None:
        """Verify all three dashboards are provisioned in Grafana."""
        grafana_url = monitoring_deployment["grafana_url"]

        status, body = _grafana_api(grafana_url, "/api/search")
        assert status == 0, f"Grafana search API failed: {body}"
        assert isinstance(body, list), f"Expected list, got: {type(body)}"

        dashboard_uids = {d.get("uid") for d in body if d.get("type") == "dash-db"}
        expected_uids = {"hyperlane-overview", "hyperlane-validator", "hyperlane-relayer"}

        missing = expected_uids - dashboard_uids
        assert not missing, (
            f"Missing dashboards: {missing}. Found UIDs: {dashboard_uids}"
        )
        log.info("All %d expected dashboards provisioned", len(expected_uids))

    def test_balance_monitor_started(self, monitoring_deployment: dict) -> None:
        """Balance monitor started and reported its watch counts."""
        ns = monitoring_deployment["namespace"]
        pod = monitoring_deployment["pod_name"]
        logs = _get_container_logs(ns, pod, "balance-monitor")
        assert "[balance-monitor] Starting" in logs, "balance monitor did not start"
        assert "account(s)," in logs, "balance monitor did not report watch counts"
        log.info("Balance monitor started and reported watches")

    def test_balance_monitor_alerts_low_wallet(self, monitoring_deployment: dict) -> None:
        """The underfunded watch alerts; funded ones stay quiet.

        Fresh deploy: assert the alert POST captured by the mock Slack webhook
        (end-to-end delivery). On reuse (--skip-monitoring-deploy) the one-shot
        alert already fired and can't be recaptured, so verify the monitor's
        persisted [WARNING] breach logs instead.
        """
        low_labels = monitoring_deployment["low_labels"]

        if monitoring_deployment.get("reused"):
            logs = _get_container_logs(
                monitoring_deployment["namespace"],
                monitoring_deployment["pod_name"],
                "balance-monitor",
            )
            warnings = "\n".join(
                ln for ln in logs.splitlines() if "[WARNING]" in ln
            )
            assert warnings, "no low-balance WARNING in balance-monitor logs"
            for label in low_labels:
                assert label in warnings, f"no breach logged for {label!r}: {warnings}"
            assert "relayer" not in warnings, (
                f"funded relayer should not breach: {warnings}"
            )
            log.info("Balance monitor logged breaches for: %s", low_labels)
            return

        payloads = monitoring_deployment["slack_payloads"]
        assert payloads, "no Slack alert captured for the underfunded wallet"
        text = "\n".join(p.get("text", "") for p in payloads)
        for label in low_labels:
            assert label in text, f"alert missing low wallet {label!r}: {text}"
        assert "relayer" not in text, f"funded relayer should not alert: {text}"
        log.info("Slack alert fired for: %s", low_labels)

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

        results = _prometheus_query(prom_url, 'up{job="relayers"}')
        assert len(results) > 0, "No up series for job=relayers"
        assert results[0]["value"][1] == "1", f"relayer target down: {results}"
        log.info("Relayer scrape target up")

    def test_agent_metrics_have_instance_label(self, monitoring_deployment: dict) -> None:
        """Agent metrics carry the hyperlane_instance label from the scrape target."""
        prom_url = monitoring_deployment["prometheus_url"]

        # hyperlane_block_height is always emitted by a running validator;
        # checkpoint metrics only appear after bridge messages are processed.
        results = _prometheus_query(
            prom_url, 'hyperlane_block_height{agent="validator"}',
        )
        assert len(results) > 0, "No validator metrics scraped"
        labels = {r["metric"].get("hyperlane_instance") for r in results}
        assert labels & {"gorchain-primary", "solana-primary"}, (
            f"hyperlane_instance label missing on agent metrics: {labels}"
        )
        log.info("Agent metrics carry hyperlane_instance: %s", labels)
