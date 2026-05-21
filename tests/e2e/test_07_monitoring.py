"""E2E tests for the hyperlane-monitoring stack.

Verifies Prometheus scraping, Grafana provisioning, Pushgateway metrics flow,
and balance monitor operation with real wallet addresses.

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

    The monitoring_deployment fixture deploys Prometheus, Grafana, Pushgateway,
    and a balance monitor. Tests verify each component is operational and that
    the full metrics pipeline works end-to-end.
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

    def test_prometheus_pushgateway_target(self, monitoring_deployment: dict) -> None:
        """Verify Pushgateway scrape target is up."""
        prom_url = monitoring_deployment["prometheus_url"]

        results = _prometheus_query(prom_url, 'up{job="pushgateway"}')
        assert len(results) > 0, "No results for up{job='pushgateway'}"

        value = results[0]["value"][1]
        assert value == "1", (
            f"Pushgateway scrape target is down (value={value})"
        )
        log.info("Pushgateway scrape target is up")

    def test_prometheus_has_balance_metrics(self, monitoring_deployment: dict) -> None:
        """Verify balance monitor metrics are flowing through Pushgateway."""
        prom_url = monitoring_deployment["prometheus_url"]

        results = _prometheus_query(
            prom_url, 'hyperlane_wallet_balance_sol{chain="gorchain"}',
        )
        assert len(results) > 0, (
            "No balance metrics found for gorchain — balance monitor may not "
            "have pushed to Pushgateway yet"
        )
        log.info(
            "Found %d balance metric(s) for gorchain", len(results),
        )

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

    def test_balance_monitor_wallets_checked(self, monitoring_deployment: dict) -> None:
        """Verify balance monitor checked all configured wallets."""
        ns = monitoring_deployment["namespace"]
        pod = monitoring_deployment["pod_name"]

        logs = _get_container_logs(ns, pod, "balance-monitor")
        assert "[balance-monitor] Starting" in logs, (
            "Balance monitor did not start — check container logs"
        )

        # Verify wallet counts reported in startup log
        assert "Gorchain wallets:" in logs, (
            "Balance monitor did not report Gorchain wallets"
        )
        assert "Solana wallets:" in logs, (
            "Balance monitor did not report Solana wallets"
        )
        log.info("Balance monitor started and reported wallet counts")

    def test_balance_metrics_have_correct_labels(
        self, monitoring_deployment: dict,
    ) -> None:
        """Verify balance metrics have correct chain, wallet, address labels."""
        prom_url = monitoring_deployment["prometheus_url"]
        expected_wallets = monitoring_deployment["expected_wallet_labels"]

        for chain in ("gorchain", "solana"):
            results = _prometheus_query(
                prom_url,
                f'hyperlane_wallet_balance_sol{{chain="{chain}"}}',
            )
            assert len(results) > 0, (
                f"No balance metrics for chain={chain}"
            )

            found_labels = {r["metric"].get("wallet") for r in results}
            for label in expected_wallets:
                assert label in found_labels, (
                    f"Wallet label '{label}' not found in {chain} metrics. "
                    f"Found: {found_labels}"
                )

            # Verify address label is present (non-empty)
            for r in results:
                assert r["metric"].get("address"), (
                    f"Missing address label for wallet={r['metric'].get('wallet')} "
                    f"on chain={chain}"
                )

        log.info("Balance metrics have correct labels on both chains")
