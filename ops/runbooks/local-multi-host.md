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
  zone (`base_domain`). `https://s3.<zone>`, `https://validator-x.<zone>`, etc. resolve via
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
- A **public DNS zone on Cloudflare** (`base_domain`) and a Cloudflare API token scoped to it.
- A **Privy** project (validator + gas-oracle signing) — see [privy-wallets.md](privy-wallets.md).
- A **GHCR** PAT (`packages:read`) for the private `gorbagana-dev/*` images (the
  docker-login user defaults to `gorbagana-dev`).

**Each managed VM:** public IPv4 with inbound **80 + 443** (Let's Encrypt HTTP-01) and
**22** (controller) open, and the connecting user needs **passwordless sudo** (or run
the playbooks with `--ask-become-pass`). Nothing else pre-installed — `setup-all.yml`
provisions Docker/kind/kubectl + laconic-so.

## 2. Privy wallets

Mint the four server wallets once per [privy-wallets.md](privy-wallets.md). Every
ID/address it lists goes into the **one** operator file,
`inventories/local/deployment-config.yml` (filled in step 5).

## 3. Chains on a separate box

Stand up both chains **out-of-band** on a separate beefy box (not in this inventory).
That box runs both chains, so the same setup script the single-host path uses applies —
clone this repo there and run it on the box (it isn't ansible-managed):

```bash
# on the chains box — needs laconic-so + docker. Install the Solana CLI (provides
# solana-test-validator + spl-token) if absent; the bridge ansible doesn't reach
# this out-of-band box:
command -v solana >/dev/null || sh -c "$(curl -sSfL https://release.anza.xyz/v3.1.9/install)"
# Private gorchain image -> GHCR login (PAT only; user defaults to gorbagana-dev):
GHCR_PAT=<pat> ops/scripts/setup-chains.sh
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

The controller connects over SSH to `ansible_host`, which defaults to `public_ip` —
override per-host only if the controller reaches a box at a different address. Confirm
both hosts answer before going further:

```bash
cd ops   # all commands below run from here
ansible -i inventories/local/hosts-multihost.yml all:!controller -m ping   # expect: SUCCESS each
```

Your zone and the chains-box RPC URLs go into `deployment-config.yml` (step 5):
`local_base_domain` (the public Cloudflare zone) plus
`local_gorchain_rpc_url`/`local_solana_rpc_url`. No committed file needs editing:
`validators-multihost.yaml` hostnames render from `local_base_domain` at load
time, and `dns_records` is derived from group membership, so the same
`group_vars` serves both topologies.

## 5. Secrets

```bash
cp inventories/local/deployment-config.example.yml inventories/local/deployment-config.yml
# then fill it in — every operator value lives here (secrets, the Privy
# IDs/addresses from step 2, and the multi-host keys: local_base_domain +
# local_*_rpc_url); setup-all fails fast naming anything missing
```

`cloudflare_api_token` is **required** (Let's Encrypt + A records). No `helius_api_key` —
the Solana side is your own chain. MinIO/Grafana credentials are generated into
`deployment-config.yml` by the `credentials` role on first run.

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

The owner/oracle pubkeys and validator addresses were already filled into
`deployment-config.yml` in step 5; now also set `local_warp_token_mint` there —
the `WARP_TOKEN_MINT` the SPL deploy just printed. The warp route render reads
it from the config at deploy time (no file to place on any host). (The specs'
`image-pull-secret` username is committed as `gorbagana-dev`; GHCR authenticates
by the PAT, the username doesn't matter.)

## 7. Run it

`deploy-all.yml` commits + pushes the deployer-derived state mid-flight (see below), so
deploy off a dedicated branch — **never `main`** (the `deploy_branch` default). The hosts
fetch the repo on that branch, so create and push it first:

```bash
git checkout -b <deploy-branch> && git push -u origin <deploy-branch>
```

Point both playbooks at the multi-host validators file via `-e validators_file=...` and
the same `-e deploy_branch=<deploy-branch>`:

```bash
# Phase 1 — provision + reconcile Cloudflare DNS + LE + generate creds
ansible-playbook -i inventories/local/hosts-multihost.yml playbooks/setup-all.yml \
  -e validators_file=$PWD/../deployment/local/bridges/default/operator/validators-multihost.yaml \
  -e deploy_branch=<deploy-branch>

# Phase 2 — deploy MinIO -> deployer Job -> publish state -> consumers + validators
ansible-playbook -i inventories/local/hosts-multihost.yml playbooks/deploy-all.yml \
  -e validators_file=$PWD/../deployment/local/bridges/default/operator/validators-multihost.yaml \
  -e deploy_branch=<deploy-branch>
```

`deploy-all.yml` runs `publish-bridge-state.yml` mid-flight: it patches the
deployer-derived values (IGP IDs/accounts, mailboxes, warp addresses/mints) into the
**local** specs and commits/pushes `deploy_branch`. Add `-e state_review=true` to review
the diff before it commits.

## 8. Access the stacks

Public DNS + LE — browse the hostnames directly once DNS propagates:
`https://warp-ui.<zone>`, `https://grafana.<zone>`, `https://prometheus.<zone>`,
`https://minio-console.<zone>`.

## 9. Try the bridge (Backpack)

Use a throwaway test wallet — never the deployer account. Same flow as the
single-host runbook's "Try the bridge" section, with two differences:

- The chains box is out-of-band (not ansible-managed), so fund the wallet by
  running the script there instead of the playbook:

  ```bash
  WALLET=<address> USDC_MINT=<the WARP_TOKEN_MINT from step 6> \
    ops/scripts/fund-test-wallet.sh   # GOR + SOL + 100 local USDC
  ```

- Backpack's custom RPC URLs are the chains box's public domains — set the
  transfer's ORIGIN chain before sending: **forward** (solana → gorchain) the
  `local_solana_rpc_url` domain, **reverse** the `local_gorchain_rpc_url`
  domain.

## 10. Reset between runs

```bash
ansible-playbook -i inventories/local/hosts-multihost.yml playbooks/stop-all.yml \
  -e validators_file=$PWD/../deployment/local/bridges/default/operator/validators-multihost.yaml
# also destroy the per-host kind clusters:
ansible-playbook -i inventories/local/hosts-multihost.yml playbooks/stop-all.yml \
  -e validators_file=$PWD/../deployment/local/bridges/default/operator/validators-multihost.yaml \
  -e destroy_cluster=true
# clean slate — also remove the deployment dirs and persisted host-path data under
# each host's kind_mount_root (keeps caddy-cert-backup; the out-of-band chains box untouched):
ansible-playbook -i inventories/local/hosts-multihost.yml playbooks/stop-all.yml \
  -e validators_file=$PWD/../deployment/local/bridges/default/operator/validators-multihost.yaml \
  -e destroy_cluster=true -e wipe_data=true
```

## 11. Limitations / notes

- **DNS propagation gates first access.** A records and LE issuance must settle before the
  hostnames resolve and serve trusted certs; the deploy preflight checks served hostnames
  resolve to each host's `public_ip`.
- **The chains box is yours to operate.** It is not in this inventory — keep its RPC
  domains reachable and the deployer/oracle funded, or the agents stall.
- **Re-running on a dirty clone.** The on-host token render edits the clone's specs in
  place (uncommitted), only on first `deploy create`. If you re-fetch a branch that also
  touched those specs, `fetch-stack --pull` can conflict — reset with `stop-all` and
  re-fetch clean.
