import logging
import subprocess
import time

import pytest

from lib.common import (
    CHAINS,
    PortForward,
    get_spl_token_balance,
    run_deployer_cli,
    wait_for_token_balance,
)

log = logging.getLogger(__name__)

# Transfer amounts (6 decimals: 1 USDC = 1_000_000)
TRANSFER_AMOUNT_SOL_TO_GOR = 1_000_000  # 1 USDC
TRANSFER_AMOUNT_GOR_TO_SOL = 500_000  # 0.5 USDC

RELAY_TIMEOUT = 120  # seconds to wait for relayer delivery
POLL_INTERVAL = 5  # seconds between balance polls

TRANSFER_RETRIES = 3  # retries for transfer-remote calls
TRANSFER_RETRY_DELAY = 5  # seconds between retries
SETTLE_DELAY = 10  # seconds to let validator settle state after relay delivery


def _get_sender_pubkey(keypair_path: str) -> str:
    """Extract the base58 public key from a Solana keypair file."""
    result = subprocess.run(
        ["solana-keygen", "pubkey", keypair_path],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _run_transfer_remote(*args: str, rpc: str, retries: int = TRANSFER_RETRIES) -> subprocess.CompletedProcess:
    """Run transfer-remote with retries for transient on-chain failures.

    Token-2022 CPI calls (especially BurnChecked on freshly-created ATAs)
    can fail transiently with InvalidAccountData right after the relayer
    creates the account. Retrying after a short delay resolves this.
    """
    for attempt in range(1, retries + 1):
        result = run_deployer_cli("token", "transfer-remote", *args, rpc=rpc)
        if result.returncode == 0:
            return result
        output = result.stdout + result.stderr
        if attempt < retries:
            log.warning(
                "transfer-remote attempt %d/%d failed (rc=%d), retrying in %ds...\n%s",
                attempt, retries, result.returncode, TRANSFER_RETRY_DELAY,
                output[:500],
            )
            time.sleep(TRANSFER_RETRY_DELAY)
        else:
            return result
    return result  # unreachable, but keeps type checker happy


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestBridge:
    """Cross-chain warp route transfer tests.

    Tests are ordered: Solana→Gorchain runs first (mints synthetic tokens),
    then Gorchain→Solana uses those synthetic tokens for the reverse transfer.
    """

    def test_transfer_solana_to_gorchain(self, bridge_setup: dict) -> None:
        """Transfer collateral USDC from Solana to synthetic gUSDC on Gorchain."""
        sender = bridge_setup["sender_keypair"]
        token_mint = bridge_setup["token_mint"]
        synthetic_mint = bridge_setup["synthetic_mint"]
        solana_warp = bridge_setup["warp_programs"]["solana"]
        recipient = _get_sender_pubkey(sender)

        solana_rpc = CHAINS["solana"]["rpc"]
        gorchain_rpc = CHAINS["gorchain"]["rpc"]
        gorchain_domain = str(CHAINS["gorchain"]["domain_id"])

        # Record initial balances
        initial_solana = get_spl_token_balance(token_mint, sender, solana_rpc)
        initial_gorchain = get_spl_token_balance(synthetic_mint, sender, gorchain_rpc)
        log.info(
            "Initial balances — Solana USDC: %s, Gorchain gUSDC: %s",
            initial_solana, initial_gorchain,
        )

        assert initial_solana >= TRANSFER_AMOUNT_SOL_TO_GOR / 1_000_000, (
            f"Sender has insufficient USDC balance: {initial_solana}"
        )

        # Execute transfer
        log.info(
            "Transferring %d base units from Solana to Gorchain (domain %s)...",
            TRANSFER_AMOUNT_SOL_TO_GOR, gorchain_domain,
        )
        result = _run_transfer_remote(
            "/tmp/key.json",
            str(TRANSFER_AMOUNT_SOL_TO_GOR), gorchain_domain, recipient,
            "collateral",
            "--program-id", solana_warp,
            rpc=solana_rpc,
        )
        output = result.stdout + result.stderr
        log.info("transfer-remote output:\n%s", output[:2000])
        assert result.returncode == 0, (
            f"transfer-remote collateral failed: {output}"
        )

        # Verify source balance decreased
        post_solana = get_spl_token_balance(token_mint, sender, solana_rpc)
        expected_decrease = TRANSFER_AMOUNT_SOL_TO_GOR / 1_000_000
        assert initial_solana - post_solana >= expected_decrease - 0.001, (
            f"Solana USDC balance didn't decrease enough: {initial_solana} -> {post_solana}"
        )
        log.info("Solana USDC balance after transfer: %s", post_solana)

        # Wait for relay delivery on Gorchain
        expected_gorchain = initial_gorchain + TRANSFER_AMOUNT_SOL_TO_GOR / 1_000_000
        log.info(
            "Waiting for Gorchain gUSDC balance to reach %s...", expected_gorchain,
        )
        final_gorchain = wait_for_token_balance(
            synthetic_mint, sender, gorchain_rpc,
            expected_min=expected_gorchain,
            timeout=RELAY_TIMEOUT,
            poll_interval=POLL_INTERVAL,
            label="Gorchain gUSDC",
        )
        log.info(
            "Bridge Solana→Gorchain complete. gUSDC balance: %s", final_gorchain,
        )

    def test_transfer_gorchain_to_solana(self, bridge_setup: dict) -> None:
        """Transfer synthetic gUSDC from Gorchain back to collateral USDC on Solana."""
        # Let gorchain validator settle state after the forward relay delivery.
        # Token-2022 BurnChecked CPI fails with InvalidAccountData if the
        # synthetic ATA was just created by the relayer and state hasn't settled.
        log.info("Waiting %ds for validator state to settle...", SETTLE_DELAY)
        time.sleep(SETTLE_DELAY)

        sender = bridge_setup["sender_keypair"]
        token_mint = bridge_setup["token_mint"]
        synthetic_mint = bridge_setup["synthetic_mint"]
        gorchain_warp = bridge_setup["warp_programs"]["gorchain"]
        recipient = _get_sender_pubkey(sender)

        solana_rpc = CHAINS["solana"]["rpc"]
        gorchain_rpc = CHAINS["gorchain"]["rpc"]
        solana_domain = str(CHAINS["solana"]["domain_id"])

        # Record initial balances
        initial_gorchain = get_spl_token_balance(synthetic_mint, sender, gorchain_rpc)
        initial_solana = get_spl_token_balance(token_mint, sender, solana_rpc)
        log.info(
            "Initial balances — Gorchain gUSDC: %s, Solana USDC: %s",
            initial_gorchain, initial_solana,
        )

        assert initial_gorchain >= TRANSFER_AMOUNT_GOR_TO_SOL / 1_000_000, (
            f"Sender has insufficient gUSDC balance: {initial_gorchain}. "
            f"Did test_transfer_solana_to_gorchain run first?"
        )

        # Execute transfer
        log.info(
            "Transferring %d base units from Gorchain to Solana (domain %s)...",
            TRANSFER_AMOUNT_GOR_TO_SOL, solana_domain,
        )
        result = _run_transfer_remote(
            "/tmp/key.json",
            str(TRANSFER_AMOUNT_GOR_TO_SOL), solana_domain, recipient,
            "synthetic",
            "--program-id", gorchain_warp,
            rpc=gorchain_rpc,
        )
        output = result.stdout + result.stderr
        log.info("transfer-remote output:\n%s", output[:2000])
        assert result.returncode == 0, (
            f"transfer-remote synthetic failed: {output}"
        )

        # Verify source balance decreased
        post_gorchain = get_spl_token_balance(synthetic_mint, sender, gorchain_rpc)
        expected_decrease = TRANSFER_AMOUNT_GOR_TO_SOL / 1_000_000
        assert initial_gorchain - post_gorchain >= expected_decrease - 0.001, (
            f"Gorchain gUSDC balance didn't decrease enough: "
            f"{initial_gorchain} -> {post_gorchain}"
        )
        log.info("Gorchain gUSDC balance after transfer: %s", post_gorchain)

        # Wait for relay delivery on Solana
        expected_solana = initial_solana + TRANSFER_AMOUNT_GOR_TO_SOL / 1_000_000
        log.info(
            "Waiting for Solana USDC balance to reach %s...", expected_solana,
        )
        final_solana = wait_for_token_balance(
            token_mint, sender, solana_rpc,
            expected_min=expected_solana,
            timeout=RELAY_TIMEOUT,
            poll_interval=POLL_INTERVAL,
            label="Solana USDC",
        )
        log.info(
            "Bridge Gorchain→Solana complete. USDC balance: %s", final_solana,
        )

    def test_relayer_processed_messages(self, bridge_setup: dict) -> None:
        """Verify relayer metrics show successfully processed messages."""
        ns = bridge_setup["namespace"]
        cluster_id = bridge_setup["relayer_cluster_id"]

        # Find relayer pod by its app label (set by laconic-so to the cluster-id)
        result = subprocess.run(
            [
                "kubectl", "-n", ns, "get", "pods",
                "-l", f"app={cluster_id}",
                "-o", "jsonpath={.items[0].metadata.name}",
            ],
            capture_output=True, text=True, check=True,
        )
        pod_name = result.stdout.strip()
        assert pod_name, f"No relayer pod found with label app={cluster_id}"

        with PortForward(ns, f"pod/{pod_name}", 19091, 9091):
            metrics_result = subprocess.run(
                ["curl", "-sf", "http://localhost:19091/metrics"],
                capture_output=True, text=True, check=False,
            )
            assert metrics_result.returncode == 0, "Failed to fetch relayer metrics"
            metrics = metrics_result.stdout

        # Check for processed operations (messages delivered)
        assert "hyperlane" in metrics, "No hyperlane metrics found in relayer output"
        log.info("Relayer metrics contain hyperlane metrics (bridge messages processed)")
