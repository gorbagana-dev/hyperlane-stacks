import json
import logging
import re

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

log = logging.getLogger(__name__)

# Warp route: solana=collateral, gorchain=synthetic
WARP_TOKEN_TYPES = {
    "solana": "collateral",
    "gorchain": "synthetic",
}


def _parse_token_query_output(output: str) -> dict:
    """Parse key fields from `hyperlane-sealevel-client token query` debug output.

    The CLI prints Rust {:#?} format. We extract fields with regex.
    Returns dict with: mailbox, decimals, remote_decimals, remote_routers (dict),
    owner, and any other parseable fields.
    """
    info: dict = {}

    # Simple scalar fields: `field_name: value,`
    for field in ("mailbox", "decimals", "remote_decimals"):
        m = re.search(rf"^\s+{field}:\s+(.+?),?\s*$", output, re.MULTILINE)
        if m:
            info[field] = m.group(1).strip().rstrip(",")

    # owner: Option<Pubkey> — printed as `Some(...)` or `None`
    m = re.search(r"^\s+owner:\s+Some\(\s*(\S+)\s*\)", output, re.MULTILINE)
    if m:
        info["owner"] = m.group(1).rstrip(",)")
    else:
        m = re.search(r"^\s+owner:\s+None", output, re.MULTILINE)
        if m:
            info["owner"] = None

    # remote_routers: HashMap<u32, H256> — printed as { domain: 0x..., }
    routers: dict[int, str] = {}
    m = re.search(r"remote_routers:\s*\{([^}]*)\}", output, re.DOTALL)
    if m:
        for entry in re.finditer(r"(\d+):\s*(0x[0-9a-fA-F]+)", m.group(1)):
            routers[int(entry.group(1))] = entry.group(2)
    info["remote_routers"] = routers

    # interchain_security_module: Option<Pubkey> — Some(pubkey) or None
    m = re.search(r"interchain_security_module:\s+Some\(\s*(\S+)\s*\)", output)
    if m:
        info["interchain_security_module"] = m.group(1).rstrip(",)")
    else:
        m = re.search(r"interchain_security_module:\s+None", output)
        if m:
            info["interchain_security_module"] = None

    # interchain_gas_paymaster: Option<(Pubkey, InterchainGasPaymasterType)>
    # Rust {:#?} prints this across multiple indented lines:
    #   Some(
    #       (
    #           pubkey,
    #           OverheadIgp(
    #               account,
    #           ),
    #       ),
    #   ),
    m = re.search(
        r"interchain_gas_paymaster:\s+Some\(\s*\(\s*(\S+?),?\s*OverheadIgp\(\s*(\S+?),?\s*\)",
        output,
        re.DOTALL,
    )
    if m:
        info["interchain_gas_paymaster"] = {
            "igp_program": m.group(1).rstrip(",)"),
            "overhead_igp_account": m.group(2).rstrip(",)"),
        }
    else:
        m = re.search(r"interchain_gas_paymaster:\s+None", output)
        if m:
            info["interchain_gas_paymaster"] = None

    # CollateralPlugin fields
    m = re.search(r"mint:\s+(\w{32,44})", output)
    if m:
        info["mint"] = m.group(1)

    return info


