"""E2E tests for IGP fee claiming.

Tests verify that the igp claim command successfully transfers accumulated
IGP fees from the IGP account to the configured beneficiary. Should run after
the bridge test (test_06_bridge) which generates claimable fees.

The claim instruction is permissionless — any funded keypair can call it,
and funds always go to the on-chain beneficiary (set at deploy time).
"""

from __future__ import annotations

import logging
import re
import subprocess

import pytest

from lib.common import CHAINS, run_deployer_cli
from lib.state_loader import BridgeStateLoader

log = logging.getLogger(__name__)


def _get_sol_balance(rpc: str, address: str) -> float:
    """Get SOL balance of an address via solana CLI."""
    result = subprocess.run(
        ["solana", "balance", "--url", rpc, address],
        capture_output=True, text=True, check=True,
    )
    # Output: "1.23456789 SOL"
    return float(result.stdout.strip().split()[0])


def _get_igp_beneficiary(
    rpc: str, program_id: str, igp_account: str,
) -> str:
    """Query the IGP account and extract the beneficiary address."""
    result = run_deployer_cli(
        "igp", "query",
        "--program-id", program_id,
        "--igp-account", igp_account,
        rpc=rpc,
    )
    assert result.returncode == 0, f"igp query failed: {result.stderr}"

    # Parse: beneficiary: <base58 address>
    match = re.search(r"beneficiary:\s*([1-9A-HJ-NP-Za-km-z]{32,44})", result.stdout)
    assert match, f"Could not parse beneficiary from igp query output:\n{result.stdout}"
    return match.group(1)


def _run_igp_claim(
    rpc: str, program_id: str, igp_account: str,
) -> subprocess.CompletedProcess[str]:
    """Run igp claim using the deployer keypair (permissionless, any key works)."""
    return run_deployer_cli(
        "igp", "claim",
        "--program-id", program_id,
        "--igp-account", igp_account,
        rpc=rpc,
    )


@pytest.mark.slow
class TestFeeClaim:
    """Tests for IGP fee claiming on both chains."""

    @pytest.fixture(autouse=True)
    def _igp_data(
        self, bridge_setup: dict, bridge_state_loader: BridgeStateLoader,
    ) -> None:
        """Load IGP program IDs and accounts from deployer state files.

        Depends on bridge_setup to ensure the full infrastructure (deployer,
        validators, relayer, bridge) is up before claiming fees.
        """
        self.igp = {}
        for chain in ("gorchain", "solana"):
            program_ids = bridge_state_loader.read_program_ids(chain)
            self.igp[chain] = {
                "program_id": program_ids["igp_program_id"],
                "igp_account": program_ids["igp_account"],
                "rpc": CHAINS[chain]["rpc"],
            }

    def _test_fee_claim_on_chain(self, chain: str) -> None:
        """Test fee claim on a single chain."""
        info = self.igp[chain]
        rpc = info["rpc"]
        program_id = info["program_id"]
        igp_account = info["igp_account"]

        # Get beneficiary address
        beneficiary = _get_igp_beneficiary(rpc, program_id, igp_account)
        log.info("  %s beneficiary: %s", chain, beneficiary)

        # Check current balances
        igp_balance_before = _get_sol_balance(rpc, igp_account)
        beneficiary_balance_before = _get_sol_balance(rpc, beneficiary)
        log.info(
            "  %s before — IGP: %s SOL, beneficiary: %s SOL",
            chain, igp_balance_before, beneficiary_balance_before,
        )

        # Get rent-exempt minimum (approximate: data size ~364 bytes → ~0.00346 SOL)
        # If IGP balance is at or near rent-exempt, there's nothing to claim
        # We use a threshold: if less than 0.001 SOL above estimated rent, warn and skip
        estimated_rent = 0.004  # conservative upper bound
        claimable = igp_balance_before - estimated_rent
        if claimable <= 0:
            pytest.skip(
                f"{chain}: IGP balance ({igp_balance_before} SOL) is at rent-exempt "
                f"minimum — no fees to claim. Run bridge test first with proper "
                f"gas oracle config."
            )

        log.info("  %s claimable (est): %.6f SOL", chain, claimable)

        # Run claim
        result = _run_igp_claim(rpc, program_id, igp_account)
        assert result.returncode == 0, (
            f"{chain} igp claim failed (rc={result.returncode}):\n{result.stderr}"
        )
        log.info("  %s claim transaction submitted", chain)

        # Verify balances changed
        igp_balance_after = _get_sol_balance(rpc, igp_account)
        beneficiary_balance_after = _get_sol_balance(rpc, beneficiary)
        log.info(
            "  %s after — IGP: %s SOL, beneficiary: %s SOL",
            chain, igp_balance_after, beneficiary_balance_after,
        )

        # IGP balance should have decreased
        assert igp_balance_after < igp_balance_before, (
            f"{chain}: IGP balance did not decrease after claim "
            f"(before={igp_balance_before}, after={igp_balance_after})"
        )

        # Beneficiary balance should have increased
        assert beneficiary_balance_after > beneficiary_balance_before, (
            f"{chain}: beneficiary balance did not increase after claim "
            f"(before={beneficiary_balance_before}, after={beneficiary_balance_after})"
        )

        transferred = beneficiary_balance_after - beneficiary_balance_before
        log.info("  %s claimed: %.6f SOL", chain, transferred)

    def test_fee_claim_gorchain(self) -> None:
        """Claim accumulated IGP fees on Gorchain."""
        self._test_fee_claim_on_chain("gorchain")

    def test_fee_claim_solana(self) -> None:
        """Claim accumulated IGP fees on Solana."""
        self._test_fee_claim_on_chain("solana")
