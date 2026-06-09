"""Probe each Ingress URL to catch http-proxy: drift or Caddy misconfig.

These don't replace functional tests (grafana dashboards, prometheus queries,
warp-ui Playwright) — they're the safety net for Ingress wiring itself.
"""
from __future__ import annotations

import subprocess
import time


def _wait_for_https(url: str, expected_status: int = 200, timeout: int = 60) -> int:
    """Probe url every 2s until expected_status arrives or timeout elapses.
    Returns the last observed HTTP status code."""
    deadline = time.time() + timeout
    last = -1
    while time.time() < deadline:
        result = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", url],
            capture_output=True, text=True, check=False,
        )
        if result.returncode == 0:
            try:
                last = int(result.stdout.strip())
            except ValueError:
                last = -1
            if last == expected_status:
                return last
        time.sleep(2)
    return last


def test_warp_ui_ingress(warp_ui_deployment):
    status = _wait_for_https("https://bridge.test/")
    assert status == 200, f"warp-ui Ingress not serving 200 (got {status})"


def test_grafana_ingress(monitoring_deployment):
    status = _wait_for_https("https://grafana.test/api/health")
    assert status == 200, f"grafana Ingress not serving 200 (got {status})"


def test_prometheus_ingress(monitoring_deployment):
    status = _wait_for_https("https://prometheus.test/-/healthy")
    assert status == 200, f"prometheus Ingress not serving 200 (got {status})"


def test_validator_gorchain_metrics_ingress(validator_gorchain):
    status = _wait_for_https("https://validator-gorchain.test/metrics")
    assert status == 200, (
        f"validator-gorchain metrics Ingress not serving 200 (got {status})"
    )


def test_validator_solana_metrics_ingress(validator_solana):
    status = _wait_for_https("https://validator-solana.test/metrics")
    assert status == 200, (
        f"validator-solana metrics Ingress not serving 200 (got {status})"
    )


def test_relayer_metrics_ingress(relayer_deployment):
    status = _wait_for_https("https://relayer.test/metrics")
    assert status == 200, f"relayer metrics Ingress not serving 200 (got {status})"


def test_minio_s3_ingress(minio_deployment):
    # Unauthenticated GET / returns 403 from MinIO's S3 API — confirms Caddy
    # is routing to minio:9000. Use /minio/health/live for a 200-based probe.
    status = _wait_for_https("https://minio-s3.test/minio/health/live")
    assert status == 200, f"minio S3 API Ingress not serving 200 (got {status})"


def test_minio_console_ingress(minio_deployment):
    status = _wait_for_https("https://minio-console.test/minio/health/cluster")
    assert status == 200, f"minio console Ingress not serving 200 (got {status})"
