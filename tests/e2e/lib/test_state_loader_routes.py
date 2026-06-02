"""Unit tests for BridgeStateLoader route discovery + per-route reads.

Pure-Python tests: they exercise the loader against a tmp_path fixture laid
out like deploy.sh's per-route artifacts under <state>/warp-routes/<route>/.
"""

from __future__ import annotations

import json
from pathlib import Path

from lib.state_loader import BridgeStateLoader


def _write_route(
    state_dir: Path,
    route: str,
    program_ids: dict,
    token_config: dict | None = None,
) -> None:
    """Lay down warp-routes/<route>/{token-config.json,warp-deploy-outputs/}."""
    route_dir = state_dir / "warp-routes" / route
    outputs = route_dir / "warp-deploy-outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    (outputs / "program-ids.json").write_text(json.dumps(program_ids))
    if token_config is None:
        token_config = {"warpRoute": {"name": route}}
    (route_dir / "token-config.json").write_text(json.dumps(token_config))


def test_discover_routes_returns_route_dir_names(tmp_path: Path) -> None:
    _write_route(tmp_path, "usdc", {"gorchain": {"base58": "A1"}})
    _write_route(tmp_path, "native", {"solana": {"base58": "B2"}})

    loader = BridgeStateLoader(tmp_path)

    assert sorted(loader.discover_routes()) == ["native", "usdc"]


def test_discover_routes_empty_when_no_warp_routes_dir(tmp_path: Path) -> None:
    loader = BridgeStateLoader(tmp_path)

    assert loader.discover_routes() == []


def test_read_route_program_addresses_maps_chain_to_base58(tmp_path: Path) -> None:
    _write_route(
        tmp_path,
        "usdc",
        {
            "gorchain": {"base58": "GORADDR", "hex": "0xdead"},
            "solana": {"base58": "SOLADDR"},
        },
    )

    loader = BridgeStateLoader(tmp_path)

    assert loader.read_route_program_addresses("usdc") == {
        "gorchain": "GORADDR",
        "solana": "SOLADDR",
    }


def test_read_route_token_config_returns_parsed_config(tmp_path: Path) -> None:
    _write_route(
        tmp_path,
        "native",
        {"solana": {"base58": "B2"}},
        token_config={"warpRoute": {"name": "native", "solana": {"type": "native"}}},
    )

    loader = BridgeStateLoader(tmp_path)

    config = loader.read_route_token_config("native")

    assert config["warpRoute"]["name"] == "native"
    assert config["warpRoute"]["solana"]["type"] == "native"
