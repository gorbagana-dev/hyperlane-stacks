# Configurable IGP beneficiary — design

**Date:** 2026-06-15
**Status:** approved, pending implementation
**Branch:** `igp-beneficiary-config` (off `main`)

## Problem

The InterchainGasPaymaster (IGP) **beneficiary** is the account that receives
accumulated bridge gas fees when `igp claim` is called. The Hyperlane sealevel
client hardcodes it to the deployer's payer key at IGP init
(`hyperlane-monorepo/rust/sealevel/client/src/core.rs:244`,
`init_igp_instruction(..., beneficiary = ctx.payer_pubkey)`).

Consequence: the `igp-fee-claim` sidecar (`config/igp-fee-claim-scripts-config/claim-fees.sh`)
periodically claims fees on both chains, and they land on the throwaway deployer
hot key — funds that are effectively stranded once that key is discarded. This is
the gap recorded as the "IGP beneficiary open question".

## Goal

Let operators route claimed IGP fees to a **known, operator-controlled address**,
configured per environment in the deployment spec / `deployment-config.yml`,
defaulting to the bridge owner when unset.

## Key decision: no fork change required

The sealevel client already exposes a post-init subcommand:

```
hyperlane-sealevel-client \
  --url <RPC> --keypair <DEPLOYER_KEY_FILE> \
  igp set-igp-beneficiary \
  --program-id <IGP_PROGRAM_ID> --igp-account <IGP_ACCOUNT> <NEW_BENEFICIARY>
```

(`rust/sealevel/client/src/main.rs:551` `SetIgpBeneficiaryArgs`,
`igp.rs:278` handler). The instruction is authorized by the **current IGP owner**.
The deployer is the IGP owner during the window between `core deploy` and the
existing `igp transfer-igp-ownership` handoff. Setting the beneficiary there —
using the deployer key, mirroring the ownership-transfer pattern already in
`deploy.sh` — avoids a fork PR and `vX.Y.Z-gorbagana.N` release cycle entirely.

## Design

### Resolution and placement

A new operator-supplied var `IGP_BENEFICIARY_PUBKEY`. In `deploy.sh`, inside the
existing per-chain ownership loop, **before** the `igp transfer-igp-ownership`
block (the deployer must still be IGP owner to sign):

```bash
EFFECTIVE_BENEFICIARY="${IGP_BENEFICIARY_PUBKEY:-${BRIDGE_OWNER_PUBKEY:-}}"
if [ -n "$EFFECTIVE_BENEFICIARY" ]; then
  IGP_ID=$(jq -r '.igp_program_id // empty' "${PROGRAMS_FILE}")
  IGP_ACCOUNT=$(jq -r '.igp_account // empty' "${PROGRAMS_FILE}")
  if [ -z "$IGP_ID" ] || [ -z "$IGP_ACCOUNT" ]; then
    echo "FATAL: igp_program_id or igp_account missing from ${CHAIN_OUTPUT} program-ids.json"
    exit 1
  fi
  echo "Setting IGP beneficiary on ${CHAIN_OUTPUT} to ${EFFECTIVE_BENEFICIARY}..."
  hyperlane-sealevel-client \
    --url "$RPC_URL" --keypair "${DEPLOYER_KEY_FILE}" \
    igp set-igp-beneficiary \
    --program-id "$IGP_ID" --igp-account "$IGP_ACCOUNT" \
    "$EFFECTIVE_BENEFICIARY"
fi
```

Decisions (operator-confirmed):
- **Default when unset:** fall back to `BRIDGE_OWNER_PUBKEY` (a known,
  operator-controlled Privy wallet present in every environment). If *both* are
  empty (bare e2e run with no owner), the block is skipped and behavior is
  unchanged (beneficiary stays the deployer key).
- **Scope:** a single shared address for both gorchain and solana (same base58
  ed25519 address is valid on both SVM chains).

Only the **base IGP account** carries a beneficiary; the overhead IGP has none
and is untouched.

### Config flow (keep-in-sync chain)

| Layer | File | Change |
|---|---|---|
| Spec (×3) | `deployment/{,staging/,local/}spec-deployer.yml` | add `IGP_BENEFICIARY_PUBKEY: { env: IGP_BENEFICIARY_PUBKEY }` to the `hyperlane-deployer-secrets` envMap + header comment |
| Compose | `compose-jobs/docker-compose-hyperlane-svm-deployer.yml` | pass `IGP_BENEFICIARY_PUBKEY=${IGP_BENEFICIARY_PUBKEY:-}` |
| Deploy script | `config/deployer-scripts-config/deploy.sh` | the set-beneficiary block above |
| E2E fixture | `tests/e2e/fixtures/test-spec-deployer.yml` | add the env key (defaults → falls back to the fixture's bridge owner) |
| Ansible env map (×3) | `ops/inventories/{prod,staging,local}/group_vars/all.yml` | `IGP_BENEFICIARY_PUBKEY: "{{ igp_beneficiary_pubkey \| default('') }}"` + list it in the deployer stack's env |
| Ansible operator config (×3) | `ops/inventories/{prod,staging,local}/deployment-config.example.yml` | `igp_beneficiary_pubkey:` with a comment (optional; defaults to bridge owner) |

`IGP_BENEFICIARY_PUBKEY` rides in the `hyperlane-deployer-secrets` bundle
alongside `BRIDGE_OWNER_PUBKEY`/`IGP_ORACLE_PUBKEY` — consistent with how the
other operator-supplied pubkeys are injected, even though the value is public.

The warp-deployer is **not** involved (IGP is set up by the core deployer only).

### Testing

TDD via e2e (the assertion is written first and must fail before the deploy
change lands):

- After the deployer Job completes, run `igp query` against the base IGP account
  on both chains and assert `beneficiary == <fixture bridge owner pubkey>` (the
  default path), proving the set-beneficiary step ran on each chain.
- Resolution-precedence check on `deploy.sh` where practical (explicit
  `IGP_BENEFICIARY_PUBKEY` wins over `BRIDGE_OWNER_PUBKEY`; both empty → no-op).

### Docs

- `specs/stack-specifications.md` — document `IGP_BENEFICIARY_PUBKEY` under the
  deployer stack, including the default-to-bridge-owner behavior and the
  set-before-handoff ordering.
- `ops/runbooks/privy-wallets.md` + staging funding table — note the bridge
  owner is now also the default IGP fee beneficiary.

## Out of scope

- Setting the beneficiary on the **already-deployed staging bridge**: IGP
  ownership there has already moved to the oracle, so changing it now is an
  oracle-signed maintenance op, not a deploy-time op. The operator will handle
  this directly; it belongs with the maintenance-ops epic, not this change.
- Any change to `claim-fees.sh` (it already claims to whatever beneficiary is set
  on-chain — no change needed).
