# Ops Layer Redesign + Built-in Ledger Signing Design

**Date:** 2026-05-29
**Status:** Brainstormed and locked (decisions below). Implementation deferred —
decomposed into three sub-projects, each gets its own spec → plan → build cycle.

This document supersedes the "atomic ops as suspended SO jobs + composite
playbooks that scp unsigned txs and broadcast via `submit-tx`" model previously
described in `docs/ops-decisions.md` and the `hyperlane-ops` stack row in
`docs/architecture-decisions.md`. Those docs have been updated to point here;
this spec is the authoritative record of the redesign.

---

## TL;DR of the pivot

**Old model (superseded):** A `hyperlane-ops` SO stack held seven atomic ops as
`laconic.suspend: "true"` jobs. Composite ansible playbooks triggered them with
`laconic-so deployment run-job <op>`, each job wrote an *unsigned* transaction to
a host-path dir, ansible scp'd it back to the controller, the operator signed it
with a Ledger out-of-band, and a `submit-tx` job broadcast it.

**New model (locked):** There is **no `hyperlane-ops` SO stack**. All
operator-attended on-chain operations run from the **ansible/operator layer**.
The `hyperlane-sealevel-client` (our `hyperlane-monorepo` fork) gains **built-in
Ledger support**, so a single command **signs on the Ledger and broadcasts in one
step**, run natively on the operator's machine. No unsigned-tx artifacts, no scp
round-trip, no `submit-tx`, no custom signer tool.

**Why the pivot happened:** investigation of the sealevel client (below) showed
the old model didn't actually work as drawn, and that built-in Ledger support was
far cheaper than expected because the entire signing stack is already in the
client's dependency tree.

---

## Investigation findings — `hyperlane-sealevel-client`

Repo: `/home/dev/git_puller/repos/hyperlane-monorepo`, CLI at
`rust/sealevel/client/`. These findings are the factual basis for every decision
below; preserved with file:line so they don't need re-deriving.

1. **Fee payer == authority, always.** The instruction builders take a single
   `owner_payer: Pubkey` that is both fee payer *and* authority (e.g.
   `set_validators_and_threshold_instruction`, `transfer_ownership_instruction`
   in `rust/sealevel/programs/.../instruction.rs`; mailbox at
   `programs/mailbox/src/instruction.rs:132-150`). There is **no way** to use a
   hot fee-payer distinct from the authority without changing the on-chain
   programs. → Ops never need a separate hot fee-payer key.

2. **Originally no Ledger support.** The client used `read_keypair_file` only —
   no `usb://`, no `RemoteKeypair`. `Cargo.toml` pins `solana-sdk = "=3.0.0"`
   (workspace).

3. **The unsigned-tx escape hatch is Squads-shaped.** When `--keypair` is a
   *pubkey* (not a readable keypair file), the client builds the tx unsigned and
   either writes `instructions.yaml` (base58 bincode) or prints base58 to stdout
   — `context.rs:316-341` (`send`), `context.rs:221-295`
   (`write_transaction_to_yaml`), `context.rs:182-219`
   (`pretty_print_transaction`). The code comment literally says "presumed to be
   a Squads multisig." The emitted artifact is **not** a format the `solana` CLI
   offline-signing consumes — feeding it to a Ledger would have required a
   bespoke signer tool.

4. **`--write-instructions` only works for the warp-route-deploy path.**
   `instructions_path` is set **only** at `router.rs:311`. For the ops commands
   we care about (`set-validators-and-threshold`, `transfer-ownership`, IGP,
   close-program), it stays `None`, so they fall into the stdout +
   `wait_for_user_confirmation()` branch — and that function does
   `stdin.read_exact(...).unwrap()` (`context.rs:393`). **In a headless k8s Job
   with no stdin this hangs or panics on EOF.** So the old "run-job emits an
   unsigned artifact" model did not actually work for the ISM/ownership ops.

