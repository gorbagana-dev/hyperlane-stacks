"""Phase 4 — Tier 2: Warp UI browser bridge tests.

Uses Playwright to drive the warp-ui in a real browser with the Backpack
wallet extension. The extension is loaded with the test keypair and
configured with custom RPC URLs for gorchain and solana.

Warp-ui URL params are used to pre-select chains/tokens:
  ?origin=solana&originToken=USDC&destination=gorchain&destinationToken=USDC

Wallet connection flow:
  1. Click "Connect wallet" on the warp-ui page
  2. Protocol selection modal → click "Solana"
  3. RainbowKit wallet list → click "Backpack"
  4. Backpack extension approves the connection

Transaction flow:
  1. Fill amount in the warp-ui form (self-transfer, recipient auto-filled)
  2. Click "Continue" → "Send to {chain}"
  3. Backpack opens a popup → unlock if needed → click "Approve"
  4. Wait for relay delivery on the destination chain
"""

import logging
import re
import time

import pytest

from lib.backpack import _WALLET_PASSWORD, switch_backpack_rpc
from lib.common import (
    CHAINS,
    get_sol_balance,
    get_spl_token_balance,
    wait_for,
    wait_for_token_balance,
)

log = logging.getLogger(__name__)

FORWARD_AMOUNT = "0.1"  # 0.1 USDC (Solana → Gorchain)
REVERSE_AMOUNT = "0.05"  # 0.05 USDC (Gorchain → Solana, less to account for fees)
NATIVE_FORWARD_AMOUNT = "0.1"  # 0.1 SOL (Solana → Gorchain)
NATIVE_REVERSE_AMOUNT = "0.05"  # 0.05 SOL (Gorchain → Solana, less to account for fees)
RELAY_TIMEOUT = 120  # seconds to wait for relayer delivery
POLL_INTERVAL = 5  # seconds between balance polls
SETTLE_DELAY = 10  # seconds between directional transfers

# Timeouts for UI interactions (ms)
UI_SETTLE = 1000
UI_SHORT = 2000
TX_SUBMIT_WAIT = 10000
TX_CONFIRM_TIMEOUT = 10000  # ms to wait for confirmation toast
TX_MAX_RETRIES = 5  # retry submit on "Plugin Closed" / wallet errors

# URL params to pre-select our chains and tokens
SOLANA_TO_GORCHAIN_PARAMS = (
    "?origin=solana&originToken=USDC"
    "&destination=gorchain&destinationToken=USDC"
)
GORCHAIN_TO_SOLANA_PARAMS = (
    "?origin=gorchain&originToken=USDC"
    "&destination=solana&destinationToken=USDC"
)
# Native SOL route params
SOL_SOLANA_TO_GORCHAIN_PARAMS = (
    "?origin=solana&originToken=SOL"
    "&destination=gorchain&destinationToken=SOL"
)
SOL_GORCHAIN_TO_SOLANA_PARAMS = (
    "?origin=gorchain&originToken=SOL"
    "&destination=solana&destinationToken=SOL"
)


def _screenshot(page, name: str) -> None:
    """Save a debug screenshot to /tmp/."""
    try:
        page.screenshot(path=f"/tmp/{name}.png")
        log.info("Screenshot saved: /tmp/%s.png", name)
    except Exception as e:
        log.warning("Failed to save screenshot %s: %s", name, e)


