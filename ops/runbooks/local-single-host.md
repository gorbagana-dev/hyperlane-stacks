# Runbook — `local` single-host (Layer 1)

Bring the whole bridge up against **self-run chains on one VM** to test the deploy-side
ansible end to end. Every stack, both hyperlane validators, and both SVM chains run on a
single box; there is no public DNS and no Let's Encrypt.

All commands run from `ops/` on the controller (your machine).

## Networking model

- Self-trusted **mkcert** certs — the `local_tls` role generates a multi-SAN cert and
  pre-seeds it into Caddy (no ACME, **no DNS provider**). `base_domain` is just a label the
  cert covers (`hyperlane.test`), not a real Cloudflare zone.
- The validator→MinIO, relayer→MinIO, and Prometheus→validator/relayer legs run
  **in-cluster over HTTP** (pod-to-pod) — no NAT hairpin.
- The two SVM chains run on the host; in-cluster pods reach them via `external-services`
  `ip:` → the kind-network gateway → `gorchain-rpc:8899` / `solana-rpc:18899` (the e2e
  pattern). Topology is **derived** from inventory group membership — no flag.

## 1. Prerequisites

**Controller** — same as `ops/README.md` → "Prerequisites":

```bash
pip install "ansible>=9" ansible-lint yamllint
ansible-galaxy collection install -r requirements.yml -p ./collections
```

Plus `git`, `ssh` with **agent forwarding**, `kubectl`.

**Accounts / access:**
- A **Privy** project (validator + gas-oracle signing) — see [privy-wallets.md](privy-wallets.md).
- A **GHCR** PAT (`packages:read`) for the private `gorbagana-dev/*` images (bridge stacks
  + the gorchain chain image). The docker-login user defaults to `gorbagana-dev`.
- **No Cloudflare**, **no public DNS zone**, **no public 80/443** — single-host serves
  Caddy on the host loopback.

**VM:** inbound **22** from the controller only, and the connecting user needs
**passwordless sudo** (bootstrap installs packages and writes under `/usr/local`,
`/srv`) — or run the playbooks with `--ask-become-pass`. `setup-all.yml` provisions
Docker/kind/kubectl + laconic-so; `prepare-chains.yml` installs the Solana CLI
(`solana`/`solana-keygen`/`solana-test-validator`/`spl-token`, Anza v3.1.9) if missing.

## 2. Privy wallets

Mint the three server wallets once per [privy-wallets.md](privy-wallets.md), then set the
IDs/addresses it lists — these must be in place **before** the prepare step (the oracle
pubkey is funded there):

- `privy_wallet_id` per validator in
  `deployment/local/bridges/default/operator/validators.yaml`
- `privy_oracle_wallet_id` in `inventories/local/deployment-config.yml`
- `GORCHAIN_VALIDATOR_ADDRESS`, `SOLANA_VALIDATOR_ADDRESS`, `IGP_ORACLE_PUBKEY` in
  `inventories/local/group_vars/all.yml`

## 3. Inventory

Single-host is the default (`inventories/local/hosts.yml` — every group, including
`chain_hosts`, points at `local-1`). Set:

```yaml
# inventories/local/host_vars/local-1.yml
public_ip: "<this host's public IPv4>"
```

The controller connects over SSH to `ansible_host`, which defaults to `public_ip` — so
the line above is enough on a local VM. Override it per-host only if the controller
reaches the box at a different address (bastion/private). Confirm connectivity before
going further:

```bash
cd ops   # all commands below run from here
ansible -i inventories/local/hosts.yml local-1 -m ping   # expect: SUCCESS / "pong"
```

Set `REPLACE_WITH_GITHUB_USERNAME` in the specs' `image-pull-secret`.

## 4. Secrets

```bash
cp inventories/local/deployment-config.example.yml inventories/local/deployment-config.yml
# fill: privy_app_id, privy_app_secret, privy_oracle_wallet_id, ghcr_pat
```

**No `cloudflare_api_token`** (single-host uses mkcert). No `helius_api_key` — the Solana
side is your own chain. MinIO/Grafana credentials are generated into `deployment-config.yml` by the
`credentials` role on first run.

## 5. Provision the host

```bash
# bootstrap (Docker/kind/laconic-so) + mkcert TLS + generate creds
ansible-playbook -i inventories/local/hosts.yml playbooks/setup-all.yml
```

## 6. Chains, accounts & SPL token (one playbook)

`prepare-chains.yml` runs on the `chain_hosts` group (here `local-1`) and does the whole
painful pre-deploy setup in order: stands up gorchain + the solana-test-validator (waiting
for health **and** slot progress on both), generates and funds every signer plus the Privy
oracle, and deploys the collateral USDC SPL mint.

```bash
ansible-playbook -i inventories/local/hosts.yml playbooks/prepare-chains.yml
```

The collateral mint is persisted to
`~/.credentials/hyperlane/warp-token-mint` and substituted into the warp route
automatically — no edit needed.

Notes:
- gorchain's dev-RPC config lives in the deploy spec's `config:` (no hand-written
  `config.env`); the chain images are pulled with the GHCR creds from `deployment-config.yml`.
