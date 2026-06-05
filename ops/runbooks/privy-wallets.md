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

The validators are EVM wallets even though both chains are Agave/SVM: Hyperlane's
checkpoint signatures are ECDSA secp256k1 **on every deployment**, and the
multisig ISM identifies validators by their `0x…` address. The gas oracle is a
Solana wallet because its only job is submitting SVM transactions. (The validators
also have a separate ed25519 *announce* key — the `HYP_DEFAULTSIGNER_KEY` hex
keyfile — which is **not** Privy.)

## Step 1 — Create the Privy app

1. Sign up at [dashboard.privy.io](https://dashboard.privy.io) and create an app.
2. **App settings → Basics**: copy the **App ID** → `privy_app_id`.
3. **App settings → API keys**: create/copy the **App Secret** → `privy_app_secret`
   (shown once).
4. Enable **server wallets** if there's a toggle. **Do not attach an
   owner/authorization key** to the wallets — our KMS proxy and gas oracle
   authenticate with app-secret Basic auth only and send no
   `privy-authorization-signature`, so an owner-gated wallet rejects every
   signing call.

## Step 2 — Export creds for the curl calls

```bash
export PRIVY_APP_ID='<your app id>'
export PRIVY_APP_SECRET='<your app secret>'
export PRIVY_BASE='https://api.privy.io'

auth=(-u "$PRIVY_APP_ID:$PRIVY_APP_SECRET" -H "privy-app-id: $PRIVY_APP_ID" -H 'Content-Type: application/json')
```

## Step 3 — Mint the three wallets

```bash
# Validator checkpoint key — gorchain (EVM / secp256k1)
curl -s "${auth[@]}" -X POST "$PRIVY_BASE/v1/wallets" -d '{"chain_type":"ethereum"}' | tee val-gorchain.json

# Validator checkpoint key — solana validator (EVM / secp256k1)
curl -s "${auth[@]}" -X POST "$PRIVY_BASE/v1/wallets" -d '{"chain_type":"ethereum"}' | tee val-solana.json

# Gas oracle (Solana / ed25519)
curl -s "${auth[@]}" -X POST "$PRIVY_BASE/v1/wallets" -d '{"chain_type":"solana"}' | tee oracle.json
```

Each response is `{"id":"…","address":"…","chain_type":"…"}` — EVM wallets give a
`0x…` address, the Solana one a base58 pubkey.

## Step 4 — Map outputs to config

```bash
jq -r '"id=\(.id)  address=\(.address)"' val-gorchain.json val-solana.json oracle.json
```

| From | Field | Set in |
|---|---|---|
| `val-gorchain.json` | `id` | `privy_wallet_id` (gorchain entry) — your topology's validators file¹ |
| `val-gorchain.json` | `address` (0x) | `GORCHAIN_VALIDATOR_ADDRESS` — `group_vars/all.yml` |
| `val-solana.json` | `id` | `privy_wallet_id` (solana entry) — validators file¹ |
| `val-solana.json` | `address` (0x) | `SOLANA_VALIDATOR_ADDRESS` — `group_vars/all.yml` |
| `oracle.json` | `id` | `privy_oracle_wallet_id` — `inventories/local/secrets.yml` |
| `oracle.json` | `address` (base58) | `IGP_ORACLE_PUBKEY` — `group_vars/all.yml` |

¹ `deployment/local/bridges/default/operator/validators.yaml` (single-host) or
`validators-multihost.yaml` (multi-host).

## Step 5 — Verify before deploying (catches the owner-key failure mode)

Replicate exactly what the KMS proxy sends, against one EVM wallet:

```bash
VID=$(jq -r .id val-gorchain.json)
curl -s "${auth[@]}" -X POST "$PRIVY_BASE/v1/wallets/$VID/rpc" \
  -d '{"chain_type":"ethereum","method":"secp256k1_sign",
       "params":{"hash":"0x0000000000000000000000000000000000000000000000000000000000000001"}}'
```

- **200 with a `signature`** → app-secret auth is enough; you're good.
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