5. **The entire Ledger stack is already in the dependency tree.**
   `solana-clap-utils 3.0.7` is a **direct** dep of the client
   (`rust/sealevel/client/Cargo.toml`). It pulls `solana-remote-wallet 3.0.7`
   and `hidapi 2.6.4` (confirmed in `rust/sealevel/Cargo.lock`:
   `solana-clap-utils` @ 7044, `solana-remote-wallet` @ 8734, `hidapi` @ 3145;
   clap-utils → remote-wallet dep visible at lock line ~7062). All
   version-aligned with `solana-sdk 3.0.x`.

6. **The signer abstraction is already trait-based.** `payer_signer()` and
   `signer_for_pubkey()` return `Box<dyn Signer>` (`context.rs:114,126`); `send()`
   consumes `&dyn Signer` (`context.rs:316`). A Ledger-backed `RemoteKeypair`
   implements `Signer` and slots into the exact same path. The only concrete
   coupling is `PayerKeypair { keypair: Keypair }` (`context.rs:27-30`) and the
   `Keypair::try_from(to_bytes())` clone trick at `context.rs:117`, which a
   non-cloneable `RemoteKeypair` can't use.

---

## Decisions locked (2026-05-29)

### Signing model
- **Built-in Ledger support** added to the client fork. A single command signs on
  the Ledger and broadcasts — run natively on the operator's machine.
- **Operator review gate = the Ledger device screen** (better than eyeballing a
  base58 blob). `--require-tx-approval` (`main.rs:111`) gives an optional terminal
  confirmation on top.
- **No secrets on the operator machine.** The Ledger holds the only private key;
  state/config come from the repo; both RPCs are **public** (operator-confirmed),
  so the operator's machine reaches them directly.
- **No `hyperlane-ops` SO stack.** The `laconic.suspend` + `run-job` SO
  enhancement (already merged to stack-orchestrator) becomes **latent
  infrastructure** — still generally useful, no v1 consumer.

### Code change (minimal-surface)
In the keypair-resolution block (`main.rs:776-792`), add a `usb://` branch
*before* today's file/pubkey logic:
- value is `usb://...` → resolve via `solana-clap-utils` remote-wallet
  (`RemoteWalletManager` + `signer_from_path`) → Ledger-backed `Box<dyn Signer>`;
- else readable keypair file → `Keypair` (as today);
- else → treat as pubkey, no signer (today's Squads/unsigned mode — **preserved**;
  warp-deploy relies on it).
Plus generalize stored signer in `context.rs` from `Keypair` to `Arc<dyn Signer>`
so a non-cloneable `RemoteKeypair` can be held. **No per-command edits.**

### Distribution
- **Prebuilt binaries published to GitHub Releases** (not GHCR — GHCR is for the
  container images). The operator runs a native binary; `docker run --device` USB
  passthrough is Linux-only and flaky on macOS, and operators already run a native
  `solana` CLI for Ledger work, so a native binary fits their reality.
- The in-cluster deployer image keeps using `read_keypair_file` with the hot
  deploy key for actual deployment — unaffected.

### Repository layout (dimension A)
```
deployment/
  spec-*.yml                          # prod spec files (flat at env root)
  bridges/
    <bridge>/
      operator/validators.yaml        # operator-managed inputs
      generated/                      # bridge instance state, committed
  staging/
    spec-*.yml                        # staging spec files
    bridges/
      <bridge>/
        operator/validators.yaml
        generated/

ops/                                  # top-level, sibling of deployment/
  playbooks/                          # env-agnostic
  roles/                              # env-agnostic
  envs/
    prod/{inventory.yml,host_vars/,group_vars/}
    staging/{inventory.yml,host_vars/,group_vars/}
```
- **`ops/` is top-level** (sibling of `deployment/`), not under `deployment/`.
- **Per-env isolation** via `ops/envs/{prod,staging}/` — no shared mutable surface
  between staging and prod (mirrors the spec-tree split).
- **Specs stay flat at the env root**; only `operator/` + `generated/` live under
  `bridges/<bridge>/`. The `bridges/<bridge>/` level gives forward-compat for
  *multiple named bridges per env* without forcing a spec relocation today.
