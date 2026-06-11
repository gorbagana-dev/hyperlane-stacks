# Staging — from-zero deployment

Staging is the prod rehearsal ground: Solana **devnet** (Helius) + a
persistent single-node **gorchain**, on three VMs, with real Cloudflare DNS
and Let's Encrypt TLS under `staging.gorbagana.wtf`. Design:
`docs/superpowers/specs/2026-06-10-staging-ops-design.md`.

| Host | Runs |
|---|---|
| `staging-bridge-ops` | MinIO, deployer Jobs, relayer, gas-oracle, monitoring, warp-ui |
| `staging-gorchain` | gorchain chain (+ Caddy RPC front), gorchain validator |
| `staging-solana-validator` | solana validator |

## 0. Prerequisites

- Three VMs reachable over SSH (agent forwarding on), each with a privileged
  user and a deploy user.
- A Cloudflare API token with DNS edit on the `gorbagana.wtf` zone.
- A Helius **devnet** project (separate key from prod).
- A Privy app for staging with an oracle server-wallet, a bridge-owner
  server-wallet, and one server-wallet per validator — follow
  [privy-wallets.md](privy-wallets.md).

Then fill in the operator values (the deploy gates refuse any value left at a
`REPLACE_WITH_*` sentinel):

| File | Variable | Value |
|---|---|---|
| `ops/inventories/staging/host_vars/<host>.yml` (all three) | `public_ip`, `privileged_user`, `deploy_user` | each VM's public IP and its two users |
| `ops/inventories/staging/secrets.yml` | every key under `REQUIRED` | `cp secrets.example.yml secrets.yml`, then fill: Cloudflare token, Privy app id/secret + oracle wallet id, Helius API key, GHCR PAT |
| `deployment/staging/bridges/default/operator/validators.yaml` | `privy_wallet_id` (`gorchain-primary` entry) | the gorchain validator server-wallet id from Privy |
| `deployment/staging/bridges/default/operator/validators.yaml` | `privy_wallet_id` (`solana-primary` entry) | the solana validator server-wallet id from Privy |
| `ops/inventories/staging/group_vars/all.yml` | `GORCHAIN_VALIDATOR_ADDRESS` | the gorchain Privy validator wallet's address |
| `ops/inventories/staging/group_vars/all.yml` | `SOLANA_VALIDATOR_ADDRESS` | the solana Privy validator wallet's address |
| `ops/inventories/staging/group_vars/all.yml` | `IGP_ORACLE_PUBKEY` | the Privy oracle wallet's Solana pubkey |
| `ops/inventories/staging/group_vars/all.yml` | `BRIDGE_OWNER_PUBKEY` | the Privy bridge-owner wallet's Solana pubkey |
| `deployment/staging/spec-warp-ui.yml` | `NEXT_PUBLIC_WALLET_CONNECT_ID` | a WalletConnect Cloud project id, or `""` to disable |

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
ansible-playbook -i inventories/staging/hosts.yml playbooks/prepare-gorchain.yml
```

Brings up the single-node gorchain via gorchain-stacks (state in
`~/chains/gorchain` on the chain host — re-runs preserve it), starts the
Caddy TLS front for `rpc.staging.gorbagana.wtf`, generates the hot signing
keys into `~/.credentials/hyperlane/` on staging-gorchain, and copies each
keyfile to the host whose spec reads it:

| Host | Keyfiles |
|---|---|
| `staging-bridge-ops` | `deployer-keypair.json`, `relayer-gorchain.key`, `relayer-solana.key`, `relayer-fee-claim.json` |
| `staging-gorchain` | `validator-gorchain.key` |
| `staging-solana-validator` | `validator-solana.key` |

Staging signs from generated throwaway key files; prod signs from
operator-provisioned key files. The bridge owner is not a keyfile on any env:
at the end of the deploy, program upgrade authority and mailbox/ISM/route
ownership transfer to `BRIDGE_OWNER_PUBKEY` — the Privy bridge-owner wallet,
which signs nothing during deployment.

### Fund the on-chain signers

The play exports every generated address to
`~/.credentials/hyperlane/addresses.env` on staging-gorchain:

| Signer | `addresses.env` var | gorchain (SOL) | solana devnet (SOL) |
|---|---|---|---|
| deployer | `DEPLOYER_KEYPAIR_ADDR` | 100 | ~10 (repeat airdrops, or top up from an operator devnet wallet) |
| gorchain validator | `VALIDATOR_GORCHAIN_ADDR` | 1 | — |
| solana validator | `VALIDATOR_SOLANA_ADDR` | — | 1 |
| relayer gorchain signer | `RELAYER_GORCHAIN_ADDR` | 1 | — |
| relayer solana signer | `RELAYER_SOLANA_ADDR` | — | 1 |
| IGP fee-claim | `RELAYER_FEE_CLAIM_ADDR` | 1 | 1 |
| Privy IGP oracle | — (`IGP_ORACLE_PUBKEY` in group_vars) | 1 | 1 |
| Privy bridge owner | — (`BRIDGE_OWNER_PUBKEY` in group_vars) | — | — (transfer target only) |

On staging-gorchain:

```bash
source ~/.credentials/hyperlane/addresses.env
ORACLE=<IGP_ORACLE_PUBKEY>   # the Privy oracle wallet's Solana pubkey

