# Staging Ops Standup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `setup-all.yml` + `deploy-all.yml` able to bring the staging bridge up end-to-end, with the local rollout's failure classes (silent `default()` fallbacks, unfilled placeholders, spec drift) closed by machine-enforced checks.

**Architecture:** Staging artifacts are committed literals exactly prod-shaped (spec: `docs/superpowers/specs/2026-06-10-staging-ops-design.md`). Correctness is enforced by an inventory env contract, a prod↔staging spec shape-parity checker, deploy-time placeholder gates, and Layer-0 tests run against all three inventories in CI. The gorchain chain comes up via an isolated playbook outside the composites.

**Tech Stack:** Ansible 2.16+ (ansible-lint, yamllint), Python 3 + PyYAML, laconic-so, Caddy (docker), GitHub Actions.

---

## Constraints (shared dev machine — read first)

- **Never run deployments, e2e suites, or anything that touches a remote host or cluster.** Allowed verification: `bash -n`, `python3` scripts that only read the repo, `yamllint`, `ansible-lint`, `ansible-playbook --syntax-check`, the Layer-0 assert tests (`ansible-playbook -i inventories/<env>/hosts.yml tests/test_*.yml` — pure localhost assertions, no changes), and git.
- **Never push.** Commit per task on the `staging-ops` branch. End every commit message with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- All `ansible-playbook` / lint commands run from `ops/` unless noted. The Layer-0 tests need the collections installed once: `ansible-galaxy collection install -r requirements.yml -p ./collections` (already done on this machine if `ops/collections/` exists).
- Repo root: `/home/dev/git_puller/repos/hyperlane-stacks`.

## Planning-time findings the tasks below encode

1. `community.general.cloudflare_dns` requires the **registered** Cloudflare zone (`gorbagana.wtf`); the role currently passes `dns_zone` (`bridge.gorbagana.wtf` / `staging.gorbagana.wtf`), which Cloudflare would reject. Task 3 splits `cloudflare_zone` from `dns_zone`.
2. Prod's warp-ui spec serves the zone apex (`bridge.gorbagana.wtf`) but `dns_records` only creates `warp-ui.<zone>` — staging serves `warp-ui.<zone>` instead; the prod mismatch gets a pebble (Task 10).
3. `setup-chains.sh` `start_gorchain` deletes any existing deployment on re-run — fatal for staging's persistent chain. Task 8 adds a `GORCHAIN_PRESERVE` knob.
4. `gen-local-keys.sh` says "never run against a prod/staging credentials dir"; the staging design deliberately uses hot keys, so Task 8 amends the banner to "local + staging; never prod".
5. Sentinels that survive to deploy time today: `REPLACE_WITH_GITHUB_USERNAME` (harmless — GHCR auths by PAT; replaced with `gorbagana-dev`) and `NEXT_PUBLIC_WALLET_CONNECT_ID: REPLACE_WITH_WALLETCONNECT_PROJECT_ID` (local gets `""`; prod/staging keep the sentinel — the new gate forces the operator to fill it before deploy). `PRIVY_WALLET_ID: REPLACE_WITH_WALLET_ID` is fine — rendered by `spec_token_renders_extra` before the gate runs.

## File map

| File | Action | Task |
|---|---|---|
| `ops/roles/common/tasks/assert_env_contract.yml` | create | 1 |
| `ops/tests/test_env_contract.yml` | create | 1 |
| `ops/inventories/{prod,staging}/group_vars/all.yml` | modify (explicit vars) | 1, 2, 3 |
| `ops/playbooks/{setup-all,deploy-all}.yml` | modify (contract play) | 1 |
| `ops/inventories/staging/hosts.yml` | modify (regroup) | 2 |
| `ops/tests/test_staging_env.yml` | create | 2, 6 |
| `ops/roles/dns_cloudflare/{defaults,tasks}/main.yml` | modify (cloudflare_zone) | 3 |
| `ops/scripts/check-spec-parity.py` | create | 4 |
| `deployment/staging/spec-*.yml` (9 files) | create | 5 |
| `deployment/staging/bridges/default/warp-routes/usdc.yml` | modify (mint) | 5 |
| `deployment/staging/bridges/default/operator/validators.yaml` | create | 6 |
| `ops/roles/stack_deploy/tasks/render_spec.yml` | modify (spec gate) | 7 |
| `ops/roles/stack_deploy/tasks/deploy.yml` | modify (env gate) | 7 |
| `deployment/**/spec-*.yml` (sentinel audit) | modify | 7 |
| `ops/playbooks/prepare-gorchain.yml` | create | 8 |
| `ops/scripts/setup-chains.sh`, `ops/scripts/gen-local-keys.sh` | modify | 8 |
| `ops/requirements.yml` | modify (community.docker) | 8 |
| `.github/workflows/ops-lint.yml` | modify | 9 |
| `ops/runbooks/staging.md` | create | 10 |
| `ops/README.md`, `docs/superpowers/specs/2026-05-29-staging-environment-design.md` | modify | 10 |
| `.pebbles/` (prod warp-ui DNS pebble) | modify | 10 |

---

### Task 1: Inventory env contract

**Files:**
- Create: `ops/roles/common/tasks/assert_env_contract.yml`
- Create: `ops/tests/test_env_contract.yml`
- Modify: `ops/inventories/staging/group_vars/all.yml` (after the `ansible_user` block, ~line 19)
- Modify: `ops/inventories/prod/group_vars/all.yml` (after the `ansible_user` block, ~line 19)
- Modify: `ops/playbooks/setup-all.yml`, `ops/playbooks/deploy-all.yml` (new first play)

- [ ] **Step 1: Write the failing test**

Create `ops/tests/test_env_contract.yml`:

```yaml
---
# Layer-0: the inventory passed via -i satisfies the env contract — every var the
# roles branch on is defined (no silent default() fallbacks). Run per env:
#   for e in local staging prod; do
#     ansible-playbook -i inventories/$e/hosts.yml tests/test_env_contract.yml
#   done
- name: Inventory satisfies the env contract
  hosts: controller
  gather_facts: false
  tasks:
    - name: Assert the env contract
      ansible.builtin.include_tasks: ../roles/common/tasks/assert_env_contract.yml
```

Create `ops/roles/common/tasks/assert_env_contract.yml`:

