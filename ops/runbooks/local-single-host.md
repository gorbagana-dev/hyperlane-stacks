# Runbook — `local` single-host (Layer 1)

Bring the whole bridge up against **self-run chains on one VM** to test the deploy-side
ansible end to end. Every stack, both hyperlane validators, and both SVM chains run on a
single box; there is no public DNS and no Let's Encrypt.

All commands run from `ops/` on the controller (your machine).

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
- A **Privy** project (validator + gas-oracle signing) — see `privy-wallets.md`.
- A **GHCR** PAT (`packages:read`) for the private `gorbagana-dev/*` images.
- **No Cloudflare**, **no public DNS zone**, **no public 80/443** — single-host serves
  Caddy on the host loopback.

**VM:** inbound **22** from the controller only. Nothing else pre-installed —
`setup-all.yml` provisions Docker/kind/kubectl + laconic-so.

## 2. Privy wallets

Mint the three server wallets once per `privy-wallets.md`, then fill the IDs/addresses it
lists:

- `privy_wallet_id` per validator in
  `deployment/local/bridges/default/operator/validators.yaml`
- `privy_oracle_wallet_id` in `inventories/local/secrets.yml`
- `GORCHAIN_VALIDATOR_ADDRESS`, `SOLANA_VALIDATOR_ADDRESS`, `IGP_ORACLE_PUBKEY` in
  `inventories/local/group_vars/all.yml`

## 3. Chains on the VM

Stand up both chains on the box, bound to `0.0.0.0` so the kind gateway can reach them.

```bash
# gorchain single-node RPC (via gorchain-stacks), binds 0.0.0.0:8899
laconic-so fetch-stack --git-ssh --pull github.com/gorbagana-dev/gorchain-stacks@main
laconic-so --stack ~/cerc/gorchain-stacks/stack-orchestrator/stacks/gorchain \
  deploy init --output gorchain-spec.yml
laconic-so --stack ~/cerc/gorchain-stacks/stack-orchestrator/stacks/gorchain \
  deploy create --spec-file gorchain-spec.yml --deployment-dir ./gorchain
printf 'PUBLIC_GOSSIP_HOST=127.0.0.1\nPUBLIC_RPC_ADDRESS=127.0.0.1:8899\nGORCHAIN_DEV_RPC=true\n' \
  > ./gorchain/config.env
laconic-so deployment --dir ./gorchain start
curl -s http://localhost:8899/health        # {"ok":...} when up

# solana-test-validator, binds 0.0.0.0:18899
solana-test-validator --ledger ~/.data/test-ledger-solana \
  --rpc-port 18899 --faucet-port 19900 --gossip-port 18001 \
  --dynamic-port-range 19050-19075 --quiet &
curl -sf http://localhost:18899/health

# fund deployer + the Privy oracle wallet on BOTH chains. The helper airdrops in
# chunks of 10 (gorchain's faucet caps each request at 10 SOL) and verifies the
# resulting balance. Run from the repo root on the chains host.
ops/scripts/fund-test-wallets.sh \
  <deployer-pubkey>      100 \
  <oracle-base58-pubkey> 1

# create the collateral USDC SPL mint on Solana (-> WARP_TOKEN_MINT)
spl-token --url http://localhost:18899 create-token --decimals 6
spl-token --url http://localhost:18899 create-account <mint>
spl-token --url http://localhost:18899 mint <mint> 1000000
```

The in-cluster bridge reaches these via `gorchain-rpc:8899` / `solana-rpc:18899`
**automatically** — you do **NOT** set `gorchain_rpc_url`/`solana_rpc_url`; leave them at
the placeholder in `group_vars/all.yml`. The chains must bind `0.0.0.0` (the commands
above do) so the kind gateway can reach them.

## 4. Inventory & zone

Single-host is the default (`inventories/local/hosts.yml` — every group, including
`chain_hosts`, points at `local-1`). Set:

```yaml
# inventories/local/host_vars/local-1.yml
public_ip: "<this host's public IPv4>"
```

```yaml
# inventories/local/group_vars/all.yml
dns_zone: "hyperlane.local"        # any label mkcert signs; not a real zone
```

