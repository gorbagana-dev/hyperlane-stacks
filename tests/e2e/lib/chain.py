"""Chain node lifecycle for Hyperlane e2e tests."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .common import log_info, wait_for_rpc_health

SOLANA_LEDGER_DIR = Path("/tmp/solana-test-ledger")


def start_solana_test_validator(port: int = 18899) -> subprocess.Popen[str]:
    log_info(f"Starting Solana test validator on port {port}...")

    # Clean up old ledger
    if SOLANA_LEDGER_DIR.exists():
        shutil.rmtree(SOLANA_LEDGER_DIR)

    proc = subprocess.Popen(
        [
            "solana-test-validator",
            "--ledger",
            str(SOLANA_LEDGER_DIR),
            "--rpc-port",
            str(port),
            "--quiet",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    log_info(f"Solana test validator started (PID {proc.pid})")

    wait_for_rpc_health(f"http://localhost:{port}", timeout=30)
    return proc


def stop_solana_test_validator(proc: subprocess.Popen[str] | None) -> None:
    log_info("Stopping Solana test validator...")

    if proc is not None:
        try:
            proc.terminate()
            proc.wait(timeout=10)
            log_info(f"Solana test validator stopped (PID {proc.pid})")
        except ProcessLookupError:
            log_info("Solana test validator process already exited")
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            log_info(f"Solana test validator killed (PID {proc.pid})")
    else:
        log_info("No Solana test validator process provided")

    if SOLANA_LEDGER_DIR.exists():
        shutil.rmtree(SOLANA_LEDGER_DIR)


def start_gorchain() -> None:
    log_info("WARNING: Gorchain must be running externally on port 8899.")
    log_info("TODO: implement gorchain start via laconic-so")
    log_info("To start gorchain manually, use the gorchain-stacks repo:")
    log_info("  laconic-so --stack gorchain-stacks/... deploy init / create / start")


def stop_gorchain() -> None:
    log_info("WARNING: Gorchain stop is a placeholder.")
    log_info("TODO: implement gorchain stop via laconic-so")
