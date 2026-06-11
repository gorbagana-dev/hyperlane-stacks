# Staging — from-zero deployment

Staging is the prod rehearsal ground: Solana **devnet** (Helius) + a
persistent single-node **gorchain**, on three VMs, with real Cloudflare DNS
and Let's Encrypt TLS under `staging.gorbagana.wtf`. Design:
`docs/superpowers/specs/2026-06-10-staging-ops-design.md`.

| Host (`host_vars/<host>.yml`) | Runs | Starting spec |
|---|---|---|
| `staging-bridge-ops` | MinIO, deployer Jobs, relayer, gas-oracle, monitoring, warp-ui | 4 vCPU / 8 GB / 80 GB SSD |
| `staging-gorchain` | gorchain chain + Caddy RPC front | 8 vCPU / 32 GB / 500 GB NVMe SSD |
| `staging-hyperlane-validators` | both hyperlane validators | 4 vCPU / 8 GB / 60 GB SSD |

Both validators live off the chain host: staging-gorchain's ports 80/443
belong to the gorchain RPC Caddy front, so the validators' kind ingress
(Caddy + Let's Encrypt) needs its own machine.

## 0. Prerequisites

- Three VMs reachable over SSH (agent forwarding on), each with a privileged
  user and a deploy user.
- A Cloudflare API token with DNS edit on the `gorbagana.wtf` zone.
- A Helius **devnet** project (separate key from prod).
- A Privy app for staging with an oracle server-wallet, a bridge-owner
  server-wallet, and one server-wallet per validator — follow
  [privy-wallets.md](privy-wallets.md).

Then fill in exactly two things:

1. `ops/inventories/staging/host_vars/<host>.yml` (one per VM in the table
   above): `public_ip`, `privileged_user`, `deploy_user`.
2. The one operator file — every other value (secrets, the Privy
   IDs/addresses, the WalletConnect id) lives here, each key commented:

   ```bash
   cp ops/inventories/staging/deployment-config.example.yml \
      ops/inventories/staging/deployment-config.yml
   # then fill it in
   ```

`setup-all` fails fast naming any missing value; the deploy gates refuse
anything left unfilled.

**One-time env fact — synthetic token metadata.** The warp deploy embeds a
metadata URI into the gorchain synthetic mint; the route menu ships a sentinel
the deploy gate refuses until it is filled. Host this JSON at a stable public
HTTPS URL — a GitHub gist works (use its **raw** URL; even "secret" gists serve
raw anonymously; gists are text-only, hence the external image URL):

```json
{
  "name": "USD Coin",
  "symbol": "USDC",
  "description": "Hyperlane-bridged Circle devnet USDC on Gorchain (staging).",
  "image": "https://raw.githubusercontent.com/solana-labs/token-list/main/assets/mainnet/EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v/logo.png"
}
```

`name`/`symbol` must equal the route menu's `remote.name`/`remote.symbol`, and
the image URL must return 200 — the deployer validates both. Commit the raw URL
as `metadataUri` in `deployment/staging/bridges/default/warp-routes/usdc.yml`
(done once for the env, then it lives in git).

All commands below run from `ops/` with `-i inventories/staging/hosts.yml`.

## 1. Provision the fleet

```bash
ansible-playbook -i inventories/staging/hosts.yml playbooks/setup-all.yml
```

Bootstraps all three hosts (Docker, kind, kubectl, laconic-so), reconciles
DNS (including `rpc.staging.gorbagana.wtf` → staging-gorchain), and
generates+distributes credentials.

## 2. Stand up gorchain (persistent) + hot keys

```bash
ansible-playbook -i inventories/staging/hosts.yml playbooks/staging/prepare-gorchain.yml
```

Brings up the single-node gorchain via gorchain-stacks (state in
`~/chains/gorchain` on the chain host — re-runs preserve it), starts the
Caddy TLS front for `rpc.staging.gorbagana.wtf`, generates the hot signing
keys into `~/.credentials/hyperlane/` on staging-gorchain, copies each
keyfile to the host whose spec reads it, and funds every on-chain signer —
no SSH-ing anywhere:

| Host | Keyfiles |
|---|---|
| `staging-bridge-ops` | `deployer-keypair.json`, `relayer-gorchain.key`, `relayer-solana.key`, `relayer-fee-claim.json` |
| `staging-hyperlane-validators` | `validator-gorchain.key`, `validator-solana.key` |

Staging signs from generated throwaway key files; prod signs from
operator-provisioned key files. The bridge owner is not a keyfile on any env:
at the end of the deploy, program upgrade authority and mailbox/ISM/route
ownership transfer to `BRIDGE_OWNER_PUBKEY` — the Privy bridge-owner wallet,
which signs nothing during deployment.

### Funding (done by the play)

The play funds each signer to its target balance (`fund-staging-signers.sh`,
driven by the generated `addresses.env` + `igp_oracle_pubkey` from
deployment-config). Balance-driven and idempotent — re-runs only top up:

| Signer | gorchain (SOL) | solana devnet (SOL) |
|---|---|---|
| deployer | 100 | 10 |
| gorchain validator | 1 | — |
| solana validator | — | 1 |
| relayer gorchain signer | 1 | — |
| relayer solana signer | — | 1 |
| IGP fee-claim | 1 | 1 |
| Privy IGP oracle | 1 | 1 |
| Privy bridge owner | — | — (transfer target only) |

gorchain funds from its own faucet (guaranteed). Devnet airdrops are
rate-limited: if the faucet refuses, the play **fails listing the underfunded
addresses** — top them up from an operator devnet wallet and re-run.

Warp collateral is Circle's devnet USDC
(`4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU`) — faucet at
https://faucet.circle.com for transfer tests.

## 3. Deploy the bridge

Staging requires an explicit `-e deploy_branch` (no default — a forgotten flag
fails up front instead of publishing bridge state to main):

```bash
ansible-playbook -i inventories/staging/hosts.yml playbooks/deploy-all.yml -e deploy_branch=<branch>
```

MinIO → deployer Job → warp deployer → **publish bridge state** → relayer →
gas-oracle → warp-ui → validators → monitoring, with the deploy gates
refusing any unfilled placeholder. The publish (commit + push of the
deployer-generated state to `deploy_branch`) happens mid-flight — add
`-e state_review=true` to gate it for an attended review on the first run.
`playbooks/publish-bridge-state.yml` exists standalone only for re-publishing
outside a full deploy.

## 4. Verify

- `https://rpc.staging.gorbagana.wtf/health` answers `ok` and slots advance.
- MinIO (`https://minio-console.staging.gorbagana.wtf`): checkpoint objects
  appear under both validator buckets.
- Grafana (`https://grafana.staging.gorbagana.wtf`): relayer + validator
  dashboards report.
- `https://staging.gorbagana.wtf`: run a devnet-USDC transfer
  solana → gorchain and back — see the next section.

## 5. Try the bridge (Backpack)

Use a throwaway test wallet — never the deployer account.

1. **Backpack** (skip if you already use it): install the extension from
   https://backpack.app and create a wallet (or import a dedicated test
   seed). Copy its Solana address.
