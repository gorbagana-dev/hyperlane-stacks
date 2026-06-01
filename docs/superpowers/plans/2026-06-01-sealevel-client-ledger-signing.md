# Sealevel Client Built-in Ledger Signing + Binary Release — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add built-in Ledger signing to the forked `hyperlane-sealevel-client` so one command signs on the device and broadcasts, publish prebuilt operator binaries to GitHub Releases, and add a Ledger-gated e2e test.

**Architecture:** A single `usb://` branch in the client's keypair-resolution choke point resolves a Ledger-backed `Box<dyn Signer>` via the already-present `solana-remote-wallet` stack; the stored signer generalizes from a concrete `Keypair` to `Arc<dyn Signer>` so a non-cloneable `RemoteKeypair` fits the existing trait-based send path with no per-command edits. A native (non-Docker) release workflow fires on GitHub Release publication. The e2e test invokes the native binary against a real device and skips when none is present.

**Tech Stack:** Rust (sealevel workspace, toolchain 1.86.0), `solana-remote-wallet 3.0.7` / `solana-derivation-path 3.0.0` / `uriparse 0.6.4`, GitHub Actions, Python/pytest (hyperlane-stacks e2e).

**Spec:** `docs/superpowers/specs/2026-06-01-sealevel-client-ledger-signing-design.md`

**Repos / working dirs:**
- Rust + CI: `/home/dev/git_puller/repos/hyperlane-monorepo`, client at `rust/sealevel/client/`, workspace root `rust/sealevel/`.
- e2e: `/home/dev/git_puller/repos/hyperlane-stacks`, tests under `tests/e2e/`.

**Branching:** Rust + CI changes are on a branch in `hyperlane-monorepo`; the e2e test is on a branch in `hyperlane-stacks`. Commit only, never push.

---

## File Structure

**hyperlane-monorepo — `rust/sealevel/client/`:**
- Create: `src/signer.rs` — Ledger spec parsing (pure, tested) + device resolution (impure). One responsibility: turn a `usb://…` string into a `Box<dyn Signer>`.
- Modify: `src/main.rs` — declare `mod signer;`; add the `usb://` branch + `Arc` construction in keypair resolution (`main.rs:776-792`).
- Modify: `src/context.rs` — store `Arc<dyn Signer>` instead of `Keypair`; rework `payer_signer()` / `signer_for_pubkey()`.
- Modify: `Cargo.toml` — add three direct deps.

**hyperlane-monorepo — CI:**
- Create: `.github/workflows/sealevel-client-release.yml` — native build + release-asset upload.

**hyperlane-stacks — `tests/e2e/`:**
- Modify: `pytest.ini` — register a `requires_ledger` marker.
- Modify: `lib/common.py` — `ledger_available()` gate + `run_native_client()` runner.
- Create: `test_13_ledger_signing.py` — the gated ownership round-trip test.

---

## Background facts (verified 2026-06-01 against source — do not re-derive)

- `solana-clap-utils 3.0.7` uses **clap 2.34** internally and its `SignerSource` / `parse_signer_source` are `pub(crate)` — **not usable**. Compose the public building blocks instead.
- Public API confirmed in the downloaded crate sources:
  - `solana_remote_wallet::locator::Locator::new_from_uri(&URIReference) -> Result<Locator, LocatorError>` (`Locator` derives `Clone, Debug, PartialEq, Eq`; fields `manufacturer: Manufacturer`, `pubkey: Option<Pubkey>`).
  - `solana_derivation_path::DerivationPath::from_uri_key_query(&URIReference) -> Result<Option<DerivationPath>, DerivationPathError>` (`DerivationPath` derives `Clone, PartialEq, Eq`, **and impls `Default`**; `new_bip44(Option<u32>, Option<u32>)` builds an expected value; `?key=0/0` → `new_bip44(Some(0), Some(0))`).
  - `solana_remote_wallet::remote_wallet::maybe_wallet_manager() -> Result<Option<Rc<RemoteWalletManager>>, RemoteWalletError>`.
  - `solana_remote_wallet::remote_keypair::generate_remote_keypair(locator: Locator, derivation_path: DerivationPath, wallet_manager: &RemoteWalletManager, confirm_key: bool, keypair_name: &str) -> Result<RemoteKeypair, RemoteWalletError>`; `RemoteKeypair: Signer`.
