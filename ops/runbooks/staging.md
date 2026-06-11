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

- Three VMs reachable over SSH (agent forwarding on), a privileged user and a
  deploy user on each. Fill `ops/inventories/staging/host_vars/*.yml`
  (`public_ip`, `privileged_user`, `deploy_user`).
- Cloudflare API token scoped to `gorbagana.wtf` DNS records.
- A Helius **devnet** project (separate key from prod).
- A Privy app for staging with: an oracle server-wallet and one server-wallet
  per validator — follow [privy-wallets.md](privy-wallets.md), then fill
  `privy_wallet_id` in
  `deployment/staging/bridges/default/operator/validators.yaml` and the
  `*_VALIDATOR_ADDRESS` / `IGP_ORACLE_PUBKEY` values in
  `ops/inventories/staging/group_vars/all.yml`.
- `cp ops/inventories/staging/secrets.example.yml ops/inventories/staging/secrets.yml`
  and fill the operator secrets.
- Set `NEXT_PUBLIC_WALLET_CONNECT_ID` in `deployment/staging/spec-warp-ui.yml`
  (a WalletConnect Cloud project id, or `""` to disable) — the deploy gate
  refuses the unfilled sentinel.

All commands below run from `ops/` with `-i inventories/staging/hosts.yml`.

## 1. Provision the fleet

    ansible-playbook -i inventories/staging/hosts.yml playbooks/setup-all.yml

Bootstraps all three hosts (Docker, kind, kubectl, laconic-so), reconciles
DNS (including `rpc.staging.gorbagana.wtf` → staging-gorchain), and
generates+distributes credentials.

## 2. Stand up gorchain (persistent) + hot keys

    ansible-playbook -i inventories/staging/hosts.yml playbooks/prepare-gorchain.yml

Brings up the single-node gorchain via gorchain-stacks (state in
`~/chains/gorchain` on the chain host — re-runs preserve it), starts the
Caddy TLS front for `rpc.staging.gorbagana.wtf`, and generates the hot
signing keys into `~/.credentials/hyperlane/` on the chain host. Staging
signs from key files by design (the prod path is Ledger).

### Distribute + fund the keys

The play prints the generated addresses. Copy each keyfile to the host whose
spec reads it (`grep -rn 'file:' deployment/staging/spec-*.yml` is the
authoritative map), into `~/.credentials/hyperlane/` on:

- `staging-bridge-ops` — deployer + relayer + warp-deployer keys
- `staging-gorchain` — `validator-gorchain.key`
- `staging-solana-validator` — `validator-solana.key`

Fund the on-chain signers:

- **gorchain** (own faucet):
  `solana airdrop 100 <ADDR> --url http://localhost:8899` on staging-gorchain
  (deployer 100; the rest 1).
- **solana devnet** (rate-limited):
  `solana airdrop 2 <ADDR> --url https://api.devnet.solana.com`, repeated as
  the faucet allows, or top up from an operator devnet wallet. The deployer
  needs the most (program deploys); also fund the Privy oracle pubkey.
- Warp collateral is Circle's devnet USDC
  (`4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU`) — faucet at
  https://faucet.circle.com for transfer tests.

## 3. Deploy the bridge

Staging requires an explicit `-e deploy_branch` (no default — a forgotten flag
fails up front instead of publishing bridge state to main):

    ansible-playbook -i inventories/staging/hosts.yml playbooks/deploy-all.yml -e deploy_branch=<branch>

MinIO → deployer Job → warp deployer → relayer → gas-oracle → warp-ui →
validators → monitoring, with the deploy gates refusing any unfilled
placeholder. First publish of the generated state, attended:

    ansible-playbook -i inventories/staging/hosts.yml playbooks/publish-bridge-state.yml -e deploy_branch=<branch> -e state_review=true

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

    ansible-playbook -i inventories/staging/hosts.yml playbooks/stop-all.yml

Full host reset before a bootstrap rehearsal additionally destroys the kind
cluster (`-e destroy_cluster=true`) and, **only if intentionally resetting
chain state**, removes `~/chains/gorchain` + the `gorchain-rpc-caddy`
container on staging-gorchain by hand. Chain state is deliberately never
destroyed by a playbook.