```yaml
---
# Fail fast when an inventory under-specifies the vars roles branch on. Undefined
# vars don't error in `when:` expressions — `topology == 'single'` on an undefined
# topology just evaluates false — which is how env gaps slip through to a live run.
- name: Env contract — required inventory vars are defined
  ansible.builtin.assert:
    that: "vars[item] is defined"
    fail_msg: >-
      Env contract: '{{ item }}' is not defined — set it in this inventory's
      group_vars/all.yml.
    quiet: true
  loop:
    - topology
    - manage_dns
    - bridge_name
    - deploy_branch
    - deployment_subdir
    - dns_zone
    - credentials_dir
    - stack_env_vars
    - stacks

- name: Env contract — topology is a known mode
  ansible.builtin.assert:
    that: "topology in ['single', 'multi']"
    fail_msg: "Env contract: topology must be 'single' or 'multi', got '{{ topology }}'"
    quiet: true

- name: Env contract — every stack has a secret-env entry
  ansible.builtin.assert:
    that: "item in stack_env_vars"
    fail_msg: "Env contract: stack_env_vars has no entry for stack '{{ item }}'"
    quiet: true
  loop: "{{ stacks.keys() | list }}"
```

- [ ] **Step 2: Run it to verify it fails for staging (and prod), passes for local**

```bash
cd /home/dev/git_puller/repos/hyperlane-stacks/ops
ansible-playbook -i inventories/local/hosts.yml tests/test_env_contract.yml     # PASS (topology derived)
ansible-playbook -i inventories/staging/hosts.yml tests/test_env_contract.yml  # FAIL: 'topology' is not defined
ansible-playbook -i inventories/prod/hosts.yml tests/test_env_contract.yml     # FAIL: 'topology' is not defined
```

- [ ] **Step 3: Add the explicit vars to staging and prod group_vars**

In `ops/inventories/staging/group_vars/all.yml`, after the `ansible_user:` line (~19), insert:

```yaml

# --- Topology / DNS mode ---
# 'single' is the local-only mkcert/no-DNS mode. Staging is always Cloudflare DNS
# + Let's Encrypt, whatever the host count — so 'multi', stated explicitly (the
# env contract rejects inventories that leave these to role defaults).
topology: multi
manage_dns: true
# SSH by public_ip from host_vars (aliases like staging-gorchain are not DNS names).
ansible_host: "{{ public_ip | default(omit) }}"
```

In `ops/inventories/prod/group_vars/all.yml`, insert the same block after its `ansible_user:` line, with "Staging" → "Prod" in the comment.

- [ ] **Step 4: Run the test against all three inventories — all pass**

```bash
for e in local staging prod; do ansible-playbook -i inventories/$e/hosts.yml tests/test_env_contract.yml; done
```

Expected: PASS ×3.

- [ ] **Step 5: Wire the contract into both composites**

Prepend this play to `ops/playbooks/setup-all.yml` (before the first `import_playbook`) and to `ops/playbooks/deploy-all.yml` (before its first play):

```yaml
- name: Enforce the inventory env contract
  hosts: controller
  gather_facts: false
  tasks:
    - name: Assert the env contract
      ansible.builtin.include_tasks: ../roles/common/tasks/assert_env_contract.yml
```

(`setup-all.yml` keeps its leading `---`; the play goes between it and the first import.)

- [ ] **Step 6: Lint + syntax-check**

```bash
yamllint . && ansible-lint roles/common/tasks/assert_env_contract.yml tests/test_env_contract.yml playbooks/setup-all.yml playbooks/deploy-all.yml
ansible-playbook --syntax-check playbooks/setup-all.yml
ansible-playbook --syntax-check playbooks/deploy-all.yml
ansible-playbook --syntax-check tests/test_env_contract.yml
```

Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add ops/roles/common/tasks/assert_env_contract.yml ops/tests/test_env_contract.yml \
  ops/inventories/staging/group_vars/all.yml ops/inventories/prod/group_vars/all.yml \
  ops/playbooks/setup-all.yml ops/playbooks/deploy-all.yml
git commit -m "ops: enforce an explicit inventory env contract

Every var the roles branch on (topology, manage_dns, ...) must be defined
per inventory; asserted as the first play of setup-all/deploy-all and as a
Layer-0 test. Staging and prod gain the explicit topology/manage_dns/
ansible_host values they silently lacked.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Staging inventory — hosts regroup + DNS records + staging env test

**Files:**
- Modify: `ops/inventories/staging/hosts.yml`
- Modify: `ops/inventories/staging/group_vars/all.yml` (`dns_zone`, `dns_records`)
- Create: `ops/tests/test_staging_env.yml`

- [ ] **Step 1: Write the failing test**

Create `ops/tests/test_staging_env.yml` (pattern: `test_local_env.yml` — inventory-independent via file lookups):

```yaml
---
# Layer-0: staging wiring — multi-host Cloudflare env, gorchain-only chain host,
# specs under deployment/staging/. Inventory-independent (reads the staging files
# directly), so it runs under any -i like the other tests.
- name: Staging wiring
  hosts: localhost
  connection: local
  gather_facts: false
  vars:
    ansible_env:
      HOME: /home/op
  tasks:
    - name: Load staging env wiring
      ansible.builtin.include_vars:
        file: "{{ playbook_dir }}/../inventories/staging/group_vars/all.yml"

    - name: Parse the staging hosts file
      ansible.builtin.set_fact:
        _staging_inv: "{{ lookup('ansible.builtin.file', playbook_dir ~ '/../inventories/staging/hosts.yml') | from_yaml }}"

    - name: Staging env resolves as designed
      ansible.builtin.assert:
        that:
          - "topology == 'multi'"
          - "manage_dns | bool"
          - "deployment_subdir == 'deployment/staging'"
          - "dns_zone == 'staging.gorbagana.wtf'"
          # gorchain is the only chain on staging (solana is Helius devnet)
          - "_staging_inv.all.children.chain_hosts.hosts.keys() | list == ['staging-gorchain']"
          # the solana validator host is inventoried (bootstrap covers all:!controller)
          - "_staging_inv.all.children.validator_hosts.hosts.keys() | list == ['staging-solana-validator']"
          # the gorchain RPC seam has a DNS record on the chain host
          - "(dns_records | selectattr('name', 'eq', 'rpc') | map(attribute='host') | list) == ['staging-gorchain']"
        quiet: true
```

- [ ] **Step 2: Run it to verify it fails**

```bash
ansible-playbook -i inventories/staging/hosts.yml tests/test_staging_env.yml
```

Expected: FAIL (no `validator_hosts` group, no `rpc` record).

- [ ] **Step 3: Rewrite `ops/inventories/staging/hosts.yml`**