- **Version trap:** the lock contains both `solana-derivation-path` 2.2.1 and 3.0.0. `remote-wallet` uses **3.0.0**, so pin `=3.0.0` and import `DerivationPath` from `solana_derivation_path` (NOT `solana_sdk::derivation_path`, which may resolve to 2.2.1 and is a different type).
- `context.rs` today: `PayerKeypair { keypair: Keypair, keypair_path: String }`; `payer_signer()` clones via `Keypair::try_from(&keypair.to_bytes()[..]).unwrap()` (`context.rs:117`) — impossible for `RemoteKeypair`. Call sites (`send_with_payer:298`, `send_with_pubkey_signer:312`) use `.as_deref()`, which works for `Option<Arc<dyn Signer>>` too (Arc derefs to `dyn Signer`).
- Sealevel toolchain: `rust/sealevel/rust-toolchain` channel `1.86.0`; CI installs `libudev-dev` (so `hidapi` already builds). Binary name: `hyperlane-sealevel-client`. Build dir: `./rust/sealevel`.
- e2e: existing `run_deployer_cli()` runs the client **in Docker** — unusable for a Ledger (USB passthrough is what we avoid). Chains: `CHAINS["solana"]["rpc"] == "http://127.0.0.1:18899"`. `mailbox transfer-ownership --program-id <id> <new-owner-pubkey>` (new_owner is a positional arg, not a flag) is signed by the current owner; `mailbox query --program-id <id>` prints the account (incl. owner). Only registered marker today is `slow`; gating is done with runtime `pytest.skip(...)`.

---

## Task 1: Ledger spec parsing (pure, tested)

**Files:**
- Modify: `rust/sealevel/client/Cargo.toml`
- Create: `rust/sealevel/client/src/signer.rs`
- Modify: `rust/sealevel/client/src/main.rs` (declare module)

- [ ] **Step 1: Add the three direct dependencies**

In `rust/sealevel/client/Cargo.toml`, under `[dependencies]`, add (keep the existing alphabetical-ish ordering near the other `solana-*` workspace entries):

```toml
solana-remote-wallet = "=3.0.7"
solana-derivation-path = "=3.0.0"
uriparse = "0.6.4"
```

(These are already compiled transitively via `solana-clap-utils`; declaring them only exposes their public API. Versions pinned to match what `solana-remote-wallet 3.0.7` resolves.)

- [ ] **Step 2: Declare the module in `main.rs`**

In `rust/sealevel/client/src/main.rs`, with the other top-level `mod` declarations (near `mod context;`), add:

```rust
mod signer;
```

- [ ] **Step 3: Write the failing unit tests**

Create `rust/sealevel/client/src/signer.rs` with ONLY the tests first (the functions come next step). This makes the test compile-fail until the function exists:

```rust
//! Resolve `--keypair usb://…` specs to a Ledger-backed signer.
//!
//! `solana-clap-utils`' `signer_from_path` wants a clap-v2 `ArgMatches` (the
//! crate uses clap 2.34 internally) and its parsers are `pub(crate)`, so we
//! compose the public building blocks from `solana-remote-wallet` /
//! `solana-derivation-path` directly.

use solana_derivation_path::DerivationPath;
use solana_remote_wallet::locator::Locator;
use uriparse::URIReference;

const USB_PREFIX: &str = "usb://";

#[cfg(test)]
mod tests {
    use super::*;
    use solana_remote_wallet::locator::Manufacturer;

    #[test]
    fn non_usb_spec_returns_none() {
        assert!(parse_ledger_spec("/home/op/key.json").is_none());
        assert!(parse_ledger_spec("9aE476sH92Vz7DMPyq5WLPkrKWivxeuTKEFKd2sZZcde").is_none());
    }

    #[test]
    fn usb_ledger_with_key_query_parses() {
        let (locator, dp) = parse_ledger_spec("usb://ledger?key=0/0").unwrap().unwrap();
        assert_eq!(locator.manufacturer, Manufacturer::Ledger);
        assert_eq!(locator.pubkey, None);
        assert_eq!(dp, DerivationPath::new_bip44(Some(0), Some(0)));
    }

    #[test]
    fn usb_ledger_no_query_defaults() {
        let (locator, dp) = parse_ledger_spec("usb://ledger").unwrap().unwrap();
        assert_eq!(locator.manufacturer, Manufacturer::Ledger);
        assert_eq!(dp, DerivationPath::default());
    }