- **v1 bridge name: `default`.** Pure identifier; chain pair is not encoded
  (it won't change). Renaming later is a `git mv`.

### Sub-project decomposition (sequence 1 → 2 → 3; 1 and 2 are independent)
1. **Client fork — built-in Ledger + binary release** (`hyperlane-monorepo`).
   The Rust change above + feature-gating decision + CI to publish prebuilt
   binaries to GitHub Releases. Dependency root for all signing playbooks; most
   technical uncertainty; brainstorm first.
2. **Deploy-side ansible** (`ops/` roles + bootstrap/deploy playbooks).
   `prerequisites_privileged`, `prerequisites_user`, `stack_deploy`,
   `state_distribute`, `dns_cloudflare`, credential distribution; playbooks
   `bootstrap-host`, `configure-dns`, `commit-bridge-state`, `deploy-all`. Gets a
   bridge fully running with **zero signing**. Independent of sub-project 1.
3. **Ops-side ansible** (signing + lifecycle playbooks). `kill-switch`,
   `restore`, `ism-update`, `add/remove-validator`, `teardown`,
   `verify-ownership` — each invokes the sub-project-1 client on `localhost` with
   the Ledger. Depends on sub-project 1.

---

## Open questions (where the brainstorm stopped)

These were being worked when we paused to write docs. Pick up here next session.

**Sub-project 1 (client fork) — being brainstormed, not yet specced:**
- **Arg UX (leaning (a), not yet confirmed):** overload existing `--keypair`
  with `usb://ledger?key=0/0` (mirrors `solana` CLI, `signer_from_path` parses it
  for free) — *vs* (b) a dedicated `--ledger` flag. Lean: (a).
- **Feature-gating (recommend always-on):** the remote-wallet/hidapi deps are
  already compiled today via `solana-clap-utils`, so always-on adds ~nothing.
  Gate only if we later need the in-cluster image to be HID-free. Verify `hidapi`
  builds cleanly in the image build env at implementation time.
- **Binary release matrix + trigger (not discussed):** which platforms
  (linux-x86_64, macos-arm64/x86_64), git-tag trigger, naming, version pinning so
  playbooks fetch a known version.
- **Testing strategy (not discussed):** a real Ledger can't run in CI. Cover the
  `usb://` path-resolution branch and keep the non-usb path under test; hardware
  signing is manual/staging-only.

**Sub-projects 2 and 3:** not yet brainstormed in detail. Roles' internal
mechanics (`state_distribute` git-pull → configmap copy; `dns_cloudflare`
additive reconciliation; two-actor `privileged_user`/`deploy_user` split) are
sketched in `docs/architecture-decisions.md` (Production Topology Model,
production bootstrap workflow) and the per-flow playbook descriptions in
`docs/ops-decisions.md` (which remain valid in *sequence*; only the *signing
mechanism* changed to the single-step Ledger client).

---

## Consequences for already-merged work

- The **`laconic.suspend` + enhanced `run-job`** stack-orchestrator feature
  (merged) has **no v1 consumer** under this design. It remains valid, general
  infrastructure; do not rip it out. If a future op genuinely needs an in-cluster
  suspended job, the mechanism is there.
- `docs/ops-decisions.md` per-flow playbook **inventory and ordering stay valid**;
  the **signing mechanism** in each (run-job → scp unsigned tx → sign →
  submit-tx) is replaced by "playbook runs the Ledger client on `localhost`,
  which signs + broadcasts in one step."
- The staging design (`2026-05-29-staging-environment-design.md`) is consistent;
  its layout references were updated to `deployment/[staging/]bridges/<bridge>/`.

## Relationship to other docs
- `docs/ops-decisions.md` — updated: ops architecture + signing UX reflect this.
- `docs/architecture-decisions.md` — updated: `hyperlane-ops` stack row removed
  (8 stacks now), Tier-1 key mgmt + emergency controls reflect built-in Ledger.
- `docs/superpowers/specs/2026-05-29-staging-environment-design.md` — layout
  reconciled.
- stack-orchestrator `docs/superpowers/specs/2026-05-28-job-suspend-and-run-job-design.md`
  — the now-latent SO feature.