```yaml
all:
  children:
    controller:
      hosts:
        localhost:
          ansible_connection: local
    deployer_hosts:
      hosts:
        staging-bridge-ops:
    minio_hosts:
      hosts:
        staging-bridge-ops:
    relayer_hosts:
      hosts:
        staging-bridge-ops:
    gas_oracle_hosts:
      hosts:
        staging-bridge-ops:
    monitoring_hosts:
      hosts:
        staging-bridge-ops:
    warp_ui_hosts:
      hosts:
        staging-bridge-ops:
    # The single-node gorchain chain (prepare-gorchain.yml). Solana is Helius
    # devnet — no solana chain host on staging.
    chain_hosts:
      hosts:
        staging-gorchain:
    # Hosts that only run validators (from validators.yaml, not the inventory).
    # No playbook targets this group; membership puts the host in `all` so
    # bootstrap-host.yml provisions it and the validator loop can delegate to it.
    validator_hosts:
      hosts:
        staging-solana-validator:
```

- [ ] **Step 4: Update staging DNS in `group_vars/all.yml`**

Replace the `dns_zone` line and extend `dns_records`:

```yaml
# Hostname suffix for every staging endpoint (specs + DNS records). Changing the
# zone is mechanical: edit this + the hostname literals in deployment/staging/
# spec-*.yml, then re-run configure-dns.yml.
dns_zone: staging.gorbagana.wtf
```

Append to the `dns_records` list:

```yaml
  # gorchain RPC — Caddy TLS front on the chain host (prepare-gorchain.yml)
  - name: rpc
    host: staging-gorchain
```

- [ ] **Step 5: Run the test to verify it passes**

```bash
ansible-playbook -i inventories/staging/hosts.yml tests/test_staging_env.yml
ansible-playbook -i inventories/staging/hosts.yml tests/test_env_contract.yml
ansible-playbook -i inventories/staging/hosts.yml tests/test_inventory.yml
```

Expected: PASS ×3 (if `test_inventory.yml` hard-fails on the new `validator_hosts` group, extend its expected-groups list rather than weakening the assert).

- [ ] **Step 6: Lint + commit**

```bash
yamllint inventories/staging tests/test_staging_env.yml && ansible-lint tests/test_staging_env.yml
git add ops/inventories/staging ops/tests/test_staging_env.yml ops/tests/test_inventory.yml
git commit -m "ops(staging): regroup hosts, add the gorchain rpc record, lock with a Layer-0 test

chain_hosts is gorchain-only (solana is Helius devnet); the solana validator
host moves to a bootstrap-only validator_hosts group; rpc.<zone> points at the
chain host for the cross-host RPC seam.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

(Drop `ops/tests/test_inventory.yml` from the `git add` if it needed no change.)

---

### Task 3: dns_cloudflare — separate the registered Cloudflare zone from the hostname suffix

`community.general.cloudflare_dns`'s `zone:` must be a zone that exists in the Cloudflare account (`gorbagana.wtf`). Both prod (`bridge.gorbagana.wtf`) and staging (`staging.gorbagana.wtf`) use a *suffix* of it as `dns_zone`, so the role as written fails against real Cloudflare.

**Files:**
- Modify: `ops/roles/dns_cloudflare/defaults/main.yml`
- Modify: `ops/roles/dns_cloudflare/tasks/main.yml`
- Modify: `ops/inventories/{prod,staging}/group_vars/all.yml`
- Modify (assert additions): `ops/tests/test_dns_expansion.yml`

- [ ] **Step 1: Extend the DNS expansion test (failing first)**

Open `ops/tests/test_dns_expansion.yml` and add a task block (mirroring its existing style) that replicates the role's prefix derivation (Step 3 defines it — keep the two expressions textually identical) and asserts the three cases:

```yaml
    - name: Relative record names nest under the registered Cloudflare zone
      ansible.builtin.assert:
        that:
          - "_prefix_staging == '.staging'"
          - "_prefix_prod == '.bridge'"
          - "_prefix_identity == ''"
        quiet: true
      vars:
        _prefix_staging: >-
          {{ '' if 'staging.gorbagana.wtf' == 'gorbagana.wtf'
             else '.' ~ ('staging.gorbagana.wtf' | regex_replace('\.' ~ ('gorbagana.wtf' | regex_escape) ~ '$', '')) }}
        _prefix_prod: >-
          {{ '' if 'bridge.gorbagana.wtf' == 'gorbagana.wtf'
             else '.' ~ ('bridge.gorbagana.wtf' | regex_replace('\.' ~ ('gorbagana.wtf' | regex_escape) ~ '$', '')) }}
        _prefix_identity: >-
          {{ '' if 'hyperlane.local' == 'hyperlane.local'
             else '.' ~ ('hyperlane.local' | regex_replace('\.' ~ ('hyperlane.local' | regex_escape) ~ '$', '')) }}
```

This locks the expression the role uses (Step 3's `_dns_zone_prefix`) — if someone edits one side, the divergence shows up here. It passes on its own; the *role-side* failure mode this task fixes (wrong `zone:` sent to Cloudflare) is asserted by inspection in Step 3 since no Layer-0 test can call the Cloudflare module.

- [ ] **Step 2: Run the extended test**

```bash
ansible-playbook -i inventories/staging/hosts.yml tests/test_dns_expansion.yml
```

Expected: PASS (the block locks the shared expression; the role change in Step 3 is what makes Cloudflare calls correct).

- [ ] **Step 3: Implement in the role**

`ops/roles/dns_cloudflare/defaults/main.yml` — add:

```yaml
# The zone as REGISTERED in Cloudflare (cloudflare_dns rejects anything else).
# dns_zone may be a deeper suffix (bridge.gorbagana.wtf) — records are then
# created relative to cloudflare_zone (s3.bridge, rpc.staging, ...).
cloudflare_zone: "{{ dns_zone }}"
```

`ops/roles/dns_cloudflare/tasks/main.yml` — after the "Derive dns_records..." task and before "Build host -> public_ip map", insert:

```yaml
- name: Derive the record-name prefix nesting dns_zone under the registered zone
  ansible.builtin.set_fact:
    _dns_zone_prefix: >-
      {{ '' if dns_zone == cloudflare_zone
         else '.' ~ (dns_zone | regex_replace('\.' ~ (cloudflare_zone | regex_escape) ~ '$', '')) }}

- name: dns_zone must equal or nest under cloudflare_zone
  ansible.builtin.assert:
    that: "dns_zone == cloudflare_zone or dns_zone.endswith('.' ~ cloudflare_zone)"
    fail_msg: >-
      dns_zone '{{ dns_zone }}' is not under cloudflare_zone '{{ cloudflare_zone }}' —
      fix group_vars.
    quiet: true
