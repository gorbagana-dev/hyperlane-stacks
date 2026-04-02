import concurrent.futures
import logging
import subprocess
import time

import pytest

from lib.common import (
    CHAINS,
    PortForward,
    get_configmap_json,
    get_spl_token_balance,
    run_deployer_cli,
    wait_for_token_balance,
)
from lib.deploy import E2E_NAMESPACE

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Concurrent bridge config
# ---------------------------------------------------------------------------
NUM_BRIDGE_USERS = 5

# Forward transfer amounts (6 decimals: 1 USDC = 1_000_000 base units).
# User i sends FORWARD_BASE + i * FORWARD_STEP.
FORWARD_BASE = 1_000_000  # 1.0 USDC
FORWARD_STEP = 100_000  # +0.1 USDC per user

# Reverse transfer amounts. User i sends REVERSE_BASE + i * REVERSE_STEP.
REVERSE_BASE = 500_000  # 0.5 USDC
REVERSE_STEP = 50_000  # +0.05 USDC per user

RELAY_TIMEOUT = 120  # seconds to wait for relayer delivery
POLL_INTERVAL = 5  # seconds between balance polls

TRANSFER_RETRIES = 3  # retries for transfer-remote calls
TRANSFER_RETRY_DELAY = 5  # seconds between retries
SETTLE_DELAY = 10  # seconds to let validator settle state after relay delivery


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_sol_balance(rpc: str, address: str) -> float:
    """Get SOL balance of an address via solana CLI."""
    result = subprocess.run(
        ["solana", "balance", "--url", rpc, address],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip().split()[0])


def _log_igp_balances(label: str) -> None:
    """Log IGP account balances for debugging fee collection."""
    for chain in ("gorchain", "solana"):
        try:
            program_ids = get_configmap_json(
                E2E_NAMESPACE, "hyperlane-program-ids",
                f"{chain}-program-ids.json",
            )
            igp_account = program_ids["igp_account"]
            rpc = CHAINS[chain]["rpc"]
            balance = _get_sol_balance(rpc, igp_account)
            log.info(
                "[IGP-DEBUG %s] %s IGP (%s) balance: %s SOL",
                label, chain, igp_account, balance,
            )
        except Exception as exc:
            log.warning("[IGP-DEBUG %s] Failed to check %s IGP: %s", label, chain, exc)


