"""Ledger device-signing e2e test (dormant fork-feature test: the architecture
no longer uses a hardware wallet — ownership goes to the Privy bridge-owner
wallet — but the forked client keeps built-in Ledger support).

Skips unless a real Ledger run is configured. Proves the native
hyperlane-sealevel-client signs a transaction on the device and broadcasts it,
by round-tripping ownership of the solana mailbox (bridge owner -> Ledger ->
bridge owner) and asserting the owner is restored. The Ledger-signed step is
the transfer back.

The deploy already transferred mailbox ownership to the bridge owner
(BRIDGE_OWNER_PUBKEY — in e2e a generated keypair, not a real Privy wallet),
so the round-trip starts from there, not the deployer.

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
        # The deploy moved mailbox ownership to the bridge owner; the round-trip
        # starts and ends there. Its keypair signs the lend-out leg (step 1).
        owner_pubkey = keypairs.owner_pubkey
        owner_keypair = str(keypairs.owner_path)
        mailbox = bridge_state_loader.read_program_ids("solana")["mailbox"]

        # Fund both fee payers: the bridge owner (signs step 1) and the Ledger
        # (signs step 2 on-device).
        for payer in (owner_pubkey, ledger_pubkey):
            subprocess.run(
                ["solana", "airdrop", "1", payer, "--url", rpc],
                check=True,
                capture_output=True,
                text=True,
            )

        # 1. Lend ownership bridge owner -> Ledger, signed by the owner
        #    keypair (via Docker).
        to_ledger = run_deployer_cli(
            "mailbox", "transfer-ownership",
            "--program-id", mailbox,
            ledger_pubkey,
            keypair_path=owner_keypair,
            rpc=rpc,
        )
        assert to_ledger.returncode == 0, to_ledger.stderr

        # 2. Transfer back Ledger -> bridge owner, signed ON THE LEDGER via the
        #    native binary. This is the step under test. If it fails, ownership
        #    stays at the Ledger — the non-zero exit surfaces that loudly.
        back = run_native_client(
            "mailbox", "transfer-ownership",
            "--program-id", mailbox,
            owner_pubkey,
            keypair=LEDGER_KEYPAIR,
            rpc=rpc,
        )
        assert back.returncode == 0, back.stderr

        # 3. Confirm ownership is restored to the bridge owner.
        query = run_deployer_cli(
            "mailbox", "query",
            "--program-id", mailbox,
            rpc=rpc,
        )
        assert query.returncode == 0, query.stderr
        assert owner_pubkey in query.stdout, (
            f"mailbox owner not restored to the bridge owner; query output:\n{query.stdout}"
        )