```

In the "Reconcile A records (additive)" task, change:

```yaml
    zone: "{{ dns_zone }}"
    record: "{{ item.name }}"
```

to:

```yaml
    zone: "{{ cloudflare_zone }}"
    record: "{{ item.name }}{{ _dns_zone_prefix }}"
```

(keep the `label` as is — it already prints the FQDN).

- [ ] **Step 4: Set `cloudflare_zone` in prod + staging group_vars**

In both `ops/inventories/prod/group_vars/all.yml` and `ops/inventories/staging/group_vars/all.yml`, directly under the `dns_zone:` line:

```yaml
cloudflare_zone: gorbagana.wtf      # the registered CF zone; dns_zone nests under it
```

(local sets nothing — the default `cloudflare_zone == dns_zone` keeps operator-owned zones working.)

- [ ] **Step 5: Tests + lint**

```bash
ansible-playbook -i inventories/staging/hosts.yml tests/test_dns_expansion.yml
yamllint roles/dns_cloudflare && ansible-lint roles/dns_cloudflare
ansible-playbook --syntax-check playbooks/configure-dns.yml
```

Expected: PASS / clean.

- [ ] **Step 6: Commit**

```bash
git add ops/roles/dns_cloudflare ops/inventories/prod/group_vars/all.yml \
  ops/inventories/staging/group_vars/all.yml ops/tests/test_dns_expansion.yml
git commit -m "ops(dns): split the registered Cloudflare zone from the hostname suffix

cloudflare_dns only accepts zones that exist in the account; prod/staging use
a suffix of gorbagana.wtf as dns_zone, so records are now created relative to
the new cloudflare_zone (s3.bridge, rpc.staging, ...). Local keeps the
identity default.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Spec shape-parity checker (red)

**Files:**
- Create: `ops/scripts/check-spec-parity.py`

- [ ] **Step 1: Write the checker**

Create `ops/scripts/check-spec-parity.py` (executable):

```python
#!/usr/bin/env python3
"""Shape-parity check: deployment/spec-*.yml (prod) vs deployment/staging/spec-*.yml.

Staging must be exactly prod-shaped: same spec files, and per file the same
structure — mapping keys, list lengths, nesting. Scalar leaf VALUES are exempt
(hostnames, domain IDs, RPC URLs legitimately differ). Exits non-zero listing
every divergence, so CI catches "staging grew a key prod doesn't have" (and
vice versa) before any promotion does.

Run from the repo root:  python3 ops/scripts/check-spec-parity.py
"""
import glob
import os
import sys

import yaml

PROD_DIR = "deployment"
STAGING_DIR = "deployment/staging"
LEAF = "<value>"


def spec_names(d):
    return sorted(os.path.basename(p) for p in glob.glob(os.path.join(d, "spec-*.yml")))


def skeleton(node):
    if isinstance(node, dict):
        return {k: skeleton(v) for k, v in node.items()}
    if isinstance(node, list):
        return [skeleton(v) for v in node]
    return LEAF


def diff(a, b, path, out):
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append(f"{path}.{k}: only in staging")
            elif k not in b:
                out.append(f"{path}.{k}: only in prod")
            else:
                diff(a[k], b[k], f"{path}.{k}", out)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(f"{path}: list length {len(a)} (prod) != {len(b)} (staging)")
        for i, (x, y) in enumerate(zip(a, b)):
            diff(x, y, f"{path}[{i}]", out)
    elif type(a) is not type(b):
        out.append(f"{path}: {type(a).__name__} (prod) != {type(b).__name__} (staging)")


def main():
    prod, staging = spec_names(PROD_DIR), spec_names(STAGING_DIR)
    problems = [f"{n}: missing from {STAGING_DIR}/" for n in prod if n not in staging]
    problems += [f"{n}: missing from {PROD_DIR}/" for n in staging if n not in prod]
    for name in sorted(set(prod) & set(staging)):
        with open(os.path.join(PROD_DIR, name)) as f:
            p = yaml.safe_load(f)
        with open(os.path.join(STAGING_DIR, name)) as f:
            s = yaml.safe_load(f)
        diff(skeleton(p), skeleton(s), name, problems)
    if problems:
        print("Spec shape parity FAILED (prod vs staging):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"Spec shape parity OK: {len(prod)} specs match.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

```bash
chmod +x ops/scripts/check-spec-parity.py
```

- [ ] **Step 2: Run to verify it fails (no staging specs yet)**

```bash
cd /home/dev/git_puller/repos/hyperlane-stacks && python3 ops/scripts/check-spec-parity.py
```

Expected: exit 1, nine "missing from deployment/staging/" lines.

- [ ] **Step 3: Commit**

```bash
git add ops/scripts/check-spec-parity.py
git commit -m "ops: add the prod<->staging spec shape-parity checker

Compares structure only (keys, nesting, list lengths) — values are exempt.
Red until the staging specs land; wired into CI later.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Staging specs ×9 + devnet USDC mint (parity green)

**Files:**
- Create: `deployment/staging/spec-{minio,deployer,warp-deployer,relayer,gas-oracle,monitoring,warp-ui,validator-gorchain,validator-solana}.yml`
- Modify: `deployment/staging/bridges/default/warp-routes/usdc.yml`

- [ ] **Step 1: Copy prod specs and substitute staging values**

```bash
cd /home/dev/git_puller/repos/hyperlane-stacks/deployment
for f in spec-*.yml; do cp "$f" "staging/$f"; done
cd staging
sed -i \
  -e 's/bridge\.gorbagana\.wtf/staging.gorbagana.wtf/g' \
  -e 's/1198486093/1198486095/g' \
  -e 's/1399811149/1399811151/g' \
  -e 's/_IS_TESTNET: "false"/_IS_TESTNET: "true"/g' \
  spec-*.yml
# rpc.gorbagana.wtf (prod gorchain) -> rpc.staging.gorbagana.wtf (Caddy front on staging-gorchain)
sed -i 's#https://rpc\.gorbagana\.wtf#https://rpc.staging.gorbagana.wtf#g' spec-*.yml
# warp-ui: prod serves the zone apex; staging serves warp-ui.<zone> so the
# dns_records 'warp-ui' entry matches the served hostname (see plan finding #2)
sed -i 's/host-name: staging\.gorbagana\.wtf/host-name: warp-ui.staging.gorbagana.wtf/' spec-warp-ui.yml
```

