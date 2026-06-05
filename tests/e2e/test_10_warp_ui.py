"""Phase 4 — Tier 1: Warp UI HTTP smoke tests.

Validates the warp-ui pod is healthy and serves correctly configured content
via Caddy TLS ingress (mkcert-trusted certificate).
"""

import logging
import re
import subprocess

import pytest

log = logging.getLogger(__name__)

# DNS resolution is handled by /etc/hosts (bridge.test -> 127.0.0.1),
# added by ensure_hosts_entry() during cluster setup.


def _curl_warp_ui(url: str, path: str = "/") -> subprocess.CompletedProcess[str]:
    """Fetch a path from the warp-ui via TLS ingress.

    Uses -s (silent) but NOT -f, so we always get the response body for
    debugging. No -k needed: mkcert CA is trusted by the host.
    """
    return subprocess.run(
        ["curl", "-s", "-w", "\n%{http_code}", f"{url}{path}"],
        capture_output=True, text=True, check=False,
    )


def _assert_curl_ok(result: subprocess.CompletedProcess[str]) -> str:
    """Assert curl succeeded with HTTP 200 and return the response body."""
    lines = result.stdout.rsplit("\n", 1)
    body = lines[0] if len(lines) > 1 else result.stdout
    status = lines[1].strip() if len(lines) > 1 else "unknown"
    assert result.returncode == 0 and status == "200", (
        f"Expected HTTP 200, got {status} (curl exit {result.returncode}): "
        f"{body[:500]}"
    )
    return body


@pytest.mark.slow
class TestWarpUI:
    """HTTP-level smoke tests for the warp-ui deployment."""

    def test_warp_ui_pod_healthy(self, warp_ui_deployment: dict) -> None:
        """Verify the warp-ui pod is Running."""
        ns = warp_ui_deployment["deployment"].namespace
        deployment_id = warp_ui_deployment["deployment"].deployment_id
        result = subprocess.run(
            [
                "kubectl", "-n", ns, "get", "pods",
                "-l", f"app={deployment_id}",
                "-o", "jsonpath={.items[0].status.phase}",
            ],
            capture_output=True, text=True, check=True,
        )
        assert result.stdout.strip() == "Running"

    def test_warp_ui_tls_ingress(self, warp_ui_deployment: dict) -> None:
        """Verify TLS ingress serves the warp-ui with a valid mkcert-trusted certificate."""
        url = warp_ui_deployment["url"]

        # Verify HTTPS works
        result = _curl_warp_ui(url)
        _assert_curl_ok(result)

        # Verify TLS certificate info is present
        cert_result = subprocess.run(
            ["curl", "-v", url + "/", "-o", "/dev/null"],
            capture_output=True, text=True, check=False,
        )
        # curl -v prints cert info to stderr
        cert_info = cert_result.stderr
        assert "SSL certificate" in cert_info or "subject:" in cert_info or "issuer:" in cert_info, (
            "No TLS certificate info in curl output"
        )
        log.info("TLS ingress verified at %s", url)

    def test_warp_ui_serves_html(self, warp_ui_deployment: dict) -> None:
        """GET / returns HTTP 200 with HTML content via TLS."""
        url = warp_ui_deployment["url"]
        result = _curl_warp_ui(url)
        body = _assert_curl_ok(result)
        html_lower = body.lower()
        assert "<html" in html_lower or "<!doctype" in html_lower, (
            "Response does not contain HTML"
        )

    def test_warp_ui_serves_runtime_config(self, warp_ui_deployment: dict) -> None:
        """Verify the app serves warpRoutes.yaml and chains.yaml with both routes."""
        url = warp_ui_deployment["url"]

        routes_result = _curl_warp_ui(url, "/warpRoutes.yaml")
        routes_body = _assert_curl_ok(routes_result)
        # Both token standards must appear (collateral/native → synthetic)
        assert "SealevelHypCollateral" in routes_body, "USDC collateral standard missing"
        assert "SealevelHypNative" in routes_body, "SOL native standard missing"
        # Each of the 2 routes has 2 chain entries → at least 4 chainName occurrences
        assert routes_body.count("chainName") >= 4, (
            f"Expected >=4 chainName entries, got {routes_body.count('chainName')}"
        )

        chains_result = _curl_warp_ui(url, "/chains.yaml")
        chains_body = _assert_curl_ok(chains_result)
        assert "gorchain" in chains_body, "gorchain missing from chains.yaml"
        assert "solana" in chains_body, "solana missing from chains.yaml"

    def test_warp_ui_chain_config_present(self, warp_ui_deployment: dict) -> None:
        """Verify served JS bundles contain actual chain config values."""
        url = warp_ui_deployment["url"]
        mailbox = warp_ui_deployment["gorchain_mailbox"]

        # Chain config is compiled into JS bundles (client-side rendered),
        # not in the initial HTML shell. Fetch bundles and check.
        result = _curl_warp_ui(url)
        html = _assert_curl_ok(result)

        js_urls = re.findall(r'src="(/_next/static/[^"]+\.js)"', html)
        assert js_urls, "No JS bundles found in HTML"

        # Concatenate JS bundle contents and check for chain config
        all_js = ""
        for js_url in js_urls[:5]:
            js_result = _curl_warp_ui(url, js_url)
            all_js += _assert_curl_ok(js_result)

        assert "gorchain" in all_js.lower() or mailbox[:8] in all_js, (
            "Chain config not found in any JS bundle"
        )