    #[test]
    fn malformed_usb_spec_is_some_err() {
        assert!(parse_ledger_spec("usb://").unwrap().is_err());
    }
}
```

- [ ] **Step 4: Run the tests to verify they fail to compile**

Run: `cd /home/dev/git_puller/repos/hyperlane-monorepo/rust/sealevel && cargo test -p hyperlane-sealevel-client signer::`
Expected: FAIL — `cannot find function parse_ledger_spec in this scope`.

- [ ] **Step 5: Implement the pure parse function**

In `rust/sealevel/client/src/signer.rs`, add above the `#[cfg(test)]` block:

```rust
/// Parse a `usb://…` spec into a Ledger `Locator` + `DerivationPath`.
///
/// Returns `None` when `spec` is not a `usb://` spec (caller falls through to
/// file/pubkey resolution). Pure — no device I/O — so it is unit-tested without
/// hardware.
pub(crate) fn parse_ledger_spec(spec: &str) -> Option<Result<(Locator, DerivationPath), String>> {
    if !spec.starts_with(USB_PREFIX) {
        return None;
    }
    Some(parse_usb_uri(spec))
}

fn parse_usb_uri(spec: &str) -> Result<(Locator, DerivationPath), String> {
    let uri = URIReference::try_from(spec).map_err(|e| format!("invalid usb:// URI: {e}"))?;
    let locator = Locator::new_from_uri(&uri).map_err(|e| format!("invalid Ledger locator: {e}"))?;
    let derivation_path = DerivationPath::from_uri_key_query(&uri)
        .map_err(|e| format!("invalid derivation path: {e}"))?
        .unwrap_or_default();
    Ok((locator, derivation_path))
}
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd /home/dev/git_puller/repos/hyperlane-monorepo/rust/sealevel && cargo test -p hyperlane-sealevel-client signer::`
Expected: PASS — 4 tests pass. (If `DerivationPath::default()` or `new_bip44` mismatch, adjust the expected value using `dp.get_query()` to inspect the actual path — but the verified mapping is `key=0/0` → `new_bip44(Some(0), Some(0))` and no-query → `default()`.)

- [ ] **Step 7: Commit**

```bash
cd /home/dev/git_puller/repos/hyperlane-monorepo
git add rust/sealevel/client/Cargo.toml rust/sealevel/Cargo.lock rust/sealevel/client/src/signer.rs rust/sealevel/client/src/main.rs
git commit -m "feat(sealevel-client): parse usb:// Ledger keypair specs"
```

---

## Task 2: Ledger device resolution (impure wrapper)

**Files:**
- Modify: `rust/sealevel/client/src/signer.rs`

No automated test — this opens the USB device and requires hardware. It is exercised by the e2e test (Task 8) and manual runs. The task gate is that it compiles.

- [ ] **Step 1: Add the imports**

At the top of `rust/sealevel/client/src/signer.rs`, extend the imports:

```rust
use solana_remote_wallet::remote_keypair::generate_remote_keypair;
use solana_remote_wallet::remote_wallet::maybe_wallet_manager;
use solana_sdk::signature::Signer;
use std::sync::Arc;
```

- [ ] **Step 2: Implement the device-resolution function**

In `rust/sealevel/client/src/signer.rs`, add after `parse_usb_uri`:

```rust
/// Resolve a `usb://…` spec to a Ledger-backed signer by opening the device.
///
/// Precondition: `spec` is a `usb://` spec (caller checks via `parse_ledger_spec`).
/// `confirm_key` is left `false` — per-transaction signing already prompts on the
/// device, so we do not force an extra pubkey-confirmation on every invocation.
pub(crate) fn ledger_signer_from_spec(spec: &str) -> Result<Arc<dyn Signer>, String> {
    let (locator, derivation_path) = parse_ledger_spec(spec)
        .expect("ledger_signer_from_spec called with a non-usb:// spec")?;
    let wallet_manager = maybe_wallet_manager()
        .map_err(|e| format!("failed to access remote wallet: {e}"))?
        .ok_or_else(|| "no Ledger device found (is it connected and unlocked?)".to_string())?;
    let keypair = generate_remote_keypair(locator, derivation_path, &wallet_manager, false, "")
        .map_err(|e| format!("failed to derive key from Ledger: {e}"))?;
    Ok(Arc::new(keypair))
}
```

(`&wallet_manager` is `&Rc<RemoteWalletManager>`; deref coercion supplies the `&RemoteWalletManager` the function wants. `Arc::new(keypair)` coerces `Arc<RemoteKeypair>` → `Arc<dyn Signer>` at the return position.)

- [ ] **Step 3: Verify it compiles**

Run: `cd /home/dev/git_puller/repos/hyperlane-monorepo/rust/sealevel && cargo build -p hyperlane-sealevel-client`
Expected: builds (a dead-code warning for `ledger_signer_from_spec` is acceptable until Task 4 wires it).

- [ ] **Step 4: Commit**

```bash
cd /home/dev/git_puller/repos/hyperlane-monorepo
git add rust/sealevel/client/src/signer.rs
git commit -m "feat(sealevel-client): resolve usb:// spec to a Ledger signer"
```

---

## Task 3: Generalize `Context` signer to `Arc<dyn Signer>`

**Files:**
- Modify: `rust/sealevel/client/src/context.rs`

- [ ] **Step 1: Add the `Arc` import**

In `rust/sealevel/client/src/context.rs`, add near the other `std` imports (e.g. after `use std::cell::RefCell;`):

```rust
use std::sync::Arc;
```

- [ ] **Step 2: Change the stored field type**

Replace the `PayerKeypair` struct (`context.rs:27-30`):

```rust
pub(crate) struct PayerKeypair {
    pub keypair: Keypair,
    pub keypair_path: String,
}
```

with:

```rust
pub(crate) struct PayerKeypair {
    pub signer: Arc<dyn Signer>,
    pub keypair_path: String,
}
```

- [ ] **Step 3: Rework `payer_signer()` and `signer_for_pubkey()`**

Replace the two methods (`context.rs:114-133`):

```rust
    pub(crate) fn payer_signer(&self) -> Option<Box<dyn Signer>> {
        if let Some(PayerKeypair { keypair, .. }) = &self.payer_keypair {
            Some(Box::new(
                Keypair::try_from(&keypair.to_bytes()[..]).unwrap(),
            ))
        } else {
            None
        }
    }

    /// If the pubkey matches the payer's pubkey, return the payer's signer.
    /// Otherwise, return None.
    pub(crate) fn signer_for_pubkey(&self, pubkey: &Pubkey) -> Option<Box<dyn Signer>> {
        if let Some(PayerKeypair { keypair, .. }) = &self.payer_keypair {
            if &keypair.pubkey() == pubkey {
                return self.payer_signer();
            }
        }
        None
    }
