# ops/ — Deploy-side ansible

Ansible layer that brings a Hyperlane SVM bridge up across machines with **zero
on-chain signing**. Two phases: provision the fleet (`setup-all.yml`), then
deploy the stacks (`deploy-all.yml`). Operator-attended signing/lifecycle
playbooks (kill-switch, restore, ISM update, teardown) are out of scope here.

> **All commands in this README run from the `ops/` directory.** From the repo
> root: `cd ops`. (Paths like `inventories/…`, `playbooks/…`, `requirements.yml`
> are relative to it; `check-spec-parity.py` is the one exception, noted inline.)

## What's in `ops/`

```
inventories/<env>/   hosts, group_vars, host_vars, deployment-config (one per env: local, staging, prod)
playbooks/           setup-all.yml, deploy-all.yml + per-step plays; staging/ holds staging-only plays
roles/               the building blocks the playbooks call (fetch_stack, stack_deploy, credentials, dns_cloudflare, …)
runbooks/            from-zero operator guides, one per environment — start here to bring a bridge up
scripts/             host-side helpers (chain setup, key generation, funding)
tests/               Layer-0 localhost assertion tests (no VM needed)
```

**New here?** Follow a [runbook](runbooks/) — those are the entry point. This
README is the mechanics reference behind them: read it to understand how a step
works or when changing the ops layer.

## Prerequisites (controller / operator machine)

```bash
cd ops   # all commands below are relative to this directory
```

- Ansible 2.16+ and the linters: `pip install "ansible>=9" ansible-lint yamllint`
- Collections: `ansible-galaxy collection install -r requirements.yml -p ./collections`
  (installs `community.general`, `kubernetes.core`, `ansible.posix`,
  `community.docker` — re-run after pulling, the list grows)
- `git`, `ssh` with **agent forwarding** to the target hosts, `dig`, `kubectl`
- SSH access to every host in the target inventory

Target hosts need nothing pre-installed — `bootstrap-host.yml` installs Docker,
kind, kubectl (privileged) and laconic-so (unprivileged).

## Environments

The environment is selected entirely by the inventory you pass:
`-i inventories/<env>/hosts.yml`. Each inventory's `group_vars/all.yml` sets the
matching `deployment_root`, so picking the inventory picks both trees:

- the inventory itself (hosts, `group_vars`, `host_vars`, secrets)
- the spec/state root `deployment/` (prod), `deployment/staging/` (staging), or
  `deployment/local/` (local), resolved as `deployment_root` in that inventory's
  `group_vars/all.yml`

There is no `-e env=` switch. Per-env isolation: the environments share no mutable
inventory or vars.

- **prod** / **staging** — mainnet / devnet, Cloudflare DNS + Let's Encrypt TLS.
  Staging additionally runs its own gorchain (a persistent single-node chain,
  brought up by `playbooks/staging/prepare-gorchain.yml` and served at
  `rpc.<zone>` behind Caddy) and signs with generated throwaway key files
  instead of prod's operator-provisioned ones. Operator guide:
  [runbooks/staging.md](runbooks/staging.md).
- **local** — own-chains testing: self-run gorchain + a local
  solana-test-validator. Single-host uses self-trusted **mkcert** certs (no DNS
  provider). Local-specific bits: no Helius (`SOLANA_RPC_URL` is the own chain),
  and the operator-supplied `base_domain` + own-chain RPC URLs ship as
  `__TOKENS__` in the specs, rendered on the host (`spec_token_renders`). Operator
  guide: [local-single-host.md](runbooks/local-single-host.md).

**From-scratch operator guides per environment live in [`runbooks/`](runbooks/)**
(start there to bring an environment up; this README is the mechanics reference
behind them).

## Operator configuration — deployment-config.yml

Each env keeps a **gitignored** `inventories/<env>/deployment-config.yml`, created
from the committed `deployment-config.example.yml`. It is the **one file an
operator fills** — secrets and bridge identity; the plays read it at runtime, so
no committed file ever needs an operator edit (host connectivity excepted:
`host_vars/<host>.yml`).

```bash
cp inventories/prod/deployment-config.example.yml inventories/prod/deployment-config.yml
# then fill it in (each key is commented)
```

