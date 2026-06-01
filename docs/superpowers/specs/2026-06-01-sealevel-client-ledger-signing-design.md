# Sealevel Client Built-in Ledger Signing + Binary Release Design

**Date:** 2026-06-01
**Status:** Brainstormed and approved. Ready for implementation plan.
**Sub-project:** 1 of 3 from the ops-layer redesign
(`2026-05-29-ops-layer-redesign-and-ledger-signing-design.md`). Dependency root
for all signing playbooks (sub-project 3).

This sub-project adds **built-in Ledger support** to the forked
`hyperlane-sealevel-client` so a single command signs on the Ledger and
broadcasts in one step, run natively on the operator's machine. It also
publishes prebuilt operator binaries to GitHub Releases.

---

## Feasibility (verified against current source, 2026-06-01)

The core change is small (~60–80 lines of Rust + a CI release workflow). The
investigation findings in the parent ops-layer spec hold against the current
client. Re-verified facts:

- **The signing stack is already compiled in, non-optionally.**
  `solana-clap-utils 3.0.7` is a direct dep (`rust/sealevel/client/Cargo.toml:21`)
  and in `Cargo.lock` pulls `solana-remote-wallet 3.0.7` → `hidapi 2.6.4`, all as
  plain (non-optional) deps. The client binary already links `hidapi` today.
  Consequence: always-on Ledger support adds nothing to the build, and the
  "keep the in-cluster image HID-free" concern is moot.
- **Signer abstraction is already trait-based.** `payer_signer()` /
  `signer_for_pubkey()` return `Box<dyn Signer>`; `send()` consumes
  `&[Option<&dyn Signer>]` (`context.rs:114,126,316`). A Ledger `RemoteKeypair`
  implements `Signer` and slots into the same path with no per-command edits.
- **Keypair resolution is a single choke point** (`main.rs:776-792`): one new
  `usb://` branch.

### Caveats baked into this design (none are blockers)

1. **clap v2 vs v4 mismatch.** `solana-clap-utils 3.0.7` internally uses
   **clap 2.34**, but the client uses clap v4. The convenient one-liner
   `signer_from_path(&ArgMatches, …)` is therefore *not* usable (it wants a
   clap-v2 `ArgMatches`). We use the ArgMatches-free path: `SignerSource::parse`
   for parsing + `solana_remote_wallet::generate_remote_keypair(...)` for device
   resolution, adding `solana-remote-wallet` as a direct dep (already compiled
   transitively → zero build cost).
2. **`Context` signer must generalize to `Arc<dyn Signer>`.** Today it stores a
   concrete `Keypair` and `payer_signer()` clones it via
   `Keypair::try_from(&keypair.to_bytes()[..])` (`context.rs:117`) — a
   `RemoteKeypair` is not cloneable and can't round-trip through bytes.
3. **Operator prerequisites** (doc, not code): blind signing enabled on the
   Ledger Solana app; on Linux, Ledger udev rules installed for HID access.

Incidental: upstream has since added a `Squads` subcommand
(`main.rs:127`, `process_squads_cmd`) — the unsigned-tx/multisig path is now
first-class. No impact on this plan; preserved as-is.

---

## Decisions locked (2026-06-01)

| Topic | Decision |
|---|---|
| **Arg UX** | Overload existing `--keypair` with `usb://ledger?key=0/0` (mirrors the stock `solana` CLI). No new flag. |
| **Feature gating** | Always-on. No `--features` gate (HID deps already compiled non-optionally). Sanity-check `hidapi` builds in the release CI image at impl time. |
| **Release matrix** | `linux-x86_64` + `macos-arm64`. |
| **Release trigger** | GitHub **Release published** event — operator creates the release in the UI (cuts the tag); the workflow builds and uploads both binaries as assets onto that release. Playbooks pin the release tag. |
| **Testing** | Fork unit test over the pure parse seam + a Ledger-gated e2e test in hyperlane-stacks (skips when no device present). |

