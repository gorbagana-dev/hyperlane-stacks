# Deployer Authorization Hardening (hyp-d9c) — Design

**Date:** 2026-06-09
**Epic:** `hyp-d9c` — Harden warp-route deploy authorization & deployer-key custody (SVM bridge)
**Tasks:** `hyp-d9c.1` (ISM ownership), `hyp-d9c.2` (warp-route ownership), `hyp-d9c.3` (relayer whitelist)

## Problem

A leaked cluster-stored deployer key can today both stand up operational warp
routes and drain existing ones, because three handoffs the documented design
assumes are incomplete:

1. **ISM ownership stays on the hot key.** Core `deploy.sh` skips the
   multisig-ISM ownership transfer on a stale "no CLI command" belief. The owner
   can `SetValidatorsAndThreshold` and forge messages on every route.
2. **Warp-route app-level ownership stays on the hot key.** Warp `deploy.sh`
   transfers only the BPF upgrade authority; the Hyperlane app-level route owner
   (gates `enroll_remote_routers`, `set_interchain_security_module`,
   `set_destination_gas`) remains the deployer key.
3. **The relayer relays everything.** No `HYP_WHITELIST`;
   `HYP_GASPAYMENTENFORCEMENT: none`. The mailbox is permissionless, so a route
   deployed by a leaked key is live with no operator action — UI listing is not
   a gate.

## Security model (target)

- **Ledger (hardware wallet) protects funds.** ISM ownership and warp-route
  app-level ownership must move off the hot key to `HARDWARE_WALLET_PUBKEY`.
- **The git-reviewed relayer whitelist gates relay-endorsement.** Derived from
  the curated route menu (`WARP_ROUTES` + the per-route artifacts the deployer
  wrote for exactly those routes), not on-chain discovery — so a rogue route
  from a leaked key cannot auto-whitelist itself.
- **Fail closed.** Every ownership handoff aborts the deploy on failure; an
  empty whitelist denies all relaying.

The deploy-time signer itself remains a long-lived cluster secret (minimizing it
is a deliberately-untracked accepted residual — once `.1`/`.2`/`.3` land, a
leaked deploy key can neither drain funds nor get rogue routes relayed).

## Fixes

### hyp-d9c.1 — Transfer ISM ownership during core deploy

File: `stack_orchestrator/data/config/deployer-scripts-config/deploy.sh`
(per-chain ownership block, currently ~lines 260–344).

- Replace the stale "no known account ownership transfer command … Skipping"
  branch (currently 298–306) with a real transfer per chain:
  ```
  hyperlane-sealevel-client --url "$RPC_URL" --keypair "$DEPLOYER_KEY_FILE" \
    multisig-ism-message-id transfer-ownership \
    --program-id "$ISM_ID" "$HARDWARE_WALLET_PUBKEY"
  ```
  where `ISM_ID = jq -r '.multisig_ism_message_id' "$PROGRAMS_FILE"`.
- **Drop `validator_announce` entirely** from the transfer/warning logic — it has
  only Init/Announce, no owner to move. Leave a one-line comment to that effect.
- **Fail closed:** this transfer has no `|| echo` guard. Tighten the existing
  **mailbox transfer** (currently 289–295) and **upgrade-authority transfers**
  (currently 277–281) from warn-only to fatal too. IGP transfers are already
  fatal. Net: any failed handoff aborts the deploy.

The CLI command exists at the pinned monorepo tree (`@hyperlane-xyz/core@10.2.0`,
`16c056a`): `MultisigIsmMessageIdSubCmd::TransferOwnership`, same
`{--program-id, <new_owner>}` shape as the working `mailbox transfer-ownership`.

### hyp-d9c.2 — Transfer warp-route app-level ownership during warp deploy

File: `stack_orchestrator/data/config/warp-deployer-scripts-config/deploy.sh`
(per-chain loop, currently ~lines 237–260).

- In the existing loop that already moves BPF upgrade authority, add — after it,
  using the same `$PROGRAM_ID` (the warp/token program base58 for that chain):
  ```
  hyperlane-sealevel-client --url "$RPC_URL" --keypair "$DEPLOYER_KEY_FILE" \
    token transfer-ownership --program-id "$PROGRAM_ID" "$HARDWARE_WALLET_PUBKEY"
  ```
- **Fail closed:** both this and the existing `set-upgrade-authority` become
  fatal (drop their `|| echo WARNING`).
- **Ordering is already correct.** `warp-route deploy` performs
  enroll-routers + set-ISM internally (before this loop), and the only post-loop
  step (synthetic-mint resolution) is a read-only `token query`. No reordering.

The CLI command exists at the pin: `TokenSubCmd::TransferOwnership`, same shape.

### hyp-d9c.3 — Gate the relayer with HYP_WHITELIST

