"""HTTP-level smoke tests for the hyperlane-explorer deployment.

Verifies the frontend serves over TLS ingress and its same-origin /api/graphql
proxy reaches in-cluster Hasura (which tracks the scraper-created message_view +
domain). A full bridge-message → message_view assertion is left to a follow-up
(mirrors test_13_warp_ui_bridge — it needs a sent message + relay wait).
"""

import subprocess
import time

import pytest


@pytest.mark.slow
class TestExplorer:
    """HTTP-level smoke tests for the explorer deployment."""

    def test_explorer_pods_running(self, explorer_deployment: dict) -> None:
        """The explorer namespace has running pods and none have failed."""
        ns = explorer_deployment["deployment"].namespace
        result = subprocess.run(
            ["kubectl", "-n", ns, "get", "pods",
             "-o", "jsonpath={.items[*].status.phase}"],
            capture_output=True, text=True, check=True,
        )
        phases = result.stdout.split()
        assert phases, "no pods found in the explorer namespace"
        assert "Running" in phases
        assert "Failed" not in phases

    def test_explorer_serves_html(self, explorer_deployment: dict) -> None:
        """GET / returns HTTP 200 with HTML content via TLS ingress."""
        url = explorer_deployment["url"]
        result = subprocess.run(
            ["curl", "-fsS", url + "/"], capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0, f"curl failed: {result.stderr}"
        body = result.stdout.lower()
        assert "<html" in body or "<!doctype" in body

    def test_graphql_proxy_resolves_domain(self, explorer_deployment: dict) -> None:
        """The same-origin /api/graphql proxy reaches Hasura and the domain table.

        The scraper seeds gorchain + solana domain rows, so the domain query
        returns them once Hasura has tracked the table. Hasura settles after the
        frontend ingress comes up (it restarts until the scraper creates the
        schema), so poll rather than asserting on a single request.
        """
        url = explorer_deployment["url"]
        deadline = time.monotonic() + 120
        last = ""
        while time.monotonic() < deadline:
            result = subprocess.run(
                ["curl", "-fsS", url + "/api/graphql",
                 "-H", "content-type: application/json",
                 "-d", '{"query":"{ domain { id name } }"}'],
                capture_output=True, text=True, check=False,
            )
            if result.returncode == 0 and '"domain"' in result.stdout:
                return
            last = result.stderr or result.stdout
            time.sleep(5)
        pytest.fail(f"/api/graphql did not resolve domain within 120s; last: {last}")
