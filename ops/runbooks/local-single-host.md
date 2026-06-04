# Runbook — `local` single-host (Layer 1)

Bring the whole bridge up against **self-run chains on one VM** to test the deploy-side
ansible end to end. Every stack, both hyperlane validators, and both SVM chains run on a
single box; there is no public DNS and no Let's Encrypt.

All ansible commands run from `ops/` on the controller (your machine).

## Networking model

- Self-trusted **mkcert** certs — the `local_tls` role generates a multi-SAN cert and
  pre-seeds it into Caddy (no ACME, **no DNS provider**). `dns_zone` is just a label the
  cert covers (e.g. `hyperlane.local`), not a real Cloudflare zone.
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
- A **GHCR** PAT (`packages:read`) + the owning GitHub username, for the private
  `gorbagana-dev/*` images (both the bridge stacks and the gorchain chain image).
- **No Cloudflare**, **no public DNS zone**, **no public 80/443** — single-host serves
  Caddy on the host loopback.

**VM:** inbound **22** from the controller only. `setup-all.yml` provisions
Docker/kind/kubectl + laconic-so. The chains additionally need the **Solana CLI** and
**`spl-token`** on the VM (the bridge ansible does not install the chain toolchain) — the
`prepare-chains.yml` scripts check for them and fail clearly if missing.

## 2. Privy wallets

Mint the three server wallets once per [privy-wallets.md](privy-wallets.md), then set the
IDs/addresses it lists — these must be in place **before** the prepare step (the oracle
pubkey is funded there):

- `privy_wallet_id` per validator in
  `deployment/local/bridges/default/operator/validators.yaml`
- `privy_oracle_wallet_id` in `inventories/local/secrets.yml`
- `GORCHAIN_VALIDATOR_ADDRESS`, `SOLANA_VALIDATOR_ADDRESS`, `IGP_ORACLE_PUBKEY` in
  `inventories/local/group_vars/all.yml`

## 3. Inventory & zone

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
ansible -i inventories/local/hosts.yml local-1 -m ping   # expect: SUCCESS / "pong"
```

```yaml
# inventories/local/group_vars/all.yml
dns_zone: "hyperlane.local"        # any label mkcert signs; not a real zone
```

In `validators.yaml`, replace `REPLACE_WITH_LOCAL_DNS_ZONE` in both hostnames so they
match `dns_zone` (e.g. `validator-gorchain.hyperlane.local`). The `host:` is already
`local-1`. Also set `REPLACE_WITH_GITHUB_USERNAME` in the specs' `image-pull-secret`.

## 4. Secrets

```bash
cp inventories/local/secrets.example.yml inventories/local/secrets.yml
# fill: privy_app_id, privy_app_secret, privy_oracle_wallet_id, ghcr_user, ghcr_pat
```

**No `cloudflare_api_token`** (single-host uses mkcert). No `helius_api_key` — the Solana
side is your own chain. MinIO/Grafana credentials are generated into `secrets.yml` by the
`credentials` role on first run.

## 5. Provision the host

```bash
export PATH=/home/dev/.ops-ansible-venv/bin:$PATH LC_ALL=C.UTF-8 LANG=C.UTF-8

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

It prints, in the final summary, the values you still need to set:
`HARDWARE_WALLET_PUBKEY` and `WARP_TOKEN_MINT`.

Notes:
- gorchain's dev-RPC config lives in the deploy spec's `config:` (no hand-written
  `config.env`); the chain images are pulled with the GHCR creds from `secrets.yml`.
- The in-cluster bridge reaches the chains via `gorchain-rpc:8899` / `solana-rpc:18899`
  **automatically** (external-services → kind gateway) — leave `gorchain_rpc_url`/
  `solana_rpc_url` at their placeholders in `group_vars/all.yml`.
- It generated these keyfiles under `~/.credentials/hyperlane/` on local-1:
  `deployer-keypair.json` (deployer + warp-deployer), `hardware-wallet.json`
  (`HARDWARE_WALLET_PUBKEY`, you hold it), `validator-{gorchain,solana}.key` (announce
  keys), `relayer-{gorchain,solana}.key` (relayer signers), `relayer-fee-claim.json`
  (IGP fee claims).
- Re-running is safe: existing keys are never overwritten, and a healthy solana validator
  is left as-is. To run it by hand instead of via ansible, the same scripts live at
  `ops/scripts/{setup-chains,gen-local-keys,deploy-spl-token}.sh`.

## 7. Fill the remaining vars

From the step-6 summary, set in `inventories/local/group_vars/all.yml`:
`HARDWARE_WALLET_PUBKEY`; and `WARP_TOKEN_MINT` in `deployment/local/spec-warp-deployer.yml`.
(`IGP_ORACLE_PUBKEY` and the validator addresses were set in step 2.)

## 8. Deploy

```bash
# MinIO -> deployer Job -> publish state -> consumers + validators
ansible-playbook -i inventories/local/hosts.yml playbooks/deploy-all.yml
```

Testing off a branch (the hosts fetch the repo themselves) — add `-e deploy_branch=<branch>`.

`deploy-all.yml` runs `publish-bridge-state.yml` mid-flight: it patches the
deployer-derived values (IGP IDs/accounts, mailboxes, warp addresses/mints) into the
**local** specs and commits/pushes `deploy_branch`. Add `-e state_review=true` to review
the diff before it commits.

## 9. Access the stacks

No public DNS — tunnel and trust the mkcert CA from your workstation.

```bash
# 1. fetch the published mkcert root CA from the host
scp <host>:~/.credentials/hyperlane/local-rootCA.pem ./local-rootCA.pem
# 2. trust it on your workstation (Linux system store shown; macOS: add to Keychain;
#    Firefox uses its own NSS store — import via Preferences > Certificates)
sudo cp local-rootCA.pem /usr/local/share/ca-certificates/hyperlane-local.crt && sudo update-ca-certificates
# 3. point the hostnames at the tunnel and open it
echo "127.0.0.1 warp-ui.<zone> grafana.<zone> prometheus.<zone> minio-console.<zone>" | sudo tee -a /etc/hosts
ssh -L 443:localhost:443 <host>
# now browse https://grafana.<zone> etc. through the tunnel
```

**If you use warp-ui**, the browser also talks directly to the chains, so add the chain
ports to the tunnel:

```bash
ssh -L 443:localhost:443 -L 8899:localhost:8899 -L 18899:localhost:18899 <host>
```

## 10. Reset between runs

```bash
ansible-playbook -i inventories/local/hosts.yml playbooks/stop-all.yml
# also destroy the shared kind cluster:
ansible-playbook -i inventories/local/hosts.yml playbooks/stop-all.yml -e destroy_cluster=true
```

The chains are separate from the bridge stacks: stop them with
`laconic-so deployment --dir ~/chains/gorchain stop --delete-volumes` and by killing the
`solana-test-validator` (its ledger is under `~/chains/data/`).

## 11. Limitations / notes

- **No hairpin.** The validator→MinIO and Prometheus scrape legs run in-cluster, so
  single-host never loops traffic out to the host's public IP and back.
- **Cert pinned to the hostname list.** The mkcert leaf cert is generated once (guarded by
  `creates:`). If you change `dns_zone` or the validator set, delete
  `~/.credentials/hyperlane/local-certs/bridge.crt` on the host and re-run `setup-all.yml`.
- **Re-running on a dirty clone.** The on-host token render edits the clone's specs in
  place (uncommitted), only on first `deploy create`. If you re-fetch a branch that also
  touched those specs, `fetch-stack --pull` can conflict — reset with `stop-all` and
  re-fetch clean.