def _get_warp_program_addresses(namespace: str) -> dict[str, str]:
    """Fetch warp-deploy-outputs ConfigMap and return {chain: base58_address}."""
    data = get_configmap_data(namespace, "hyperlane-warp-deploy-outputs")
    assert data, "hyperlane-warp-deploy-outputs has no data"

    programs: dict[str, str] = {}
    for _key, raw in data.items():
        for chain_name, entry in json.loads(raw).items():
            if entry.get("base58"):
                programs[chain_name] = entry["base58"]
    return programs


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestWarpDeployer:
    def test_warp_token_configmap(self, warp_deployment: dict) -> None:
        """Validate warp token-config has correct structure and references."""
        ns = warp_deployment["namespace"]
        token_mint = warp_deployment["token_mint"]

        wait_for_configmap(ns, "hyperlane-token-config", CONFIGMAP_TIMEOUT)
        parsed = get_configmap_json(ns, "hyperlane-token-config", "token-config.json")

        warp_route = parsed.get("warpRoute")
        assert isinstance(warp_route, dict), "token-config missing 'warpRoute' object"

        # Verify token mint matches
        assert warp_route.get("tokenMint") == token_mint, (
            f"tokenMint mismatch: expected {token_mint}, got {warp_route.get('tokenMint')}"
        )

        # Verify chain references
        collateral = warp_route.get("collateral")
        assert isinstance(collateral, dict), "warpRoute missing 'collateral'"
        assert collateral.get("chain") == "solana", (
            f"collateral.chain: expected 'solana', got '{collateral.get('chain')}'"
        )

        synthetic = warp_route.get("synthetic")
        assert isinstance(synthetic, dict), "warpRoute missing 'synthetic'"
        assert synthetic.get("chain") == "gorchain", (
            f"synthetic.chain: expected 'gorchain', got '{synthetic.get('chain')}'"
        )

        # Verify mailbox addresses are valid
        for side_name, side in [("collateral", collateral), ("synthetic", synthetic)]:
            mailbox = side.get("mailbox")
            assert mailbox and is_base58_pubkey(mailbox), (
                f"warpRoute.{side_name}.mailbox is not valid base58: {mailbox}"
            )

    def test_warp_deploy_outputs(self, warp_deployment: dict) -> None:
        """Validate warp-deploy-outputs has program IDs in hex+base58 format."""
        ns = warp_deployment["namespace"]
        wait_for_configmap(ns, "hyperlane-warp-deploy-outputs", CONFIGMAP_TIMEOUT)

        data = get_configmap_data(ns, "hyperlane-warp-deploy-outputs")
        assert data, "hyperlane-warp-deploy-outputs has no data"

        # The CLI writes program-ids.json with chain -> {hex, base58} entries
        for key, raw in data.items():
            parsed = json.loads(raw)
            assert isinstance(parsed, dict), f"warp output '{key}' is not a JSON object"

            for chain_name, entry in parsed.items():
                assert isinstance(entry, dict), (
                    f"warp output '{key}'.{chain_name} is not an object"
                )
                hex_val = entry.get("hex")
                base58_val = entry.get("base58")
                assert hex_val and hex_val.startswith("0x"), (
                    f"warp output '{key}'.{chain_name}.hex missing or not 0x-prefixed"
                )
                assert base58_val and is_base58_pubkey(base58_val), (
                    f"warp output '{key}'.{chain_name}.base58 not valid: {base58_val}"
                )

    def test_warp_programs_exist_on_chain(self, warp_deployment: dict) -> None:
        """Verify warp route programs are actually deployed on-chain."""
        ns = warp_deployment["namespace"]
        warp_programs = _get_warp_program_addresses(ns)

        for chain_name, addr in warp_programs.items():
            chain_info = CHAINS.get(chain_name)
            if not chain_info:
                log.warning("No chain config for %s, skipping", chain_name)
                continue
            assert_program_on_chain(
                chain_name, chain_info["rpc"], addr, label="warp-token",
            )

    def test_warp_token_state_on_chain(self, warp_deployment: dict) -> None:
        """Query warp token accounts and validate on-chain state.

        Uses `hyperlane-sealevel-client token query` (via docker run) to
        inspect the deployed warp token programs. Validates mailbox matches
        core deployment, remote routers are configured, and decimals are correct.
        """
        ns = warp_deployment["namespace"]
        token_mint = warp_deployment["token_mint"]

        warp_programs = _get_warp_program_addresses(ns)

        # Get core program addresses for cross-reference
        core_mailboxes: dict[str, str] = {}
        core_isms: dict[str, str] = {}
        core_igps: dict[str, str] = {}
        for chain_name in CHAINS:
            program_ids = get_configmap_json(
                ns, "hyperlane-program-ids", f"{chain_name}-program-ids.json"
            )
            core_mailboxes[chain_name] = program_ids["mailbox"]
            core_isms[chain_name] = program_ids["multisig_ism_message_id"]
            core_igps[chain_name] = program_ids["overhead_igp_account"]

        for chain_name, program_id in warp_programs.items():
            chain_info = CHAINS.get(chain_name)
            if not chain_info:
                continue
            rpc = chain_info["rpc"]
            token_type = WARP_TOKEN_TYPES.get(chain_name)
            if not token_type:
                continue

            log.info(
                "Querying warp token state: %s (%s) on %s",
                program_id, token_type, chain_name,
            )
            result = run_deployer_cli(
                "token", "query",
                "--program-id", program_id,
                token_type,
                rpc=rpc,
            )
            assert result.returncode == 0, (
                f"token query failed for {chain_name} ({token_type}): "
                f"{result.stderr.strip()}"
            )

            output = result.stdout + result.stderr
            log.info("token query output (%s):\n%s", chain_name, output[:2000])

            info = _parse_token_query_output(output)

            # Validate mailbox matches core deployment
            if info.get("mailbox"):
                assert info["mailbox"] == core_mailboxes[chain_name], (
                    f"{chain_name}: warp token mailbox {info['mailbox']} != "
                    f"core mailbox {core_mailboxes[chain_name]}"
                )
                log.info("%s: mailbox matches core deployment", chain_name)

            # Validate decimals (SPL token = 6 for USDC-like)
            if info.get("decimals"):
                assert info["decimals"] == "6", (
                    f"{chain_name}: expected decimals=6, got {info['decimals']}"
                )

            # Validate remote routers are configured
            routers = info.get("remote_routers", {})
            assert len(routers) > 0, (
                f"{chain_name}: warp token has no remote routers configured"
            )

            # For each chain, the remote router should point to the other chain's domain
            if chain_name == "gorchain":
                remote_domain = CHAINS["solana"]["domain_id"]
            else:
                remote_domain = CHAINS["gorchain"]["domain_id"]

            assert remote_domain in routers, (
                f"{chain_name}: remote router for domain {remote_domain} not found. "
                f"Configured routers: {routers}"
            )
            log.info(
                "%s: remote router for domain %d = %s",
                chain_name, remote_domain, routers[remote_domain],
            )

            # For collateral type, verify mint matches the test SPL token
            if token_type == "collateral" and info.get("mint"):
                assert info["mint"] == token_mint, (
                    f"{chain_name}: collateral mint {info['mint']} != "
                    f"expected token mint {token_mint}"
                )
                log.info("%s: collateral mint matches test token", chain_name)

            # Validate ISM is set and matches core deployment
            ism = info.get("interchain_security_module")
            assert ism is not None, (
                f"{chain_name}: interchain_security_module is None "
                f"(expected {core_isms[chain_name]})"
            )
            assert ism == core_isms[chain_name], (
                f"{chain_name}: ISM {ism} != core ISM {core_isms[chain_name]}"
            )
            log.info("%s: ISM matches core deployment", chain_name)

            # Validate IGP is set and overhead_igp_account matches core deployment
            igp = info.get("interchain_gas_paymaster")
            assert igp is not None, (
                f"{chain_name}: interchain_gas_paymaster is None "
                f"(expected overhead_igp_account={core_igps[chain_name]})"
            )
            assert isinstance(igp, dict), (
                f"{chain_name}: interchain_gas_paymaster has unexpected format: {igp}"
            )
            assert igp["overhead_igp_account"] == core_igps[chain_name], (
                f"{chain_name}: IGP overhead account {igp['overhead_igp_account']} != "
                f"core overhead_igp_account {core_igps[chain_name]}"
            )
            log.info("%s: IGP matches core deployment", chain_name)