---

## Components

### A. Rust change — `hyperlane-monorepo` fork, `rust/sealevel/client/`

**`Cargo.toml`** — add one direct dep:
```toml
solana-remote-wallet = "=3.0.7"
```
Already compiled transitively; this only exposes its public API. No feature flags.

**New pure seam** (small module, e.g. `src/signer.rs`) — the testable unit:
```rust
/// Returns None if `s` is not a usb:// spec (caller falls through to existing
/// file/pubkey logic). Pure string→struct parsing via SignerSource::parse —
/// no device I/O, so it unit-tests without hardware.
fn parse_ledger_spec(s: &str) -> Option<Result<(RemoteWalletLocator, DerivationPath)>>;
```
A thin non-pure wrapper takes that result, calls
`solana_remote_wallet::remote_wallet::maybe_wallet_manager()` +
`generate_remote_keypair(...)`, and yields a `Box<dyn Signer>` (Ledger-backed).

**`main.rs:776-792`** — prepend a branch to keypair resolution:
1. `usb://…` → resolve to a Ledger-backed signer (wrapper above);
2. else readable keypair file → `Keypair` (today's path);
3. else → treat as pubkey, no signer (today's Squads/unsigned path — **preserved**;
   warp-deploy relies on it).

**`context.rs`** — generalize the stored signer from concrete `Keypair` to
`Arc<dyn Signer>`:
- `PayerKeypair { keypair: Keypair, … }` → hold `Arc<dyn Signer>` + pubkey + path
  string;
- `payer_signer()` / `signer_for_pubkey()` clone the `Arc` instead of the
  `to_bytes()` round-trip at `:117`;
- adjust the two call sites (`send_with_payer`, `send_with_pubkey_signer`) for the
  `Arc` → `&dyn Signer` deref. **No per-command edits.**

### B. Distribution — `.github/workflows/` on the fork

A release workflow triggered on `release: published`:
- builds `linux-x86_64` and `macos-arm64`;
- uploads both binaries as assets onto the triggering release;
- the in-cluster deployer image is unaffected (it keeps `read_keypair_file` + the
  hot deploy key).

### C. Operator prerequisites (documented, not code)

- Ledger Solana app: **blind signing enabled** (Hyperlane ISM/ownership/IGP
  instructions are not decodable transfers).
- Linux: **Ledger udev rules** installed for HID access (else permission-denied).

---

## Data flow

Operator runs natively:
```
hyperlane-sealevel-client --url <rpc> --keypair usb://ledger?key=0/0 <op subcommand>
```
The resolver builds the Ledger signer → `send()` fetches a fresh blockhash, signs
**on the device**, and broadcasts — one step. `pretty_print_transaction` still
prints the tx; `--require-tx-approval` adds an optional terminal gate on top of
the device's own confirmation screen.

---

## Error handling

- No device / locked / wrong app → surface the error from `maybe_wallet_manager()`
  / `generate_remote_keypair` (do not `unwrap`-panic).
- User rejects on device, or blind signing disabled → `RemoteWalletError`
  surfaced as a non-zero exit.
- Malformed `usb://` spec → parse error from the pure seam.

---

## Testing

- **Fork unit test:** `parse_ledger_spec` over valid/invalid `usb://` specs, and
  that a non-`usb://` value returns `None` (file/pubkey resolution still works).
  CI-safe, no hardware.
- **hyperlane-stacks e2e test:** a real-Ledger signing test that runs **only if a
  Ledger is present** (pytest skip on device-absent / env flag), invoking the
  released binary against a chain and asserting the op lands on-chain.

---

## Scope boundaries

- In-cluster deployer image keeps `read_keypair_file` + hot deploy key (untouched).
- No `hyperlane-ops` SO stack.
- Squads/unsigned path preserved.
- Playbooks that *call* this binary are **sub-project 3**, not this spec.