def _connect_wallet(page, context) -> None:
    """Connect Backpack wallet via the warp-ui wallet modal.

    Flow: "Connect wallet" button → protocol modal ("Solana") →
    RainbowKit wallet list → "Backpack" → approve Backpack connection popup.

    Skips connection if wallet is already connected. The header button
    changes from "Connect wallet" to "Backpack {address}" when connected.
    The sidebar may always have a "Connect wallet" link regardless of state,
    so we check the header area specifically.
    """
    # Wait for page to fully render and potentially auto-reconnect
    page.wait_for_timeout(UI_SHORT)

    # Check if wallet is already connected: when connected, a button in the
    # header shows the wallet name + truncated address (e.g. "Backpack 5oK1...").
    # We look for a button whose text contains a base58 address pattern.
    all_buttons = page.get_by_role("button")
    for i in range(all_buttons.count()):
        text = all_buttons.nth(i).text_content() or ""
        # Connected state shows something like "Backpack 5oK1A...5TaM"
        if re.search(r"[1-9A-HJ-NP-Za-km-z]{4,}\.{2,}", text):
            log.info("Wallet already connected: %s", text.strip())
            return

    # Find and click "Connect wallet"
    connect_btn = page.get_by_role("button", name=re.compile(r"connect wallet", re.IGNORECASE))
    if connect_btn.count() == 0:
        log.info("No 'Connect wallet' button — wallet may be connected")
        return

    log.info("Clicking 'Connect wallet' button")
    connect_btn.last.dispatch_event("click")
    page.wait_for_timeout(UI_SETTLE)

    # Step 1: Select "Solana" from the protocol selection modal
    solana_btn = page.get_by_role("button", name=re.compile(r"^Solana"))
    if solana_btn.count() == 0:
        solana_btn = page.get_by_text("Solana", exact=True)
    if solana_btn.count() > 0:
        log.info("Protocol selection modal detected — clicking Solana")
        solana_btn.first.click(force=True)
        page.wait_for_timeout(UI_SETTLE)
    else:
        log.info("No protocol selection modal — assuming direct wallet list")

    # Step 2: Click "Backpack" in the RainbowKit wallet list
    backpack_btn = page.get_by_role("button", name="Backpack")
    if backpack_btn.count() == 0:
        more_options = page.get_by_text("More options")
        if more_options.count() > 0:
            more_options.click()
            page.wait_for_timeout(UI_SETTLE)
        backpack_btn = page.get_by_role("button", name="Backpack")

    if backpack_btn.count() == 0:
        _screenshot(page, "wallet-modal-debug")
        buttons = page.get_by_role("button").all_text_contents()
        log.error("Wallet list buttons: %s", buttons)
        raise AssertionError(
            f"'Backpack' not found in wallet list. Available: {buttons}"
        )

    log.info("Found 'Backpack' in wallet list — clicking")
    # force=True: wallet-adapter-modal-overlay intercepts pointer events.
    # expect_page: register popup listener BEFORE the click.
    # On repeat connections Backpack may auto-connect without a popup.
    try:
        with context.expect_page(timeout=TX_SUBMIT_WAIT) as popup_info:
            backpack_btn.first.click(force=True)
        approve_backpack_popup_page(popup_info.value)
    except Exception:
        # Catches both Playwright TimeoutError (popup didn't open because
        # wallet auto-connected) and any other transient errors.
        log.info("No Backpack popup — wallet auto-connected")

    # Give the UI time to update after connection
    page.wait_for_timeout(UI_SHORT)


def approve_backpack_popup_page(popup) -> None:
    """Approve a Backpack popup page that's already been captured.

    Handles the optional password unlock screen — Backpack may ask for the
    wallet password before showing the confirmation screen. If a password
    input is present, fill it and click Unlock first.
    """
    popup.wait_for_load_state("networkidle")
    popup.wait_for_timeout(1000)
    log.info("Backpack popup opened: %s", popup.url)

    # Handle optional password unlock screen
    password_input = popup.locator('input[type="password"]')
    if password_input.count() > 0:
        log.info("Backpack password prompt detected — unlocking")
        password_input.first.fill(_WALLET_PASSWORD)
        popup.wait_for_timeout(500)
        unlock_btn = popup.get_by_text("Unlock", exact=True)
        if unlock_btn.count() == 0:
            unlock_btn = popup.get_by_role("button", name="Unlock")
        unlock_btn.last.click(force=True)
        popup.wait_for_timeout(2000)

    confirm_btn = popup.get_by_test_id("transaction-confirm-button")
    if confirm_btn.count() == 0:
        confirm_btn = popup.get_by_text("Approve", exact=True)
    confirm_btn.last.click(force=True)
    log.info("Clicked Approve in Backpack popup")
    try:
        popup.wait_for_event("close", timeout=3000)
    except Exception:
        pass


