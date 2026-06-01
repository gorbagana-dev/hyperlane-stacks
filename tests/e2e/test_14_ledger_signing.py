"""Ledger hardware-signing e2e test.

Skips unless a real Ledger run is configured. Proves the native
hyperlane-sealevel-client signs a transaction on the device and broadcasts it,
by round-tripping ownership of the solana mailbox (deployer -> Ledger -> deployer)
and asserting the owner is restored. The Ledger-signed step is the transfer back.

Run with:
    E2E_LEDGER=1 \
    HYPERLANE_SEALEVEL_CLIENT_BIN=/path/to/hyperlane-sealevel-client \
    E2E_LEDGER_PUBKEY=<ledger solana pubkey> \
    pytest tests/e2e/test_14_ledger_signing.py -m requires_ledger
"""

import os
import subprocess

import pytest

from lib.common import (
    CHAINS,
    ledger_available,
    run_deployer_cli,
    run_native_client,
)

LEDGER_KEYPAIR = "usb://ledger?key=0/0"


@pytest.mark.slow
@pytest.mark.requires_ledger
class TestLedgerSigning:
    def test_ledger_signs_ownership_roundtrip(
        self,
        bridge_setup: dict,
        bridge_state_loader,
        keypairs,
    ) -> None:
        if not ledger_available():
            pytest.skip(
                "Ledger run not configured: set E2E_LEDGER=1 and "
                "HYPERLANE_SEALEVEL_CLIENT_BIN"
            )
        ledger_pubkey = os.environ.get("E2E_LEDGER_PUBKEY")
        if not ledger_pubkey:
            pytest.skip("set E2E_LEDGER_PUBKEY to the Ledger's Solana pubkey")

        rpc = CHAINS["solana"]["rpc"]
        deployer_pubkey = keypairs.deployer_pubkey
        mailbox = bridge_state_loader.read_program_ids("solana")["mailbox"]

        # Fund the Ledger account so it can pay transaction fees.
        subprocess.run(
            ["solana", "airdrop", "1", ledger_pubkey, "--url", rpc],
            check=True,
            capture_output=True,
            text=True,
        )

        # 1. Transfer mailbox ownership deployer -> Ledger (signed by deployer hot key, via Docker).
        to_ledger = run_deployer_cli(
            "mailbox", "transfer-ownership",
            "--program-id", mailbox,
            ledger_pubkey,
            rpc=rpc,
        )
        assert to_ledger.returncode == 0, to_ledger.stderr

        # 2. Transfer back Ledger -> deployer, signed ON THE LEDGER via the native binary.
        #    This is the step under test. If it fails, ownership stays at the Ledger
        #    (the hot key cannot recover it) — the non-zero exit surfaces that loudly.
        back = run_native_client(
            "mailbox", "transfer-ownership",
            "--program-id", mailbox,
            deployer_pubkey,
            keypair=LEDGER_KEYPAIR,
            rpc=rpc,
        )
        assert back.returncode == 0, back.stderr

        # 3. Confirm ownership is restored to the deployer.
        query = run_deployer_cli(
            "mailbox", "query",
            "--program-id", mailbox,
            rpc=rpc,
        )
        assert query.returncode == 0, query.stderr
        assert deployer_pubkey in query.stdout, (
            f"mailbox owner not restored to deployer; query output:\n{query.stdout}"
        )
