"""hyp-d9c: assert ownership moved to the bridge owner and the relayer
whitelist covers exactly the deployed warp programs."""
import os
import re
from pathlib import Path

import pytest

from lib.common import run_deployer_cli

GORCHAIN_RPC = "http://localhost:8899"
SOLANA_RPC = "http://localhost:18899"
RPC = {"gorchain": GORCHAIN_RPC, "solana": SOLANA_RPC}
OWNER_RE = re.compile(r"owner:\s*Some\(\s*([1-9A-HJ-NP-Za-km-z]{32,44})")


@pytest.fixture(scope="module", autouse=True)
def _require_warp_deployment(warp_deployment):
    """Gate these assertions on the warp deploy (which transitively runs the core
    deploy) so the ownership handoffs and relayer-whitelist.json exist regardless
    of run order."""


@pytest.fixture(scope="module")
def owner_pubkey():
    pk = os.environ.get("BRIDGE_OWNER_PUBKEY")
    if not pk:
        pytest.skip("BRIDGE_OWNER_PUBKEY not set; ownership transfer not exercised")
    return pk


def _owner_from(stdout: str) -> str | None:
    m = OWNER_RE.search(stdout)
    return m.group(1) if m else None


def test_ism_owner_is_bridge_owner(bridge_state_loader, owner_pubkey):
    for chain in ("gorchain", "solana"):
        ism = bridge_state_loader.read_program_ids(chain)["multisig_ism_message_id"]
        res = run_deployer_cli(
            "multisig-ism-message-id", "query", "--program-id", ism, rpc=RPC[chain]
        )
        assert res.returncode == 0, res.stderr
        owner = _owner_from(res.stdout)
        assert owner == owner_pubkey, f"{chain} ISM owner {owner!r} != owner {owner_pubkey!r}"


def test_route_owners_are_bridge_owner(bridge_state_loader, owner_pubkey):
    routes = bridge_state_loader.discover_routes()
    assert routes, "no deployed warp routes found in state"
    for route in routes:
        token_config = bridge_state_loader.read_route_token_config(route)["warpRoute"]
        programs = bridge_state_loader.read_route_program_addresses(route)
        for chain, program_id in programs.items():
            token_type = token_config[chain]["type"]  # collateral|synthetic|native
            res = run_deployer_cli(
                "token", "query", "--program-id", program_id, token_type, rpc=RPC[chain]
            )
            assert res.returncode == 0, res.stderr
            owner = _owner_from(res.stdout)
            assert owner == owner_pubkey, (
                f"{route}/{chain} token owner {owner!r} != owner {owner_pubkey!r}"
            )


def test_whitelist_matches_deployed_programs(bridge_state_loader):
    whitelist = bridge_state_loader.read_json("relayer-whitelist.json")
    actual = {e["recipientaddress"] for e in whitelist}

    expected = set()
    for route in bridge_state_loader.discover_routes():
        wpids = bridge_state_loader.read_json(
            str(Path("warp-routes") / route / "warp-deploy-outputs" / "program-ids.json")
        )
        for _chain, entry in wpids.items():
            if isinstance(entry, dict) and entry.get("hex"):
                h = entry["hex"]
                expected.add(h if h.startswith("0x") else "0x" + h)

    assert expected, "no warp program hexes found in state"
    assert actual == expected, f"whitelist {actual} != deployed programs {expected}"
