"""Backpack wallet extension helpers for Playwright browser tests.

Provides functions to download, configure, and interact with the Backpack
browser extension in automated tests. Used by the warp-ui bridge tests to
connect a real Solana wallet and approve transactions.

Extension setup flow:
1. Download and unpack the Backpack CRX (cached in .backpack-ext/)
2. Launch Chromium with --load-extension pointing to the unpacked dir
3. Import the test keypair into Backpack
4. Set custom RPC URLs for gorchain and solana
5. During tests, approve transaction popups in the extension window

Selector notes (discovered via interactive exploration):
- Backpack uses React Native Web: interactive elements are <div data-testid>
  rather than <button>. Use get_by_test_id() for all extension UI clicks.
- An overlay div (class r-105ug2t / css-g5y9jx) intercepts all pointer events
  inside the extension popup. All clicks within the extension need force=True.
- React Navigation keeps all screens in DOM simultaneously. Background screen
  elements appear earlier in DOM order; use .last to target the topmost screen.
- The extension opens a separate popout.html window for wallet connection and
  transaction approval requests. Catch it with context.wait_for_event("page").
"""

import hashlib
import json
import logging
import shutil
import subprocess
import zipfile
from pathlib import Path

import base58

log = logging.getLogger(__name__)

# Backpack extension ID on Chrome Web Store
BACKPACK_EXTENSION_ID = "aflkmfhebedbjioipglgcbcmnbpgliof"

# Cache directory for downloaded extension (relative to tests/e2e/)
BACKPACK_CACHE_DIR = Path(__file__).parent.parent / ".backpack-ext"

# Pinned version — update this when upgrading Backpack
BACKPACK_VERSION = "0.10.111"

# Password used for the test wallet (not a real account — test environment only)
_WALLET_PASSWORD = "TestPassword123!"


def _compute_ext_id(ext_path: Path) -> str:
    """Compute Chromium's deterministic extension ID for a side-loaded extension.

    Chromium derives the ID from the SHA-256 of the extension directory path,
    then maps each hex nibble to a-p (a=0 ... p=15).
    """
    digest = hashlib.sha256(str(ext_path).encode()).hexdigest()
    mapping = {str(i): chr(ord("a") + i) for i in range(10)}
    mapping.update({c: chr(ord("a") + 10 + i) for i, c in enumerate("abcdef")})
    return "".join(mapping[c] for c in digest[:32])


def get_backpack_extension_path() -> Path:
    """Return path to unpacked Backpack extension, downloading if needed."""
    ext_dir = BACKPACK_CACHE_DIR / BACKPACK_VERSION
    if ext_dir.exists() and any(ext_dir.iterdir()):
        log.info("Using cached Backpack extension at %s", ext_dir)
        return ext_dir

    log.info("Downloading Backpack extension v%s...", BACKPACK_VERSION)
    ext_dir.mkdir(parents=True, exist_ok=True)

    # Download CRX from Chrome Web Store via crx-dl or similar
    # The CRX is a ZIP with a header — we strip the header and unzip.
    crx_path = BACKPACK_CACHE_DIR / f"backpack-{BACKPACK_VERSION}.crx"

    # Use curl to download from Chrome Web Store
    crx_url = (
        f"https://clients2.google.com/service/update2/crx"
        f"?response=redirect&acceptformat=crx2,crx3"
        f"&prodversion=131.0&x=id%3D{BACKPACK_EXTENSION_ID}"
        f"%26installsource%3Dondemand%26uc"
    )
    subprocess.run(
        ["curl", "-sL", "-o", str(crx_path), crx_url],
        check=True,
    )

    # CRX files are ZIP files with a header. Try unzipping directly;
    # if that fails, skip the CRX header (varies: 16 bytes for CRX2,
    # variable for CRX3).
    try:
        with zipfile.ZipFile(crx_path) as zf:
            zf.extractall(ext_dir)
    except zipfile.BadZipFile:
        # Try skipping CRX3 header
        with open(crx_path, "rb") as f:
            data = f.read()
        # Find PK\x03\x04 (ZIP magic) in the file
        zip_start = data.find(b"PK\x03\x04")
        if zip_start < 0:
            raise RuntimeError("Cannot find ZIP content in CRX file") from None
        import io
        with zipfile.ZipFile(io.BytesIO(data[zip_start:])) as zf:
            zf.extractall(ext_dir)

    crx_path.unlink(missing_ok=True)
    log.info("Backpack extension unpacked to %s", ext_dir)
    return ext_dir


