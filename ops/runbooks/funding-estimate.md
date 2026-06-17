# Funding estimate — how much native token each account needs

How much to fund every signer for a bridge deployment, derived by measuring the
**staging** deployment on-chain (`staging-test` branch state, queried 2026-06-17).
Staging is one warp route (USDC, Solana collateral ↔ gorchain synthetic), so the
numbers below are "core + one route" — scale per the per-route line for more.

Amounts are in the chain's native token: **SOL** on Solana, **GOR** on gorchain
(same SVM rent math, so the figures track each other closely). On **staging**,
Solana is devnet and gorchain is the operator's own faucet chain, so token there
is free; on **prod** both are mainnet, so this is real money — budget the Solana
(Helius mainnet) leg in particular.

**Does the devnet measurement map to mainnet? Yes, 1-1.** Rent is the bulk of the
cost and it's a fixed function of account size: `getMinimumBalanceForRentExemption`
returns identical values on devnet, mainnet-beta, and gorchain (the per-byte rate
works out to the same 6960 lamports/byte on all three). It does **not** vary with
SOL price or network congestion. Congestion only touches transaction fees — base
fee is a flat 5000 lamports/signature, and *priority* fees (0 on the staging
deploy) may be needed to land deploy txs on a busy mainnet. That adds at most
~0.1, not whole SOL, so keep a small buffer but the SOL figures carry over. The
only thing that really changes is fiat: mainnet SOL costs real money.

## TL;DR — recommended funding per chain

| Account | Fund (1 route) | Why |
|---|--:|---|
| **deployer** | **~10** | Pays every deploy tx; needs the full ~8.2 up front (see breakdown). ~1.9 is reclaimable afterwards via `retire-keys.yml`. |
| relayer (per-chain signer) | 1–2 | Only signer with ongoing burn (delivery txs); scales with message volume. |
| validator (announce key) | 0.1 | One-time announce only (~0.002 measured); drained by `retire-keys` after, so funded minimally and **not** balance-monitored. |
| IGP fee-claim | 0.5 | Periodic claim txs (fees only). |
| Privy IGP oracle | 0.5 | Periodic gas-oracle posts (fees only). |
| Privy bridge owner | 0.5 | Receives ownership; pays only occasional admin txs. |

**Per additional warp route: +~3.3 per chain on the deployer** (one warp program
+ one prefunded ATA-payer PDA). Nothing else changes.

The deployer target is **route-dependent** (~8 + ~3.3 per warp route) and the key
is drained back to a treasury after deploy, so over-funding isn't lost. The prod
table sets Solana to **10** (a one-route baseline) and gorchain to **100** (extra
headroom for multiple routes / retries). Size it to your planned route count: the
funding gate checks the *target*, not the actual per-route need, so an under-set
target can run out mid-deploy.

> Watch these balances in production with the balance-monitor (see
> [monitoring.md](monitoring.md)) — the relayer is the one that drains over time.

## Where the deployer's ~8.2 goes

Measured deployer consumption (Solana devnet): funded 10, **8.07 consumed**. It
splits into a one-time **core** cost and a per-route cost, and ~90% of it is
**rent-exempt deposits locked in program accounts**, not burned fees.

**Why it's this much:** almost all of it is *rent* — the SOL an account must hold
to occupy validator state, sized to the account's bytes. It's a refundable deposit,
not a fee: it stays locked while the account exists and comes back if the account
is closed. Program bytecode accounts are big (~100–300 KB each) and there are five
of them, so the deposits add up. Actual fees burned are ~0.01.

### Program-deployment rent (the dominant cost)

Each Hyperlane program is an upgradeable BPF program; its `programData` account
must be funded to rent-exemption, and `solana program deploy` sizes that account
at **2× the binary** (upgrade headroom). Measured:

| Program | Solana rent | gorchain rent |
|---|--:|--:|
| mailbox / merkle-tree-hook | 1.218 | 1.218 |
| IGP | 1.645 | 1.645 |
| multisig ISM | 1.125 | 1.125 |
| validator-announce | 0.900 | 0.900 |
| warp token (collateral / synthetic) | 2.260 | 2.222 |
| **Total** | **7.15** | **7.11** |

The first four (**core**, ~4.89) are one-time. The warp program (~2.25) is
**per route**.

### Plus operational/state accounts

| Item | ~SOL | Notes |
|---|--:|---|
| Warp ATA-payer PDA | 1.0 / route / chain | Prefunded reserve used to create recipient token accounts. |
| IGP + mailbox/ISM/VA/route init PDAs | ~0.05 | Small rent deposits. |
| Synthetic mint (gorchain side) | ~0.004 | — |
| Transaction fees | ~0.01 | The only truly *burned* amount. |

