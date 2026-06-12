# Privy server wallets — shared setup

Both the `local` runbooks (single- and multi-host), and prod/staging, sign with
**Privy server wallets**. This page is the one-time "mint the wallets and record
the IDs/addresses" procedure; each runbook links here and then says which vars to
fill. Do it once per bridge.

## What signs with Privy, and why two curve types

| Signer | Job | Privy wallet type |
|---|---|---|
| Validator checkpoint key (×2, one per chain) | Signs Hyperlane checkpoints (the validator's protocol identity) | **Ethereum / secp256k1** |
| Gas oracle | Submits IGP config txs on both SVM chains | **Solana / ed25519** |
| Bridge owner | Receives program upgrade authority + mailbox/ISM/route ownership at the end of each deploy. Signs nothing during deployment (pure transfer target); future maintenance ops sign with it | **Solana / ed25519** |

The validators are EVM wallets even though both chains are Agave/SVM: Hyperlane's
checkpoint signatures are ECDSA secp256k1 **on every deployment**, and the
multisig ISM identifies validators by their `0x…` address. The gas oracle is a
Solana wallet because its only job is submitting SVM transactions. (The validators
also have a separate ed25519 *announce* key — the `HYP_DEFAULTSIGNER_KEY` hex
keyfile — which is **not** Privy.)

## Step 1 — Create the Privy app

1. Sign up at [dashboard.privy.io](https://dashboard.privy.io) and create an app.
2. Creating the app pops **"Save your new API keys"** with both values: copy the
   **App ID** → `privy_app_id` and the **App secret** → `privy_app_secret`. The
   secret is shown only this once — if you lose it, reset it from the app's
   API-keys settings.

## Step 2 — Export creds for the curl calls

```bash
export PRIVY_APP_ID='<your app id>'
export PRIVY_APP_SECRET='<your app secret>'
export PRIVY_BASE='https://api.privy.io'

auth=(-u "$PRIVY_APP_ID:$PRIVY_APP_SECRET" -H "privy-app-id: $PRIVY_APP_ID" -H 'Content-Type: application/json')
```

## Step 3 — Mint the four wallets

```bash
# Validator checkpoint key — gorchain (EVM / secp256k1)
curl -s "${auth[@]}" -X POST "$PRIVY_BASE/v1/wallets" -d '{"chain_type":"ethereum"}' | tee val-gorchain.json

# Validator checkpoint key — solana validator (EVM / secp256k1)
curl -s "${auth[@]}" -X POST "$PRIVY_BASE/v1/wallets" -d '{"chain_type":"ethereum"}' | tee val-solana.json

# Gas oracle (Solana / ed25519)
curl -s "${auth[@]}" -X POST "$PRIVY_BASE/v1/wallets" -d '{"chain_type":"solana"}' | tee oracle.json

# Bridge owner (Solana / ed25519)
curl -s "${auth[@]}" -X POST "$PRIVY_BASE/v1/wallets" -d '{"chain_type":"solana"}' | tee owner.json
```

Each response is `{"id":"…","address":"…","chain_type":"…"}` — EVM wallets give a
`0x…` address, the Solana one a base58 pubkey.

## Step 4 — Map outputs to config

```bash
jq -r '"id=\(.id)  address=\(.address)"' val-gorchain.json val-solana.json oracle.json owner.json
```

Every value goes into the env's **one** operator file,
`ops/inventories/<env>/deployment-config.yml`:

| From | Field | deployment-config.yml key |
|---|---|---|
| `val-gorchain.json` | `id` | `privy_validator_wallet_ids.gorchain-primary` |
| `val-gorchain.json` | `address` (0x) | `gorchain_validator_address` |
| `val-solana.json` | `id` | `privy_validator_wallet_ids.solana-primary` |
| `val-solana.json` | `address` (0x) | `solana_validator_address` |
| `oracle.json` | `id` | `privy_oracle_wallet_id` |
| `oracle.json` | `address` (base58) | `igp_oracle_pubkey` |
| `owner.json` | `id` | nothing consumes it yet — record it; future maintenance ops sign with this wallet |
| `owner.json` | `address` (base58) | `bridge_owner_pubkey` |

## Step 5 — Verify before deploying (catches the owner-key failure mode)

Replicate exactly what the KMS proxy sends, against one EVM wallet:

```bash
VID=$(jq -r .id val-gorchain.json)
curl -s "${auth[@]}" -X POST "$PRIVY_BASE/v1/wallets/$VID/rpc" \
  -d '{"chain_type":"ethereum","method":"secp256k1_sign","params":{"hash":"0x0000000000000000000000000000000000000000000000000000000000000001"}}'
```

(Keep the JSON on one line — a stray space after a continuation backslash sends
the request with no body, and Privy answers with a misleading "Invalid
discriminator value" listing every method.)

- **200 with `{"method":"secp256k1_sign","data":{"signature":"0x…","encoding":"hex"}}`**
  (verified against the live API) → app-secret auth is enough; you're good.
- **401/403** → the wallet requires an authorization key. Recreate it without an
  owner and retry.

Confirm the oracle wallet is reachable and the right type:

```bash
OID=$(jq -r .id oracle.json)
curl -s "${auth[@]}" "$PRIVY_BASE/v1/wallets/$OID"   # expect chain_type:"solana"
```

When the gorchain validator pod starts, its KMS proxy logs `Recovered validator
public key, Ethereum address: 0x…` — it must equal `GORCHAIN_VALIDATOR_ADDRESS`.

## Funding

- The **EVM checkpoint keys never touch a chain** (they sign off-chain
  checkpoints) — do **not** fund them.
- The **Solana oracle wallet** submits IGP txs on both chains — fund its base58
  pubkey on gorchain **and** solana (see the chains step in your runbook).
- The **bridge-owner wallet** signs nothing during deployment (it only receives
  ownership) — no funding needed until maintenance ops start signing with it.
