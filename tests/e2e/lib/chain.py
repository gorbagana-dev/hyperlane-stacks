"""Chain node lifecycle for Hyperlane e2e tests."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from .common import force_rmtree, log_info, run_cmd, wait_for_rpc_health

LEDGER_BASE = Path("/tmp")

GORCHAIN_STACKS_REPO = "git.vdb.to/LaconicNetwork/gorchain-stacks@update-repo-url"
CERC_REPO_BASE_DIR = Path(os.environ.get("CERC_REPO_BASE_DIR", os.path.expanduser("~/cerc")))


def _kill_stray_validator(port: int) -> None:
    """Kill a leftover solana-test-validator bound to the given port."""
    result = subprocess.run(
        ["lsof", "-ti", f"tcp:{port}"], capture_output=True, text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        for pid in result.stdout.strip().split("\n"):
            log_info(f"Killing stray process on port {port} (PID {pid})")
            subprocess.run(["kill", pid], capture_output=True)


def start_solana_test_validator(
    port: int = 18899,
    faucet_port: int = 19900,
    gossip_port: int = 18001,
    dynamic_port_range: str = "19050-19075",
    name: str = "solana",
) -> subprocess.Popen[str]:
    log_info(f"Starting {name} test validator on port {port}...")

    _kill_stray_validator(port)

    ledger_dir = LEDGER_BASE / f"test-ledger-{name}"
    if ledger_dir.exists():
        shutil.rmtree(ledger_dir)

    stderr_log = LEDGER_BASE / f"test-validator-{name}.stderr"
    stderr_fh = stderr_log.open("w")

    # Use non-default ports for gossip, faucet, and dynamic range to avoid
    # conflicts with gorchain running on the host (ports 8001, 9050-9075, 9900).
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
    )
    log_info(f"{name} test validator started (PID {proc.pid})")

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
    return proc


def stop_solana_test_validator(proc: subprocess.Popen[str] | None, name: str = "solana") -> None:
    log_info(f"Stopping {name} test validator...")

    if proc is not None:
        try:
            proc.terminate()
            proc.wait(timeout=10)
            log_info(f"{name} test validator stopped (PID {proc.pid})")
        except ProcessLookupError:
            log_info(f"{name} test validator process already exited")
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            log_info(f"{name} test validator killed (PID {proc.pid})")
    else:
        log_info(f"No {name} test validator process provided")

    ledger_dir = LEDGER_BASE / f"test-ledger-{name}"
    if ledger_dir.exists():
        shutil.rmtree(ledger_dir)


# ---------------------------------------------------------------------------
# Gorchain stack (docker via laconic-so)
# ---------------------------------------------------------------------------


def fetch_gorchain_stack() -> Path:
    """Fetch the gorchain-stacks repo via laconic-so and return the stack path."""
    log_info("Fetching gorchain-stacks repo...")
    run_cmd(["laconic-so", "fetch-stack", "--git-ssh", "--pull", GORCHAIN_STACKS_REPO])
    stack_path = CERC_REPO_BASE_DIR / "gorchain-stacks" / "stack-orchestrator" / "stacks" / "gorchain"
    if not stack_path.is_dir():
        raise RuntimeError(f"Gorchain stack not found at {stack_path}")
    return stack_path


def build_gorchain_images() -> None:
    """Fetch gorchain-stacks and build container images via laconic-so."""
    stack_path = fetch_gorchain_stack()

    log_info("Setting up gorchain repositories...")
    run_cmd(["laconic-so", "--stack", str(stack_path), "setup-repositories", "--git-ssh"])

    log_info("Building gorchain container images...")
    run_cmd(["laconic-so", "--stack", str(stack_path), "build-containers"])

    log_info("Gorchain images built successfully")


def start_gorchain_stack(deploy_dir: Path) -> None:
    """Deploy and start gorchain via laconic-so.

    Follows the same pattern as gorchain-stacks/tests: deploy init/create,
    write config.env, then start. Images must already be built via
    build_gorchain_images().
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