```

with:

```rust
    pub(crate) fn payer_signer(&self) -> Option<Arc<dyn Signer>> {
        self.payer_keypair.as_ref().map(|pk| pk.signer.clone())
    }

    /// If the pubkey matches the payer's pubkey, return the payer's signer.
    /// Otherwise, return None.
    pub(crate) fn signer_for_pubkey(&self, pubkey: &Pubkey) -> Option<Arc<dyn Signer>> {
        let pk = self.payer_keypair.as_ref()?;
        if &pk.signer.pubkey() == pubkey {
            return Some(pk.signer.clone());
        }
        None
    }
```

(`send_with_payer` and `send_with_pubkey_signer` already call `.as_deref()` on the result; `Option<Arc<dyn Signer>>::as_deref()` yields `Option<&dyn Signer>`, so those call sites are unchanged. `Keypair` is still imported/used elsewhere in the file, so leave its import.)

- [ ] **Step 4: Verify the whole client still type-checks**

Run: `cd /home/dev/git_puller/repos/hyperlane-monorepo/rust/sealevel && cargo check -p hyperlane-sealevel-client`
Expected: FAIL at `main.rs:781-784` only — `PayerKeypair { keypair: payer_keypair, … }` no longer has a `keypair` field. (That construction is fixed in Task 4.) No other call site should error; if any other file errors on `payer_signer`/`signer_for_pubkey`, it is a real call site to reconcile — search with `grep -rn "payer_signer\|signer_for_pubkey" src/` and adjust to the `Arc` return.

- [ ] **Step 5: Commit (with Task 4, since the crate doesn't compile alone)**

Do not commit yet — the crate is intentionally mid-refactor. Commit together at the end of Task 4.

---

## Task 4: Wire the `usb://` branch into `main.rs`

**Files:**
- Modify: `rust/sealevel/client/src/main.rs`

- [ ] **Step 1: Add imports**

In `rust/sealevel/client/src/main.rs`, add (near the existing `use` block / `std` imports):

