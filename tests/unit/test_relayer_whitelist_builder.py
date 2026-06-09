"""Unit tests for build-relayer-whitelist.sh — pure tempdir, no cluster."""
import json
import subprocess
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "stack_orchestrator/data/config/warp-deployer-scripts-config/build-relayer-whitelist.sh"
)


def _run(tmp_path, warp_routes, menu, programs):
    state = tmp_path / "state"
    routes_dir = tmp_path / "config" / "warp-routes"
    routes_dir.mkdir(parents=True)
    for stem, doc in menu.items():
        (routes_dir / f"{stem}.json").write_text(json.dumps(doc))
    for name, pids in programs.items():
        out = state / "warp-routes" / name / "warp-deploy-outputs"
        out.mkdir(parents=True)
        (out / "program-ids.json").write_text(json.dumps(pids))
    env = {
        "STATE_DIR": str(state),
        "WARP_ROUTES_DIR": str(routes_dir),
        "WARP_ROUTES": warp_routes,
        "PATH": "/usr/local/bin:/usr/bin:/bin",
    }
    subprocess.run(["bash", str(SCRIPT)], env=env, check=True, capture_output=True, text=True)
    return json.loads((state / "relayer-whitelist.json").read_text())


def test_unions_both_chain_program_hexes(tmp_path):
    menu = {"usdc": {"name": "USDC"}}
    programs = {"USDC": {"gorchain": {"hex": "0x" + "11" * 32}, "solana": {"hex": "0x" + "22" * 32}}}
    wl = _run(tmp_path, "usdc", menu, programs)
    assert {"recipientaddress": "0x" + "11" * 32} in wl
    assert {"recipientaddress": "0x" + "22" * 32} in wl
    assert len(wl) == 2


def test_dedupes_shared_program_across_routes(tmp_path):
    menu = {"usdc": {"name": "USDC"}, "sol": {"name": "SOL"}}
    shared = "0x" + "11" * 32
    programs = {
        "USDC": {"gorchain": {"hex": shared}, "solana": {"hex": "0x" + "22" * 32}},
        "SOL": {"gorchain": {"hex": shared}, "solana": {"hex": "0x" + "33" * 32}},
    }
    wl = _run(tmp_path, "usdc,sol", menu, programs)
    recipients = [e["recipientaddress"] for e in wl]
    assert recipients.count(shared) == 1
    assert len(wl) == 3


def test_prefixes_bare_hex_with_0x(tmp_path):
    menu = {"usdc": {"name": "USDC"}}
    programs = {"USDC": {"gorchain": {"hex": "44" * 32}, "solana": {"hex": "0x" + "55" * 32}}}
    wl = _run(tmp_path, "usdc", menu, programs)
    assert {"recipientaddress": "0x" + "44" * 32} in wl


def test_empty_program_ids_yields_deny_all_sentinel(tmp_path):
    menu = {"empty": {"name": "EMPTY"}}
    programs = {"EMPTY": {}}  # no chains -> no rules -> deny-all sentinel
    wl = _run(tmp_path, "empty", menu, programs)
    assert wl == [{"recipientaddress": "0x" + "00" * 32}]
