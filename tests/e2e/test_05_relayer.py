"""E2E tests for the hyperlane-relayer stack.

Tests verify:
- Relayer pod is running with all containers healthy
- Agent config was loaded via init container
- Prometheus metrics endpoint is serving
- No fatal errors in logs
- IGP fee claim sidecar is running
- Relayer is connecting to checkpoint storage (MinIO)
"""

from __future__ import annotations

import logging

import pytest
from conftest import RelayerInfo

from lib.common import PortForward, run_cmd

log = logging.getLogger(__name__)


@pytest.mark.slow
class TestRelayer:
    """Tests for the hyperlane-relayer stack."""

    def test_relayer_pod_running(self, relayer_deployment: RelayerInfo) -> None:
        """Relayer pod reaches Running phase."""
        ns = relayer_deployment.namespace
        cluster_id = relayer_deployment.cluster_id

        result = run_cmd([
            "kubectl", "get", "pods",
            "-n", ns,
            "-l", f"app={cluster_id}",
            "-o", "jsonpath={.items[0].status.phase}",
        ])
        assert result.stdout.strip() == "Running", (
            f"Expected relayer pod phase 'Running', got '{result.stdout.strip()}'"
        )

    def test_all_containers_ready(self, relayer_deployment: RelayerInfo) -> None:
        """All containers in the relayer pod are ready."""
        ns = relayer_deployment.namespace
        cluster_id = relayer_deployment.cluster_id

        result = run_cmd([
            "kubectl", "get", "pods",
            "-n", ns,
            "-l", f"app={cluster_id}",
            "-o", "jsonpath={.items[0].status.containerStatuses[*].ready}",
        ])
        statuses = result.stdout.strip().split()
        assert len(statuses) >= 2, f"Expected at least 2 containers, got {len(statuses)}"
        for status in statuses:
            assert status == "true", f"Container not ready: {result.stdout}"

    def test_relayer_agent_config_loaded(self, relayer_deployment: RelayerInfo) -> None:
        """Relayer loaded the agent config (no config file errors in logs)."""
        ns = relayer_deployment.namespace
        cluster_id = relayer_deployment.cluster_id

        result = run_cmd([
            "kubectl", "logs",
            "-n", ns,
            "-l", f"app={cluster_id}",
            "-c", "relayer",
            "--tail=100",
        ])
        logs = result.stdout
        assert "No such file or directory" not in logs, "Relayer cannot find config file"
        assert "config file not found" not in logs.lower(), "Relayer config file not found"

    def test_relayer_metrics_endpoint(self, relayer_deployment: RelayerInfo) -> None:
        """Relayer exposes Prometheus metrics on port 9091."""
        ns = relayer_deployment.namespace
        cluster_id = relayer_deployment.cluster_id

        result = run_cmd([
            "kubectl", "get", "pods",
            "-n", ns,
            "-l", f"app={cluster_id}",
            "-o", "jsonpath={.items[0].metadata.name}",
        ])
        pod_name = result.stdout.strip()
        assert pod_name, "Could not find relayer pod"

        local_port = 19092
        with PortForward(ns, f"pod/{pod_name}", local_port, 9091):
            result = run_cmd([
                "curl", "-sf", f"http://localhost:{local_port}/metrics",
            ])
            metrics = result.stdout
            assert "# HELP" in metrics, "Metrics endpoint not returning Prometheus format"
            assert "# TYPE" in metrics, "Metrics endpoint not returning Prometheus format"

    def test_relayer_no_fatal_errors(self, relayer_deployment: RelayerInfo) -> None:
        """Relayer logs contain no FATAL or PANIC messages."""
        ns = relayer_deployment.namespace
        cluster_id = relayer_deployment.cluster_id

        result = run_cmd([
            "kubectl", "logs",
            "-n", ns,
            "-l", f"app={cluster_id}",
            "-c", "relayer",
            "--tail=200",
        ])
        logs = result.stdout
        for keyword in ("FATAL", "PANIC"):
            assert keyword not in logs, (
                f"Found {keyword} in relayer logs:\n"
                + "\n".join(line for line in logs.splitlines() if keyword in line)
            )

    def test_igp_fee_claim_sidecar_running(self, relayer_deployment: RelayerInfo) -> None:
        """IGP fee claim sidecar container is running and started its loop."""
        ns = relayer_deployment.namespace
        cluster_id = relayer_deployment.cluster_id

        # Check container is running
        result = run_cmd([
            "kubectl", "get", "pods",
            "-n", ns,
            "-l", f"app={cluster_id}",
            "-o", "jsonpath={.items[0].status.containerStatuses[?(@.name==\"igp-fee-claim\")].state}",
        ])
        assert "running" in result.stdout.lower(), (
            f"igp-fee-claim container is not running: {result.stdout}"
        )

        # Check logs show the sidecar started
        result = run_cmd([
            "kubectl", "logs",
            "-n", ns,
            "-l", f"app={cluster_id}",
            "-c", "igp-fee-claim",
            "--tail=50",
        ])
        logs = result.stdout
        assert "IGP fee claim sidecar starting" in logs, (
            f"IGP fee claim sidecar did not start properly. Logs:\n{logs}"
        )

    def test_relayer_checkpoint_syncer_connected(self, relayer_deployment: RelayerInfo) -> None:
        """Relayer is connecting to MinIO for checkpoint syncing (no S3 connection errors)."""
        ns = relayer_deployment.namespace
        cluster_id = relayer_deployment.cluster_id

        result = run_cmd([
            "kubectl", "logs",
            "-n", ns,
            "-l", f"app={cluster_id}",
            "-c", "relayer",
            "--tail=200",
        ])
        logs = result.stdout
        s3_errors = [
            "ConnectError",
            "NoSuchBucket",
            "InvalidAccessKeyId",
            "SignatureDoesNotMatch",
        ]
        for err in s3_errors:
            assert err not in logs, (
                f"Found S3 error '{err}' in relayer logs — checkpoint syncer may not be connected:\n"
                + "\n".join(line for line in logs.splitlines() if err in line)
            )