- **Secrets:** `cloudflare_api_token`, `privy_app_id`, `privy_app_secret`,
  `privy_oracle_wallet_id`, `helius_api_key` (builds the secret
  `SOLANA_RPC_URL`), `ghcr_pat` (private GHCR pulls).
- **Bridge identity (not sensitive):** `bridge_owner_pubkey`,
  `igp_oracle_pubkey`, `gorchain_validator_address`, `solana_validator_address`
  (referenced by `group_vars` secret-env values), `privy_validator_wallet_ids`
  (merged into the validator set by label), and `wallet_connect_id` (rendered
  into the warp-ui spec's sentinel at deploy time; `""` disables WalletConnect).
- **Generated automatically** by the `credentials` role on first run and written
  back into `deployment-config.yml` (never rotated on re-run): `minio_root_user`,
  `minio_root_password`, `grafana_admin_password`, and per-validator `minio_iam`
  (key_id/secret pairs).

`distribute-credentials.yml` (part of `setup-all.yml`) asserts the required keys
are present and fails fast naming any that are missing.

## Configuration model

laconic-so writes a spec's `config:` block **verbatim** — it does not expand
`${VAR}` — so three kinds of value reach a pod by three different routes:

- **`config:` values** (chain RPC URLs, domain/chain IDs, `*_IS_TESTNET`) are
  committed literals in the per-env `deployment/[staging/]spec-*.yml`. Editing
  them is a spec edit, not an ansible var change.
- **Secrets** are injected as env vars: each spec lists them under
  `secrets: { env: NAME }`, and `stack_deploy` resolves each `NAME` from the
  `stack_env_vars` map (see below). `SOLANA_RPC_URL` is a **secret** — the Helius
  URL embeds an API key — built in `group_vars` from `helius_api_key`.
- **Deployment-derived values** (IGP program IDs/accounts, mailboxes) are not
  known until the deployer Job runs; `publish-bridge-state.yml` patches them into
  the committed specs after the fact (see below).

### Domain / chain IDs

SVM chains have no EIP-155 `chainId`; Hyperlane derives a `u32` **domain** from
the chain name (`chainId == domainId`) — `1399811149`/`1198486093` for solana/
gorchain on prod, `…1151`/`…6095` on staging (devnet). They're **immutable once
deployed** and live as committed `config:` literals in the per-env specs. Full
derivation (the name+network-byte math) is in
[`docs/stack-specifications.md`](../docs/stack-specifications.md) → Stack 1 →
Domain / chain IDs.

## Inventory + topology

`inventories/<env>/hosts.yml` declares one group per singleton stack
(`deployer_hosts`, `minio_hosts`, `relayer_hosts`, `gas_oracle_hosts`,
`monitoring_hosts`, `warp_ui_hosts`) plus `controller`. In a single-host run
every group points at the same host. Validators are **not** in the inventory —
they come from `deployment/[staging/]bridges/default/operator/validators.yaml`
(pure topology: label, chain, host, hostname — each validator's Privy wallet id
comes from deployment-config's `privy_validator_wallet_ids`). Moving a singleton to another
host is an inventory edit only; no spec or playbook change.

Per-host facts live in `host_vars/<alias>.yml`: `public_ip`, `privileged_user`,
`deploy_user`, `kind_mount_root`.

## Running it

```bash
# Phase 1 — provision every host (Docker/kind/kubectl, laconic-so, DNS, creds)
ansible-playbook -i inventories/prod/hosts.yml playbooks/setup-all.yml

# Phase 2 — deploy the stacks (MinIO → deployer Job → publish state → consumers)
ansible-playbook -i inventories/prod/hosts.yml playbooks/deploy-all.yml
```

Individual steps are runnable on their own, e.g.:

```bash
ansible-playbook -i inventories/prod/hosts.yml playbooks/bootstrap-host.yml -e target=bridge-host-1
ansible-playbook -i inventories/prod/hosts.yml playbooks/configure-dns.yml
ansible-playbook -i inventories/prod/hosts.yml playbooks/distribute-credentials.yml
```

`deploy-all.yml` runs hands-off end to end. The one attended option is the state
publish:

- **`publish-bridge-state.yml`** is imported by `deploy-all.yml` mid-flight
  (after the deployer Jobs, before the consumers); standalone it exists for
  re-publishing outside a full deploy. It runs on the deployer host: it copies the
  deployer-produced `generated/` state into the on-host clone, **patches the
  deployment-derived `config:` keys** (IGP program IDs/accounts into
  `spec-relayer.yml`/`spec-gas-oracle.yml`, mailboxes + warp addresses/mints into
  `spec-warp-ui.yml`), then auto-commits + pushes `deploy_branch` by default. Pass
  **`-e state_review=true`** to print the diff and pause for approval before
  commit/push. Its git-add is scoped to the `bridges/<bridge>/generated/` paths
  **plus** the three patched specs, and it skips entirely if nothing changed.

Reset a host between test runs:

```bash
ansible-playbook -i inventories/prod/hosts.yml playbooks/stop-all.yml
# also destroy the shared kind cluster:
ansible-playbook -i inventories/prod/hosts.yml playbooks/stop-all.yml -e destroy_cluster=true
```

## How a stack gets deployed

Every deploy host fetches the stack repo itself, so `laconic-so` reads the specs
and stack definitions locally — one host or several work the same way, with
no repo paths leaking from the operator's machine. The `fetch_stack` role runs
first on each host:

```
CERC_REPO_BASE_DIR=~/deployments laconic-so fetch-stack \
  github.com/gorbagana-dev/hyperlane-stacks@<deploy_branch> --git-ssh --pull
```

→ clones/updates `~/deployments/hyperlane-stacks` on the host (over the forwarded
SSH agent — no creds stored on hosts) and checks out `deploy_branch` (default
`main`; `-e deploy_branch=<branch>` to test off main). `repo_root`/`deployment_root`
then resolve against that on-host clone.

`stack_deploy` then runs `laconic-so --stack {{ repo_root }}/… deploy create
--spec-file {{ deployment_root }}/spec-*.yml` directly against the committed spec
(no `deploy init` — SO reads the spec file as-is) → patches a readable
`deployment-id` → `deployment start --perform-cluster-management`. Cluster sharing
is automatic — every spec sets `kind-cluster-name: hyperlane`, so all stacks on a
host share the `kind-hyperlane` cluster. Single-stack stops use
`--skip-cluster-management` so they never tear it down. It's idempotent: re-running
skips `create` if the deployment already exists.

Each stack's secret `laconic-so` environment is assembled from the
`stack_env_vars` map in `group_vars/all.yml` — a list of the **secret** env-var
names each spec injects via `secrets: { env: NAME }`, resolved to the same-named
ansible vars. `config:` vars are committed spec literals and are **not** in this
map. **Keep the map in sync** with each spec's `secrets:` block.

Deployments live under `~/deployments/<stack-name>` on each host (the `deploy_base`
role default); the fetched stack repo is `~/deployments/hyperlane-stacks` — one
clone per host, shared by `fetch_stack`, `state_distribute`, and `stack_deploy`.

State flows deployer-host → git → consumer-hosts: `publish-bridge-state.yml`
(see *Running it* above) commits the patched specs + `generated/` on
`deploy_branch` from the deployer host; each consumer's `fetch_stack --pull`
brings them down, and `state_distribute` copies the `agent-config` ConfigMap from
the clone into each stack's `configmaps/`.

## Linting

```bash
yamllint .
ansible-lint .
for p in playbooks/*.yml tests/test_*.yml; do ansible-playbook --syntax-check "$p"; done
```

The localhost assertion tests under `tests/` need no VM and lock the logic-bearing
contracts (validators derivation, DNS expansion, credential idempotency, secret-env
assembly, deploy/state paths, publish scoping):

```bash
for t in tests/test_*.yml; do ansible-playbook -i inventories/prod/hosts.yml "$t"; done
```

The env contract test (`tests/test_env_contract.yml`) runs per inventory — point
`-i` at each of `inventories/{prod,staging,local}/hosts.yml`. The spec shape-parity
checker keeps the per-env spec trees structurally aligned (run from the repo root):

```bash
python3 ops/scripts/check-spec-parity.py
```

CI runs the Layer-0 suite (lint + syntax-check + localhost tests) against all three
inventories on every PR (`.github/workflows/ops-lint.yml`).
