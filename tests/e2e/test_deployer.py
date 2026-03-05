import json
import logging
import subprocess
import time

import pytest

from lib.deploy import DeploymentInfo

log = logging.getLogger(__name__)

DEPLOYER_JOB_TIMEOUT = 1200
CONFIGMAP_TIMEOUT = 30


def _kubectl_get_configmap(namespace: str, name: str) -> dict:
    result = subprocess.run(
        ["kubectl", "-n", namespace, "get", "configmap", name, "-o", "json"],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def _wait_for_job_complete(namespace: str, job_name: str, timeout: int) -> None:
    """Wait for a k8s Job to complete successfully.

    Uses kubectl wait --for=condition=complete, falling back to polling
    if the Job hasn't been created yet.
    """
    deadline = time.monotonic() + timeout

    # First wait for the Job to exist
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["kubectl", "-n", namespace, "get", "job", job_name],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            break
        time.sleep(5)
    else:
        raise TimeoutError(
            f"Job {job_name} not found in namespace {namespace} within {timeout}s"
        )

    # Now wait for completion
    remaining = int(deadline - time.monotonic())
    if remaining <= 0:
        remaining = 1
    result = subprocess.run(
        [
            "kubectl", "wait",
            "--for=condition=complete",
            f"--timeout={remaining}s",
            "-n", namespace,
            f"job/{job_name}",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # Check if the job failed
        status_result = subprocess.run(
            [
                "kubectl", "-n", namespace, "get", "job", job_name,
                "-o", "jsonpath={.status.conditions[?(@.type=='Failed')].status}",
            ],
            capture_output=True,
            text=True,
        )
        if status_result.stdout.strip() == "True":
            raise RuntimeError(
                f"Job {job_name} failed. stderr: {result.stderr.strip()}"
            )
        raise TimeoutError(
            f"Job {job_name} did not complete within {remaining}s. "
            f"stderr: {result.stderr.strip()}"
        )


def _wait_for_configmap(namespace: str, name: str, timeout: int) -> None:
    """Poll until a ConfigMap exists (kubectl wait does not support ConfigMaps)."""
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["kubectl", "-n", namespace, "get", "configmap", name],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return
        last_error = (result.stderr or "").strip()
        time.sleep(2)
    raise TimeoutError(
        f"ConfigMap {name} not found in namespace {namespace} within {timeout}s. "
        f"Last error: {last_error}"
    )


def _dump_job_logs(namespace: str, job_name: str) -> None:
    result = subprocess.run(
        ["kubectl", "logs", "-n", namespace, f"job/{job_name}", "--tail=200"],
        capture_output=True,
        text=True,
    )
    if result.stdout:
        log.info("--- Job logs (%s) ---\n%s", job_name, result.stdout)
    elif result.stderr:
        log.warning("--- Could not fetch job logs (%s): %s", job_name, result.stderr.strip())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestDeployer:
    def test_deployer_job_succeeds(self, deployer_deployment: DeploymentInfo) -> None:
        ns = deployer_deployment.namespace
        job_name = f"{deployer_deployment.cluster_id}-job-hyperlane-svm-deployer"
        try:
            _wait_for_job_complete(ns, job_name, DEPLOYER_JOB_TIMEOUT)
        except (TimeoutError, RuntimeError, subprocess.CalledProcessError):
            _dump_job_logs(ns, job_name)
            pytest.fail("Deployer job did not complete successfully")

    def test_program_ids_configmap(self, deployer_deployment: DeploymentInfo) -> None:
        ns = deployer_deployment.namespace
        _wait_for_configmap(ns, "hyperlane-program-ids", CONFIGMAP_TIMEOUT)

        cm = _kubectl_get_configmap(ns, "hyperlane-program-ids")
        data = cm.get("data", {})

        assert "gorchain-program-ids.json" in data and data["gorchain-program-ids.json"], (
            "program-ids missing gorchain data"
        )
        assert "solana-program-ids.json" in data and data["solana-program-ids.json"], (
            "program-ids missing solana data"
        )

    def test_agent_config_configmap(self, deployer_deployment: DeploymentInfo) -> None:
        ns = deployer_deployment.namespace
        _wait_for_configmap(ns, "hyperlane-agent-config", CONFIGMAP_TIMEOUT)

        cm = _kubectl_get_configmap(ns, "hyperlane-agent-config")
        raw = cm.get("data", {}).get("agent-config.json", "")
        assert raw, "agent-config data is empty"

        parsed = json.loads(raw)
        assert isinstance(parsed, dict), "agent-config is not a JSON object"

    def test_gas_oracle_configmap(self, deployer_deployment: DeploymentInfo) -> None:
        ns = deployer_deployment.namespace
        _wait_for_configmap(ns, "hyperlane-gas-oracle-config", CONFIGMAP_TIMEOUT)
        _kubectl_get_configmap(ns, "hyperlane-gas-oracle-config")

    def test_multisig_configmap(self, deployer_deployment: DeploymentInfo) -> None:
        ns = deployer_deployment.namespace
        _wait_for_configmap(ns, "hyperlane-multisig-config", CONFIGMAP_TIMEOUT)
        _kubectl_get_configmap(ns, "hyperlane-multisig-config")