**Builder.** New `stack_orchestrator/data/config/warp-deployer-scripts-config/build-relayer-whitelist.sh`,
invoked from warp `deploy.sh` immediately after `build-warp-ui-config.sh`. It
mirrors `build-warp-ui-config.sh`'s `WARP_ROUTES` loop, reads each route's
`${STATE_DIR}/warp-routes/<name>/warp-deploy-outputs/program-ids.json`, and emits
the union of per-chain `hex` recipients to `${STATE_DIR}/relayer-whitelist.json`:

```json
[{"recipientaddress":"0x<gorchain prog>"},{"recipientaddress":"0x<solana prog>"}, …]
```

- Recipient-only granularity (Option A). On-chain enrollment already rejects
  spoofed senders, so pinning the recipient (the warp program) is the meaningful
  relayer-level gate and fully defeats the rogue-route threat, with none of the
  brittleness of directed-edge tuples.
- Menu-scoped: iterating `WARP_ROUTES` (not scanning `/state`) means only
  curated routes are whitelisted.
- Empty route set → `[]` (deny-all, fail-safe).
- If a chain's `hex` lacks a `0x` prefix, prepend it; recipients are 32-byte
  H256 (64 hex chars), which Solana pubkeys satisfy.

**Wiring (env injection).**
- **compose** `docker-compose-hyperlane-relayer.yml`: add `HYP_WHITELIST:
  ${HYP_WHITELIST}` to the relayer `environment:`.
- **spec** `deployment/spec-relayer.yml` (+ `deployment/local/spec-relayer.yml`
  if present + staging): add `HYP_WHITELIST: '[]'` under `config:` (explicit
  default per the no-nested-defaults rule).
- **e2e** `tests/e2e/conftest.py`: alongside the existing relayer IGP patch, read
  `relayer-whitelist.json` via `bridge_state_loader.read_json(...)` and set the
  relayer spec's `config.HYP_WHITELIST = json.dumps(<list>, separators=(',',':'))`.
- **prod** `ops/playbooks/publish-bridge-state.yml`: slurp
  `relayer-whitelist.json` and add one entry to the existing `replace` loop
  patching `HYP_WHITELIST` into `spec-relayer.yml` as compact single-line JSON.
  No `stack_env_vars` change — it is deployment-derived state committed into the
  spec like the IGP IDs, not an operator-supplied env.

## E2E (positive assertions)

New module `tests/e2e/test_09_ownership_whitelist.py` (exact number per existing
ordering), asserting post-deploy:

1. ISM owner == `hardware_wallet_pubkey` on both chains — parse the
   `Access control: AccessControl { owner: Some(<pubkey>) … }` line from
   `multisig-ism-message-id query --program-id <ISM>`.
2. Each route's token owner == `hardware_wallet_pubkey` on both chains — parse
   the owner from `token query --program-id <prog> <type>` (confirm the field
   name in implementation; fall back to a behavioral check only if the query
   does not expose it).
3. `relayer-whitelist.json` / the relayer pod's `HYP_WHITELIST` contains exactly
   the deployed routes' program hexes (both chains, no extras).
4. The existing `test_08_bridge.py` relay path stays green under the tightened
   transfers + active whitelist (no change to that test).

The e2e already sets `HARDWARE_WALLET_PUBKEY` (conftest, from
`keypairs.hardware_wallet_pubkey`), so the transfer branches execute and these
assertions have a real target.

## Keep-in-sync / docs

- `CLAUDE.md`: add `build-relayer-whitelist.sh` / `relayer-whitelist.json` to the
  relayer row of the keep-in-sync table; note `HYP_WHITELIST` as a relayer config
  key.
- `docs/ops-decisions.md` (Ownership Transfer) and
  `docs/architecture-decisions.md` (Key Management): drop the "gap" caveats for
  `.1`/`.2`/`.3`; record the fail-closed policy and the menu-derived whitelist.
- Pebbles: `hyp-d9c.1/.2/.3` → `in_progress` at start, `closed` on completion.

## Out of scope (follow-ups, filed as pebbles)

- **Derive prod consumer values on-the-fly from generated artifacts** instead of
  regex-patching committed specs in `publish-bridge-state.yml`. Preferred
  long-term, but a separate concern; this design follows the existing patch
  pattern for `HYP_WHITELIST`.
- **Extend `verify-ownership`** (currently `deployment/ops-archive/scripts/verify-ownership.sh`,
  upgrade-authority only, archived) to verify app-level ISM/route owners.

## Risks / judgment calls

- Tightening the *existing* mailbox/upgrade-authority transfers to fatal may
  surface a latent failure in the e2e run — that is the intended fail-closed
  behavior, and the e2e is where it would be caught.
- The whitelist is committed into `spec-relayer.yml` in prod (public program
  addresses, exactly like the IGP IDs already are) — not secret.
