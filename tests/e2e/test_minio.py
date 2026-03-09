"""E2E tests for the hyperlane-minio stack.

Verifies MinIO pod, init job (bucket creation), S3 API, and console.
This stack is a prerequisite for validator and relayer stacks.
"""

from __future__ import annotations

import base64
import logging
import secrets
import subprocess
import time
from collections.abc import Generator

import pytest

from lib.cluster import create_namespace
from lib.common import (
    E2E_DIR,
    wait_for_job_complete,
    wait_for_pod_phase,
)
from lib.deploy import (
    DEPLOY_DIR,
    DeploymentInfo,
    deploy_prepare,
    deploy_start,
    get_cluster_id,
    stop_stack,
)

log = logging.getLogger(__name__)

MINIO_SPEC = E2E_DIR / "fixtures" / "test-spec-minio.yml"
MINIO_USER = f"minio-{secrets.token_hex(4)}"
MINIO_PASSWORD = secrets.token_hex(16)
MC_IMAGE = "minio/mc:latest"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _recover_minio_credentials(namespace: str) -> None:
    """Read minio credentials from k8s secret (for --skip-minio-deploy reuse)."""
    global MINIO_USER, MINIO_PASSWORD
    for field in ("MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD"):
        result = subprocess.run(
            ["kubectl", "get", "secret", "hyperlane-minio-secrets", "-n", namespace,
             "-o", f"jsonpath={{.data.{field}}}"],
            capture_output=True, text=True, check=True,
        )
        value = base64.b64decode(result.stdout.strip()).decode()
        if field == "MINIO_ROOT_USER":
            MINIO_USER = value
        else:
            MINIO_PASSWORD = value
    log.info("Recovered minio credentials from k8s secret")


def create_minio_secrets(namespace: str) -> None:
    """Create the hyperlane-minio-secrets k8s Secret (idempotent)."""
    cmd = (
        f"kubectl create secret generic hyperlane-minio-secrets "
        f"-n {namespace} "
        f"--from-literal=MINIO_ROOT_USER={MINIO_USER} "
        f"--from-literal=MINIO_ROOT_PASSWORD={MINIO_PASSWORD} "
        f"--dry-run=client -o yaml | kubectl apply -f -"
    )
    subprocess.run(cmd, shell=True, check=True, capture_output=True, text=True)


def run_mc_command(*args: str, host_port: int = 9000) -> subprocess.CompletedProcess[str]:
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
            f"{MINIO_USER} {MINIO_PASSWORD} && {mc_args}",
        ],
        capture_output=True,
        text=True,
    )


