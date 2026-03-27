import logging
import re
import subprocess

import pytest

from lib.common import (
    CHAINS,
    CONFIGMAP_TIMEOUT,
    assert_program_on_chain,
    get_configmap_data,
    get_configmap_json,
    is_base58_pubkey,
    run_deployer_cli,
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
        """Validate gas oracle config has expected structure.

        Format is a nested map: {local_chain: {remote_chain: {oracleConfig: {...}, overhead: N}}}
        """
        ns = deployer_deployment.namespace
        wait_for_configmap(ns, "hyperlane-gas-oracle-config", CONFIGMAP_TIMEOUT)

        configs = get_configmap_json(
            ns, "hyperlane-gas-oracle-config", "gas-oracle-configs.json"
        )
        assert isinstance(configs, dict), "gas-oracle-configs is not a dict"

        remote_chain = {"gorchain": "solana", "solana": "gorchain"}

        for chain in CHAINS:
            assert chain in configs, f"gas-oracle-configs missing chain '{chain}'"
            remote = remote_chain[chain]
            assert remote in configs[chain], (
                f"gas-oracle-configs.{chain} missing remote chain '{remote}'"
            )
            entry = configs[chain][remote]

            oracle = entry.get("oracleConfig")
            assert isinstance(oracle, dict), (
                f"{chain}.{remote}: missing or invalid oracleConfig"
            )
            for field in ("tokenExchangeRate", "gasPrice", "tokenDecimals"):
                assert field in oracle, (
                    f"{chain}.{remote}.oracleConfig missing '{field}'"
                )

            # Verify exchange rate is non-trivial (not a dummy "1" value).
            # Rates vary widely with real prices — e.g. if gGOR >> SOL,
            # solana→gorchain rate can be << 1e19. Just check it's > 0.
            rate = int(oracle["tokenExchangeRate"])
            assert rate > 0, (
                f"{chain}.{remote}: tokenExchangeRate ({rate}) must be > 0"
            )
            # Verify gas price is positive. With EVM-default gasAmount (44-68k)
            # and large gGOR/SOL exchange rates, gasPrice must be very low
            # (e.g. 1 lamport per gas unit) to keep IGP quotes reasonable.
            gas_price = int(oracle["gasPrice"])
            assert gas_price > 0, (
                f"{chain}.{remote}: gasPrice ({gas_price}) must be > 0"
            )

            assert "overhead" in entry, f"{chain}.{remote} missing 'overhead'"

    def test_multisig_configmap(self, deployer_deployment: DeploymentInfo) -> None:
        """Validate multisig configs have validators and threshold.

        Each chain's config is keyed by the remote chain name (gorchain's ISM
        validates messages from solana, so its key is "solana").
        """
        ns = deployer_deployment.namespace
        wait_for_configmap(ns, "hyperlane-multisig-config", CONFIGMAP_TIMEOUT)

        # Map each chain to its expected remote chain key
        remote_chain = {"gorchain": "solana", "solana": "gorchain"}

        for chain in CHAINS:
            key = f"{chain}-multisig.json"
            multisig = get_configmap_json(ns, "hyperlane-multisig-config", key)

            remote = remote_chain[chain]
            assert remote in multisig, (
                f"{chain}: multisig config missing remote chain key '{remote}'"
            )
            remote_config = multisig[remote]

            validators = remote_config.get("validators")
            assert isinstance(validators, list) and len(validators) > 0, (
                f"{chain}: multisig.{remote}.validators must be a non-empty list"
            )
            for addr in validators:
                assert addr.startswith("0x") and len(addr) == 42, (
                    f"{chain}: validator address not H160 format: {addr}"
                )

            threshold = remote_config.get("threshold")
            assert isinstance(threshold, int) and threshold >= 1, (
                f"{chain}: multisig.{remote}.threshold must be int >= 1, got {threshold}"
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

    def test_ism_configured_on_chain(self, deployer_deployment: DeploymentInfo) -> None:
        """Verify multisig ISM validators and threshold are set on-chain.

        For each chain, query the ISM program with the remote chain's domain ID
        and check that the configured validators and threshold match the multisig
        ConfigMap.
        """
        ns = deployer_deployment.namespace
        remote_chain = {"gorchain": "solana", "solana": "gorchain"}

        for chain_name, chain_info in CHAINS.items():
            program_ids = get_configmap_json(
                ns, "hyperlane-program-ids", f"{chain_name}-program-ids.json"
            )
            ism_id = program_ids["multisig_ism_message_id"]
            remote = remote_chain[chain_name]
            remote_domain = str(CHAINS[remote]["domain_id"])

            # Get expected config from ConfigMap
            multisig = get_configmap_json(
                ns, "hyperlane-multisig-config", f"{chain_name}-multisig.json"
            )
            expected_validators = [
                v.lower() for v in multisig[remote]["validators"]
            ]
            expected_threshold = multisig[remote]["threshold"]

            # Query on-chain ISM state
            result = run_deployer_cli(
                "multisig-ism-message-id", "query",
                "--program-id", ism_id,
                "--domains", remote_domain,
                rpc=chain_info["rpc"],
            )
            output = result.stdout + result.stderr
            log.info(
                "%s: ISM query output:\n%s", chain_name, output[:2000],
            )
            assert result.returncode == 0, (
                f"{chain_name}: ISM query failed: {output}"
            )

            # Parse validators from Rust debug output
            # Format: validators: [\n  0xabcd...,\n]
            on_chain_validators = re.findall(r"(0x[0-9a-fA-F]{40})", output)
            assert on_chain_validators, (
                f"{chain_name}: no validator addresses found in ISM query output"
            )
            assert [v.lower() for v in on_chain_validators] == expected_validators, (
                f"{chain_name}: on-chain ISM validators {on_chain_validators} "
                f"don't match expected {expected_validators}"
            )

            # Parse threshold from: threshold: N
            threshold_match = re.search(r"threshold:\s*(\d+)", output)
            assert threshold_match, (
                f"{chain_name}: threshold not found in ISM query output"
            )
            assert int(threshold_match.group(1)) == expected_threshold, (
                f"{chain_name}: on-chain threshold {threshold_match.group(1)} "
                f"doesn't match expected {expected_threshold}"
            )

            log.info(
                "%s: ISM on-chain config verified (validators=%s, threshold=%d)",
                chain_name, on_chain_validators, expected_threshold,
            )

    def test_igp_configured_on_chain(self, deployer_deployment: DeploymentInfo) -> None:
        """Verify IGP gas oracle config is set on-chain.

        For each chain, query the IGP program and check that the gas oracle
        for the remote domain has the expected exchange rate, gas price, and
        token decimals.
        """
        ns = deployer_deployment.namespace
        remote_chain = {"gorchain": "solana", "solana": "gorchain"}

        # Load expected config from ConfigMap
        gas_config = get_configmap_json(
            ns, "hyperlane-gas-oracle-config", "gas-oracle-configs.json"
        )

        for chain_name, chain_info in CHAINS.items():
            program_ids = get_configmap_json(
                ns, "hyperlane-program-ids", f"{chain_name}-program-ids.json"
            )
            igp_id = program_ids["igp_program_id"]
            igp_account = program_ids["igp_account"]
            remote = remote_chain[chain_name]
            remote_domain = CHAINS[remote]["domain_id"]

            expected = gas_config[chain_name][remote]["oracleConfig"]

            # Query on-chain IGP state
            result = run_deployer_cli(
                "igp", "query",
                "--program-id", igp_id,
                "--igp-account", igp_account,
                rpc=chain_info["rpc"],
            )
            output = result.stdout + result.stderr
            log.info(
                "%s: IGP query output:\n%s", chain_name, output[:2000],
            )
            assert result.returncode == 0, (
                f"{chain_name}: IGP query failed: {output}"
            )

            # Verify the remote domain appears in gas_oracles
            assert str(remote_domain) in output, (
                f"{chain_name}: remote domain {remote_domain} not found in IGP output"
            )

            # Parse token_exchange_rate from: token_exchange_rate: <N>
            rate_match = re.search(r"token_exchange_rate:\s*(\d+)", output)
            assert rate_match, (
                f"{chain_name}: token_exchange_rate not found in IGP output"
            )
            assert rate_match.group(1) == expected["tokenExchangeRate"], (
                f"{chain_name}: on-chain token_exchange_rate {rate_match.group(1)} "
                f"doesn't match expected {expected['tokenExchangeRate']}"
            )

            # Parse gas_price from: gas_price: <N>
            gas_match = re.search(r"gas_price:\s*(\d+)", output)
            assert gas_match, (
                f"{chain_name}: gas_price not found in IGP output"
            )
            assert gas_match.group(1) == expected["gasPrice"], (
                f"{chain_name}: on-chain gas_price {gas_match.group(1)} "
                f"doesn't match expected {expected['gasPrice']}"
            )

            # Parse token_decimals from: token_decimals: <N>
            dec_match = re.search(r"token_decimals:\s*(\d+)", output)
            assert dec_match, (
                f"{chain_name}: token_decimals not found in IGP output"
            )
            assert int(dec_match.group(1)) == expected["tokenDecimals"], (
                f"{chain_name}: on-chain token_decimals {dec_match.group(1)} "
                f"doesn't match expected {expected['tokenDecimals']}"
            )

            log.info(
                "%s: IGP on-chain config verified (rate=%s, gas_price=%s, decimals=%s)",
                chain_name, rate_match.group(1), gas_match.group(1), dec_match.group(1),
            )