```rust
use std::sync::Arc;
```

Ensure `solana_sdk::signature::Signer` is in scope (it is, via existing `.pubkey()` calls). The signer module functions are referenced as `signer::ledger_signer_from_spec` and `signer::parse_ledger_spec`.

- [ ] **Step 2: Replace the keypair-resolution block**

Replace `main.rs:776-792`:

```rust
    let keypair_path = cli.keypair.unwrap_or(config.keypair_path);
    let (payer_pubkey, payer_keypair) = if let Ok(payer_keypair) = read_keypair_file(&keypair_path)
    {
        (
            payer_keypair.pubkey(),
            Some(PayerKeypair {
                keypair: payer_keypair,
                keypair_path: keypair_path.clone(),
            }),
        )
    } else {
        println!(
            "Provided key is not a keypair file, treating as a public key {}",
            keypair_path
        );
        (Pubkey::from_str(&keypair_path).unwrap(), None)
    };
```

with:

```rust
    let keypair_path = cli.keypair.unwrap_or(config.keypair_path);
    let (payer_pubkey, payer_keypair) = if signer::parse_ledger_spec(&keypair_path).is_some() {
        let signer = signer::ledger_signer_from_spec(&keypair_path).unwrap_or_else(|e| {
            eprintln!("Ledger signer error: {e}");
            std::process::exit(1);
        });
        (
            signer.pubkey(),
            Some(PayerKeypair {
                signer,
                keypair_path: keypair_path.clone(),
            }),
        )
    } else if let Ok(payer_keypair) = read_keypair_file(&keypair_path) {
        let signer: Arc<dyn Signer> = Arc::new(payer_keypair);
        (
            signer.pubkey(),
            Some(PayerKeypair {
                signer,
                keypair_path: keypair_path.clone(),
            }),
        )
    } else {
        println!(
            "Provided key is not a keypair file, treating as a public key {}",
            keypair_path
        );
        (Pubkey::from_str(&keypair_path).unwrap(), None)
    };
```

- [ ] **Step 3: Verify the crate compiles and existing tests pass**

Run: `cd /home/dev/git_puller/repos/hyperlane-monorepo/rust/sealevel && cargo test -p hyperlane-sealevel-client`
Expected: PASS — the `signer::tests` (4) and the pre-existing `core.rs` test pass; crate builds clean.

- [ ] **Step 4: Commit Tasks 3 + 4 together**

```bash
cd /home/dev/git_puller/repos/hyperlane-monorepo
git add rust/sealevel/client/src/context.rs rust/sealevel/client/src/main.rs
git commit -m "feat(sealevel-client): sign with a Ledger via --keypair usb://

Store the payer as Arc<dyn Signer> so a non-cloneable RemoteKeypair fits the
existing send path, and resolve usb:// specs to a Ledger signer in the keypair
resolution choke point. File and pubkey (Squads/unsigned) paths unchanged."
```

---

## Task 5: Lint, format, and full-workspace verification

**Files:** none (verification only)

- [ ] **Step 1: Format**

Run: `cd /home/dev/git_puller/repos/hyperlane-monorepo/rust/sealevel && cargo fmt --all`
Then verify clean: `cargo fmt --all --check`
Expected: no diff.

- [ ] **Step 2: Clippy (matches CI: `-D warnings`)**

Run: `cd /home/dev/git_puller/repos/hyperlane-monorepo/rust/sealevel && cargo clippy -- -D warnings`
Expected: no warnings. (If clippy flags the `Some(parse_usb_uri(spec))` early return or the `expect` in `ledger_signer_from_spec`, address idiomatically without changing behavior.)

- [ ] **Step 3: Full sealevel check (matches CI)**

Run: `cd /home/dev/git_puller/repos/hyperlane-monorepo/rust/sealevel && cargo check --release --all-features --all-targets`
Expected: builds clean.

- [ ] **Step 4: Commit any fmt-only changes**

```bash
cd /home/dev/git_puller/repos/hyperlane-monorepo
git add -A rust/sealevel
git commit -m "style(sealevel-client): cargo fmt" || echo "nothing to commit"
```

---

## Task 6: GitHub Release workflow (native binaries)

**Files:**
- Create: `.github/workflows/sealevel-client-release.yml`

Both targets are **native** host builds: `ubuntu-24.04` is x86_64, `macos-14` is Apple Silicon (arm64). No cross-compilation.

