# Runbook — `local` own-chains environment

Bring the whole bridge up against **self-run chains** for end-to-end testing of the
deploy-side ansible (testing Layers 1-2). Neither prod (mainnet) nor staging
(devnet/Helius) inputs fit — `local` is its own committed inventory + spec tree.

- **Layer 1 (single-host):** every stack on one VM. This is the default
  `inventories/local/hosts.yml`.
- **Layer 2 (multi-host):** the same specs, stacks spread across `local-1..local-N`.
  Exercises the cross-host fetch/publish/delegate paths.

**Networking model.** `local` mirrors prod/staging: Caddy + Cloudflare DNS + real
Let's Encrypt, under an **operator-supplied public zone** (`dns_zone`). That makes
multi-host "just work" — `https://s3.<zone>`, `https://validator-x.<zone>`, etc.
resolve via public DNS to the right host's Caddy, and the Rust validator trusts the
LE-issued MinIO cert. The only own-chains-specific bit is that **the chains are
reached at their own domains** (set up out-of-band with the nodes), rendered into
the specs from `group_vars`.

> **No public DNS?** Fall back to a local ACME server (step-ca/Pebble) pointed at by
> Caddy in place of Let's Encrypt — see "Fallback" at the bottom. Build that only if
> you can't get a zone provisioned.

All commands run from `ops/` on the controller (your machine).

---

## 1. Prerequisites

**Controller** — same as `ops/README.md` → "Prerequisites":

```bash
pip install "ansible>=9" ansible-lint yamllint
ansible-galaxy collection install -r requirements.yml -p ./collections
```

Plus `git`, `ssh` with **agent forwarding**, `dig`, `kubectl`.

**Accounts / access:**
- A **public DNS zone on Cloudflare** (`dns_zone`, e.g. `staging.gorbagana.wtf`) and
  a **Cloudflare API token** scoped to it.
- A **Privy** project (validator + gas-oracle signing).
- A **GHCR** PAT (`packages:read`) for the private `gorbagana-dev/*` images.

**Per VM:** public IPv4 with inbound **80 + 443** open (Let's Encrypt HTTP-01) and
**22** from the controller. Target hosts need nothing else pre-installed —
`setup-all.yml` provisions Docker/kind/kubectl + laconic-so.

## 2. Stand up the chains (out-of-band, with domains)

`local` only *consumes* chain RPCs. On the host(s) in the `chain_hosts` group, bring
up **gorchain** (via `gorchain-stacks`) and a **solana-test-validator**, and give
each a **domain endpoint** (its own DNS + TLS, as part of the node setup). Fund the
deployer/validator/relayer keypairs on both chains, and create the collateral USDC
mint on the Solana side (its address → `WARP_TOKEN_MINT`).

## 3. Inventory + zone

Single-host is the default. Set:

```yaml
# inventories/local/host_vars/local-1.yml
public_ip: "<this host's public IPv4>"
```

```yaml
# inventories/local/group_vars/all.yml
dns_zone: "staging.gorbagana.wtf"               # your public zone
gorchain_rpc_url: "https://<gorchain-rpc-domain>"
solana_rpc_url: "https://<solana-rpc-domain>"
```

The chain RPCs and `__DNS_ZONE__` ship as tokens in the committed specs and are
rendered into the on-host clone before `deploy create` — you don't edit the specs.

For **multi-host**, follow the commented block at the bottom of `hosts.yml` (split
the groups across `local-1..local-N`, add `host_vars/local-2.yml`) and spread the
`dns_records` `host:` fields across the hosts that serve each stack.

Edit `deployment/local/bridges/default/operator/validators.yaml`: set each
validator's `privy_wallet_id` and `host`, and replace `REPLACE_WITH_LOCAL_DNS_ZONE`
in the hostnames so they match `dns_zone` (e.g. `validator-gorchain.staging.gorbagana.wtf`).

## 4. Secrets

```bash
cp inventories/local/secrets.example.yml inventories/local/secrets.yml
# fill: cloudflare_api_token, privy_app_id, privy_app_secret,
#       privy_oracle_wallet_id, ghcr_pat
```

No `helius_api_key` — the Solana side is your own chain. MinIO/Grafana credentials
are generated into `secrets.yml` by the `credentials` role on first run.

## 5. Keyfiles (on each host that runs the stack)

Place the operator signing keys under `~/.credentials/hyperlane/` on the relevant
host (single-host: all on `local-1`). The `credentials` role drops the per-validator
MinIO IAM files; these signing keys are operator-placed:

```
deployer-keypair.json      # Solana keypair JSON array (deployer + warp-deployer)
validator-gorchain.key     # hex validator signing key
validator-solana.key       # hex validator signing key
relayer-gorchain.key       # hex relayer signing key
relayer-solana.key         # hex relayer signing key
relayer-fee-claim.json     # Solana keypair JSON array (IGP fee claims)
```

Also fill the operator pubkeys/addresses in `group_vars/all.yml`
(`HARDWARE_WALLET_PUBKEY`, `IGP_ORACLE_PUBKEY`, `*_VALIDATOR_ADDRESS`),
`REPLACE_WITH_GITHUB_USERNAME` in the specs' `image-pull-secret`, and
`WARP_TOKEN_MINT` in `spec-warp-deployer.yml`.

## 6. Run it

```bash
# Phase 1 — provision + reconcile Cloudflare DNS + generate creds
ansible-playbook -i inventories/local/hosts.yml playbooks/setup-all.yml

# Phase 2 — deploy MinIO → deployer Job → publish state → consumers + validators
ansible-playbook -i inventories/local/hosts.yml playbooks/deploy-all.yml
```

Testing off a branch (the hosts fetch the repo themselves):

```bash
ansible-playbook -i inventories/local/hosts.yml playbooks/deploy-all.yml \
  -e deploy_branch=<your-branch>
```

`deploy-all.yml` runs `publish-bridge-state.yml` mid-flight on the deployer host: it
patches the deployer-derived values (IGP IDs/accounts, mailboxes, warp
addresses/mints) into the **local** specs and commits/pushes `deploy_branch`. Add
`-e state_review=true` to review the diff before it commits.

## 7. Access the stacks

Public DNS + LE, so just browse the hostnames (after DNS propagates):
`https://warp-ui.<zone>`, `https://grafana.<zone>`, `https://prometheus.<zone>`,
`https://minio-console.<zone>`.

## 8. Reset between runs

```bash
ansible-playbook -i inventories/local/hosts.yml playbooks/stop-all.yml
# also destroy the shared kind cluster:
ansible-playbook -i inventories/local/hosts.yml playbooks/stop-all.yml -e destroy_cluster=true
```

## 9. Known limitations / notes

- **Re-running on a dirty clone.** The on-host token render edits the clone's spec
  files in place (uncommitted). It only runs on first `deploy create` (skipped once a
  deployment exists), but if you re-fetch a branch that also touched those specs,
  `fetch-stack --pull` can conflict. Reset with `stop-all` and re-fetch clean.

## Fallback — no public DNS (local ACME)

If you can't get a public zone, the only thing that changes is the **cert source**:
stand up a local ACME server (step-ca or Pebble), point the caddy-ingress
controller's `acmeCA` at it instead of Let's Encrypt, and distribute its root to the
controller (browser trust) and to Prometheus (`ca_file`). The MinIO/S3 path is the
catch — `aws-sdk-rust` won't trust a non-public CA, so it would have to drop to a
plain-HTTP Caddy site for `s3.<zone>`. This needs a small caddy-ingress enhancement
(non-LE issuer + a per-host HTTP-only site) and is **not built** — use it only if the
public-DNS path is unavailable.
