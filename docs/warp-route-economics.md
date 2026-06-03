# Warp Route Economics: Value, Decimals, and Liquidity

Reference notes on how Hyperlane warp routes move value — written to settle the
recurring questions "what's the exchange rate?" and "can I bridge token X to a
different token Y?". Findings are grounded in `hyperlane-monorepo` sealevel code
and the Hyperlane docs; references are inline.

> TL;DR — A warp route is a **bridge for one asset**, not a swap. It always moves
> value **1:1** (lock 1 ⇄ mint/release 1). There is **no exchange rate** for the
> bridged amount; the only adjustment is a fixed power-of-10 **decimal rescale**.
> The only "exchange rate" anywhere in Hyperlane is in the IGP gas oracle, and it
> applies to **relayer fees**, never the transferred token.

---

## 1. A warp route bridges one asset, 1:1

Every warp route has exactly one underlying asset. One side **takes custody** of
the real token when you send; the other side **provides** the token on receipt.
The number of (whole) tokens is preserved end-to-end — you cannot configure a
price or ratio.

There is no rate to keep in sync because no two distinct assets are ever
exchanged. "1 USDC in → 1 gUSDC out" is not special to USDC; it is how *every*
route behaves.

---

## 2. The only adjustment: decimal normalization (not a price)

The same asset may use different decimal counts on different chains. The route
rescales the integer amount by a fixed power of ten so the human-readable value
matches. This is pure base-10 shifting — no price input.

`rust/sealevel/libraries/hyperlane-sealevel-token/src/accounts.rs:202`:

```rust
pub fn convert_decimals(amount: U256, from_decimals: u8, to_decimals: u8) -> Option<U256> {
    match from_decimals.cmp(&to_decimals) {
        Ordering::Greater => amount / 10^(from - to),   // remote has fewer decimals
        Ordering::Less    => amount * 10^(to - from),   // remote has more decimals
        Ordering::Equal   => amount,
    }
}
```

Applied on both directions of a transfer
(`accounts.rs:77` `local_amount_to_remote_amount`, `accounts.rs:83`
`remote_amount_to_local_amount`). `remote_decimals` is fixed once at deploy time
from the init config (`accounts.rs:37`) — it is configuration, but it is a
decimal count, not a value/price.

**Example.** A token with 9 decimals on Solana bridged to a chain that uses 6
decimals: sending `1.500000000` (= `1_500_000_000` base units) arrives as
`1.500000` (= `1_500_000` base units). Same value, different integer width.

---

## 3. Two supply models, and a third topology

Whether the "provide on receipt" side **mints** or **releases from a pool**
depends on each side's token type.

### Synthetic side — elastic supply (mint / burn)

The synthetic (wrapped) side mints on receipt and burns on send, so its supply
tracks whatever is locked elsewhere. It can always satisfy a transfer.

`rust/sealevel/programs/hyperlane-sealevel-token/src/plugin.rs` —
`transfer_in` **burns** (`burn_checked`), the receive path **mints**
(`mint_to_checked`).

### Collateral / native side — fixed pool (lock / release)

The collateral and native sides hold real tokens in an escrow balance. Sending
**locks** into the escrow; receiving **releases** from it. Nothing is minted.