Notes for the implementer:
- The substitutions cover: domain+chain IDs (gorchain `1198486093→1198486095`, solana `1399811149→1399811151`), testnet flags, every `*.bridge.gorbagana.wtf` hostname (s3, minio-console, grafana, prometheus, relayer, validator-*, `AWS_ENDPOINT_URL_S3`, `PROMETHEUS_*_TARGETS`), and the gorchain RPC URL.
- Everything else stays byte-identical to prod: `secrets:` blocks (incl. `HYP_CHAINS_SOLANA_CUSTOMRPCURLS`), `configmaps:`, `volumes:`, `namespace:` overrides in the validator specs, `kind-cluster-name`/`kind-mount-root`, `acme-email: admin@gorbagana.wtf`, `image-pull-secret`, commented `image-overrides` examples, and the sentinels `PRIVY_WALLET_ID: "REPLACE_WITH_WALLET_ID"` (rendered at deploy) and `NEXT_PUBLIC_WALLET_CONNECT_ID` (operator-filled; Task 7 gates it).
- After the seds, read each of the nine files once and sanity-check the comments still make sense (e.g. a comment naming "mainnet" should say devnet — fix comment text only, never structure).

- [ ] **Step 2: Pin the devnet USDC mint in the warp route**

In `deployment/staging/bridges/default/warp-routes/usdc.yml` replace:

```yaml
  token: "REPLACE_WITH_STAGING_USDC_MINT_ADDRESS"
```

with:

```yaml
  # Circle's devnet USDC mint — real Circle metadata, faucetable.
  token: "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"
```

- [ ] **Step 3: Verify — parity green + no prod values leaked**

```bash
cd /home/dev/git_puller/repos/hyperlane-stacks
python3 ops/scripts/check-spec-parity.py                       # exit 0, "9 specs match"
grep -rn 'bridge\.gorbagana\.wtf\|1198486093\|1399811149\|_IS_TESTNET: "false"' deployment/staging/  # NO matches
grep -c '_IS_TESTNET: "true"' deployment/staging/spec-deployer.yml   # 2
python3 - <<'EOF'
import glob, yaml
for p in glob.glob('deployment/staging/spec-*.yml'):
    yaml.safe_load(open(p)); print('ok', p)
EOF
```

- [ ] **Step 4: Commit**

```bash
git add deployment/staging/
git commit -m "deployment(staging): prod-shaped specs with devnet values

Devnet domain/chain IDs (gorchain 1198486095, solana 1399811151), testnet
flags, staging.gorbagana.wtf hostnames, Caddy-fronted gorchain RPC, Circle
devnet USDC as the warp collateral. Shape-parity checker is green.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Staging validators.yaml + test additions

**Files:**
- Create: `deployment/staging/bridges/default/operator/validators.yaml`
- Modify: `ops/tests/test_staging_env.yml`

- [ ] **Step 1: Extend the staging test (failing first)**

Append to the tasks in `ops/tests/test_staging_env.yml`:

```yaml
    - name: Parse the staging validators file
      ansible.builtin.set_fact:
        _staging_validators: >-
          {{ (lookup('ansible.builtin.file',
                     playbook_dir ~ '/../../deployment/staging/bridges/default/operator/validators.yaml')
              | from_yaml)['validators'] }}

    - name: Staging validators are the prod-shaped 1-of-1 pair
      ansible.builtin.assert:
        that:
          # labels must match the *_PRIMARY_* MinIO IAM env names in the specs
          - "_staging_validators | map(attribute='label') | list == ['gorchain-primary', 'solana-primary']"
          - "(_staging_validators | selectattr('chain', 'eq', 'gorchain') | first).host == 'staging-gorchain'"
          - "(_staging_validators | selectattr('chain', 'eq', 'solana') | first).host == 'staging-solana-validator'"
          - "_staging_validators | map(attribute='hostname') | select('search', '\\.staging\\.gorbagana\\.wtf$') | list | length == 2"
        quiet: true
```

Run: `ansible-playbook -i inventories/staging/hosts.yml tests/test_staging_env.yml` — FAIL (file missing).

- [ ] **Step 2: Create `deployment/staging/bridges/default/operator/validators.yaml`**

```yaml
---
# Staging validator set — 1-of-1 per chain, prod-shaped. Labels are load-bearing:
# the committed validator specs hardcode the derived MinIO IAM env names
# (GORCHAIN_PRIMARY_KEY_ID, SOLANA_PRIMARY_KEY_ID, ...). Fill privy_wallet_id from
# the staging Privy project (ops/runbooks/privy-wallets.md) before deploy-all —
# the deploy-time placeholder gate refuses unfilled values.
validators:
  - label: gorchain-primary
    chain: gorchain
    host: staging-gorchain
    privy_wallet_id: REPLACE_WITH_STAGING_GORCHAIN_PRIVY_WALLET_ID
    hostname: validator-gorchain.staging.gorbagana.wtf
  - label: solana-primary
    chain: solana
    host: staging-solana-validator
    privy_wallet_id: REPLACE_WITH_STAGING_SOLANA_PRIVY_WALLET_ID
    hostname: validator-solana.staging.gorbagana.wtf
```

- [ ] **Step 3: Tests pass**

```bash
ansible-playbook -i inventories/staging/hosts.yml tests/test_staging_env.yml   # PASS
ansible-playbook -i inventories/staging/hosts.yml tests/test_validators.yml    # PASS (uses the env's operator file)
```

If `test_validators.yml` reads only the local/prod file by explicit path, leave it; the staging shape is covered by `test_staging_env.yml`.

- [ ] **Step 4: Commit**

```bash
git add deployment/staging/bridges/default/operator/validators.yaml ops/tests/test_staging_env.yml
git commit -m "deployment(staging): validator set definition (1-of-1 per chain)

Feeds the validator deploy loop, per-validator MinIO IAM, and DNS auto-append.
Privy wallet ids are operator-filled; the deploy gate enforces it.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: Deploy-time placeholder gates + sentinel audit

**Files:**
- Modify: `ops/roles/stack_deploy/tasks/render_spec.yml` (append gate)
- Modify: `ops/roles/stack_deploy/tasks/deploy.yml` (extend the existing secret-env assert)
- Modify: `deployment/spec-*.yml`, `deployment/local/spec-*.yml`, `deployment/staging/spec-*.yml` (username sentinel), `deployment/local/spec-warp-ui.yml` (walletconnect)

- [ ] **Step 1: Append the spec gate to `render_spec.yml`**

At the end of `ops/roles/stack_deploy/tasks/render_spec.yml`:

