# ops/ — Deploy-side ansible

Ansible layer that brings a Hyperlane SVM bridge up across machines with **zero
on-chain signing**. Two phases: provision the fleet (`setup-all.yml`), then
deploy the stacks (`deploy-all.yml`). Operator-attended signing/lifecycle
playbooks (kill-switch, restore, ISM update, teardown) are **sub-project 3** and
not here.

Design: `docs/superpowers/specs/2026-06-01-deploy-side-ansible-design.md`.

## Prerequisites (controller / operator machine)

- Ansible 2.16+ and the linters: `pip install "ansible>=9" ansible-lint yamllint`
- Collections: `ansible-galaxy collection install -r requirements.yml -p ./collections`
  (installs `community.general`, `kubernetes.core`, `ansible.posix`)
- `git`, `ssh` with **agent forwarding** to the target hosts, `dig`, `kubectl`
- SSH access to every host in the target inventory

Target hosts need nothing pre-installed — `bootstrap-host.yml` installs Docker,
kind, kubectl (privileged) and laconic-so (unprivileged).

## Environments

The environment is selected entirely by the inventory you pass:
`-i inventories/<env>/hosts.yml`. Each inventory's `group_vars/all.yml` sets the
matching `deployment_root`, so picking the inventory picks both trees:

- the inventory itself (hosts, `group_vars`, `host_vars`, secrets)
- the spec/state root `deployment/` (prod) or `deployment/staging/` (staging),
  resolved as `deployment_root` in that inventory's `group_vars/all.yml`

There is no `-e env=` switch. Per-env isolation: staging and prod share no
mutable inventory or vars.

## Secrets

Each env keeps a **gitignored** `inventories/<env>/secrets.yml`, created from the
committed `secrets.example.yml`:

```bash
cp inventories/prod/secrets.example.yml inventories/prod/secrets.yml
# then fill in the REQUIRED operator-supplied secrets
```

- **Operator-supplied (required):** `cloudflare_api_token`, `privy_app_id`,
  `privy_app_secret`, `privy_oracle_wallet_id`, `helius_api_key` (builds the
  secret `SOLANA_RPC_URL`), `ghcr_pat` (private GHCR pulls).
- **Generated automatically** by the `credentials` role on first run and written
  back into `secrets.yml` (never rotated on re-run): `minio_root_user`,
  `minio_root_password`, `grafana_admin_password`, and per-validator `minio_iam`
  (key_id/secret pairs).

`distribute-credentials.yml` asserts the required keys are present and fails fast
naming any that are missing.

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

Both chains are **SVM** (Solana / agave fork) — there is no EIP-155 `chainId` to
look up; an SVM chain identifies by its genesis hash. Hyperlane instead assigns a
`u32` **domain** derived from the chain name and sets `chainId == domainId`. The
derivation: take the first ASCII characters of the name as big-endian bytes, then a
trailing **network byte** (`0x4D`/`0x4E`/`0x4F` for mainnet/testnet/devnet):

```
"Sol" = 0x53 0x6F 0x6C
  solana mainnet  0x536F6C4D = 1399811149   (canonical Hyperlane value)
  solana testnet  0x536F6C4E = 1399811150
  solana devnet   0x536F6C4F = 1399811151

"Gor" = 0x47 0x6F 0x72
  gorchain mainnet 0x476F724D = 1198486093   (prod)
  gorchain devnet  0x476F724F = 1198486095   (staging)
```

Solana uses its canonical registered values. gorchain has no canonical Hyperlane
domain (we deploy our own core on it), so we mint one the same way. These are
**immutable once deployed** (baked into the on-chain contracts) — used as
committed `config:` literals in the per-env specs: prod `deployment/spec-*.yml`
(gorchain `1198486093`, solana `1399811149`), staging `deployment/staging/spec-*.yml`
(gorchain `1198486095`, solana `1399811151`). To verify a value:

```python
python3 -c "b=b'Gor'+bytes([0x4D]); print(int.from_bytes(b,'big'))"  # 1198486093
```

## Inventory + topology

`inventories/<env>/hosts.yml` declares one group per singleton stack
(`deployer_hosts`, `minio_hosts`, `relayer_hosts`, `gas_oracle_hosts`,
`monitoring_hosts`, `warp_ui_hosts`) plus `controller`. In a single-host run
every group points at the same host. Validators are **not** in the inventory —
they come from `deployment/[staging/]bridges/default/operator/validators.yaml`
(label, chain, host, privy_wallet_id, hostname). Moving a singleton to another
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

- **`publish-bridge-state.yml`** runs on the deployer host: it copies the
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
and stack definitions locally — single-host and multi-host work the same way, with
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

State flows deployer-host → git → consumer-hosts. `publish-bridge-state.yml` runs
**on the deployer host** (the artifacts are already there): it copies the deployer's
`generated/` from the host-path volume into the on-host clone, patches the
deployment-derived `config:` keys (IGP IDs/accounts into
`spec-relayer.yml`/`spec-gas-oracle.yml`, mailboxes + warp addresses/mints into
`spec-warp-ui.yml`), then commits (with the operator's git identity, read from the
controller) and pushes `deploy_branch` over the forwarded agent. Each consumer's
`fetch_stack --pull` then brings down the published specs + `generated/`, and
`state_distribute` copies the `agent-config` ConfigMap from the clone into each
stack's `configmaps/`.

## Linting (Layer 0)

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

CI runs the Layer-0 lint + syntax-check on every PR (`.github/workflows/ops-lint.yml`).

## Known follow-ups (verify/extend on a real VM run)

- **Multi-host validators:** the validator loop delegates per-host via
  `include_role` + `apply: delegate_to`; confirm fact/`ansible_env` behavior on a
  real multi-VM split (correct for single-host v1).