def launch_browser_with_backpack(playwright, extension_path: Path):
    """Launch Chromium with Backpack extension loaded.

    Uses launch_persistent_context because extensions require it.

    Note: headless=False is required for extensions. Set DISPLAY env var
    to an Xvfb display before calling sync_playwright().start() to run
    without a visible window.
    """
    user_data_dir = BACKPACK_CACHE_DIR / "user-data"
    if user_data_dir.exists():
        shutil.rmtree(user_data_dir)
    user_data_dir.mkdir(parents=True)

    # Build Chromium args. If DISPLAY is set to an Xvfb display,
    # force Chromium to use X11 (not Wayland) via --ozone-platform=x11.
    import os
    chrome_args = [
        f"--disable-extensions-except={extension_path}",
        f"--load-extension={extension_path}",
        "--no-first-run",
        "--disable-gpu",
    ]
    display = os.environ.get("DISPLAY", "")
    if display and display != ":0" and display != ":1":
        # Likely an Xvfb display — force X11 backend
        chrome_args.append("--ozone-platform=x11")

    context = playwright.chromium.launch_persistent_context(
        str(user_data_dir),
        headless=False,
        viewport={"width": 1280, "height": 720},
        args=chrome_args,
        ignore_https_errors=True,
    )

    return context


def setup_backpack_wallet(context, keypair_path: str, rpc_urls: dict) -> str:
    """Import a keypair into Backpack and configure custom RPC URLs.

    Args:
        context: Playwright browser context with Backpack loaded
        keypair_path: Path to Solana keypair JSON file (array of 64 bytes)
        rpc_urls: Dict with 'gorchain' and 'solana' RPC URLs

    Returns:
        The wallet's public key (base58)
    """
    # Read keypair. Solana keypair JSON = 64-byte array: first 32 = private, last 32 = public.
    # Backpack requires the full 64-byte keypair encoded as base58 (not just the 32-byte private key).
    keypair_bytes = json.loads(Path(keypair_path).read_text())
    full_keypair_b58 = base58.b58encode(bytes(keypair_bytes)).decode()
    public_key = base58.b58encode(bytes(keypair_bytes[32:])).decode()

    log.info("Setting up Backpack wallet for pubkey: %s", public_key)

    # Compute the side-loaded extension ID deterministically from the directory path.
    # (Side-loaded extensions don't have the CWS-assigned ID; Chromium derives it from
    # the path hash. This differs from BACKPACK_EXTENSION_ID which is the CWS ID.)
    ext_dir = BACKPACK_CACHE_DIR / BACKPACK_VERSION
    ext_id = _compute_ext_id(ext_dir)
    log.info("Side-loaded extension ID: %s", ext_id)

    # ── Onboarding flow ────────────────────────────────────────────────────────
    page = context.new_page()
    page.goto(f"chrome-extension://{ext_id}/onboarding.html")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1000)

    # Step 1: Accept Terms of Service (enables the import/create buttons)
    page.get_by_test_id("terms-of-service-checkbox").click()
    page.wait_for_timeout(500)

    # Step 2: "I already have a wallet"
    page.get_by_test_id("import-wallet-button").click()
    page.wait_for_timeout(1500)

    # Step 3: Select Solana network (first match — no token list yet in onboarding)
    page.get_by_text("Solana", exact=True).first.click()
    page.wait_for_timeout(1500)

    # Step 4: Choose "Private key" import method
    page.get_by_text("Private key").first.click()
    page.wait_for_timeout(1500)

    # Step 5: Enter full 64-byte keypair encoded as base58
    page.get_by_test_id("private-key-input").fill(full_keypair_b58)
    page.wait_for_timeout(500)
    page.get_by_test_id("private-key-import-button").click()
    page.wait_for_timeout(2000)

    # Step 6: Set wallet password
    page.get_by_test_id("password-input").fill(_WALLET_PASSWORD)
    page.get_by_test_id("password-confirm-input").fill(_WALLET_PASSWORD)
    page.wait_for_timeout(500)
    page.get_by_test_id("password-next-button").click()
    page.wait_for_timeout(2000)

    # Do NOT click "Open Backpack" — that closes the onboarding tab and opens
    # a separate popup window (TargetClosedError). Open popup.html directly instead.
    page.close()

    # ── RPC configuration ──────────────────────────────────────────────────────
    # Navigate to Settings → Solana → RPC Connection once, then set both URLs.
    # Backpack has two configurable slots reachable by clicking "Custom" twice:
    #   1st click → "Custom" slot (used for gorchain, port 8899)
    #   2nd click → "Localhost" slot with setup instructions (used for solana, port 18899)
    popup = context.new_page()
    popup.goto(f"chrome-extension://{ext_id}/popup.html")
    popup.wait_for_load_state("networkidle")
    popup.wait_for_timeout(2000)

    _navigate_to_rpc_page(popup)

    # Set gorchain RPC (first Custom slot)
    if "gorchain" in rpc_urls:
        _set_custom_rpc(popup, rpc_urls["gorchain"])

    # Set solana RPC (second Custom/Localhost slot — still on RPC Connection page)
    if "solana" in rpc_urls:
        _set_custom_rpc(popup, rpc_urls["solana"])

    popup.close()

    log.info("Backpack wallet setup complete for pubkey: %s", public_key)
    return public_key