In `validators.yaml`, replace `REPLACE_WITH_LOCAL_DNS_ZONE` in both hostnames so they
match `dns_zone` (e.g. `validator-gorchain.hyperlane.local`). The `host:` is already
`local-1`.

## 5. Secrets

```bash
cp inventories/local/secrets.example.yml inventories/local/secrets.yml
# fill: privy_app_id, privy_app_secret, privy_oracle_wallet_id, ghcr_pat
```

**No `cloudflare_api_token`** (single-host uses mkcert). No `helius_api_key` — the Solana
side is your own chain. MinIO/Grafana credentials are generated into `secrets.yml` by the
`credentials` role on first run.

## 6. Keyfiles & group_vars

These are throwaway test keys — generate them with the helper (needs the Solana CLI;
prints the pubkeys to paste + the addresses to fund, and never overwrites existing
files):

```bash
ops/scripts/gen-local-keys.sh        # writes into ~/.credentials/hyperlane/ on local-1
```

It drops the keyfiles the stack consumes:

```
deployer-keypair.json      # Solana keypair JSON array (deployer + warp-deployer)
hardware-wallet.json       # keypair whose pubkey -> HARDWARE_WALLET_PUBKEY (you hold it)
validator-gorchain.key     # hex validator announce key (HYP_DEFAULTSIGNER_KEY)
validator-solana.key       # hex validator announce key
relayer-gorchain.key       # hex relayer signing key (HYP_CHAINS_GORCHAIN_SIGNER_KEY)
relayer-solana.key         # hex relayer signing key
relayer-fee-claim.json     # Solana keypair JSON array (IGP fee claims)
```

Fund the printed addresses on both chains (step 3). Then fill in `group_vars/all.yml`:
paste the helper's `HARDWARE_WALLET_PUBKEY`; set `IGP_ORACLE_PUBKEY`,
`GORCHAIN_VALIDATOR_ADDRESS`, `SOLANA_VALIDATOR_ADDRESS`; `REPLACE_WITH_GITHUB_USERNAME` in
the specs' `image-pull-secret`; and `WARP_TOKEN_MINT` (the `<mint>` from step 3) in
`spec-warp-deployer.yml`.

## 7. Run it

```bash
export PATH=/home/dev/.ops-ansible-venv/bin:$PATH LC_ALL=C.UTF-8 LANG=C.UTF-8

# Phase 1 — provision + mkcert TLS + generate creds
ansible-playbook -i inventories/local/hosts.yml playbooks/setup-all.yml

# Phase 2 — deploy MinIO -> deployer Job -> publish state -> consumers + validators
ansible-playbook -i inventories/local/hosts.yml playbooks/deploy-all.yml
```

Testing off a branch (the hosts fetch the repo themselves) — add `-e deploy_branch=<branch>`.

`deploy-all.yml` runs `publish-bridge-state.yml` mid-flight: it patches the
deployer-derived values (IGP IDs/accounts, mailboxes, warp addresses/mints) into the
**local** specs and commits/pushes `deploy_branch`. Add `-e state_review=true` to review
the diff before it commits.

## 8. Access the stacks

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

## 9. Reset between runs

```bash
ansible-playbook -i inventories/local/hosts.yml playbooks/stop-all.yml
# also destroy the shared kind cluster:
ansible-playbook -i inventories/local/hosts.yml playbooks/stop-all.yml -e destroy_cluster=true
```

## 10. Limitations / notes

- **No hairpin.** The validator→MinIO and Prometheus scrape legs run in-cluster, so
  single-host never loops traffic out to the host's public IP and back.
- **Cert pinned to the hostname list.** The mkcert leaf cert is generated once (guarded by
  `creates:`). If you change `dns_zone` or the validator set, delete
  `~/.credentials/hyperlane/local-certs/bridge.crt` on the host and re-run `setup-all.yml`.
- **Re-running on a dirty clone.** The on-host token render edits the clone's specs in
  place (uncommitted), only on first `deploy create`. If you re-fetch a branch that also
  touched those specs, `fetch-stack --pull` can conflict — reset with `stop-all` and
  re-fetch clean.
