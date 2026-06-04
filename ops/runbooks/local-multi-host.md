# Runbook — `local` multi-host (Layer 2)

Bring the bridge up across **two ansible-managed hosts** to exercise cross-host routing —
every S3 write, chain-RPC call, and metrics scrape crosses a host boundary. This mirrors
the prod/staging ingress path (Caddy + Cloudflare + Let's Encrypt). The two SVM chains run
out-of-band on a separate beefy box, reached at domains.

| Machine | Runs | Managed by |
|---|---|---|
| chains box (beefy) | gorchain test validator (RPC) + `solana-test-validator` (RPC) | **out-of-band** — not in this inventory |
| `local-services` | MinIO, monitoring, gas-oracle, warp-ui, deployer | this ansible |
| `local-agents` | gorchain + solana hyperlane validators, relayer | this ansible |

All commands run from `ops/` on the controller (your machine).

## Networking model

- Caddy + **Cloudflare DNS** + real **Let's Encrypt** under an operator-supplied public
  zone (`dns_zone`). `https://s3.<zone>`, `https://validator-x.<zone>`, etc. resolve via
  public DNS to the right host's Caddy, and the Rust S3 client trusts the LE-issued cert
  (cross-host routing needs both). Topology is **derived** from inventory group membership
  (the agents and MinIO are on different hosts) — no flag.
- The chains run on a separate box at their own domains; the in-cluster agents **and**
  warp-ui reach them at those URLs (`gorchain_rpc_url`/`solana_rpc_url`). No chain
  `external-services` block.

## 1. Prerequisites

**Controller** — same as `ops/README.md` → "Prerequisites":

```bash
pip install "ansible>=9" ansible-lint yamllint
ansible-galaxy collection install -r requirements.yml -p ./collections
```

Plus `git`, `ssh` with **agent forwarding**, `dig`, `kubectl`.

**Accounts / access:**
- A **public DNS zone on Cloudflare** (`dns_zone`) and a Cloudflare API token scoped to it.
- A **Privy** project (validator + gas-oracle signing) — see [privy-wallets.md](privy-wallets.md).
- A **GHCR** PAT (`packages:read`) for the private `gorbagana-dev/*` images.

**Each managed VM:** public IPv4 with inbound **80 + 443** (Let's Encrypt HTTP-01) and
**22** (controller) open. Nothing else pre-installed — `setup-all.yml` provisions
Docker/kind/kubectl + laconic-so.

## 2. Privy wallets

Mint the three server wallets once per [privy-wallets.md](privy-wallets.md), then fill the IDs/addresses it
lists:

- `privy_wallet_id` per validator in
  `deployment/local/bridges/default/operator/validators-multihost.yaml`
- `privy_oracle_wallet_id` in `inventories/local/secrets.yml`
- `GORCHAIN_VALIDATOR_ADDRESS`, `SOLANA_VALIDATOR_ADDRESS`, `IGP_ORACLE_PUBKEY` in
  `inventories/local/group_vars/all.yml`

## 3. Chains on a separate box

Stand up both chains **out-of-band** on a separate beefy box (not in this inventory).
That box runs both chains, so the same setup script the single-host path uses applies —
clone this repo there and run it on the box (it isn't ansible-managed):

```bash
# on the chains box — needs laconic-so, docker, the Solana CLI + spl-token.
# Private gorchain image -> GHCR login.
GHCR_USER=<gh-user> GHCR_PAT=<pat> ops/scripts/setup-chains.sh
```

It brings up gorchain (dev-RPC values in the deploy spec's `config:`, **no** hand-written
`config.env`) and a solana-test-validator (ledger under `./chains/data`), and waits for
health **and** slot progress on both. Funding + the SPL mint happen in step 6, on this
same box, once the keys exist.

Front each chain RPC with a reachable domain/TLS (out-of-band DNS + reverse proxy), then
**set both URLs** in `group_vars/all.yml` — they feed the in-cluster agents *and* warp-ui:

```yaml
# inventories/local/group_vars/all.yml
gorchain_rpc_url: "https://<gorchain-rpc-domain>"
solana_rpc_url: "https://<solana-rpc-domain>"
```

## 4. Inventory & zone

Use the committed `inventories/local/hosts-multihost.yml`. Set:

```yaml
# inventories/local/host_vars/local-services.yml  and  local-agents.yml
public_ip: "<that host's public IPv4>"
```

```yaml
# inventories/local/group_vars/all.yml
dns_zone: "staging.gorbagana.wtf"        # your public Cloudflare zone
```

Edit `validators-multihost.yaml`: set each validator's `privy_wallet_id` and replace
`REPLACE_WITH_LOCAL_DNS_ZONE` in the hostnames to match `dns_zone` (e.g.
`validator-gorchain.staging.gorbagana.wtf`). The `host:` is already `local-agents`.
`dns_records` is derived from group membership, so the same `group_vars` serves both
topologies.

## 5. Secrets

```bash
cp inventories/local/secrets.example.yml inventories/local/secrets.yml
# fill: cloudflare_api_token, privy_app_id, privy_app_secret,
#       privy_oracle_wallet_id, ghcr_user, ghcr_pat
```

`cloudflare_api_token` is **required** (Let's Encrypt + A records). No `helius_api_key` —
the Solana side is your own chain. MinIO/Grafana credentials are generated into
`secrets.yml` by the `credentials` role on first run.

## 6. Keyfiles, funding & USDC mint

These are throwaway test keys. Generate + fund them and deploy the SPL mint **on the
chains box** (it has the chain toolchain and localhost access to both chains) — the same
scripts as single-host, run by hand since the box isn't ansible-managed:

```bash
# on the chains box
ops/scripts/gen-local-keys.sh --fund --oracle <IGP_ORACLE_PUBKEY>   # generate + fund all signers + oracle
ops/scripts/deploy-spl-token.sh                                     # prints WARP_TOKEN_MINT
```

Then copy each host's keys from the chains box to that host's `~/.credentials/hyperlane/`
(the keys must live where the stack that reads them runs):

```
local-services (deployer host):
  deployer-keypair.json    # Solana keypair JSON array (deployer + warp-deployer)
local-agents (validators + relayer host):
  validator-gorchain.key   # hex validator announce key (HYP_DEFAULTSIGNER_KEY)
  validator-solana.key     # hex validator announce key
  relayer-gorchain.key     # hex relayer signing key (HYP_CHAINS_GORCHAIN_SIGNER_KEY)
  relayer-solana.key       # hex relayer signing key
  relayer-fee-claim.json   # Solana keypair JSON array (IGP fee-claim sidecar)
```

You keep `hardware-wallet.json` (its pubkey → `HARDWARE_WALLET_PUBKEY`; not deployed).
Then fill in `group_vars/all.yml`: `HARDWARE_WALLET_PUBKEY` (from the helper),
`IGP_ORACLE_PUBKEY`, `GORCHAIN_VALIDATOR_ADDRESS`, `SOLANA_VALIDATOR_ADDRESS`;
`REPLACE_WITH_GITHUB_USERNAME` in the specs' `image-pull-secret`; and `WARP_TOKEN_MINT`
(from the SPL deploy) in `deployment/local/spec-warp-deployer.yml`.

## 7. Run it

Point both playbooks at the multi-host validators file via `-e validators_file=...`:

```bash
export PATH=/home/dev/.ops-ansible-venv/bin:$PATH LC_ALL=C.UTF-8 LANG=C.UTF-8

# Phase 1 — provision + reconcile Cloudflare DNS + LE + generate creds
ansible-playbook -i inventories/local/hosts-multihost.yml playbooks/setup-all.yml \
  -e validators_file=$PWD/deployment/local/bridges/default/operator/validators-multihost.yaml

# Phase 2 — deploy MinIO -> deployer Job -> publish state -> consumers + validators
ansible-playbook -i inventories/local/hosts-multihost.yml playbooks/deploy-all.yml \
  -e validators_file=$PWD/deployment/local/bridges/default/operator/validators-multihost.yaml
```

Testing off a branch (the hosts fetch the repo themselves) — add `-e deploy_branch=<branch>`.

`deploy-all.yml` runs `publish-bridge-state.yml` mid-flight: it patches the
deployer-derived values (IGP IDs/accounts, mailboxes, warp addresses/mints) into the
**local** specs and commits/pushes `deploy_branch`. Add `-e state_review=true` to review
the diff before it commits.

## 8. Access the stacks

Public DNS + LE — browse the hostnames directly once DNS propagates:
`https://warp-ui.<zone>`, `https://grafana.<zone>`, `https://prometheus.<zone>`,
`https://minio-console.<zone>`.

## 9. Reset between runs

```bash
ansible-playbook -i inventories/local/hosts-multihost.yml playbooks/stop-all.yml \
  -e validators_file=$PWD/deployment/local/bridges/default/operator/validators-multihost.yaml
# also destroy the per-host kind clusters:
ansible-playbook -i inventories/local/hosts-multihost.yml playbooks/stop-all.yml \
  -e validators_file=$PWD/deployment/local/bridges/default/operator/validators-multihost.yaml \
  -e destroy_cluster=true
```

## 10. Limitations / notes

- **DNS propagation gates first access.** A records and LE issuance must settle before the
  hostnames resolve and serve trusted certs; the deploy preflight checks served hostnames
  resolve to each host's `public_ip`.
- **The chains box is yours to operate.** It is not in this inventory — keep its RPC
  domains reachable and the deployer/oracle funded, or the agents stall.
- **Re-running on a dirty clone.** The on-host token render edits the clone's specs in
  place (uncommitted), only on first `deploy create`. If you re-fetch a branch that also
  touched those specs, `fetch-stack --pull` can conflict — reset with `stop-all` and
  re-fetch clean.