def _navigate_to_rpc_page(popup) -> None:
    """Navigate from wallet home to Settings → Solana → RPC Connection page.

    Backpack uses React Native Web: interactive elements are <div> with touch
    handlers, not <button>. An overlay div intercepts pointer events. We use
    dispatch_event("click") to bypass both the overlay and RNW's touch system.
    .last targets the topmost screen (React Navigation keeps all screens in DOM).
    """
    popup.get_by_test_id("avatar-popover-button").dispatch_event("click")
    popup.wait_for_timeout(1000)
    popup.get_by_text("Settings", exact=True).last.dispatch_event("click")
    popup.wait_for_timeout(1000)
    popup.get_by_text("Solana", exact=True).last.dispatch_event("click")
    popup.wait_for_timeout(1000)
    popup.get_by_text("RPC Connection").last.dispatch_event("click")
    popup.wait_for_timeout(1000)


def _set_custom_rpc(popup, rpc_url: str) -> None:
    """Set a custom RPC URL from the RPC Connection page.

    Clicks "Custom" to open the URL input, fills the URL, then clicks Update.
    After Update the page stays on (or near) the RPC Connection page so this
    can be called twice to set two different slots.
    """
    popup.get_by_text("Custom").last.dispatch_event("click")
    popup.wait_for_timeout(1000)
    popup.get_by_placeholder("RPC URL").fill(rpc_url)
    popup.wait_for_timeout(500)
    popup.get_by_text("Update", exact=True).last.dispatch_event("click")
    popup.wait_for_timeout(1000)
    log.info("Set Backpack RPC URL: %s", rpc_url)


def switch_backpack_rpc(context, rpc_url: str) -> None:
    """Switch Backpack's active RPC to the given URL.

    Backpack can only be connected to one Solana RPC at a time. Before
    bridging, the active RPC must match the origin chain:
      - Solana→Gorchain: use Solana RPC (localhost:18899)
      - Gorchain→Solana: use Gorchain RPC (localhost:8899)

    Opens a dedicated Backpack extension page, changes the RPC, and
    closes it before returning. All other pages in the context are
    left untouched.
    """
    ext_dir = BACKPACK_CACHE_DIR / BACKPACK_VERSION
    ext_id = _compute_ext_id(ext_dir)

    bp_page = context.new_page()
    try:
        bp_page.goto(f"chrome-extension://{ext_id}/popup.html")
        bp_page.wait_for_load_state("networkidle")
        bp_page.wait_for_timeout(2000)

        _navigate_to_rpc_page(bp_page)
        _set_custom_rpc(bp_page, rpc_url)
        log.info("Switched Backpack RPC to %s", rpc_url)
    finally:
        bp_page.close()
        # Brief pause to let the extension settle after page close
        context.pages[0].wait_for_timeout(500) if context.pages else None


def approve_backpack_popup(context, timeout_ms: int = 30000) -> None:
    """Wait for and approve a Backpack popout (connection or transaction).

    Backpack opens a new browser page at popout.html when a dApp requests
    wallet connection or a transaction signature. This function detects the
    popup, clicks "Approve" (data-testid="transaction-confirm-button"), and
    waits for it to close.

    Args:
        context: Playwright browser context
        timeout_ms: Max time to wait for popup (ms)
    """
    popup = context.wait_for_event("page", timeout=timeout_ms)
    popup.wait_for_load_state("networkidle")
    popup.wait_for_timeout(1000)
    log.info("Backpack popup opened: %s", popup.url)

    # The confirm button uses the same testid for both connection and transaction approvals.
    # force=True is required due to the backdrop overlay intercepting pointer events.
    confirm_btn = popup.get_by_test_id("transaction-confirm-button")
    if confirm_btn.count() == 0:
        # Fallback: try text match in case testid changes across Backpack versions
        confirm_btn = popup.get_by_text("Approve", exact=True)
    confirm_btn.last.click(force=True)
    log.info("Clicked Approve in Backpack popup")

    # Wait for popup to close (it auto-closes after approval)
    try:
        popup.wait_for_event("close", timeout=10000)
    except Exception:
        pass  # popup may have already closed


def approve_backpack_transaction(context, timeout_ms: int = 30000) -> None:
    """Wait for and approve a transaction popup from Backpack.

    Backpack opens a new browser window/popup when a dApp requests a
    transaction signature. This function detects the popup, clicks
    "Approve", and waits for it to close.

    Args:
        context: Playwright browser context
        timeout_ms: Max time to wait for popup (ms)
    """
    approve_backpack_popup(context, timeout_ms=timeout_ms)