```yaml

# Placeholder gate — nothing unrendered may reach laconic-so. Everything legit is
# substituted above (tokens, program-ids, whitelist, per-validator wallet ids), so
# any survivor is an unfilled operator value or a broken render. Catch it here with
# names, not as a half-configured pod at runtime.
- name: Read back the fully-rendered spec
  ansible.builtin.slurp:
    src: "{{ _spec_file }}"
  register: _rendered_spec_raw
  when: not ansible_check_mode

- name: No unrendered placeholders survive in the spec
  ansible.builtin.assert:
    that: "_leftover | length == 0"
    fail_msg: >-
      Unrendered placeholders in {{ _spec_file }}: {{ _leftover | unique | join(', ') }}
      — fill the operator value (or fix the render) and re-run.
    quiet: true
  vars:
    _leftover: "{{ _rendered_spec_raw.content | b64decode | regex_findall('REPLACE_WITH[A-Z0-9_]*|__[A-Z0-9_]+__') }}"
  when: not ansible_check_mode
```

- [ ] **Step 2: Extend the secret-env assert in `deploy.yml`**

In the existing task `Required secret env values for this stack are set and rendered`, add one condition to `that:`:

```yaml
      - "not (stack_env[item] | default('', true) | string is search('REPLACE_WITH'))"
```

and extend its `fail_msg` first line to: `Secret env '{{ item }}' for stack '{{ stack_name }}' is empty, an unrendered template, or a REPLACE_WITH placeholder (its source var was never filled).`

- [ ] **Step 3: Sentinel audit — fix what legitimately survives today**

```bash
cd /home/dev/git_puller/repos/hyperlane-stacks
# GHCR auths by the PAT; the username only needs to be non-empty — use the org name.
grep -rl 'REPLACE_WITH_GITHUB_USERNAME' deployment/ | xargs sed -i 's/username: REPLACE_WITH_GITHUB_USERNAME/username: gorbagana-dev/'
```

In `deployment/local/spec-warp-ui.yml` replace:

```yaml
  NEXT_PUBLIC_WALLET_CONNECT_ID: "REPLACE_WITH_WALLETCONNECT_PROJECT_ID"
```

with:

```yaml
  # No WalletConnect on local — the UI degrades to injected wallets only.
  NEXT_PUBLIC_WALLET_CONNECT_ID: ""
```

Prod and staging `spec-warp-ui.yml` keep the sentinel: it is a real operator decision (a WalletConnect Cloud project id, or `""` to disable), and the new gate now *enforces* filling it before deploy — that is the intended behavior, document it in the runbook (Task 10).

- [ ] **Step 4: Verify**

```bash
grep -rn 'REPLACE_WITH_GITHUB_USERNAME' deployment/            # no matches
python3 ops/scripts/check-spec-parity.py                       # still green (both sides changed alike)
cd ops && yamllint roles/stack_deploy && ansible-lint roles/stack_deploy
ansible-playbook --syntax-check playbooks/deploy-all.yml
for e in local staging prod; do ansible-playbook -i inventories/$e/hosts.yml tests/test_env_contract.yml; done
```

- [ ] **Step 5: Commit**

```bash
git add ops/roles/stack_deploy deployment/
git commit -m "ops: fail-closed placeholder gates at deploy time

The fully-rendered spec must contain no REPLACE_WITH/__TOKEN__ survivors, and
no secret env value may be an unfilled placeholder. Audit: GHCR username
sentinel replaced with the org name (auth is by PAT); local warp-ui ships
without WalletConnect; prod/staging keep the WalletConnect sentinel for the
gate to enforce.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8: prepare-gorchain.yml — persistent staging chain + Caddy TLS front

**Files:**
- Modify: `ops/scripts/setup-chains.sh` (GORCHAIN_PRESERVE knob)
- Modify: `ops/scripts/gen-local-keys.sh` (banner scope)
- Modify: `ops/requirements.yml` (community.docker)
- Create: `ops/playbooks/prepare-gorchain.yml`

- [ ] **Step 1: Add the preserve knob to `setup-chains.sh`**

In the env-knobs comment block at the top, add a line:

```bash
#   GORCHAIN_PRESERVE [unset]  set to reuse a healthy/existing gorchain deployment
#                              instead of recreating it (staging: persistent chain)
```

In `start_gorchain()`, immediately after `echo "== gorchain =="` and **before** the GHCR login block, insert:

```bash
  if [ -n "${GORCHAIN_PRESERVE:-}" ] && curl -fS "$GORCHAIN_RPC/health" >/dev/null 2>&1; then
    echo "  already running at $GORCHAIN_RPC (GORCHAIN_PRESERVE) — leaving it"
    wait_for_chain "$GORCHAIN_RPC" gorchain
    return 0
  fi
```

And replace the stale-deployment line:

```bash
  [ -d "$deploy_dir" ] && { echo "  removing stale deployment $deploy_dir"; laconic-so deployment --dir "$deploy_dir" stop --delete-volumes 2>/dev/null || true; rm -rf "$deploy_dir"; }
```

with:

```bash
  if [ -d "$deploy_dir" ]; then
    if [ -n "${GORCHAIN_PRESERVE:-}" ]; then
      echo "  existing deployment (not healthy) — starting it instead of recreating"
      laconic-so deployment --dir "$deploy_dir" start
      wait_for_chain "$GORCHAIN_RPC" gorchain
      return 0
    fi
    echo "  removing stale deployment $deploy_dir"
    laconic-so deployment --dir "$deploy_dir" stop --delete-volumes 2>/dev/null || true
    rm -rf "$deploy_dir"
  fi
```

(Note this early-return path skips the fetch-stack/spec steps by design — the deployment already embeds them.)

- [ ] **Step 2: Amend the `gen-local-keys.sh` scope**

The staging design deliberately signs with hot key files (`signer: hot-key-file`), so the script's scope widens from "local only" to "hot-key environments". Change the header comment lines:

```bash
# Generate THROWAWAY test signing keys for the `local` own-chains environment.
...
# to fund. TEST KEYS ONLY — never run this against a prod/staging credentials dir.
```

to:

```bash
# Generate hot signing keys for the hot-key-signer environments: `local` and
# `staging` (per the staging design, staging signs from key files for fast
# iteration). NEVER run this against a prod credentials dir.
```

and in the banner heredoc change:

```
 gen-local-keys — TEST signing keys for the LOCAL environment
...
 Throwaway keys only. Do NOT use for prod/staging.
```

to:

```
 gen-local-keys — hot signing keys (local / staging environments)
...
 Hot keys only. Do NOT use for prod.
```

- [ ] **Step 3: Add community.docker to `ops/requirements.yml`**

```yaml
  - name: community.docker
    version: ">=3.0.0"
