"""E2E tests for the hyperlane-minio stack.

Verifies MinIO pod, init job (bucket creation), S3 API, and console.
This stack is a prerequisite for validator and relayer stacks.
"""

from __future__ import annotations

import subprocess

import pytest
from conftest import MinioInfo

from lib.common import PortForward

MC_IMAGE = "minio/mc:RELEASE.2025-08-13T08-35-41Z"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run_mc_command(
    *args: str, minio: MinioInfo, host_port: int = 9000,
) -> subprocess.CompletedProcess[str]:
    """Run a minio/mc command via docker against a port-forwarded MinIO.

    Assumes kubectl port-forward is active on localhost:{host_port}.
    Uses --network host so the container can reach localhost.
    """
    mc_args = " ".join(str(a) for a in args)
    return subprocess.run(
        [
            "docker", "run", "--rm", "--network", "host",
            "--entrypoint", "sh",
            MC_IMAGE,
            "-c",
            f"mc alias set test http://localhost:{host_port} "
            f"{minio.user} {minio.password} && {mc_args}",
        ],
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestMinio:
    """Tests for the hyperlane-minio stack."""

    def test_minio_pod_running(self, minio_deployment: MinioInfo) -> None:
        """MinIO pod reaches Running phase."""
        ns = minio_deployment.namespace
        deployment_id = minio_deployment.deployment_id

        result = subprocess.run(
            ["kubectl", "get", "pods", "-n", ns,
             "-l", f"app={deployment_id}",
             "-o", "jsonpath={.items[0].status.phase}"],
            capture_output=True, text=True, check=True,
        )
        assert result.stdout.strip() == "Running", f"Expected Running, got: {result.stdout}"

    def test_minio_init_job_completed(self, minio_deployment: MinioInfo) -> None:
        """minio-init job completed successfully (buckets created)."""
        ns = minio_deployment.namespace
        deployment_id = minio_deployment.deployment_id
        job_name = f"{deployment_id}-job-hyperlane-minio-init"

        result = subprocess.run(
            ["kubectl", "get", "job", job_name, "-n", ns,
             "-o", "jsonpath={.status.succeeded}"],
            capture_output=True, text=True, check=True,
        )
        assert result.stdout.strip() == "1", f"Init job not succeeded: {result.stdout}"

    def test_minio_s3_api_responds(self, minio_deployment: MinioInfo) -> None:
        """MinIO S3 API responds to requests via port-forward."""
        ns = minio_deployment.namespace
        deployment_id = minio_deployment.deployment_id
        pod_label = f"app={deployment_id}"

        # Find the minio pod name
        pod_name = subprocess.run(
            ["kubectl", "get", "pods", "-n", ns, "-l", pod_label,
             "-o", "jsonpath={.items[0].metadata.name}"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        with PortForward(ns, f"pod/{pod_name}", 19000, 9000):
            result = run_mc_command("mc", "admin", "info", "test", minio=minio_deployment, host_port=19000)
            assert result.returncode == 0, (
                f"mc admin info failed (rc={result.returncode}): "
                f"stdout={result.stdout} stderr={result.stderr}"
            )

    def test_minio_buckets_exist(self, minio_deployment: MinioInfo) -> None:
        """Both validator buckets were created by minio-init."""
        ns = minio_deployment.namespace
        deployment_id = minio_deployment.deployment_id
        pod_label = f"app={deployment_id}"

        pod_name = subprocess.run(
            ["kubectl", "get", "pods", "-n", ns, "-l", pod_label,
             "-o", "jsonpath={.items[0].metadata.name}"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        with PortForward(ns, f"pod/{pod_name}", 19000, 9000):
            result = run_mc_command("mc", "ls", "test/", minio=minio_deployment, host_port=19000)
            assert result.returncode == 0, f"mc ls failed: {result.stderr}"

            buckets = result.stdout
            assert "hyperlane-validator-gorchain" in buckets, (
                f"gorchain bucket not found in: {buckets}"
            )
            assert "hyperlane-validator-solana" in buckets, (
                f"solana bucket not found in: {buckets}"
            )

    def test_minio_console_accessible(self, minio_deployment: MinioInfo) -> None:
        """MinIO console (port 9001) responds to HTTP requests."""
        ns = minio_deployment.namespace
        deployment_id = minio_deployment.deployment_id
        pod_label = f"app={deployment_id}"

        pod_name = subprocess.run(
            ["kubectl", "get", "pods", "-n", ns, "-l", pod_label,
             "-o", "jsonpath={.items[0].metadata.name}"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        with PortForward(ns, f"pod/{pod_name}", 19001, 9001):
            # Simple HTTP check — the console serves a web UI
            result = subprocess.run(
                ["curl", "-sf", "-o", "/dev/null", "-w", "%{http_code}",
                 "http://localhost:19001/"],
                capture_output=True, text=True,
            )
            status = result.stdout.strip()
            assert status.startswith("2") or status.startswith("3"), (
                f"Console returned HTTP {status} (curl rc={result.returncode})"
            )