def _fill_amount(page, amount: str) -> None:
    """Fill the transfer amount in the amount input (type=number, role=spinbutton)."""
    amount_input = page.get_by_role("spinbutton")
    amount_input.fill(amount)
    page.wait_for_timeout(UI_SETTLE)


def _try_submit_once(page, context, dest_chain: str) -> bool:
    """Attempt one Continue → Send → Approve cycle. Returns True if confirmed."""
    continue_btn = page.get_by_role("button", name="Continue")
    continue_btn.click()
    page.wait_for_timeout(UI_SHORT)

    send_btn = page.get_by_role("button", name=re.compile(r"send to", re.IGNORECASE))
    with context.expect_page(timeout=TX_SUBMIT_WAIT) as popup_info:
        send_btn.first.click()
    approve_backpack_popup_page(popup_info.value)

    # Wait for "Transfer transaction sent" confirmation toast
    try:
        page.get_by_text(re.compile(r"transfer transaction sent", re.IGNORECASE)).wait_for(
            state="visible", timeout=TX_CONFIRM_TIMEOUT,
        )
        log.info("Transfer transaction confirmed by UI")
        return True
    except Exception:
        _screenshot(page, f"tx-attempt-{dest_chain}")
        log.warning("Transfer not confirmed — likely 'Plugin Closed' wallet error")
        return False


def _submit_transfer(page, context, dest_chain: str, amount: str) -> None:
    """Submit a bridge transfer with retry on wallet errors.

    Backpack intermittently fails with "Plugin Closed" when the popup
    closes before the wallet adapter finishes sendTransaction. Retrying
    (reload page → re-fill → re-submit) works reliably.
    """
    for attempt in range(1, TX_MAX_RETRIES + 1):
        if _try_submit_once(page, context, dest_chain):
            return
        if attempt < TX_MAX_RETRIES:
            log.info("Retrying transfer (attempt %d/%d)...", attempt + 1, TX_MAX_RETRIES)
            page.reload()
            page.wait_for_load_state("load")
            page.wait_for_function("() => document.title !== ''", timeout=30_000)
            _fill_amount(page, amount)

    raise AssertionError(
        f"Transfer to {dest_chain} failed after {TX_MAX_RETRIES} attempts. "
        "Check screenshots and Backpack/browser console for details."
    )