class PortForward:
    """Context manager for kubectl port-forward."""

    def __init__(self, namespace: str, target: str, local_port: int, remote_port: int):
        self.namespace = namespace
        self.target = target
        self.local_port = local_port
        self.remote_port = remote_port
        self.proc: subprocess.Popen | None = None

    def __enter__(self) -> PortForward:
        self.proc = subprocess.Popen(
            [
                "kubectl", "port-forward",
                "-n", self.namespace,
                self.target,
                f"{self.local_port}:{self.remote_port}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # Wait for port-forward to establish
        time.sleep(3)
        if self.proc.poll() is not None:
            stderr = self.proc.stderr.read().decode() if self.proc.stderr else ""
            raise RuntimeError(f"port-forward failed to start: {stderr}")
        return self

    def __exit__(self, *exc) -> None:
        if self.proc:
            self.proc.terminate()
            self.proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def minio_deployment(
    request: pytest.FixtureRequest,
    kind_cluster: None,
) -> Generator[DeploymentInfo, None, None]:
    """Deploy the hyperlane-minio stack.

    Self-contained: only requires a Kind cluster. Creates its own namespace
    and secrets. Uses the default cluster-id (KIND_CLUSTER_NAME), so when the
    full suite runs, all stacks share the same namespace naturally.
    """
    skip_cleanup = request.config.getoption("--skip-cleanup")
    skip_minio = request.config.getoption("--skip-minio-deploy", default=False)

    if skip_minio:
        deploy_dir = DEPLOY_DIR / "hyperlane-minio"
        cluster_id = get_cluster_id(deploy_dir)
        namespace = f"laconic-{cluster_id}"
        log.info("Reusing existing minio deployment (namespace: %s)", namespace)
        _recover_minio_credentials(namespace)
        yield DeploymentInfo(deploy_dir=deploy_dir, cluster_id=cluster_id, namespace=namespace)
        return

    log.info("Preparing minio stack...")
    deploy_info = deploy_prepare("hyperlane-minio", MINIO_SPEC)
    namespace = deploy_info.namespace
    cluster_id = deploy_info.cluster_id

    # Create namespace before deploy start — secrets must exist before pods start.
    # Idempotent: no-ops if deployer_deployment already created it.
    log.info("Creating namespace %s...", namespace)
    create_namespace(namespace)

    log.info("Creating minio secrets in namespace %s...", namespace)
    create_minio_secrets(namespace)

    log.info("Starting minio stack...")
    deploy_start(deploy_info.deploy_dir)

    log.info("Waiting for minio pod to be running...")
    wait_for_pod_phase(namespace, f"app={cluster_id}", "Running", timeout=120)

    log.info("Waiting for minio-init job to complete...")
    job_name = f"{cluster_id}-job-hyperlane-minio-init"
    wait_for_job_complete(namespace, job_name, timeout=120)
    log.info("MinIO stack deployed and initialized")

    yield deploy_info

    if not skip_cleanup:
        log.info("Stopping minio stack...")
        stop_stack("hyperlane-minio")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestMinio:
    """Tests for the hyperlane-minio stack."""

    def test_minio_pod_running(self, minio_deployment: DeploymentInfo) -> None:
        """MinIO pod reaches Running phase."""
        ns = minio_deployment.namespace
        cluster_id = minio_deployment.cluster_id

        result = subprocess.run(
            ["kubectl", "get", "pods", "-n", ns,
             "-l", f"app={cluster_id}",
             "-o", "jsonpath={.items[0].status.phase}"],
            capture_output=True, text=True, check=True,
        )
        assert result.stdout.strip() == "Running", f"Expected Running, got: {result.stdout}"

    def test_minio_init_job_completed(self, minio_deployment: DeploymentInfo) -> None:
        """minio-init job completed successfully (buckets created)."""
        ns = minio_deployment.namespace
        cluster_id = minio_deployment.cluster_id
        job_name = f"{cluster_id}-job-hyperlane-minio-init"

        result = subprocess.run(
            ["kubectl", "get", "job", job_name, "-n", ns,
             "-o", "jsonpath={.status.succeeded}"],
            capture_output=True, text=True, check=True,
        )
        assert result.stdout.strip() == "1", f"Init job not succeeded: {result.stdout}"

    def test_minio_s3_api_responds(self, minio_deployment: DeploymentInfo) -> None:
        """MinIO S3 API responds to requests via port-forward."""
        ns = minio_deployment.namespace
        cluster_id = minio_deployment.cluster_id
        pod_label = f"app={cluster_id}"

        # Find the minio pod name
        pod_name = subprocess.run(
            ["kubectl", "get", "pods", "-n", ns, "-l", pod_label,
             "-o", "jsonpath={.items[0].metadata.name}"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        with PortForward(ns, f"pod/{pod_name}", 19000, 9000):
            result = run_mc_command("mc", "admin", "info", "test", host_port=19000)
            assert result.returncode == 0, (
                f"mc admin info failed (rc={result.returncode}): "
                f"stdout={result.stdout} stderr={result.stderr}"
            )

    def test_minio_buckets_exist(self, minio_deployment: DeploymentInfo) -> None:
        """Both validator buckets were created by minio-init."""
        ns = minio_deployment.namespace
        cluster_id = minio_deployment.cluster_id
        pod_label = f"app={cluster_id}"

        pod_name = subprocess.run(
            ["kubectl", "get", "pods", "-n", ns, "-l", pod_label,
             "-o", "jsonpath={.items[0].metadata.name}"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        with PortForward(ns, f"pod/{pod_name}", 19000, 9000):
            result = run_mc_command("mc", "ls", "test/", host_port=19000)
            assert result.returncode == 0, f"mc ls failed: {result.stderr}"

            buckets = result.stdout
            assert "hyperlane-validator-gorchain" in buckets, (
                f"gorchain bucket not found in: {buckets}"
            )
            assert "hyperlane-validator-solana" in buckets, (
                f"solana bucket not found in: {buckets}"
            )

    def test_minio_console_accessible(self, minio_deployment: DeploymentInfo) -> None:
        """MinIO console (port 9001) responds to HTTP requests."""
        ns = minio_deployment.namespace
        cluster_id = minio_deployment.cluster_id
        pod_label = f"app={cluster_id}"

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
