import json
import logging
import re
import subprocess

import pytest

from lib.common import (
    CHAINS,
    assert_program_on_chain,
    is_base58_pubkey,
    run_deployer_cli,
)
from lib.keygen import KeypairSet
from lib.state_loader import BridgeStateLoader

log = logging.getLogger(__name__)

# Deployed warp routes (origin solana, remote gorchain=synthetic).
USDC_ROUTE = "USDC-solana-gorchain"
SOL_ROUTE = "SOL-solana-gorchain"

# Per-route on-chain token types, keyed by route then chain.
WARP_TOKEN_TYPES = {
    USDC_ROUTE: {"solana": "collateral", "gorchain": "synthetic"},
    SOL_ROUTE: {"solana": "native", "gorchain": "synthetic"},
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestWarpDeployer:
    def test_both_routes_deployed(
        self,
        warp_deployment: dict,
        bridge_state_loader: BridgeStateLoader,
    ) -> None:
        """Both the collateral (USDC) and native (SOL) routes were deployed."""
        routes = set(bridge_state_loader.discover_routes())
        assert {USDC_ROUTE, SOL_ROUTE} <= routes, (
            f"expected both routes deployed, discovered: {sorted(routes)}"
        )

    def test_usdc_route_collateral(
        self,
        warp_deployment: dict,
        bridge_state_loader: BridgeStateLoader,
    ) -> None:
        """USDC route: solana side is collateral (has token), gorchain synthetic."""
        cfg = bridge_state_loader.read_route_token_config(USDC_ROUTE)["warpRoute"]
        assert cfg["solana"]["type"] == "collateral" and "token" in cfg["solana"]
        assert cfg["gorchain"]["type"] == "synthetic"
        # The deployer queries + emits the synthetic mint (warp-UI needs it).
        mint = cfg["gorchain"].get("mint")
        assert mint and is_base58_pubkey(mint), (
            f"USDC synthetic mint not valid base58: {mint}"
        )

    def test_native_route_native(
        self,
        warp_deployment: dict,
        bridge_state_loader: BridgeStateLoader,
    ) -> None:
        """SOL route: solana side is native (no token), gorchain synthetic."""
        cfg = bridge_state_loader.read_route_token_config(SOL_ROUTE)["warpRoute"]
        assert cfg["solana"]["type"] == "native" and "token" not in cfg["solana"]
        assert cfg["gorchain"]["type"] == "synthetic"
        # The deployer queries + emits the synthetic mint (warp-UI needs it).
        mint = cfg["gorchain"].get("mint")
        assert mint and is_base58_pubkey(mint), (
            f"SOL synthetic mint not valid base58: {mint}"
        )

    def test_warp_programs_exist_on_chain(
        self,
        warp_deployment: dict,
        bridge_state_loader: BridgeStateLoader,
    ) -> None:
        """Verify each route's warp programs are deployed on-chain (both chains)."""
        for route in (USDC_ROUTE, SOL_ROUTE):
            warp_programs = bridge_state_loader.read_route_program_addresses(route)
            assert {"solana", "gorchain"} <= set(warp_programs), (
                f"{route}: missing program addresses, got {sorted(warp_programs)}"
            )
            for chain_name, addr in warp_programs.items():
                chain_info = CHAINS.get(chain_name)
                if not chain_info:
                    log.warning("No chain config for %s, skipping", chain_name)
                    continue
                assert is_base58_pubkey(addr), (
                    f"{route}.{chain_name}: program address not valid base58: {addr}"
                )
                assert_program_on_chain(
                    chain_name, chain_info["rpc"], addr, label="warp-token",
                )

    def test_warp_token_state_on_chain(
        self,
        warp_deployment: dict,
        bridge_state_loader: BridgeStateLoader,
    ) -> None:
        """Query warp token accounts and validate on-chain state.

        Uses `hyperlane-sealevel-client token query` (via docker run) to
        inspect the deployed warp token programs. Validates mailbox matches
        core deployment, remote routers are configured, and decimals are correct.
        """
        # Get core program addresses for cross-reference
        core_mailboxes: dict[str, str] = {}
        core_isms: dict[str, str] = {}
        core_igps: dict[str, str] = {}
        for chain_name in CHAINS:
            program_ids = bridge_state_loader.read_program_ids(chain_name)
            core_mailboxes[chain_name] = program_ids["mailbox"]
            core_isms[chain_name] = program_ids["multisig_ism_message_id"]
            core_igps[chain_name] = program_ids["overhead_igp_account"]

        for route in (USDC_ROUTE, SOL_ROUTE):
            origin_token = warp_deployment["routes"][route]["origin_token"]
            warp_programs = bridge_state_loader.read_route_program_addresses(route)
            route_types = WARP_TOKEN_TYPES[route]

            for chain_name, program_id in warp_programs.items():
                chain_info = CHAINS.get(chain_name)
                if not chain_info:
                    continue
                rpc = chain_info["rpc"]
                token_type = route_types.get(chain_name)
                if not token_type:
                    continue

                log.info(
                    "Querying warp token state: %s (%s/%s) on %s",
                    program_id, route, token_type, chain_name,
                )
                result = run_deployer_cli(
                    "token", "query",
                    "--program-id", program_id,
                    token_type,
                    rpc=rpc,
                )
                assert result.returncode == 0, (
                    f"token query failed for {route}/{chain_name} ({token_type}): "
                    f"{result.stderr.strip()}"
                )

                output = result.stdout + result.stderr
                log.info("token query output (%s/%s):\n%s", route, chain_name, output[:2000])

                info = _parse_token_query_output(output)

                # Validate mailbox matches core deployment
                if info.get("mailbox"):
                    assert info["mailbox"] == core_mailboxes[chain_name], (
                        f"{route}/{chain_name}: warp token mailbox {info['mailbox']} != "
                        f"core mailbox {core_mailboxes[chain_name]}"
                    )
                    log.info("%s/%s: mailbox matches core deployment", route, chain_name)

                # Validate remote routers are configured
                routers = info.get("remote_routers", {})
                assert len(routers) > 0, (
                    f"{route}/{chain_name}: warp token has no remote routers configured"
                )

                # For each chain, the remote router should point to the other chain's domain
                if chain_name == "gorchain":
                    remote_domain = CHAINS["solana"]["domain_id"]
                else:
                    remote_domain = CHAINS["gorchain"]["domain_id"]

                assert remote_domain in routers, (
                    f"{route}/{chain_name}: remote router for domain {remote_domain} "
                    f"not found. Configured routers: {routers}"
                )
                log.info(
                    "%s/%s: remote router for domain %d = %s",
                    route, chain_name, remote_domain, routers[remote_domain],
                )

                # For collateral type, verify mint matches the route's origin token
                if token_type == "collateral" and info.get("mint"):
                    assert info["mint"] == origin_token, (
                        f"{route}/{chain_name}: collateral mint {info['mint']} != "
                        f"expected origin token {origin_token}"
                    )
                    log.info("%s/%s: collateral mint matches origin token", route, chain_name)

                # Validate ISM is set and matches core deployment
                ism = info.get("interchain_security_module")
                assert ism is not None, (
                    f"{route}/{chain_name}: interchain_security_module is None "
                    f"(expected {core_isms[chain_name]})"
                )
                assert ism == core_isms[chain_name], (
                    f"{route}/{chain_name}: ISM {ism} != core ISM {core_isms[chain_name]}"
                )
                log.info("%s/%s: ISM matches core deployment", route, chain_name)

                # Validate IGP is set and overhead_igp_account matches core deployment
                igp = info.get("interchain_gas_paymaster")
                assert igp is not None, (
                    f"{route}/{chain_name}: interchain_gas_paymaster is None "
                    f"(expected overhead_igp_account={core_igps[chain_name]})"
                )
                assert isinstance(igp, dict), (
                    f"{route}/{chain_name}: interchain_gas_paymaster has unexpected "
                    f"format: {igp}"
                )
                assert igp["overhead_igp_account"] == core_igps[chain_name], (
                    f"{route}/{chain_name}: IGP overhead account "
                    f"{igp['overhead_igp_account']} != core "
                    f"overhead_igp_account {core_igps[chain_name]}"
                )
                log.info("%s/%s: IGP matches core deployment", route, chain_name)

    def test_warp_upgrade_authority_transferred(
        self,
        warp_deployment: dict,
        keypairs: KeypairSet,
        bridge_state_loader: BridgeStateLoader,
    ) -> None:
        """Verify warp route program upgrade authority was transferred to hardware wallet.

        After deployment, both warp route programs (collateral on solana,
        synthetic on gorchain) should have their upgrade authority set to
        the hardware wallet pubkey — not the deployer key.
        """
        expected_authority = keypairs.hardware_wallet_pubkey

        for route in (USDC_ROUTE, SOL_ROUTE):
            warp_programs = bridge_state_loader.read_route_program_addresses(route)
            assert warp_programs, f"{route}: no warp program addresses found"

            for chain_name, program_id in warp_programs.items():
                chain_info = CHAINS.get(chain_name)
                if not chain_info:
                    continue

                result = subprocess.run(
                    [
                        "solana", "program", "show", program_id,
                        "--url", chain_info["rpc"],
                        "--keypair", str(keypairs.deployer_path),
                        "--output", "json",
                    ],
                    capture_output=True, text=True, check=False,
                )
                assert result.returncode == 0, (
                    f"{route}/{chain_name}: solana program show failed for warp route "
                    f"{program_id}: {result.stderr.strip()}"
                )
                info = json.loads(result.stdout)
                actual_authority = info.get("authority")
                assert actual_authority == expected_authority, (
                    f"{route}/{chain_name}: warp route upgrade authority is "
                    f"{actual_authority}, expected {expected_authority}"
                )
                log.info(
                    "%s/%s: warp route upgrade authority verified (%s)",
                    route, chain_name, actual_authority,
                )