`rust/sealevel/programs/hyperlane-sealevel-token-collateral/src/plugin.rs` — both
`transfer_in` (line 241) and `transfer_out` (line 319) use plain
`transfer_checked` (SPL transfer to/from the program's own token account). No
mint, no burn.

### Common route shapes

| Route | Origin side | Remote side | Supply behavior |
|---|---|---|---|
| Collateral → synthetic | lock real token | mint wrapped | classic wrapped-asset bridge (e.g. USDC → gUSDC) |
| Native → synthetic | lock native coin | mint wrapped | e.g. SOL → wrapped-SOL |
| **Collateral → collateral** | lock real token | **release from pool** | same token already exists on both chains |
| **Native → native** | lock native coin | **release from pool** | e.g. ETH between Optimism and Arbitrum |

The last two have **no synthetic side at all**. Per the Hyperlane SVM warp route
guide, they are valid *only when the same asset already exists on both chains*,
and they call out that "rebalancing liquidity is an important consideration"
(<https://docs.hyperlane.xyz/docs/guides/warp-routes/svm/svm-warp-route-guide>).

---

## 4. Why collateral↔collateral / native↔native needs liquidity rebalancing

A fixed-pool side can only pay out what it already holds — it cannot mint. So if
flow is net one-directional (everyone bridges A→B), B's pool drains while A's
pool grows. Once B is empty, transfers to B **fail**: there is no elastic supply
to cover them.

This is unique to the all-collateral / all-native topologies. The synthetic
model never hits it, because the synthetic side mints on demand. That is exactly
why the docs flag rebalancing only for this case.

Hyperlane ships tooling for it — the movable-collateral / rebalancer mechanism:
`solidity/contracts/token/libs/MovableCollateralRouter.sol` (and `HypNative.sol`,
`extensions/HypERC4626.sol`). An operator / market-maker periodically moves
collateral back to the drained side.

**Rule of thumb:** synthetic somewhere ⇒ supply is elastic, no rebalancing.
All sides collateral/native ⇒ fixed pools, you own the liquidity problem.

---

## 5. "Bridge SOL to GOR" — why that is NOT a warp route

A frequent confusion: native-to-native sounds like it could swap one chain's
native coin for another's (e.g. SOL on Solana → GOR on Gorbagana). It cannot.

Native-to-native means the **same asset** exists natively on both chains (ETH is
ETH on Optimism and on Arbitrum). SOL and GOR are **different assets**, so moving
between them is a **swap**, not a bridge — outside what any warp route does.

| Topology | Same asset on both chains? | Valid warp route? |
|---|---|---|
| native(SOL) → synthetic(wrapped-SOL) | yes (real vs wrapped SOL) | ✅ |
| native(ETH @ Optimism) ↔ native(ETH @ Arbitrum) | yes (ETH is ETH) | ✅ |
| native(SOL) ↔ native(GOR) | **no — SOL ≠ GOR** | ❌ swap, needs a DEX |

To exchange SOL for GOR at a market rate you would layer a DEX/AMM (or two warp
routes plus a swap venue between the wrapped tokens) on top — the pricing lives
there, never in the warp route.

---

## 6. The one real exchange rate in Hyperlane: IGP gas, not the bridged token

The Interchain Gas Paymaster (IGP) *does* carry a price ratio — but only to let a
sender pre-pay, in the **origin** chain's gas token, for gas the relayer will
spend on the **destination** chain.

`rust/sealevel/programs/hyperlane-sealevel-igp/src/accounts.rs` —
`token_exchange_rate` (line 239), `TOKEN_EXCHANGE_RATE_SCALE = 10^19` (line 16),
used in `quote_gas_payment` (line 187):

```rust
destination_gas_cost = gas_amount * gas_price;
origin_cost = destination_gas_cost * token_exchange_rate / TOKEN_EXCHANGE_RATE_SCALE;
```

This is a genuine gas-token-to-gas-token price, but it is applied to the
**relayer fee only** — it never touches the amount of token being bridged.

**Kept in sync off-chain:** the rate is static on-chain until an off-chain agent
overwrites `gas_price` + `token_exchange_rate` in the oracle account. In this
repo that updater is the `hyperlane-gas-oracle/` service; in the monorepo it is
the gas-price-oracle tooling under `typescript/infra`. So it is polled-and-pushed
periodically, not live.

---

## 7. Summary table

| | Bridged token amount | Relayer gas fee |
|---|---|---|
| Mechanism | warp route (lock/mint or lock/release) | IGP gas oracle |
| Rate used | none — always 1:1 | `token_exchange_rate` (gas tokens) |
| Only adjustment | decimal rescale (`convert_decimals`) | full price ratio |
| Configured at deploy? | `remote_decimals` (fixed) | yes, written on-chain |
| Kept in sync with prices? | n/a (no price involved) | off-chain updater pushes periodically |
| Liquidity concern? | only if all sides collateral/native (rebalance) | n/a |

## References

- Sealevel token (synthetic, mint/burn + decimal conversion):
  `rust/sealevel/libraries/hyperlane-sealevel-token/src/accounts.rs`,
  `rust/sealevel/programs/hyperlane-sealevel-token/src/plugin.rs`
- Sealevel collateral (lock/release pool):
  `rust/sealevel/programs/hyperlane-sealevel-token-collateral/src/plugin.rs`
- IGP gas oracle (the only exchange rate):
  `rust/sealevel/programs/hyperlane-sealevel-igp/src/accounts.rs`
- Rebalancer / movable collateral:
  `solidity/contracts/token/libs/MovableCollateralRouter.sol`
- Hyperlane SVM warp route guide (native↔native, collateral↔collateral,
  rebalancing note):
  <https://docs.hyperlane.xyz/docs/guides/warp-routes/svm/svm-warp-route-guide>
- This repo's routes and direction convention: `docs/architecture-decisions.md`,
  `deployment/spec-warp-deployer.yml`
