# Using the bridge

A short guide for anyone who wants to move tokens across the bridge at
https://bridge.gorbagana.wtf. The bridge connects **Gorchain** and **Solana**:
you send a token on one chain and receive it on the other.

## What you need

- A **Solana-compatible wallet** browser extension (e.g. Backpack, Phantom, or
  Solflare). Both Gorchain and Solana use the same wallet type, so one wallet can
  hold accounts on both sides.
- A little native token on the **chain you send from**, to pay network and bridge
  fees (the page shows the exact fees before you confirm).

## Step by step

1. **Open the bridge** at https://bridge.gorbagana.wtf.

2. **Connect your wallet.** Click **Connect Wallet** (top right) and approve the
   connection in your wallet popup. Connect a wallet for the chain you're sending
   from; if you also connect one on the destination chain, the recipient address
   fills in automatically.

3. **Choose the route.** In the **Send** box pick the chain you're sending from
   and the token; in the **Receive** box pick the chain you're sending to. Use the
   swap button between the two boxes to flip the direction.

4. **Enter the amount.** Type how much to send, or click **Max** to use your full
   balance (your available balance is shown beneath the field).

5. **Set the recipient.** This defaults to your connected wallet on the
   destination chain. To send to someone else, paste their wallet address.

6. **Review.** The page shows the fees (interchain gas, local network gas, and any
   token fee) and the amount that will arrive. Check these before continuing.

7. **Confirm in your wallet.** Approve the transaction(s) your wallet prompts for
   — some tokens need a short approval step first, then the transfer itself.

8. **Wait for delivery.** Bridging completes once the message is relayed to the
   destination chain (usually a short wait). You can follow the status in the
   transfer details, and open it in the [explorer](https://explorer.bridge.gorbagana.wtf)
   for the full cross-chain trace.

## Tips & troubleshooting

- **"Connect Wallet" keeps showing / wrong account** — make sure your wallet is
  unlocked and the right account is selected, then reconnect.
- **Not enough for fees** — fees are paid in the native token of the chain you
  send *from*. Top that account up and try again.
- **Funds not arrived yet** — relaying takes a little time; check the transfer
  details and the explorer link before retrying. The tokens leave the source
  chain and arrive on the destination chain, so they won't show in both at once.
- **A token or chain you expect is missing** — only the routes configured for this
  deployment appear; if one is missing, it hasn't been added (an operator task).