- [ ] **Step 1: Create the workflow**

Create `/home/dev/git_puller/repos/hyperlane-monorepo/.github/workflows/sealevel-client-release.yml`:

```yaml
name: sealevel-client-release

on:
  release:
    types: [published]

jobs:
  build:
    strategy:
      fail-fast: false
      matrix:
        include:
          - os: ubuntu-24.04
            label: linux-x86_64
          - os: macos-14
            label: macos-arm64
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4

      - name: Install Linux dependencies
        if: runner.os == 'Linux'
        run: sudo apt-get update && sudo apt-get install -y libudev-dev pkg-config

      - uses: dtolnay/rust-toolchain@stable

      - name: Build hyperlane-sealevel-client
        working-directory: ./rust/sealevel
        run: cargo build --release --bin hyperlane-sealevel-client

      - name: Package
        working-directory: ./rust/sealevel
        run: |
          OUT="hyperlane-sealevel-client-${{ matrix.label }}"
          cp "target/release/hyperlane-sealevel-client" "$OUT"
          tar -czf "$OUT.tar.gz" "$OUT"

      - name: Upload to release
        working-directory: ./rust/sealevel
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          gh release upload "${{ github.event.release.tag_name }}" \
            "hyperlane-sealevel-client-${{ matrix.label }}.tar.gz" \
            --repo "${{ github.repository }}" --clobber
```

