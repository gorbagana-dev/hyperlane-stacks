import json
import logging
import subprocess

import pytest

from lib.common import kubectl_json, wait_for_configmap
from lib.deploy import DeploymentInfo

log = logging.getLogger(__name__)

CONFIGMAP_TIMEOUT = 30


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestDeployer:
    def test_deployer_job_succeeds(self, deployer_deployment: DeploymentInfo) -> None:
        """Verify the deployer Job completed (guaranteed by the fixture)."""
        ns = deployer_deployment.namespace
        job_name = f"{deployer_deployment.cluster_id}-job-hyperlane-svm-deployer"
        result = subprocess.run(
            [
                "kubectl", "-n", ns, "get", "job", job_name,
                "-o", "jsonpath={.status.conditions[?(@.type=='Complete')].status}",
            ],
            capture_output=True, text=True, check=True,
        )
        assert result.stdout.strip() == "True", f"Job {job_name} is not in Complete state"

    def test_program_ids_configmap(self, deployer_deployment: DeploymentInfo) -> None:
        ns = deployer_deployment.namespace
        wait_for_configmap(ns, "hyperlane-program-ids", CONFIGMAP_TIMEOUT)

        cm = kubectl_json(["-n", ns, "get", "configmap", "hyperlane-program-ids"])
        data = cm.get("data", {})

        assert "gorchain-program-ids.json" in data and data["gorchain-program-ids.json"], (
            "program-ids missing gorchain data"
        )
        assert "solana-program-ids.json" in data and data["solana-program-ids.json"], (
            "program-ids missing solana data"
        )

    def test_agent_config_configmap(self, deployer_deployment: DeploymentInfo) -> None:
        ns = deployer_deployment.namespace
        wait_for_configmap(ns, "hyperlane-agent-config", CONFIGMAP_TIMEOUT)

        cm = kubectl_json(["-n", ns, "get", "configmap", "hyperlane-agent-config"])
        raw = cm.get("data", {}).get("agent-config.json", "")
        assert raw, "agent-config data is empty"

        parsed = json.loads(raw)
        assert isinstance(parsed, dict), "agent-config is not a JSON object"

    def test_gas_oracle_configmap(self, deployer_deployment: DeploymentInfo) -> None:
        ns = deployer_deployment.namespace
        wait_for_configmap(ns, "hyperlane-gas-oracle-config", CONFIGMAP_TIMEOUT)
        kubectl_json(["-n", ns, "get", "configmap", "hyperlane-gas-oracle-config"])

    def test_multisig_configmap(self, deployer_deployment: DeploymentInfo) -> None:
        ns = deployer_deployment.namespace
        wait_for_configmap(ns, "hyperlane-multisig-config", CONFIGMAP_TIMEOUT)
        kubectl_json(["-n", ns, "get", "configmap", "hyperlane-multisig-config"])