@pytest.mark.slow
class TestWarpUIBridge:
    """Browser-driven bridge transfer tests using Backpack wallet extension."""

    @pytest.mark.parametrize("params,label", [
        (SOLANA_TO_GORCHAIN_PARAMS, "usdc"),
        (SOL_SOLANA_TO_GORCHAIN_PARAMS, "sol"),
    ])
    def test_warp_ui_loads_in_browser(self, warp_ui_browser: dict, params: str, label: str) -> None:
        """Verify the UI loads and shows our chains via URL params."""
        page = warp_ui_browser["context"].new_page()
        try:
            url = warp_ui_browser["url"]
            page.goto(f"{url}{params}")
            page.wait_for_load_state("load")
            page.wait_for_function("() => document.title !== ''", timeout=30_000)

            title = page.title()
            assert title, "Page has no title"
            log.info("Page title: %s", title)

            content = page.content().lower()
            assert "send" in content or "transfer" in content, (
                "Transfer form not found in rendered page"
            )
            assert "fatal error" not in content, (
                "Fatal error displayed on page"
            )
            log.info("Warp UI loaded successfully in browser (%s route)", label)
        except Exception:
            _screenshot(page, f"warp-ui-load-fail-{label}")
            raise
        finally:
            page.close()

    @pytest.mark.parametrize("params,label", [
        (SOLANA_TO_GORCHAIN_PARAMS, "usdc"),
        (SOL_SOLANA_TO_GORCHAIN_PARAMS, "sol"),
    ])
    def test_warp_ui_wallet_connects(self, warp_ui_browser: dict, params: str, label: str) -> None:
        """Verify Backpack wallet connects and UI reflects connected state."""
        page = warp_ui_browser["context"].new_page()
        try:
            url = warp_ui_browser["url"]
            page.goto(f"{url}{params}")
            page.wait_for_load_state("load")
            page.wait_for_function("() => document.title !== ''", timeout=30_000)

            _connect_wallet(page, warp_ui_browser["context"])

            # After connection, check for connected state
            page.wait_for_timeout(UI_SETTLE)
            content = page.content().lower()
            connect_btns = page.get_by_role("button", name=re.compile(r"connect wallet", re.IGNORECASE))
            assert (
                "disconnect" in content
                or "connected" in content
                or connect_btns.count() == 0
                or any(
                    len(t) > 10 and not t.lower().startswith("connect")
                    for t in (connect_btns.all_text_contents() if connect_btns.count() > 0 else [])
                )
            ), "Wallet did not connect — UI still shows 'Connect wallet'"

            log.info("Backpack wallet connected successfully (%s route)", label)
        except Exception:
            _screenshot(page, f"wallet-connect-fail-{label}")
            raise
        finally:
            page.close()

    # Full round-trip transfers are USDC-only: bridge_setup provides SPL mint
    # addresses and balance probes for the collateral/synthetic token pair.
    # Native SOL selectability is already covered by test_warp_ui_loads_in_browser[sol].
    def test_warp_ui_bridge_usdc_solana_to_gorchain(self, warp_ui_browser: dict, bridge_setup: dict) -> None:
        """Transfer collateral USDC from Solana to synthetic USDC on Gorchain."""
        context = warp_ui_browser["context"]
        sender = bridge_setup["sender_keypair"]
        token_mint = bridge_setup["token_mint"]
        synthetic_mint = bridge_setup["synthetic_mint"]
        solana_rpc = CHAINS["solana"]["rpc"]
        gorchain_rpc = CHAINS["gorchain"]["rpc"]

        # Backpack is already on Solana RPC (localhost:18899) from setup —
        # correct for this direction (Solana → Gorchain).

        page = context.new_page()
        initial_solana = get_spl_token_balance(token_mint, sender, solana_rpc)
        initial_gorchain = get_spl_token_balance(synthetic_mint, sender, gorchain_rpc)
        log.info("Initial balances — Solana USDC: %s, Gorchain USDC: %s", initial_solana, initial_gorchain)

        try:
            url = warp_ui_browser["url"]
            page.goto(f"{url}{SOLANA_TO_GORCHAIN_PARAMS}")
            page.wait_for_load_state("load")
            page.wait_for_function("() => document.title !== ''", timeout=30_000)

            _connect_wallet(page, context)
            _fill_amount(page, FORWARD_AMOUNT)
            _submit_transfer(page, context, "gorchain", FORWARD_AMOUNT)

        except Exception:
            _screenshot(page, "bridge-sol-to-gor-fail")
            raise
        finally:
            page.close()

        expected = round(initial_gorchain + float(FORWARD_AMOUNT), 6)
        final_balance = wait_for_token_balance(
            synthetic_mint, sender, gorchain_rpc,
            expected_min=expected,
            timeout=RELAY_TIMEOUT,
            poll_interval=POLL_INTERVAL,
            label="Gorchain USDC (UI bridge)",
        )
        final_solana = get_spl_token_balance(token_mint, sender, solana_rpc)
        assert final_solana < initial_solana, (
            f"origin USDC not debited on Solana: {initial_solana} -> {final_solana}"
        )
        log.info(
            "UI bridge USDC Solana→Gorchain complete. Solana: %s -> %s, Gorchain: %s",
            initial_solana, final_solana, final_balance,
        )

    def test_warp_ui_bridge_usdc_gorchain_to_solana(self, warp_ui_browser: dict, bridge_setup: dict) -> None:
        """Transfer synthetic USDC from Gorchain back to collateral USDC on Solana."""
        log.info("Waiting %ds for validator state to settle...", SETTLE_DELAY)
        time.sleep(SETTLE_DELAY)

        context = warp_ui_browser["context"]
        sender = bridge_setup["sender_keypair"]

        # ConnectionProvider uses a fixed solana RPC endpoint so autoConnect
        # always works. But Backpack must be on gorchain RPC for transaction
        # simulation — it simulates against its own active RPC before showing
        # the approve dialog. Switch AFTER page load + wallet connect (so
        # autoConnect succeeds), but BEFORE submitting the transaction.

        page = context.new_page()
        token_mint = bridge_setup["token_mint"]
        synthetic_mint = bridge_setup["synthetic_mint"]
        solana_rpc = CHAINS["solana"]["rpc"]
        gorchain_rpc = CHAINS["gorchain"]["rpc"]

        initial_solana = get_spl_token_balance(token_mint, sender, solana_rpc)
        initial_gorchain = get_spl_token_balance(synthetic_mint, sender, gorchain_rpc)
        log.info("Initial balances — Gorchain USDC: %s, Solana USDC: %s", initial_gorchain, initial_solana)
        assert initial_gorchain >= float(REVERSE_AMOUNT), (
            f"insufficient synthetic USDC to bridge back: {initial_gorchain} < {REVERSE_AMOUNT}. "
            "Did the USDC forward test run first?"
        )

        try:
            url = warp_ui_browser["url"]
            page.goto(f"{url}{GORCHAIN_TO_SOLANA_PARAMS}")
            page.wait_for_load_state("load")
            page.wait_for_function("() => document.title !== ''", timeout=30_000)

            _connect_wallet(page, context)

            # Switch Backpack to gorchain RPC after wallet is connected
            switch_backpack_rpc(context, CHAINS["gorchain"]["rpc"])
            _fill_amount(page, REVERSE_AMOUNT)
            _submit_transfer(page, context, "solana", REVERSE_AMOUNT)

        except Exception:
            _screenshot(page, "bridge-gor-to-sol-fail")
            raise
        finally:
            page.close()

        expected = round(initial_solana + float(REVERSE_AMOUNT), 6)
        final_balance = wait_for_token_balance(
            token_mint, sender, solana_rpc,
            expected_min=expected,
            timeout=RELAY_TIMEOUT,
            poll_interval=POLL_INTERVAL,
            label="Solana USDC (UI bridge)",
        )
        final_gorchain = get_spl_token_balance(synthetic_mint, sender, gorchain_rpc)
        assert final_gorchain < initial_gorchain, (
            f"origin synthetic USDC not debited on Gorchain: {initial_gorchain} -> {final_gorchain}"
        )
        log.info(
            "UI bridge USDC Gorchain→Solana complete. Gorchain: %s -> %s, Solana: %s",
            initial_gorchain, final_gorchain, final_balance,
        )

    def test_warp_ui_bridge_sol_solana_to_gorchain(self, warp_ui_browser: dict, bridge_setup: dict) -> None:
        """Transfer native SOL from Solana to synthetic SOL on Gorchain (native route, UI)."""
        context = warp_ui_browser["context"]
        sender = bridge_setup["sender_keypair"]
        synthetic_mint = bridge_setup["routes"]["SOL-solana-gorchain"]["synthetic_mint"]
        solana_rpc = CHAINS["solana"]["rpc"]
        gorchain_rpc = CHAINS["gorchain"]["rpc"]

        page = context.new_page()
        initial_solana = get_sol_balance(sender, solana_rpc)
        initial_gorchain = get_spl_token_balance(synthetic_mint, sender, gorchain_rpc)
        log.info("Initial balances — Solana SOL: %s, Gorchain synthetic SOL: %s", initial_solana, initial_gorchain)

        try:
            url = warp_ui_browser["url"]
            page.goto(f"{url}{SOL_SOLANA_TO_GORCHAIN_PARAMS}")
            page.wait_for_load_state("load")
            page.wait_for_function("() => document.title !== ''", timeout=30_000)

            _connect_wallet(page, context)
            # A prior reverse test may have left Backpack on Gorchain RPC; this
            # direction originates on Solana, so simulate against the Solana RPC.
            switch_backpack_rpc(context, CHAINS["solana"]["rpc"])
            _fill_amount(page, NATIVE_FORWARD_AMOUNT)
            _submit_transfer(page, context, "gorchain", NATIVE_FORWARD_AMOUNT)

        except Exception:
            _screenshot(page, "bridge-native-sol-to-gor-fail")
            raise
        finally:
            page.close()

        expected = round(initial_gorchain + float(NATIVE_FORWARD_AMOUNT), 9)
        final_balance = wait_for_token_balance(
            synthetic_mint, sender, gorchain_rpc,
            expected_min=expected,
            timeout=RELAY_TIMEOUT,
            poll_interval=POLL_INTERVAL,
            label="Gorchain synthetic SOL (UI bridge)",
        )
        final_solana = get_sol_balance(sender, solana_rpc)
        assert final_solana < initial_solana, (
            f"origin native SOL not debited on Solana: {initial_solana} -> {final_solana}"
        )
        log.info(
            "UI native bridge SOL Solana→Gorchain complete. Solana SOL: %s -> %s, synthetic: %s",
            initial_solana, final_solana, final_balance,
        )

    def test_warp_ui_bridge_sol_gorchain_to_solana(self, warp_ui_browser: dict, bridge_setup: dict) -> None:
        """Transfer synthetic SOL from Gorchain back to native SOL on Solana (native route, UI).

        Requires test_warp_ui_bridge_sol_solana_to_gorchain to have run first
        (it credits the synthetic SOL this direction spends).
        """
        log.info("Waiting %ds for validator state to settle...", SETTLE_DELAY)
        time.sleep(SETTLE_DELAY)

        context = warp_ui_browser["context"]
        sender = bridge_setup["sender_keypair"]
        synthetic_mint = bridge_setup["routes"]["SOL-solana-gorchain"]["synthetic_mint"]
        solana_rpc = CHAINS["solana"]["rpc"]
        gorchain_rpc = CHAINS["gorchain"]["rpc"]

        page = context.new_page()
        initial_solana = get_sol_balance(sender, solana_rpc)
        initial_gorchain = get_spl_token_balance(synthetic_mint, sender, gorchain_rpc)
        log.info("Initial balances — Gorchain synthetic SOL: %s, Solana SOL: %s", initial_gorchain, initial_solana)
        assert initial_gorchain >= float(NATIVE_REVERSE_AMOUNT), (
            f"insufficient synthetic SOL to bridge back: {initial_gorchain} < {NATIVE_REVERSE_AMOUNT}. "
            "Did the SOL forward test run first?"
        )

        try:
            url = warp_ui_browser["url"]
            page.goto(f"{url}{SOL_GORCHAIN_TO_SOLANA_PARAMS}")
            page.wait_for_load_state("load")
            page.wait_for_function("() => document.title !== ''", timeout=30_000)

            _connect_wallet(page, context)
            # Origin is Gorchain — Backpack must simulate against the Gorchain RPC.
            switch_backpack_rpc(context, CHAINS["gorchain"]["rpc"])
            _fill_amount(page, NATIVE_REVERSE_AMOUNT)
            _submit_transfer(page, context, "solana", NATIVE_REVERSE_AMOUNT)

        except Exception:
            _screenshot(page, "bridge-native-gor-to-sol-fail")
            raise
        finally:
            page.close()

        # Destination is native SOL (not an SPL token), so poll the lamport balance.
        expected = round(initial_solana + float(NATIVE_REVERSE_AMOUNT), 9)

        def _native_released() -> float | None:
            bal = get_sol_balance(sender, solana_rpc)
            return bal if bal >= expected - 0.0001 else None

        final_balance = wait_for(
            _native_released,
            timeout=RELAY_TIMEOUT,
            interval=POLL_INTERVAL,
            description="Solana native SOL release (UI bridge)",
        )
        final_gorchain = get_spl_token_balance(synthetic_mint, sender, gorchain_rpc)
        assert final_gorchain < initial_gorchain, (
            f"origin synthetic SOL not debited on Gorchain: {initial_gorchain} -> {final_gorchain}"
        )
        log.info(
            "UI native bridge SOL Gorchain→Solana complete. Gorchain: %s -> %s, Solana SOL: %s",
            initial_gorchain, final_gorchain, final_balance,
        )
