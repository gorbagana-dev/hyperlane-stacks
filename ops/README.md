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
  `privy_app_secret`.
- **Generated automatically** by the `credentials` role on first run and written
  back into `secrets.yml` (never rotated on re-run): `minio_root_user`,
  `minio_root_password`, and per-validator `minio_iam` (key_id/secret pairs).

`distribute-credentials.yml` asserts the required keys are present and fails fast
naming any that are missing.

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

# Phase 2 — deploy the stacks (MinIO → deployer Job → commit state → consumers)
ansible-playbook -i inventories/prod/hosts.yml playbooks/deploy-all.yml
```

Individual steps are runnable on their own, e.g.:

```bash
ansible-playbook -i inventories/prod/hosts.yml playbooks/bootstrap-host.yml -e target=bridge-host-1
ansible-playbook -i inventories/prod/hosts.yml playbooks/configure-dns.yml
ansible-playbook -i inventories/prod/hosts.yml playbooks/distribute-credentials.yml
```

`deploy-all.yml` runs hands-off end to end. The one attended option is the state
commit:

- **`commit-bridge-state.yml`** auto-commits + pushes the deployer-produced
  `generated/` state by default. Pass **`-e state_review=true`** to print the diff
  and pause for approval before commit/push. It stages only the
  `bridges/<bridge>/generated/` paths and skips entirely if nothing changed.

Reset a host between test runs:

```bash
ansible-playbook -i inventories/prod/hosts.yml playbooks/stop-all.yml
# also destroy the shared kind cluster:
ansible-playbook -i inventories/prod/hosts.yml playbooks/stop-all.yml -e destroy_cluster=true
```

## How a stack gets deployed

`stack_deploy` mirrors the proven e2e sequence: `laconic-so deploy init` →
overwrite the generated spec with the committed `deployment/spec-*.yml` →
`deploy create` → patch a readable `deployment-id` → `deployment start
--perform-cluster-management`. Cluster sharing is automatic — every spec sets
`kind-cluster-name: hyperlane`, so all stacks on a host share the `kind-hyperlane`
cluster. Single-stack stops use `--skip-cluster-management` so they never tear it
down. It's idempotent: re-running skips `init`/`create` if the deployment already
exists.

Each stack's `laconic-so` environment is assembled from the `stack_env_vars` map
in `group_vars/all.yml` (a list of env-var names per stack, resolved to the
same-named ansible vars). **Keep that map in sync** when a spec's `config:`/
`secrets:` env vars change.

State flows deployer-host → git → consumer-hosts: `commit-bridge-state.yml`
commits the deployer's `generated/` files, and `state_distribute` git-pulls them
on each consumer host (over the forwarded SSH agent — no creds stored on hosts)
and copies them into each stack's `configmaps/`.

## Linting (Layer 0)

```bash
yamllint .
ansible-lint .
for p in playbooks/*.yml tests/test_*.yml; do ansible-playbook --syntax-check "$p"; done
```

The localhost assertion tests under `tests/` need no VM and lock the logic-bearing
contracts (validators derivation, DNS expansion, credential idempotency, env
assembly, state paths, commit scoping):

```bash
for t in tests/test_*.yml; do ansible-playbook -i inventories/prod/hosts.yml "$t"; done
```

CI runs the Layer-0 lint + syntax-check on every PR (`.github/workflows/ops-lint.yml`).

## Known follow-ups (verify/extend on a real VM run)

- **Optional warp-deployer** is not wired into `deploy-all.yml`. For a warp route,
  deploy `spec-warp-deployer.yml` via `stack_deploy` and re-run
  `commit-bridge-state.yml` between the deployer and the consumers.
- **Deployer state path:** `commit-bridge-state.yml` pulls from
  `{kind_mount_root}/bridge/generated/` (the deployer spec's `bridge-state`
  host-path volume). Confirm the files land there as expected on the first real
  deployer run.
- **gas-oracle / warp-ui state → env:** these consume state via laconic-so
  `conftest` rather than a ConfigMap; their `state_distribute` runs with an empty
  `configmap_names` (git-pull only). Confirm the conftest wiring on first run.
- **Multi-host validators:** the validator loop delegates per-host via
  `include_role` + `apply: delegate_to`; confirm fact/`ansible_env` behavior on a
  real multi-VM split (correct for single-host v1).