# gorchain — own faucet
solana airdrop 100 "$DEPLOYER_KEYPAIR_ADDR" --url http://localhost:8899
for a in "$VALIDATOR_GORCHAIN_ADDR" "$RELAYER_GORCHAIN_ADDR" "$RELAYER_FEE_CLAIM_ADDR" "$ORACLE"; do
  solana airdrop 1 "$a" --url http://localhost:8899
done

# solana devnet — 2 SOL per request, rate-limited; repeat the deployer line until ~10
solana airdrop 2 "$DEPLOYER_KEYPAIR_ADDR" --url https://api.devnet.solana.com
for a in "$VALIDATOR_SOLANA_ADDR" "$RELAYER_SOLANA_ADDR" "$RELAYER_FEE_CLAIM_ADDR" "$ORACLE"; do
  solana airdrop 2 "$a" --url https://api.devnet.solana.com
done
```

Warp collateral is Circle's devnet USDC
(`4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU`) — faucet at
https://faucet.circle.com for transfer tests.

## 3. Deploy the bridge

Staging requires an explicit `-e deploy_branch` (no default — a forgotten flag
fails up front instead of publishing bridge state to main):

```bash
ansible-playbook -i inventories/staging/hosts.yml playbooks/deploy-all.yml -e deploy_branch=<branch>
```

MinIO → deployer Job → warp deployer → relayer → gas-oracle → warp-ui →
validators → monitoring, with the deploy gates refusing any unfilled
placeholder. First publish of the generated state, attended:

```bash
ansible-playbook -i inventories/staging/hosts.yml playbooks/publish-bridge-state.yml -e deploy_branch=<branch> -e state_review=true
```

## 4. Verify

- `https://rpc.staging.gorbagana.wtf/health` answers `ok` and slots advance.
- MinIO (`https://minio-console.staging.gorbagana.wtf`): checkpoint objects
  appear under both validator buckets.
- Grafana (`https://grafana.staging.gorbagana.wtf`): relayer + validator
  dashboards report.
- `https://staging.gorbagana.wtf`: run a devnet-USDC transfer
  solana → gorchain and back.

## 5. Reset

Stacks only (chain + state survive):

```bash
ansible-playbook -i inventories/staging/hosts.yml playbooks/stop-all.yml
```

Full host reset before a bootstrap rehearsal additionally destroys the kind
cluster (`-e destroy_cluster=true`) and, **only if intentionally resetting
chain state**, removes `~/chains/gorchain` + the `gorchain-rpc-caddy`
container on staging-gorchain by hand. Chain state is deliberately never
destroyed by a playbook.
