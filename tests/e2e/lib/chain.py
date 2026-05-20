"""Chain node lifecycle for Hyperlane e2e tests."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from .common import force_rmtree, log_info, run_cmd, wait_for_rpc_health

LEDGER_BASE = Path(__file__).resolve().parent.parent / ".data"

GORCHAIN_STACKS_REPO = "github.com/gorbagana-dev/gorchain-stacks@main"
CERC_REPO_BASE_DIR = Path(os.environ.get("CERC_REPO_BASE_DIR", os.path.expanduser("~/cerc")))


def _is_port_open(port: int) -> bool:
    """Check if something is listening on the given port."""
    result = subprocess.run(
        ["lsof", "-ti", f"tcp:{port}"], capture_output=True, text=True,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _kill_by_port(port: int) -> None:
    """Kill all processes bound to the given port."""
    result = subprocess.run(
        ["lsof", "-ti", f"tcp:{port}"], capture_output=True, text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        for pid in result.stdout.strip().split("\n"):
            log_info(f"Killing process on port {port} (PID {pid})")
            subprocess.run(["kill", pid], capture_output=True)


def is_solana_validator_running(port: int = 18899) -> bool:
    """Check if a Solana test validator is healthy on the given port."""
    result = subprocess.run(
        ["curl", "-sf", f"http://localhost:{port}/health", "-o", "/dev/null"],
        capture_output=True,
    )
    return result.returncode == 0


def start_solana_test_validator(
    port: int = 18899,
    faucet_port: int = 19900,
    gossip_port: int = 18001,
    dynamic_port_range: str = "19050-19075",
    name: str = "solana",
    fresh: bool = False,
) -> None:
    log_info(f"Starting {name} test validator on port {port}...")

    _kill_by_port(port)

    LEDGER_BASE.mkdir(parents=True, exist_ok=True)

    ledger_dir = LEDGER_BASE / f"test-ledger-{name}"
    if fresh and ledger_dir.exists():
        log_info(f"Wiping existing ledger: {ledger_dir}")
        shutil.rmtree(ledger_dir)
    elif ledger_dir.exists():
        log_info(f"Reusing existing ledger: {ledger_dir}")

    stderr_log = LEDGER_BASE / f"test-validator-{name}.stderr"
    stderr_fh = stderr_log.open("w")

    # start_new_session=True detaches the validator from pytest's process
    # group so Ctrl+C doesn't kill it. This preserves chain state (deployed
    # programs, accounts, balances) across pytest re-runs.
    proc = subprocess.Popen(
        [
            "solana-test-validator",
            "--ledger",
            str(ledger_dir),
            "--rpc-port",
            str(port),
            "--faucet-port",
            str(faucet_port),
            "--gossip-port",
            str(gossip_port),
            "--dynamic-port-range",
            dynamic_port_range,
            "--quiet",
        ],
        stdout=subprocess.DEVNULL,
        stderr=stderr_fh,
        start_new_session=True,
    )
    log_info(f"{name} test validator started (PID {proc.pid}, detached)")

    # Give the process a moment to fail on startup (e.g. port conflicts)
    time.sleep(2)
    rc = proc.poll()
    if rc is not None:
        stderr_fh.close()
        stderr_output = stderr_log.read_text().strip()
        raise RuntimeError(
            f"{name} test validator exited immediately (rc={rc}): {stderr_output}"
        )
    # Close the file handle — stderr continues writing to the file via the OS fd
    stderr_fh.close()

    wait_for_rpc_health(f"http://localhost:{port}", timeout=60)


def stop_solana_test_validator(port: int = 18899, name: str = "solana", delete_data: bool = True) -> None:
    """Kill the Solana test validator by port and optionally clean up its ledger."""
    log_info(f"Stopping {name} test validator on port {port}...")
    _kill_by_port(port)

    if delete_data:
        ledger_dir = LEDGER_BASE / f"test-ledger-{name}"
        if ledger_dir.exists():
            log_info(f"Removing ledger: {ledger_dir}")
            shutil.rmtree(ledger_dir)


# ---------------------------------------------------------------------------
# Gorchain stack (docker via laconic-so)
# ---------------------------------------------------------------------------


def fetch_gorchain_stack() -> Path:
    """Fetch the gorchain-stacks repo via laconic-so and return the stack path."""
    log_info("Fetching gorchain-stacks repo...")
    run_cmd(["laconic-so", "fetch-stack", "--pull", GORCHAIN_STACKS_REPO])
    stack_path = CERC_REPO_BASE_DIR / "gorchain-stacks" / "stack-orchestrator" / "stacks" / "gorchain"
    if not stack_path.is_dir():
        raise RuntimeError(f"Gorchain stack not found at {stack_path}")
    return stack_path


def start_gorchain_stack(deploy_dir: Path) -> None:
    """Deploy and start gorchain via laconic-so.

    Follows the same pattern as gorchain-stacks/tests: deploy init/create,
    write config.env, then start. Uses published images from ghcr.io.
    """
    stack_path = fetch_gorchain_stack()

    if deploy_dir.exists():
        log_info(f"Removing stale gorchain deployment directory: {deploy_dir}")
        force_rmtree(deploy_dir)

    spec_file = deploy_dir.parent / "gorchain-spec.yml"

    log_info("Running gorchain deploy init...")
    run_cmd([
        "laconic-so", "--stack", str(stack_path),
        "deploy", "init", "--output", str(spec_file),
    ])

    log_info("Running gorchain deploy create...")
    run_cmd([
        "laconic-so", "--stack", str(stack_path),
        "deploy", "create", "--spec-file", str(spec_file),
        "--deployment-dir", str(deploy_dir),
    ])

    # Write environment config after deploy create (same as gorchain-stacks tests)
    config_env = deploy_dir / "config.env"
    config_env.write_text(
        "PUBLIC_GOSSIP_HOST=127.0.0.1\n"
        "PUBLIC_RPC_ADDRESS=127.0.0.1:8899\n"
        "GORCHAIN_DEV_RPC=true\n"
    )
    log_info(f"Wrote gorchain config to {config_env}")

    log_info("Starting gorchain stack...")
    run_cmd(["laconic-so", "deployment", "--dir", str(deploy_dir), "start"])

    log_info("Waiting for gorchain RPC to be healthy...")
    wait_for_rpc_health("http://localhost:8899", timeout=600)
    log_info("Gorchain stack is running")


def stop_gorchain_stack(deploy_dir: Path) -> None:
    """Stop and clean up gorchain stack."""
    if not deploy_dir.is_dir():
        log_info("Gorchain deployment directory not found, skipping stop")
        return

    log_info("Stopping gorchain stack...")
    run_cmd(
        ["laconic-so", "deployment", "--dir", str(deploy_dir), "stop", "--delete-volumes"],
        check=False,
    )
    log_info("Gorchain stack stopped")