**Reconciliation (Solana, 1 route):** core 4.89 + warp 2.26 + ATA-payer 1.0 +
PDAs/fees ~0.06 ≈ **8.2**. gorchain mirrors this (~8.1).

## What you actually spend vs. get back

- **Locked for the bridge's life (~7.1/chain):** program rent. Reclaimable only by
  closing the programs on permanent decommission (see below); stays locked while
  the bridge runs.
- **Operational reserve (~1.0/route/chain):** sits in the ATA-payer PDA, still
  usable.
- **Reclaimable (~1.9):** the deployer's leftover, drained back to a treasury by
  `retire-keys.yml` after deploy.
- **Burned (~0.01):** transaction fees.

So a single-route bridge ties up **~8 per chain** semi-permanently, plus the
small operational reserves on the other signers.

### Reclaiming rent if you decommission

If you permanently shut the bridge down, the program rent — the ~7.1/chain bulk —
comes back via:

```
solana program close <program_id> --recipient <treasury>
```

- **Who signs:** the **upgrade authority**, which is the **bridge owner**
  (`BRIDGE_OWNER_PUBKEY` — the Privy server wallet; `AUiSJK…` on staging). The
  deployer can't — it handed authority off at deploy. There's no playbook for it
  (`retire-keys.yml` only drains the hot signer keyfiles), so it's a manual close
  tx signed through Privy, one per program.
- **Irreversible:** closing burns the program ID (you can't redeploy to it) and
  bricks the bridge — only do it after draining escrowed collateral and letting
  in-flight messages settle.
- **Not recovered this way:** the smaller deposits — state PDAs, the ~1/route
  ATA-payer PDAs, the synthetic mint. They're owned by the programs with no admin
  "close-all" path, so treat them as sunk (~1+/chain).

Net: on teardown you can claw ~7/chain back to a treasury; the rest stays locked.

## Cutting the cost (the 2× lever)

The programs are deployed **upgradeable** so a security fix or a new Hyperlane
release can ship *in place* — same program ID, same routes and PDAs — instead of
deploying a new program and migrating the whole bridge (and its funds). For a
bridge holding value, that patch path is worth keeping.

What you can tune is the **2× size headroom**, which is what doubles the rent — and
it's *not* required to upgrade. Three choices, cheapest last to recover from:

| Deploy mode | Rent | Upgradeable? |
|---|--:|---|
| default `--max-len` 2× | ~7.1/chain | yes — to a binary up to 2× the size |
| tight `--max-len` (= current size) | **~3.6/chain** | yes — to a binary up to current size; `solana program extend` (pays the extra rent then) if a later version is bigger |
| `--final` | ~3.6/chain | **no** — not advised for a fund-custody bridge |

Hyperlane's deploy uses the default 2×. **Deploy-tight + extend-on-demand** keeps
upgradeability while halving the upfront deposit — you only pay for headroom if and
when a larger version actually needs it.

## Measured per-account consumption (staging-test, 2026-06-17)

Over the staging lifetime (deploy + runtime to date). Confirms the operational
signers barely move — 1 token each is generous for test traffic; the relayer is
the only one whose burn scales with volume.

| Account | Address | Solana devnet | gorchain |
|---|---|--:|--:|
| deployer | `7XTpUZEh…` | 8.07 consumed (10→1.93) | deploy cost ~8.1¹ |
| relayer | sol `9mjJKHtJ…` / gor `9GTCDHsd…` | 0.018 | ~0.021 |
| validator | sol `82VPWyuZ…` / gor `CYNX7MRN…` | 0.0022 | ~0.002 |
| IGP fee-claim | `C4Rcdc1o…` | 0.0001 | ~0.0001 |
| Privy IGP oracle | `5VcYt3R4…` | 0.0024 | ~0.002 |

¹ On staging the gorchain deployer was over-funded by the chain's faucet (still
holds ~222), so its consumption can't be read as funded-minus-current; the ~8.1
is the deployment cost measured directly from the gorchain program/PDA rents. On
**prod** gorchain is mainnet with no operator faucet, so budget the deployer the
same ~10 GOR as the Solana side.

## Reproduce

Query any public RPC (`getBalance`, and `getAccountInfo` jsonParsed → `programData`
→ `getBalance` for program rent). Program IDs live in the deployment's
`generated/program-ids.json` and per-route `warp-deploy-outputs/program-ids.json`;
the ATA-payer addresses are printed in the warp `deploy.log` ("Funding ATA payer …").
Signer addresses come from the operator (the generated keyfiles' pubkeys).