2. **Fund it** — GOR on gorchain + devnet SOL, balance-driven and idempotent:

   ```bash
   ansible-playbook -i inventories/staging/hosts.yml playbooks/fund-test-wallet.yml -e wallet=<address>
   ```

   Devnet airdrops are rate-limited: on shortfall the play fails naming the
   gap — re-run later or top up from another devnet wallet. **Devnet USDC**
   comes from Circle: https://faucet.circle.com → token USDC, network
   **Solana Devnet**, the same address.
3. **Point Backpack at the transfer's ORIGIN chain** (Settings → your
   wallet → Solana → RPC connection) — the wallet must broadcast on the
   chain you are sending FROM:
   - **forward** (solana devnet → gorchain): preset **Devnet**
     (`https://api.devnet.solana.com`)
   - **reverse** (gorchain → solana devnet): **Custom RPC** →
     `https://rpc.staging.gorbagana.wtf`
4. Open `https://staging.gorbagana.wtf`, connect Backpack, pick the
   direction + amount, transfer. After the relay (≈a minute), switch the
   RPC per step 3 to see the balance on the destination side. No token
   import is needed — Backpack discovers SPL token accounts on-chain.
   Both sides carry on-chain metadata: devnet USDC is Circle's mint, and
   the gorchain synthetic is a Token-2022 mint whose name/symbol the warp
   deploy embedded from the route menu (no logo — staging leaves the
   optional `metadataUri` empty). To double-check a balance, the route's
   mints are in the published
   `deployment/staging/bridges/default/generated/warp-routes/warpRoutes.yaml`
   on `deploy_branch`.

## 6. Reset

Stacks only (chain + state survive):

```bash
ansible-playbook -i inventories/staging/hosts.yml playbooks/stop-all.yml
```

Full host reset before a bootstrap rehearsal additionally destroys the kind
cluster (`-e destroy_cluster=true`) and, **only if intentionally resetting
chain state**, removes `~/chains/gorchain` + the `gorchain-rpc-caddy`
container on staging-gorchain by hand. Chain state is deliberately never
destroyed by a playbook.