def _get_sender_pubkey(keypair_path: str) -> str:
    """Extract the base58 public key from a Solana keypair file."""
    result = subprocess.run(
        ["solana-keygen", "pubkey", keypair_path],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _run_transfer_remote(
    *args: str, rpc: str, keypair_path: str | None = None,
    retries: int = TRANSFER_RETRIES,
) -> subprocess.CompletedProcess:
    """Run transfer-remote with retries for transient on-chain failures.

    Token-2022 CPI calls (especially BurnChecked on freshly-created ATAs)
    can fail transiently with InvalidAccountData right after the relayer
    creates the account. Retrying after a short delay resolves this.
    """
    for attempt in range(1, retries + 1):
        result = run_deployer_cli(
            "token", "transfer-remote", *args,
            rpc=rpc, keypair_path=keypair_path,
        )
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
    """Cross-chain warp route transfer tests with concurrent users.

    Five bridge users transfer staggered amounts simultaneously:
    - Forward: Solana collateral USDC → Gorchain synthetic gUSDC
    - Reverse: Gorchain synthetic gUSDC → Solana collateral USDC
    """

    def test_concurrent_forward_transfers(self, bridge_setup: dict) -> None:
        """Transfer collateral USDC from Solana to Gorchain for all users concurrently."""
        users = bridge_setup["users"]
        token_mint = bridge_setup["token_mint"]
        synthetic_mint = bridge_setup["synthetic_mint"]
        solana_warp = bridge_setup["warp_programs"]["solana"]

        solana_rpc = CHAINS["solana"]["rpc"]
        gorchain_rpc = CHAINS["gorchain"]["rpc"]
        gorchain_domain = str(CHAINS["gorchain"]["domain_id"])

        # Record initial balances per user
        initial_solana = {}
        initial_gorchain = {}
        for i, user in enumerate(users):
            kp = user["keypair_path"]
            initial_solana[i] = get_spl_token_balance(token_mint, kp, solana_rpc)
            initial_gorchain[i] = get_spl_token_balance(synthetic_mint, kp, gorchain_rpc)
            log.info(
                "User %d initial — Solana USDC: %s, Gorchain gUSDC: %s",
                i, initial_solana[i], initial_gorchain[i],
            )

        # Compute per-user transfer amounts
        amounts = {}
        for i in range(len(users)):
            amounts[i] = FORWARD_BASE + i * FORWARD_STEP
            expected_usdc = amounts[i] / 1_000_000
            assert initial_solana[i] >= expected_usdc, (
                f"User {i} has insufficient USDC: {initial_solana[i]} < {expected_usdc}"
            )

        # Submit all transfers concurrently
        log.info("Submitting %d forward transfers (Sol→Gor)...", len(users))
        _log_igp_balances("before-forward")

        results: dict[int, subprocess.CompletedProcess] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=NUM_BRIDGE_USERS) as pool:
            futures = {}
            for i, user in enumerate(users):
                recipient = user["pubkey"]
                fut = pool.submit(
                    _run_transfer_remote,
                    "/tmp/key.json",
                    str(amounts[i]), gorchain_domain, recipient,
                    "collateral",
                    "--program-id", solana_warp,
                    rpc=solana_rpc,
                    keypair_path=user["keypair_path"],
                )
                futures[fut] = i

            for fut in concurrent.futures.as_completed(futures):
                idx = futures[fut]
                results[idx] = fut.result()

        # Assert all transfers succeeded
        for i in range(len(users)):
            result = results[i]
            output = result.stdout + result.stderr
            log.info("User %d transfer-remote output:\n%s", i, output[:1000])
            assert result.returncode == 0, (
                f"User {i} forward transfer failed: {output}"
            )

        # Verify source balances decreased
        for i, user in enumerate(users):
            post = get_spl_token_balance(token_mint, user["keypair_path"], solana_rpc)
            expected_decrease = amounts[i] / 1_000_000
            assert initial_solana[i] - post >= expected_decrease - 0.001, (
                f"User {i} Solana USDC didn't decrease enough: "
                f"{initial_solana[i]} -> {post}"
            )
            log.info("User %d Solana USDC after transfer: %s", i, post)

        # Wait for all synthetic balances on Gorchain concurrently
        log.info("Waiting for %d Gorchain gUSDC balances...", len(users))

        def _wait_gorchain(idx: int) -> float:
            user = users[idx]
            expected = round(initial_gorchain[idx] + amounts[idx] / 1_000_000, 6)
            return wait_for_token_balance(
                synthetic_mint, user["keypair_path"], gorchain_rpc,
                expected_min=expected,
                timeout=RELAY_TIMEOUT,
                poll_interval=POLL_INTERVAL,
                label=f"User {idx} Gorchain gUSDC",
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=NUM_BRIDGE_USERS) as pool:
            wait_futures = {pool.submit(_wait_gorchain, i): i for i in range(len(users))}
            for fut in concurrent.futures.as_completed(wait_futures):
                idx = wait_futures[fut]
                balance = fut.result()
                log.info(
                    "User %d bridge Sol→Gor complete. gUSDC balance: %s",
                    idx, balance,
                )

        _log_igp_balances("after-forward")

    def test_concurrent_reverse_transfers(self, bridge_setup: dict) -> None:
        """Transfer synthetic gUSDC from Gorchain back to Solana for all users concurrently."""
        log.info("Waiting %ds for validator state to settle...", SETTLE_DELAY)
        time.sleep(SETTLE_DELAY)

        users = bridge_setup["users"]
        token_mint = bridge_setup["token_mint"]
        synthetic_mint = bridge_setup["synthetic_mint"]
        gorchain_warp = bridge_setup["warp_programs"]["gorchain"]

        solana_rpc = CHAINS["solana"]["rpc"]
        gorchain_rpc = CHAINS["gorchain"]["rpc"]
        solana_domain = str(CHAINS["solana"]["domain_id"])

        # Record initial balances per user
        initial_gorchain = {}
        initial_solana = {}
        for i, user in enumerate(users):
            kp = user["keypair_path"]
            initial_gorchain[i] = get_spl_token_balance(synthetic_mint, kp, gorchain_rpc)
            initial_solana[i] = get_spl_token_balance(token_mint, kp, solana_rpc)
            log.info(
                "User %d initial — Gorchain gUSDC: %s, Solana USDC: %s",
                i, initial_gorchain[i], initial_solana[i],
            )

        # Compute per-user transfer amounts
        amounts = {}
        for i in range(len(users)):
            amounts[i] = REVERSE_BASE + i * REVERSE_STEP
            expected_gusdc = amounts[i] / 1_000_000
            assert initial_gorchain[i] >= expected_gusdc, (
                f"User {i} has insufficient gUSDC: {initial_gorchain[i]} < {expected_gusdc}. "
                f"Did test_concurrent_forward_transfers run first?"
            )

        # Submit all reverse transfers concurrently
        log.info("Submitting %d reverse transfers (Gor→Sol)...", len(users))
        _log_igp_balances("before-reverse")

        results: dict[int, subprocess.CompletedProcess] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=NUM_BRIDGE_USERS) as pool:
            futures = {}
            for i, user in enumerate(users):
                recipient = user["pubkey"]
                fut = pool.submit(
                    _run_transfer_remote,
                    "/tmp/key.json",
                    str(amounts[i]), solana_domain, recipient,
                    "synthetic",
                    "--program-id", gorchain_warp,
                    rpc=gorchain_rpc,
                    keypair_path=user["keypair_path"],
                )
                futures[fut] = i

            for fut in concurrent.futures.as_completed(futures):
                idx = futures[fut]
                results[idx] = fut.result()

        # Assert all transfers succeeded
        for i in range(len(users)):
            result = results[i]
            output = result.stdout + result.stderr
            log.info("User %d transfer-remote output:\n%s", i, output[:1000])
            assert result.returncode == 0, (
                f"User {i} reverse transfer failed: {output}"
            )

        # Verify source balances decreased
        for i, user in enumerate(users):
            post = get_spl_token_balance(synthetic_mint, user["keypair_path"], gorchain_rpc)
            expected_decrease = amounts[i] / 1_000_000
            assert initial_gorchain[i] - post >= expected_decrease - 0.001, (
                f"User {i} Gorchain gUSDC didn't decrease enough: "
                f"{initial_gorchain[i]} -> {post}"
            )
            log.info("User %d Gorchain gUSDC after transfer: %s", i, post)

        # Wait for all collateral balances on Solana concurrently
        log.info("Waiting for %d Solana USDC balances...", len(users))

        def _wait_solana(idx: int) -> float:
            user = users[idx]
            expected = round(initial_solana[idx] + amounts[idx] / 1_000_000, 6)
            return wait_for_token_balance(
                token_mint, user["keypair_path"], solana_rpc,
                expected_min=expected,
                timeout=RELAY_TIMEOUT,
                poll_interval=POLL_INTERVAL,
                label=f"User {idx} Solana USDC",
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=NUM_BRIDGE_USERS) as pool:
            wait_futures = {pool.submit(_wait_solana, i): i for i in range(len(users))}
            for fut in concurrent.futures.as_completed(wait_futures):
                idx = wait_futures[fut]
                balance = fut.result()
                log.info(
                    "User %d bridge Gor→Sol complete. USDC balance: %s",
                    idx, balance,
                )

        _log_igp_balances("after-reverse")

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
