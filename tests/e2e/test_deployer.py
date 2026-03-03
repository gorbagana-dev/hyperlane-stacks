import json
import logging
import subprocess

import pytest

from lib.deploy import DeploymentInfo

log = logging.getLogger(__name__)

DEPLOYER_POD_TIMEOUT = 1200
CONFIGMAP_TIMEOUT = 30


def _kubectl_get_configmap(namespace: str, name: str) -> dict:
    result = subprocess.run(
        ["kubectl", "-n", namespace, "get", "configmap", name, "-o", "json"],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def _wait_for_pod_phase(namespace: str, label: str, phase: str, timeout: int) -> None:
    """Wait for a pod matching *label* to reach the given phase."""
    subprocess.run(
        [
            "kubectl",
            "wait",
            "--for=jsonpath={.status.phase}=" + phase,
            "-n",
            namespace,
            "-l",
            label,
            f"--timeout={timeout}s",
            "pod",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _wait_for_configmap(namespace: str, name: str, timeout: int) -> None:
    """Poll until a ConfigMap exists (kubectl wait does not support ConfigMaps)."""
    import time

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


def _dump_pod_logs(namespace: str, label: str) -> None:
    result = subprocess.run(
        ["kubectl", "logs", "-n", namespace, "-l", label, "--tail=200"],
        capture_output=True,
        text=True,
    )
    if result.stdout:
        log.info("--- Pod logs (%s) ---\n%s", label, result.stdout)
    elif result.stderr:
        log.warning("--- Could not fetch pod logs (%s): %s", label, result.stderr.strip())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestDeployer:
    def test_deployer_pod_succeeds(self, deployer_deployment: DeploymentInfo) -> None:
        ns = deployer_deployment.namespace
        pod_label = f"app={deployer_deployment.cluster_id}"
        try:
            _wait_for_pod_phase(ns, pod_label, "Succeeded", DEPLOYER_POD_TIMEOUT)
        except subprocess.CalledProcessError:
            _dump_pod_logs(ns, pod_label)
            pytest.fail("Deployer pod did not reach Succeeded phase")

    def test_program_ids_configmap(self, deployer_deployment: DeploymentInfo) -> None:
        ns = deployer_deployment.namespace
        _wait_for_configmap(ns, "hyperlane-program-ids", CONFIGMAP_TIMEOUT)

        cm = _kubectl_get_configmap(ns, "hyperlane-program-ids")
        data = cm.get("data", {})

        assert "gorchain-program-ids.json" in data and data["gorchain-program-ids.json"], (
            "program-ids missing gorchain data"
        )
        assert "solanasvm-program-ids.json" in data and data["solanasvm-program-ids.json"], (
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
