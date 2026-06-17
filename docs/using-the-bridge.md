# Using the bridge

A short guide for anyone who wants to move tokens across the bridge at
https://bridge.gorbagana.wtf. The bridge connects **Gorchain** and **Solana**:
you send a token on one chain and receive it on the other.

## What you need

- The **[Backpack](https://backpack.app/)** wallet browser extension. Backpack is
  the supported wallet because it lets you set a **custom RPC URL** — Gorchain is a
  custom chain, so the wallet must be pointed at it to send from it. Other wallets
  aren't supported.
- A little native token on the **chain you send from**, to pay network and bridge
  fees (the page shows the exact fees before you confirm).

> **Important:** Backpack signs, submits, and confirms each transaction against
> its *own* configured RPC. That RPC must match the chain you are **sending from**,
> or Backpack will hang on "Confirming Transaction". You re-point it whenever you
> change direction — see step 4.

## Step by step

1. **Open the bridge** at https://bridge.gorbagana.wtf.

2. **Connect Backpack.** Click **Connect Wallet** (top right) and approve the
   connection. Connect the account for the chain you're sending from; if you also
   connect one on the destination chain, the recipient address fills in
   automatically.

3. **Choose the route.** In the **Send** box pick the chain you're sending from
   and the token; in the **Receive** box pick the chain you're sending to. Use the
   swap button between the two boxes to flip the direction.

4. **Point Backpack at the source chain's RPC.** In Backpack open
   **Settings → Solana → RPC Connection** and set the URL to match the chain in
   your **Send** box:
   - Sending **from Gorchain** → `https://rpc.gorbagana.wtf`
   - Sending **from Solana** → a reliable Solana mainnet RPC

   If you later swap the direction, change this again. Skipping this step is the
   most common cause of a transfer that hangs at "Confirming Transaction".

5. **Enter the amount.** Type how much to send, or click **Max** to use your full
   balance (your available balance is shown beneath the field).

6. **Set the recipient.** This defaults to your connected wallet on the
   destination chain. To send to someone else, paste their wallet address.

7. **Review.** The page shows the fees (interchain gas, local network gas, and any
   token fee) and the amount that will arrive. Check these before continuing.

8. **Confirm in Backpack.** Approve the transaction(s) it prompts for — some
   tokens need a short approval step first, then the transfer itself.

9. **Wait for delivery.** Bridging completes once the message is relayed to the
   destination chain (usually a short wait). You can follow the status in the
   transfer details, and open it in the [explorer](https://explorer.bridge.gorbagana.wtf)
   for the full cross-chain trace.

## Tips & troubleshooting

- **Stuck on "Confirming Transaction"** — Backpack's RPC doesn't match the chain
  you're sending from. Set it to the source chain's RPC (step 4) and retry. The
  transfer often still lands on-chain even when the wallet hangs, so check the
  explorer before resending.
- **"Connect Wallet" keeps showing / wrong account** — make sure Backpack is
  unlocked and the right account is selected, then reconnect.
- **Not enough for fees** — fees are paid in the native token of the chain you
  send *from*. Top that account up and try again.
- **Funds not arrived yet** — relaying takes a little time; check the transfer
  details and the explorer link before retrying. The tokens leave the source
  chain and arrive on the destination chain, so they won't show in both at once.
- **A token or chain you expect is missing** — only the routes configured for this
  deployment appear; if one is missing, it hasn't been added (an operator task).