(`dtolnay/rust-toolchain@stable` matches the repo's other workflows; `rust/sealevel/rust-toolchain` pins channel `1.86.0` at build time. `--clobber` makes a re-run idempotent.)

- [ ] **Step 2: Lint the YAML**

Run: `cd /home/dev/git_puller/repos/hyperlane-monorepo && python3 -c "import yaml,sys; yaml.safe_load(open('.github/workflows/sealevel-client-release.yml')); print('yaml ok')"`
Expected: `yaml ok`.

- [ ] **Step 3: Commit**

```bash
cd /home/dev/git_puller/repos/hyperlane-monorepo
git add .github/workflows/sealevel-client-release.yml
git commit -m "ci(sealevel-client): publish operator binaries on release"
```

---

## Task 7: e2e harness — marker + native runner + gate (hyperlane-stacks)

**Files:**
- Modify: `tests/e2e/pytest.ini`
- Modify: `tests/e2e/lib/common.py`

- [ ] **Step 1: Register the `requires_ledger` marker**

In `/home/dev/git_puller/repos/hyperlane-stacks/tests/e2e/pytest.ini`, extend the `markers` block:

```ini
markers =
    slow: marks tests as slow (deselect with '-m "not slow"')
    requires_ledger: needs a physically connected Ledger (skips unless E2E_LEDGER=1 and HYPERLANE_SEALEVEL_CLIENT_BIN are set)
```

- [ ] **Step 2: Add the gate + native runner to `lib/common.py`**

In `/home/dev/git_puller/repos/hyperlane-stacks/tests/e2e/lib/common.py`, ensure `import os` and `import subprocess` are present at the top (add if missing), then add near `run_deployer_cli` (around line 640):

```python
def ledger_available() -> bool:
    """True when a Ledger-backed e2e run is requested and a native client binary is configured."""
    return os.environ.get("E2E_LEDGER") == "1" and bool(
        os.environ.get("HYPERLANE_SEALEVEL_CLIENT_BIN")
    )


def run_native_client(
    *args: str,
    keypair: str,
    rpc: str,
) -> subprocess.CompletedProcess[str]:
    """Run the NATIVE ``hyperlane-sealevel-client`` binary (not the Docker image).

    A Ledger is USB-HID on the host, so the binary must run natively. The binary
    path comes from ``HYPERLANE_SEALEVEL_CLIENT_BIN``. ``keypair`` is passed
    verbatim as ``--keypair`` (e.g. ``usb://ledger?key=0/0``).
    """
    bin_path = os.environ["HYPERLANE_SEALEVEL_CLIENT_BIN"]
    return run_cmd(
        [bin_path, "--keypair", keypair, "--url", rpc, *args],
        check=False,
    )
```

- [ ] **Step 3: Verify it imports**

Run: `cd /home/dev/git_puller/repos/hyperlane-stacks/tests/e2e && python3 -c "from lib.common import ledger_available, run_native_client; print('import ok')"`
Expected: `import ok`.

- [ ] **Step 4: Lint**

Run: `cd /home/dev/git_puller/repos/hyperlane-stacks && ruff check tests/e2e/lib/common.py`
Expected: passes (no new findings on the added lines).

- [ ] **Step 5: Commit**

```bash
cd /home/dev/git_puller/repos/hyperlane-stacks
git add tests/e2e/pytest.ini tests/e2e/lib/common.py
git commit -m "test(e2e): add Ledger gate and native sealevel-client runner"
```

---

## Task 8: e2e Ledger signing test (hyperlane-stacks)

**Files:**
- Create: `tests/e2e/test_13_ledger_signing.py`

The test proves the native client signs with a Ledger and the tx lands, via a mailbox-owner round-trip that restores the original owner. It runs last, is gated, and entangles only the solana mailbox owner (restored before exit). Operator prerequisites: Ledger connected + unlocked, Solana app open with **blind signing enabled**; on Linux, Ledger udev rules installed.

- [ ] **Step 1: Write the test**

Create `/home/dev/git_puller/repos/hyperlane-stacks/tests/e2e/test_13_ledger_signing.py`:

```python
"""Ledger hardware-signing e2e test.

Skips unless a real Ledger run is configured. Proves the native
hyperlane-sealevel-client signs a transaction on the device and broadcasts it,
by round-tripping ownership of the solana mailbox (deployer -> Ledger -> deployer)
and asserting the owner is restored. The Ledger-signed step is the transfer back.

Run with:
    E2E_LEDGER=1 \
    HYPERLANE_SEALEVEL_CLIENT_BIN=/path/to/hyperlane-sealevel-client \
    E2E_LEDGER_PUBKEY=<ledger solana pubkey> \
    pytest tests/e2e/test_13_ledger_signing.py -m slow
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
```

- [ ] **Step 2: Verify it collects and skips cleanly without a Ledger**

Run: `cd /home/dev/git_puller/repos/hyperlane-stacks/tests/e2e && python3 -m pytest test_13_ledger_signing.py --collect-only -q`
Expected: collects `TestLedgerSigning::test_ledger_signs_ownership_roundtrip` with no import/collection errors.

(A full run requires the deployed-bridge fixtures + hardware; do not run the body here. Collection success is the gate. If `--collect-only` triggers heavyweight session fixtures in this harness, instead assert importability: `python3 -c "import test_13_ledger_signing"` from `tests/e2e`.)

- [ ] **Step 3: Lint**

Run: `cd /home/dev/git_puller/repos/hyperlane-stacks && ruff check tests/e2e/test_13_ledger_signing.py`
Expected: passes.

- [ ] **Step 4: Commit**

```bash
cd /home/dev/git_puller/repos/hyperlane-stacks
git add tests/e2e/test_13_ledger_signing.py
git commit -m "test(e2e): Ledger-signed mailbox ownership round-trip"
```

---

## Manual verification (operator, with hardware — outside CI)

After a release is published and binaries are built:

1. Download the `linux-x86_64` (or `macos-arm64`) asset, extract, `chmod +x`.
2. Connect + unlock the Ledger, open the Solana app, enable **blind signing**. On Linux, install Ledger udev rules.
3. Confirm key derivation: `./hyperlane-sealevel-client --url <rpc> --keypair usb://ledger?key=0/0 mailbox query --program-id <id>` (read-only; should print without prompting to sign).
4. Run the gated e2e test against a deployed staging bridge with `E2E_LEDGER=1`, `HYPERLANE_SEALEVEL_CLIENT_BIN`, `E2E_LEDGER_PUBKEY` set; approve the prompt on the device when the transfer-back signs.

---

## Self-review notes (coverage against the spec)

- **Built-in Ledger via `--keypair usb://`** → Tasks 1, 2, 4.
- **`Arc<dyn Signer>` Context generalization** → Task 3.
- **clap v2/v4 caveat (compose public building blocks, not `signer_from_path`)** → Task 1 (design note + implementation).
- **Always-on, no feature gate** → Task 1 (plain deps, no `[features]`).
- **Release matrix linux-x86_64 + macos-arm64, on Release published** → Task 6.
- **Fork unit test on the pure parse seam** → Task 1.
- **Ledger-gated e2e test in hyperlane-stacks** → Tasks 7, 8.
- **Operator prerequisites (blind signing, udev)** → documented in Task 8 + Manual verification.
- **Preserve file + pubkey/Squads paths** → Task 4 (both branches retained).
```