- The in-cluster bridge reaches the chains via `gorchain-rpc:8899` / `solana-rpc:18899`
  **automatically** (external-services → kind gateway) — leave `gorchain_rpc_url`/
  `solana_rpc_url` at their placeholders in `group_vars/all.yml`.
- It generated these keyfiles under `~/.credentials/hyperlane/` on local-1:
  `deployer-keypair.json` (deployer + warp-deployer), `validator-{gorchain,solana}.key`
  (announce keys), `relayer-{gorchain,solana}.key` (relayer signers),
  `relayer-fee-claim.json` (IGP fee claims).
- Re-running is safe: existing keys are never overwritten, and a healthy solana validator
  is left as-is. To run it by hand instead of via ansible, the same scripts live at
  `ops/scripts/{setup-chains,gen-local-keys,deploy-spl-token}.sh`.

## 7. Fill the remaining vars

Nothing left from key generation: the collateral mint was persisted in step 6 and
is substituted into the warp route automatically; `BRIDGE_OWNER_PUBKEY`,
`IGP_ORACLE_PUBKEY`, and the validator addresses all come from Privy and were set
in step 2.

## 8. Deploy

`deploy-all.yml` commits + pushes the deployer-derived state mid-flight (see below), so
deploy off a dedicated branch — **never `main`**. The hosts fetch the repo on that
branch, so create and push it first, then pass it as `deploy_branch` (required on
local — there is no default):

```bash
git checkout -b <deploy-branch> && git push -u origin <deploy-branch>

# MinIO -> deployer Job -> publish state -> consumers + validators
ansible-playbook -i inventories/local/hosts.yml playbooks/deploy-all.yml \
  -e deploy_branch=<deploy-branch>
```

`deploy-all.yml` runs `publish-bridge-state.yml` mid-flight: it patches the
deployer-derived values (IGP IDs/accounts, mailboxes, warp addresses/mints) into the
**local** specs and commits/pushes `deploy_branch`. Add `-e state_review=true` to review
the diff before it commits.

## 9. Access the stacks

No public DNS, but Caddy listens on the host's public `:443`. The `local-access` playbook
trusts the host's mkcert CA on your workstation and resolves the bridge hostnames to the
host IP, so the UIs open directly — no tunnel.

```bash
# trust the CA + write /etc/hosts on your workstation. Needs workstation sudo (add -K if
# your user isn't passwordless sudo).
ansible-playbook -i inventories/local/hosts.yml playbooks/local-access.yml
# now browse https://grafana.<zone>, https://warp-ui.<zone>, etc. directly
```

**If you use warp-ui**, the browser talks directly to the chains over `localhost`
(mixed-content-exempt), so forward just the two chain ports:

```bash
ssh -L 8899:localhost:8899 -L 18899:localhost:18899 <host>
```

## 10. Update the warp routes (add a follow-on route)

Edit `WARP_ROUTES` in `deployment/local/spec-warp-deployer.yml` (e.g. `"usdc,sol"` —
the menu lives in `deployment/local/bridges/default/warp-routes/`), commit + push the
deploy branch, then:

```bash
ansible-playbook -i inventories/local/hosts.yml playbooks/update-warp-routes.yml \
  -e deploy_branch=<deploy-branch>
```

Already-deployed routes self-skip in the deployer Job; the playbook publishes the
regenerated bridge state and restarts the relayer (whitelist) and warp-ui (route list).
Removing a stem from `WARP_ROUTES` soft-disables that route the same way — it drops
out of the whitelist and the UI; its on-chain programs remain.

## 11. Reset between runs

```bash
ansible-playbook -i inventories/local/hosts.yml playbooks/stop-all.yml
# also destroy the shared kind cluster:
ansible-playbook -i inventories/local/hosts.yml playbooks/stop-all.yml -e destroy_cluster=true
# clean slate — also remove the deployment dirs and persisted host-path data under
# kind_mount_root (keeps the mkcert caddy-cert-backup; chains under ~/chains untouched):
ansible-playbook -i inventories/local/hosts.yml playbooks/stop-all.yml \
  -e destroy_cluster=true -e wipe_data=true
```

The chains are separate from the bridge stacks: stop them with
`laconic-so deployment --dir ~/chains/gorchain stop --delete-volumes` and by killing the
`solana-test-validator` (its ledger is under `~/chains/data/`).

## 12. Limitations / notes

- **No hairpin.** The validator→MinIO and Prometheus scrape legs run in-cluster, so
  single-host never loops traffic out to the host's public IP and back.
- **Cert pinned to the hostname list.** The mkcert leaf cert is generated once (guarded by
  `creates:`). If you change `base_domain` or the validator set, delete
  `~/.credentials/hyperlane/local-certs/bridge.crt` on the host and re-run `setup-all.yml`.
- **Re-running on a dirty clone.** The on-host token render edits the clone's specs in
  place (uncommitted), only on first `deploy create`. If you re-fetch a branch that also
  touched those specs, `fetch-stack --pull` can conflict — reset with `stop-all` and
  re-fetch clean.