```

Run: `ansible-galaxy collection install -r requirements.yml -p ./collections` (local download only — allowed).

- [ ] **Step 4: Create `ops/playbooks/prepare-gorchain.yml`**

```yaml
---
# Staging chain play — stand up the PERSISTENT single-node gorchain on the
# chain host, front its RPC with Caddy + Let's Encrypt at rpc.<dns_zone>, and
# generate the hot signing keys. Deliberately NOT part of setup-all/deploy-all
# (same isolation as the local prepare-chains.yml): chains are pre-deploy.
#
#   ansible-playbook -i inventories/staging/hosts.yml playbooks/prepare-gorchain.yml
#
# Run AFTER setup-all.yml: bootstrap installs docker/laconic-so, and
# configure-dns must have created rpc.<dns_zone> before Caddy can pass the
# ACME challenge. Re-runs preserve the chain (GORCHAIN_PRESERVE) — state under
# ~/chains/gorchain survives until an operator destroys it deliberately.
#
# Solana is Helius devnet — nothing to stand up. Funding the generated keys is
# a runbook step (gorchain: own faucet; devnet: rate-limited airdrops).
- name: Prepare the staging gorchain chain + RPC TLS front + hot keys
  hosts: chain_hosts
  gather_facts: true
  vars_files:
    - "{{ inventory_dir }}/secrets.yml"
  vars:
    chains_dir: "{{ ansible_env.HOME }}/chains"
    scripts_dst: "{{ ansible_env.HOME }}/.bridge-setup-scripts"
    caddy_dir: "{{ ansible_env.HOME }}/gorchain-rpc-caddy"
    gorchain_rpc: "http://localhost:8899"
    solana_bin: "{{ ansible_env.HOME }}/.local/share/solana/install/active_release/bin"
    tool_path: "{{ ansible_env.HOME }}/bin:{{ solana_bin }}:{{ ansible_env.PATH }}"
    solana_version: "v3.1.9"
  pre_tasks:
    - name: GHCR PAT must be set (private gorchain images)
      ansible.builtin.assert:
        that: "(ghcr_pat | default('') | length) > 0"
        fail_msg: "Set ghcr_pat in secrets.yml for the private gorchain images."
  tasks:
    - name: Install the Solana CLI (Anza) if missing
      ansible.builtin.shell:  # noqa: command-instead-of-module
        cmd: |
          set -o pipefail
          curl -sSfL "https://release.anza.xyz/{{ solana_version }}/install" | sh
        executable: /bin/bash
        creates: "{{ solana_bin }}/solana"

    - name: Ensure the setup-scripts dir exists
      ansible.builtin.file:
        path: "{{ scripts_dst }}"
        state: directory
        mode: "0755"

    - name: Ship the setup scripts
      ansible.builtin.copy:
        src: "../scripts/{{ item }}"
        dest: "{{ scripts_dst }}/{{ item }}"
        mode: "0755"
      loop:
        - setup-chains.sh
        - gen-local-keys.sh
        - fund-test-wallets.sh

    - name: Stand up gorchain (persistent — preserved across re-runs)
      ansible.builtin.command:
        cmd: "{{ scripts_dst }}/setup-chains.sh"
      environment:
        PATH: "{{ tool_path }}"
        CHAINS_DIR: "{{ chains_dir }}"
        GHCR_USER: "{{ ghcr_user | default('gorbagana-dev') }}"
        GHCR_PAT: "{{ ghcr_pat }}"
        SKIP_SOLANA: "1"
        GORCHAIN_PRESERVE: "1"
      register: chain_out
      changed_when: true

    - name: Generate the hot signing keys (existing files are never overwritten)
      ansible.builtin.command:
        cmd: "{{ scripts_dst }}/gen-local-keys.sh --yes"
      environment:
        PATH: "{{ tool_path }}"
        CRED_DIR: "{{ credentials_dir }}"
      register: keys_out
      changed_when: true

    - name: Caddy config dir
      ansible.builtin.file:
        path: "{{ caddy_dir }}"
        state: directory
        mode: "0755"

    # WebSocket exposure lands with the fast-bridging work (hyp-d34); HTTP only here.
    - name: Render the Caddyfile (TLS front for the gorchain RPC)
      ansible.builtin.copy:
        dest: "{{ caddy_dir }}/Caddyfile"
        mode: "0644"
        content: |
          rpc.{{ dns_zone }} {
            reverse_proxy 127.0.0.1:8899
          }
      register: _caddyfile

    - name: Run the Caddy RPC front (host network; certs persist in the volume)
      community.docker.docker_container:
        name: gorchain-rpc-caddy
        image: caddy:2
        restart_policy: unless-stopped
        network_mode: host
        recreate: "{{ _caddyfile is changed }}"
        volumes:
          - "{{ caddy_dir }}/Caddyfile:/etc/caddy/Caddyfile:ro"
          - gorchain-rpc-caddy-data:/data

    - name: Show generated key addresses (fund per the staging runbook)
      ansible.builtin.debug:
        var: keys_out.stdout_lines
```

- [ ] **Step 5: Verify**

```bash
bash -n scripts/setup-chains.sh && bash -n scripts/gen-local-keys.sh
cd ops && ansible-playbook --syntax-check playbooks/prepare-gorchain.yml
yamllint playbooks/prepare-gorchain.yml requirements.yml && ansible-lint playbooks/prepare-gorchain.yml
ansible-playbook -i inventories/local/hosts.yml tests/test_local_env.yml   # unchanged local behavior
```

- [ ] **Step 6: Commit**

```bash
git add ops/scripts/setup-chains.sh ops/scripts/gen-local-keys.sh ops/requirements.yml ops/playbooks/prepare-gorchain.yml
git commit -m "ops(staging): isolated gorchain chain play with persistent state and a Caddy TLS front

setup-chains.sh gains GORCHAIN_PRESERVE (reuse instead of recreate);
gen-local-keys widens to hot-key environments (local + staging, never prod);
rpc.<zone> is served by a host-level Caddy with Let's Encrypt. Not part of the
setup-all/deploy-all composites by design.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 9: CI — parity check + Layer-0 tests against all three inventories

**Files:**
- Modify: `.github/workflows/ops-lint.yml`

- [ ] **Step 1: Extend the workflow**

In `.github/workflows/ops-lint.yml`:

1. Trigger paths — the parity check must run when specs change:

```yaml
    paths: ["ops/**", "deployment/**", ".github/workflows/ops-lint.yml"]
```

2. After the `Install collections` step add:

```yaml
      - name: spec shape parity (prod vs staging)
        working-directory: .
        run: python3 ops/scripts/check-spec-parity.py
```

