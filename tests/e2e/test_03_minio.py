"""E2E tests for the hyperlane-minio stack.

Verifies MinIO pod, init job (bucket creation), S3 API, and console.
This stack is a prerequisite for validator and relayer stacks.
"""

from __future__ import annotations

import base64
import json
import secrets
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


def run_mc_as(
    key_id: str,
    secret: str,
    *args: str,
    host_port: int = 9000,
) -> subprocess.CompletedProcess[str]:
    """Run a minio/mc command with explicit IAM user credentials.

    Used to verify per-user bucket access isolation.
    """
    mc_args = " ".join(str(a) for a in args)
    return subprocess.run(
        [
            "docker", "run", "--rm", "--network", "host",
            "--entrypoint", "sh",
            MC_IMAGE,
            "-c",
            f"mc alias set user http://localhost:{host_port} "
            f"{key_id} {secret} && {mc_args}",
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

    def test_minio_provision_job_completed(self, minio_deployment: MinioInfo) -> None:
        """minio-provision-initial job completed successfully."""
        ns = minio_deployment.namespace

        # Verify CronJob exists and is suspended
        cj_result = subprocess.run(
            ["kubectl", "get", "cronjob", "minio-provision", "-n", ns,
             "-o", "jsonpath={.spec.suspend}"],
            capture_output=True, text=True, check=True,
        )
        assert cj_result.stdout.strip() == "true", (
            f"Expected CronJob to be suspended, got: {cj_result.stdout}"
        )

        # Verify the initial provisioning job succeeded
        job_result = subprocess.run(
            ["kubectl", "get", "job", "minio-provision-initial", "-n", ns,
             "-o", "jsonpath={.status.succeeded}"],
            capture_output=True, text=True, check=True,
        )
        assert job_result.stdout.strip() == "1", (
            f"minio-provision-initial not succeeded: {job_result.stdout}"
        )

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
        """Both validator buckets were created by minio-provision-initial."""
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
            assert "hyperlane-validator-gorchain-primary" in buckets, (
                f"gorchain-primary bucket not found in: {buckets}"
            )
            assert "hyperlane-validator-solana-primary" in buckets, (
                f"solana-primary bucket not found in: {buckets}"
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

    def test_minio_users_created(self, minio_deployment: MinioInfo) -> None:
        """Per-validator IAM users were created by minio-provision-initial."""
        ns = minio_deployment.namespace
        deployment_id = minio_deployment.deployment_id
        pod_label = f"app={deployment_id}"

        pod_name = subprocess.run(
            ["kubectl", "get", "pods", "-n", ns, "-l", pod_label,
             "-o", "jsonpath={.items[0].metadata.name}"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        with PortForward(ns, f"pod/{pod_name}", 19000, 9000):
            result = run_mc_command(
                "mc", "admin", "user", "list", "test",
                minio=minio_deployment, host_port=19000,
            )
            assert result.returncode == 0, f"mc admin user list failed: {result.stderr}"
            users = result.stdout
            assert minio_deployment.gorchain_key_id in users, (
                f"gorchain-primary user '{minio_deployment.gorchain_key_id}' not found: {users}"
            )
            assert minio_deployment.solana_key_id in users, (
                f"solana-primary user '{minio_deployment.solana_key_id}' not found: {users}"
            )

    def test_minio_policies_attached(self, minio_deployment: MinioInfo) -> None:
        """Bucket-scoped IAM policies are attached to each validator user."""
        ns = minio_deployment.namespace
        deployment_id = minio_deployment.deployment_id
        pod_label = f"app={deployment_id}"

        pod_name = subprocess.run(
            ["kubectl", "get", "pods", "-n", ns, "-l", pod_label,
             "-o", "jsonpath={.items[0].metadata.name}"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        with PortForward(ns, f"pod/{pod_name}", 19000, 9000):
            for key_id, label in [
                (minio_deployment.gorchain_key_id, "gorchain-primary"),
                (minio_deployment.solana_key_id, "solana-primary"),
            ]:
                result = run_mc_command(
                    "mc", "admin", "user", "info", "test", key_id,
                    minio=minio_deployment, host_port=19000,
                )
                assert result.returncode == 0, (
                    f"mc admin user info failed for {key_id}: {result.stderr}"
                )
                policy_name = f"policy-{label}"
                assert policy_name in result.stdout, (
                    f"Policy '{policy_name}' not attached to user '{key_id}': {result.stdout}"
                )

    def test_bucket_isolation(self, minio_deployment: MinioInfo) -> None:
        """Each validator's IAM user can access its own bucket but not the other's."""
        ns = minio_deployment.namespace
        deployment_id = minio_deployment.deployment_id
        pod_label = f"app={deployment_id}"

        pod_name = subprocess.run(
            ["kubectl", "get", "pods", "-n", ns, "-l", pod_label,
             "-o", "jsonpath={.items[0].metadata.name}"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        with PortForward(ns, f"pod/{pod_name}", 19000, 9000):
            # gorchain-primary user can access its own bucket
            result = run_mc_as(
                minio_deployment.gorchain_key_id, minio_deployment.gorchain_secret,
                "mc", "ls", "user/hyperlane-validator-gorchain-primary",
                host_port=19000,
            )
            assert result.returncode == 0, (
                f"gorchain user could not list own bucket: {result.stderr}"
            )

            # gorchain-primary user cannot access solana bucket
            result = run_mc_as(
                minio_deployment.gorchain_key_id, minio_deployment.gorchain_secret,
                "mc", "ls", "user/hyperlane-validator-solana-primary",
                host_port=19000,
            )
            assert result.returncode != 0, (
                "gorchain user should NOT have access to solana-primary bucket"
            )

            # solana-primary user can access its own bucket
            result = run_mc_as(
                minio_deployment.solana_key_id, minio_deployment.solana_secret,
                "mc", "ls", "user/hyperlane-validator-solana-primary",
                host_port=19000,
            )
            assert result.returncode == 0, (
                f"solana user could not list own bucket: {result.stderr}"
            )

    def test_subsequent_validator_provisioning(self, minio_deployment: MinioInfo) -> None:
        """Triggering the CronJob provisions a new validator label without redeploying MinIO.

        Simulates the operator workflow for adding a second validator per chain post-deployment.
        """
        ns = minio_deployment.namespace
        deployment_id = minio_deployment.deployment_id
        pod_label = f"app={deployment_id}"

        # Generate credentials for a new validator label
        new_label = "gorchain-secondary"
        new_key_id = f"gc2-{secrets.token_hex(6)}"
        new_secret = secrets.token_hex(20)
        new_bucket = f"hyperlane-validator-{new_label}"

        # Patch the minio-validator-secrets Secret to add the new validator
        # (In prod this is done via `kubectl edit secret` or Ansible before triggering the CronJob)
        new_users = b"gorchain-primary,solana-primary,gorchain-secondary"
        subprocess.run(
            ["kubectl", "patch", "secret", "minio-validator-secrets", "-n", ns,
             "--type=merge",
             "-p", json.dumps({"data": {
                 "MINIO_USERS":                base64.b64encode(new_users).decode(),
                 "GORCHAIN_SECONDARY_KEY_ID":  base64.b64encode(new_key_id.encode()).decode(),
                 "GORCHAIN_SECONDARY_SECRET":  base64.b64encode(new_secret.encode()).decode(),
             }})],
            capture_output=True, text=True, check=True,
        )

        # Trigger the CronJob
        additional_job = "minio-provision-secondary-test"
        subprocess.run(
            ["kubectl", "create", "job", additional_job,
             "--from=cronjob/minio-provision", "-n", ns],
            capture_output=True, text=True, check=True,
        )

        try:
            from lib.common import wait_for_job_complete
            wait_for_job_complete(ns, additional_job, timeout=300)

            # Verify new bucket and user exist
            pod_name = subprocess.run(
                ["kubectl", "get", "pods", "-n", ns, "-l", pod_label,
                 "-o", "jsonpath={.items[0].metadata.name}"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()

            with PortForward(ns, f"pod/{pod_name}", 19000, 9000):
                result = run_mc_command(
                    "mc", "ls", "test/",
                    minio=minio_deployment, host_port=19000,
                )
                assert new_bucket in result.stdout, (
                    f"New bucket '{new_bucket}' not found after re-provision: {result.stdout}"
                )

                result = run_mc_command(
                    "mc", "admin", "user", "list", "test",
                    minio=minio_deployment, host_port=19000,
                )
                assert new_key_id in result.stdout, (
                    f"New user '{new_key_id}' not found after re-provision: {result.stdout}"
                )
        finally:
            # Clean up: delete the test job and roll back the secret patch
            subprocess.run(
                ["kubectl", "delete", "job", additional_job, "-n", ns, "--ignore-not-found"],
                capture_output=True, text=True,
            )
            # Restore original MINIO_USERS and remove the temporary secondary keys.
            # Using JSON Patch remove — tolerate errors (key may not exist if setup failed early).
            original_users = base64.b64encode(b"gorchain-primary,solana-primary").decode()
            subprocess.run(
                ["kubectl", "patch", "secret", "minio-validator-secrets", "-n", ns,
                 "--type=json",
                 "-p", json.dumps([
                     {"op": "remove", "path": "/data/GORCHAIN_SECONDARY_KEY_ID"},
                     {"op": "remove", "path": "/data/GORCHAIN_SECONDARY_SECRET"},
                     {"op": "replace", "path": "/data/MINIO_USERS", "value": original_users},
                 ])],
                capture_output=True, text=True,
                # No check=True — key may not exist if setup patch failed
            )
