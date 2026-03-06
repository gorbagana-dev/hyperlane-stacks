import logging
import subprocess

import pytest

from lib.common import (
    CHAINS,
    CONFIGMAP_TIMEOUT,
    assert_program_on_chain,
    get_configmap_data,
    get_configmap_json,
    is_base58_pubkey,
    wait_for_configmap,
)
from lib.deploy import DeploymentInfo

log = logging.getLogger(__name__)

# Expected fields in core program-ids.json (written by sealevel-client core deploy)
CORE_PROGRAM_ID_FIELDS = [
    "mailbox",
    "validator_announce",
    "multisig_ism_message_id",
    "igp_program_id",
    "overhead_igp_account",
    "igp_account",
]

# Expected fields in agent-config.json per chain
AGENT_CONFIG_ADDRESS_FIELDS = [
    "mailbox",
    "interchainGasPaymaster",
    "interchainSecurityModule",
    "validatorAnnounce",
    "merkleTreeHook",
]


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
        """Validate program-ids ConfigMap has correct structure and valid pubkeys."""
        ns = deployer_deployment.namespace
        wait_for_configmap(ns, "hyperlane-program-ids", CONFIGMAP_TIMEOUT)

        for chain in CHAINS:
            key = f"{chain}-program-ids.json"
            program_ids = get_configmap_json(ns, "hyperlane-program-ids", key)

            for field in CORE_PROGRAM_ID_FIELDS:
                value = program_ids.get(field)
                assert value, f"{chain} program-ids missing '{field}'"
                assert is_base58_pubkey(value), (
                    f"{chain} program-ids.{field} is not a valid base58 pubkey: {value}"
                )

    def test_agent_config_configmap(self, deployer_deployment: DeploymentInfo) -> None:
        """Validate agent-config structure, field values, and cross-references."""
        ns = deployer_deployment.namespace
        wait_for_configmap(ns, "hyperlane-agent-config", CONFIGMAP_TIMEOUT)

        agent_config = get_configmap_json(ns, "hyperlane-agent-config", "agent-config.json")
        chains = agent_config.get("chains")
        assert isinstance(chains, dict), "agent-config missing 'chains' object"

        for chain_name, chain_info in CHAINS.items():
            chain = chains.get(chain_name)
            assert chain, f"agent-config missing chain '{chain_name}'"

            # Protocol and domain
            assert chain.get("protocol") == "sealevel", (
                f"{chain_name}: expected protocol 'sealevel', got '{chain.get('protocol')}'"
            )
            assert chain.get("domainId") == chain_info["domain_id"], (
                f"{chain_name}: expected domainId {chain_info['domain_id']}, got {chain.get('domainId')}"
            )

            # RPC URL present
            rpc_urls = chain.get("rpcUrls", [])
            assert rpc_urls, f"{chain_name}: rpcUrls is empty"

            # All address fields must be valid base58 (not "null" or empty)
            for field in AGENT_CONFIG_ADDRESS_FIELDS:
                value = chain.get(field)
                assert value and value != "null", (
                    f"{chain_name}: agent-config.{field} is missing or null"
                )
                assert is_base58_pubkey(value), (
                    f"{chain_name}: agent-config.{field} is not valid base58: {value}"
                )

        # Cross-reference agent-config addresses with program-ids
        for chain_name in CHAINS:
            program_ids = get_configmap_json(
                ns, "hyperlane-program-ids", f"{chain_name}-program-ids.json"
            )
            ac = chains[chain_name]

            assert ac["mailbox"] == program_ids["mailbox"], (
                f"{chain_name}: agent-config.mailbox doesn't match program-ids"
            )
            assert ac["interchainGasPaymaster"] == program_ids["overhead_igp_account"], (
                f"{chain_name}: agent-config.interchainGasPaymaster doesn't match "
                f"program-ids.overhead_igp_account"
            )
            assert ac["interchainSecurityModule"] == program_ids["multisig_ism_message_id"], (
                f"{chain_name}: agent-config.interchainSecurityModule doesn't match "
                f"program-ids.multisig_ism_message_id"
            )
            assert ac["merkleTreeHook"] == program_ids["mailbox"], (
                f"{chain_name}: agent-config.merkleTreeHook doesn't match program-ids.mailbox"
            )
            assert ac["validatorAnnounce"] == program_ids["validator_announce"], (
                f"{chain_name}: agent-config.validatorAnnounce doesn't match "
                f"program-ids.validator_announce"
            )

    def test_gas_oracle_configmap(self, deployer_deployment: DeploymentInfo) -> None:
        """Validate gas oracle config has expected structure."""
        ns = deployer_deployment.namespace
        wait_for_configmap(ns, "hyperlane-gas-oracle-config", CONFIGMAP_TIMEOUT)

        configs = get_configmap_json(
            ns, "hyperlane-gas-oracle-config", "gas-oracle-configs.json"
        )
        assert isinstance(configs, list), "gas-oracle-configs is not a list"
        assert len(configs) > 0, "gas-oracle-configs is empty"

        for entry in configs:
            for field in ("domain", "token_exchange_rate", "gas_price", "token_decimals"):
                assert field in entry, f"gas-oracle entry missing '{field}'"

    def test_multisig_configmap(self, deployer_deployment: DeploymentInfo) -> None:
        """Validate multisig configs have validators and threshold."""
        ns = deployer_deployment.namespace
        wait_for_configmap(ns, "hyperlane-multisig-config", CONFIGMAP_TIMEOUT)

        for chain in CHAINS:
            key = f"{chain}-multisig.json"
            multisig = get_configmap_json(ns, "hyperlane-multisig-config", key)

            validators = multisig.get("validators")
            assert isinstance(validators, list) and len(validators) > 0, (
                f"{chain}: multisig.validators must be a non-empty list"
            )
            for addr in validators:
                assert addr.startswith("0x") and len(addr) == 42, (
                    f"{chain}: validator address not H160 format: {addr}"
                )

            threshold = multisig.get("threshold")
            assert isinstance(threshold, int) and threshold >= 1, (
                f"{chain}: multisig.threshold must be int >= 1, got {threshold}"
            )

    def test_registry_configmap(self, deployer_deployment: DeploymentInfo) -> None:
        """Validate registry ConfigMap has chain metadata for both chains."""
        ns = deployer_deployment.namespace
        wait_for_configmap(ns, "hyperlane-registry", CONFIGMAP_TIMEOUT)

        data = get_configmap_data(ns, "hyperlane-registry")
        raw = data.get("metadata.yaml", "")
        assert raw, "hyperlane-registry missing metadata.yaml"

        # Verify both chains are present with key fields (simple string checks
        # to avoid adding pyyaml dependency)
        for chain_name, chain_info in CHAINS.items():
            assert f"{chain_name}:" in raw, (
                f"registry metadata.yaml missing chain '{chain_name}'"
            )
            assert f"domainId: {chain_info['domain_id']}" in raw, (
                f"{chain_name}: registry missing expected domainId"
            )
            assert "rpcUrls:" in raw, f"{chain_name}: registry missing rpcUrls"
            assert "isTestnet:" in raw, f"{chain_name}: registry missing isTestnet"

    def test_programs_exist_on_chain(self, deployer_deployment: DeploymentInfo) -> None:
        """Verify core programs are actually deployed on-chain via RPC."""
        ns = deployer_deployment.namespace

        for chain_name, chain_info in CHAINS.items():
            program_ids = get_configmap_json(
                ns, "hyperlane-program-ids", f"{chain_name}-program-ids.json"
            )

            # Check mailbox and validator_announce (real deployed programs)
            for program in ("mailbox", "validator_announce"):
                assert_program_on_chain(
                    chain_name,
                    chain_info["rpc"],
                    program_ids[program],
                    label=program,
                )