3. After the `syntax-check playbooks + tests` step add:

```yaml
      - name: layer-0 tests (all inventories)
        run: |
          for env in local staging prod; do
            cp "inventories/$env/secrets.example.yml" "inventories/$env/secrets.yml"
            for t in tests/test_*.yml; do
              echo "== $env :: $t =="
              ansible-playbook -i "inventories/$env/hosts.yml" "$t"
            done
          done
```

(PyYAML ships with the `ansible` pip install — no extra dependency step.)

- [ ] **Step 2: Run the matrix locally — fix any env-coupling it uncovers**

```bash
cd /home/dev/git_puller/repos/hyperlane-stacks/ops
for env in local staging prod; do
  [ -f "inventories/$env/secrets.yml" ] || cp "inventories/$env/secrets.example.yml" "inventories/$env/secrets.yml"
  for t in tests/test_*.yml; do echo "== $env :: $t =="; ansible-playbook -i "inventories/$env/hosts.yml" "$t" || echo "FAILED: $env $t"; done
done
```

**Do not delete or overwrite an existing `inventories/<env>/secrets.yml` on this machine** — only create it if absent. Expected: all pass. If a test fails under a non-native inventory (e.g. it assumed a prod-only group), make the test inventory-independent the way `test_local_env.yml` is (explicit `include_vars` of its target env + file lookups), never by weakening its assertions. Record each such fix in the commit message.

- [ ] **Step 3: Lint + commit**

```bash
yamllint ../.github/workflows/ops-lint.yml
git add .github/workflows/ops-lint.yml ops/tests/
git commit -m "ci(ops): spec shape-parity gate + Layer-0 tests for every inventory

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

(Drop `ops/tests/` from the add if Step 2 needed no fixes.)

---

### Task 10: Staging runbook + doc sync + pebbles

**Files:**
- Create: `ops/runbooks/staging.md`
- Modify: `ops/README.md`, `ops/runbooks/README.md`
- Modify: `docs/superpowers/specs/2026-05-29-staging-environment-design.md` (header note)
- Modify: `.pebbles/` (one new pebble)

- [ ] **Step 1: Write `ops/runbooks/staging.md`**

```markdown
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

    ansible-playbook -i inventories/staging/hosts.yml playbooks/deploy-all.yml

MinIO → deployer Job → warp deployer → relayer → gas-oracle → warp-ui →
validators → monitoring, with the deploy gates refusing any unfilled
placeholder. First publish of the generated state, attended:

    ansible-playbook -i inventories/staging/hosts.yml playbooks/publish-bridge-state.yml -e state_review=true

## 4. Verify

- `https://rpc.staging.gorbagana.wtf/health` answers `ok` and slots advance.
- MinIO (`https://minio-console.staging.gorbagana.wtf`): checkpoint objects
  appear under both validator buckets.
- Grafana (`https://grafana.staging.gorbagana.wtf`): relayer + validator
  dashboards report.
- `https://warp-ui.staging.gorbagana.wtf`: run a devnet-USDC transfer
  solana → gorchain and back.

## 5. Reset

Stacks only (chain + state survive):

    ansible-playbook -i inventories/staging/hosts.yml playbooks/stop-all.yml

Full host reset before a bootstrap rehearsal additionally destroys the kind
cluster (`-e destroy_cluster=true`) and, **only if intentionally resetting
chain state**, removes `~/chains/gorchain` + the `gorchain-rpc-caddy`
container on staging-gorchain by hand. Chain state is deliberately never
destroyed by a playbook.
```

- [ ] **Step 2: Sync the surrounding docs**

- `ops/runbooks/README.md`: add a `staging.md` bullet alongside the local guides.
- `ops/README.md` Environments section: in the prod/staging bullet, append: staging specifics — chain via `playbooks/prepare-gorchain.yml` (persistent, Caddy-fronted RPC), hot-key signing, runbook `runbooks/staging.md`. In the Linting section, note the env contract test, the parity checker (`python3 ops/scripts/check-spec-parity.py` from the repo root), and that CI runs the Layer-0 suite against all three inventories.
- `docs/superpowers/specs/2026-05-29-staging-environment-design.md`: add under the title:

```markdown
> **2026-06-10:** implementation-facing parts superseded by
> `2026-06-10-staging-ops-design.md` (the ops layer landed with a different
> layout: `ops/inventories/`, `publish-bridge-state.yml`). This doc remains
> the source for staging's purpose, lifecycle, and rehearsal surface.
```

- [ ] **Step 3: File the prod warp-ui DNS pebble**

```bash
pb create --title "Prod warp-ui serves the zone apex but dns_records only creates warp-ui.<zone>" \
  --type bug --priority P2 \
  --description "deployment/spec-warp-ui.yml serves host-name bridge.gorbagana.wtf (the dns_zone apex), but ops/roles/dns_cloudflare reconciles only the dns_records names (warp-ui -> warp-ui.bridge.gorbagana.wtf) — no A record for the apex is ever created, so stack_deploy preflight (hostname-resolution check) will fail on a prod deploy unless the apex record exists out-of-band. Staging avoided this by serving warp-ui.staging.gorbagana.wtf (see 2026-06-10-staging-ops-design.md). Fix direction: either add an apex entry mechanism to dns_records (name '@'-style relative handling in ops/roles/dns_cloudflare/tasks/main.yml) or move prod warp-ui to warp-ui.bridge.gorbagana.wtf — operator decision, the apex is the published UI URL."
```

- [ ] **Step 4: Verify + commit**

```bash
cd /home/dev/git_puller/repos/hyperlane-stacks/ops && yamllint . && ansible-lint .
cd .. && python3 ops/scripts/check-spec-parity.py
git add ops/runbooks/ ops/README.md docs/superpowers/specs/2026-05-29-staging-environment-design.md .pebbles/events.jsonl
git commit -m "docs(ops): staging runbook + README/design-doc sync; pebble for the prod warp-ui apex record

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Final verification (whole branch)

```bash
cd /home/dev/git_puller/repos/hyperlane-stacks
python3 ops/scripts/check-spec-parity.py
cd ops
yamllint . && ansible-lint .
for p in playbooks/*.yml tests/test_*.yml; do ansible-playbook --syntax-check "$p"; done
for env in local staging prod; do
  for t in tests/test_*.yml; do ansible-playbook -i "inventories/$env/hosts.yml" "$t"; done
done
```

All green. The actual staging deployment (setup-all / prepare-gorchain / deploy-all against the three VMs) is **operator-run** — hand off with the runbook; do not run it from this machine.
