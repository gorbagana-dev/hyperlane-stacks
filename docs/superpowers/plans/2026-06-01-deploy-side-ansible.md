# Deploy-Side Ansible Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `ops/` ansible layer that brings a Hyperlane SVM bridge fully running across machines with zero on-chain signing — provisioning (`setup-all.yml`) then deployment (`deploy-all.yml`).

**Architecture:** Six single-responsibility roles + thin orchestration playbooks at a top-level `ops/`, with per-env inventory trees (`prod`, `staging`). Stacks are deployed by shelling out to `laconic-so` exactly as the proven e2e harness does. Logic-bearing pieces (env-var assembly, `validators.yaml` derivations, DNS record expansion, credential idempotency, commit scoping) get real localhost assertion tests; imperative system roles are gated by yamllint + ansible-lint + `--syntax-check` + `--check`, with hardware-touching behavior verified manually on VMs.

**Tech Stack:** Ansible 2.16+, `community.general` (cloudflare_dns), `kubernetes.core`, `laconic-so`, kind, Docker, Cloudflare DNS. Spec/state inputs live in `deployment/[staging/]`.

**Design spec:** `docs/superpowers/specs/2026-06-01-deploy-side-ansible-design.md`. Read it first.

---

## Conventions used by every task

- **All ansible work lives under `ops/`.** Run lint/syntax from the repo root.
- **Env resolution:** every playbook takes `-e env=prod` (default) or `-e env=staging`. A `vars_files`/`pre_tasks` block resolves two paths: `deployment_root` (`deployment/` or `deployment/staging/`) and the inventory (`ops/inventories/<env>/hosts.yml`). Bridge name is `default`.
- **Test harness for logic:** assertion playbooks under `ops/tests/` run on `localhost` with `connection: local`, load fixtures from `ops/tests/fixtures/`, and use the `ansible.builtin.assert` module. They need no VM and run in CI.
- **Lint gate (every task):** from repo root,
  `yamllint ops/ && ansible-lint ops/ && for p in ops/playbooks/*.yml ops/tests/*.yml; do ansible-playbook --syntax-check "$p"; done`
  must pass before commit.
- **Check-mode awareness:** any task that shells out to `laconic-so`/`kind`/`docker`/`kubectl` (things that can't dry-run) must carry `when: not ansible_check_mode` so `--check` exercises the rest.
- **Commits:** branch is `deploy-side-ansible`. Commit per task. Never push (the operator handles pushes). No branch/release names in commit messages.

---

## File Structure

```
ops/
  ansible.cfg                       # Task 1
  requirements.yml                  # Task 1
  .yamllint                         # Task 1
  .ansible-lint                     # Task 1
  README.md                         # Task 17
  inventories/
    prod/
      hosts.yml                     # Task 2
      group_vars/all.yml            # Task 2
      host_vars/bridge-host-1.yml   # Task 2 (example)
      secrets.example.yml           # Task 2
    staging/                        # Task 2 (mirror)
      hosts.yml
      group_vars/all.yml
      host_vars/staging-bridge-ops.yml
      secrets.example.yml
  roles/
    common/tasks/load_validators.yml          # Task 3
    prerequisites_privileged/{tasks,defaults}/main.yml   # Task 4
    prerequisites_user/{tasks,defaults}/main.yml         # Task 5
    dns_cloudflare/{tasks,defaults}/main.yml             # Task 7
    credentials/
      tasks/{main,generate,distribute}.yml    # Tasks 9-10
      defaults/main.yml
    stack_deploy/
      tasks/{main,preflight,deploy}.yml       # Tasks 12-13
      defaults/main.yml
    state_distribute/{tasks,defaults}/main.yml           # Task 14
  playbooks/
    bootstrap-host.yml              # Task 6
    configure-dns.yml               # Task 8
    distribute-credentials.yml      # Task 11
    commit-bridge-state.yml         # Task 15
    setup-all.yml                   # Task 16
    deploy-all.yml                  # Task 16
    stop-all.yml                    # Task 16
  tests/
    fixtures/validators.yaml        # Task 3
    test_validators.yml             # Task 3
    test_dns_expansion.yml          # Task 7
    test_credentials_idempotent.yml # Task 9
    test_credentials_required.yml   # Task 10
    test_stack_env.yml              # Task 12
    test_state_paths.yml            # Task 14
    test_commit_scope.yml           # Task 15
.github/workflows/ops-lint.yml      # Task 1
.gitignore                          # Task 2 (append secrets.yml)
```

---

### Task 1: Scaffold `ops/` tooling + Layer-0 lint CI

**Files:**
- Create: `ops/ansible.cfg`, `ops/requirements.yml`, `ops/.yamllint`, `ops/.ansible-lint`, `.github/workflows/ops-lint.yml`
- Create: `ops/tests/.gitkeep`

- [ ] **Step 1: Write `ops/ansible.cfg`**

```ini
[defaults]
roles_path = ./roles
collections_path = ./collections
inventory = ./inventories/prod/hosts.yml
host_key_checking = True
retry_files_enabled = False
stdout_callback = yaml
interpreter_python = auto_silent

[ssh_connection]
ssh_args = -o ForwardAgent=yes -o ControlMaster=auto -o ControlPersist=60s
pipelining = True
```

- [ ] **Step 2: Write `ops/requirements.yml`**

```yaml
---
collections:
  - name: community.general
    version: ">=8.0.0"
  - name: kubernetes.core
    version: ">=3.0.0"
```

- [ ] **Step 3: Write `ops/.yamllint`**

```yaml
---
extends: default
rules:
  line-length:
    max: 120
    level: warning
  truthy:
    allowed-values: ["true", "false"]
    check-keys: false
  comments:
    min-spaces-from-content: 1
ignore: |
  collections/
```

- [ ] **Step 4: Write `ops/.ansible-lint`**

```yaml
---
profile: production
exclude_paths:
  - collections/
  - tests/fixtures/
```

- [ ] **Step 5: Write `.github/workflows/ops-lint.yml`**

```yaml
name: ops-lint

on:
  pull_request:
    paths: ["ops/**", ".github/workflows/ops-lint.yml"]
  workflow_dispatch:

permissions:
  contents: read

jobs:
  lint:
    runs-on: ubuntu-24.04
    defaults:
      run:
        working-directory: ./ops
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Install tooling
        run: pip install "ansible>=9" ansible-lint yamllint
      - name: Install collections
        run: ansible-galaxy collection install -r requirements.yml -p ./collections
      - name: yamllint
        run: yamllint .
      - name: ansible-lint
        run: ansible-lint .
      - name: syntax-check playbooks + tests
        run: |
          shopt -s nullglob
          for p in playbooks/*.yml tests/test_*.yml; do
            echo "== $p =="
            ansible-playbook --syntax-check "$p"
          done
```

- [ ] **Step 6: Create the tests dir placeholder**

`ops/tests/.gitkeep` (empty file).

- [ ] **Step 7: Install tooling locally and verify lint runs clean on the scaffold**

```bash
cd ops
pip install "ansible>=9" ansible-lint yamllint
ansible-galaxy collection install -r requirements.yml -p ./collections
yamllint . && ansible-lint .
```
Expected: both exit 0 (nothing to lint yet beyond config files; no errors).

- [ ] **Step 8: Commit**

```bash
git add ops/ansible.cfg ops/requirements.yml ops/.yamllint ops/.ansible-lint ops/tests/.gitkeep .github/workflows/ops-lint.yml
git commit -m "build(ops): ansible scaffold + Layer-0 lint CI"
```

---

### Task 2: Inventory, vars, secrets template (prod + staging)

**Files:**
- Create: `ops/inventories/prod/hosts.yml`, `ops/inventories/prod/group_vars/all.yml`, `ops/inventories/prod/host_vars/bridge-host-1.yml`, `ops/inventories/prod/secrets.example.yml`
- Create: staging mirror under `ops/inventories/staging/`
- Modify: `.gitignore` (append `secrets.yml`)
- Test: `ops/tests/test_inventory.yml`

- [ ] **Step 1: Write the failing inventory assertion test `ops/tests/test_inventory.yml`**

```yaml
---
- name: Inventory structure assertions
  hosts: localhost
  connection: local
  gather_facts: false
  vars:
    expected_groups:
      - controller
      - deployer_hosts
      - minio_hosts
      - relayer_hosts
      - gas_oracle_hosts
      - monitoring_hosts
      - warp_ui_hosts
  tasks:
    - name: Every singleton group is defined
      ansible.builtin.assert:
        that: "item in groups"
        fail_msg: "Inventory group '{{ item }}' missing"
      loop: "{{ expected_groups }}"

    - name: stack_env_vars map covers every deployable stack
      ansible.builtin.assert:
        that: "item in stack_env_vars"
        fail_msg: "stack_env_vars missing key '{{ item }}'"
      loop:
        - hyperlane-minio
        - hyperlane-svm-deployer
        - hyperlane-relayer
        - hyperlane-gas-oracle
        - hyperlane-monitoring
        - hyperlane-warp-ui
        - hyperlane-validator
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd ops
ansible-playbook -i inventories/prod/hosts.yml tests/test_inventory.yml
```
Expected: FAIL — `groups` lacks the named groups / `stack_env_vars` undefined.

- [ ] **Step 3: Write `ops/inventories/prod/hosts.yml`**

```yaml
---
all:
  children:
    controller:
      hosts:
        localhost:
          ansible_connection: local
    deployer_hosts:
      hosts:
        bridge-host-1:
    minio_hosts:
      hosts:
        bridge-host-1:
    relayer_hosts:
      hosts:
        bridge-host-1:
    gas_oracle_hosts:
      hosts:
        bridge-host-1:
    monitoring_hosts:
      hosts:
        bridge-host-1:
    warp_ui_hosts:
      hosts:
        bridge-host-1:
```

- [ ] **Step 4: Write `ops/inventories/prod/group_vars/all.yml`**

```yaml
---
# --- Environment wiring ---
bridge_name: default
deployment_root: "{{ playbook_dir }}/../../../deployment"   # prod = repo deployment/
repo_root: "{{ playbook_dir }}/../../.."

# --- laconic-so install (prerequisites_user) ---
laconic_so_version: "v1.1.0-b3e9366-202605111309"
laconic_so_release_base_url: "https://github.com/cerc-io/stack-orchestrator/releases/download"

# --- Chain config (canonical chain-specific vars; see CLAUDE.md) ---
gorchain_rpc_url: "https://gorchain-rpc.bridge.gorbagana.wtf"
solana_rpc_url: "REPLACE_WITH_HELIUS_MAINNET_URL"
gorchain_domain_id: 99999
solana_domain_id: 99998
gorchain_chain_id: 99999
solana_chain_id: 101

# --- Ownership / wallets (public values only) ---
hardware_wallet_pubkey: "REPLACE_WITH_HW_WALLET_PUBKEY"

# --- DNS ---
dns_zone: bridge.gorbagana.wtf
dns_record_ttl: 300
dns_records:
  - { name: s3, host: bridge-host-1 }
  - { name: minio-console, host: bridge-host-1 }
  - { name: grafana, host: bridge-host-1 }
  - { name: prometheus, host: bridge-host-1 }
  - { name: warp-ui, host: bridge-host-1 }
  - { name: relayer, host: bridge-host-1 }

# --- Per-stack env-var maps ---
# Each key is a stack name; the value lists the env var NAMES that stack's
# spec consumes. stack_deploy resolves each NAME to the same-named ansible
# variable (from group_vars config or secrets.yml) when building the shell
# environment for `laconic-so`. Keep in sync with deployment/spec-*.yml.
stack_env_vars:
  hyperlane-minio:
    - MINIO_ROOT_USER
    - MINIO_ROOT_PASSWORD
    - MINIO_USERS
  hyperlane-svm-deployer:
    - GORCHAIN_RPC_URL
    - SOLANA_RPC_URL
    - GORCHAIN_DOMAIN_ID
    - SOLANA_DOMAIN_ID
    - HARDWARE_WALLET_PUBKEY
  hyperlane-relayer:
    - GORCHAIN_RPC_URL
    - SOLANA_RPC_URL
  hyperlane-gas-oracle:
    - GORCHAIN_RPC_URL
    - SOLANA_RPC_URL
    - PRIVY_APP_ID
    - PRIVY_APP_SECRET
  hyperlane-monitoring: []
  hyperlane-warp-ui:
    - GORCHAIN_RPC_URL
    - SOLANA_RPC_URL
  hyperlane-validator:
    - GORCHAIN_RPC_URL
    - SOLANA_RPC_URL
    - PRIVY_APP_ID
    - PRIVY_APP_SECRET
```

> The env-var *names* above are the union each spec declares today; when a
> compose/spec env var is added or renamed, update this map (CLAUDE.md "Keep in
> sync" rule). Per-label MinIO creds (`<LABEL>_KEY_ID/_SECRET`) are injected by
> the `credentials` role, not listed here.

- [ ] **Step 5: Write `ops/inventories/prod/host_vars/bridge-host-1.yml`**

```yaml
---
public_ip: "REPLACE_WITH_HOST_PUBLIC_IP"
privileged_user: ubuntu
deploy_user: ubuntu
kind_mount_root: /srv/kind/hyperlane
ansible_user: ubuntu
```

- [ ] **Step 6: Write `ops/inventories/prod/secrets.example.yml`**

```yaml
---
# Copy to secrets.yml (gitignored) and fill in. Operator-supplied secrets that
# cannot be generated. Generated secrets (MinIO root/IAM, relayer key) are
# written here automatically by the `credentials` role and need not be set.

# --- Operator-supplied (REQUIRED) ---
cloudflare_api_token: ""        # scoped to dns_zone records
privy_app_id: ""
privy_app_secret: ""

# --- Generated by `credentials` role (leave unset on first run) ---
# minio_root_user: ""
# minio_root_password: ""
# minio_iam: {}                 # { "<label>": { key_id, secret } }
# relayer_keypair: ""
```

- [ ] **Step 7: Mirror the staging tree**

Create `ops/inventories/staging/hosts.yml` with the three staging hosts from the
staging design (`staging-gorchain`, `staging-solana-validator`,
`staging-bridge-ops`) — put `deployer_hosts`/`minio_hosts`/`relayer_hosts`/
`gas_oracle_hosts`/`monitoring_hosts`/`warp_ui_hosts` on `staging-bridge-ops`,
plus `controller: localhost`. Create `ops/inventories/staging/group_vars/all.yml`
identical to prod's **except** `deployment_root: "{{ playbook_dir }}/../../../deployment/staging"`,
`dns_zone: staging.bridge.gorbagana.wtf`, `solana_rpc_url` pointing at the Helius
**devnet** URL, and `dns_records[].host` set to `staging-bridge-ops`. Create
`ops/inventories/staging/host_vars/staging-bridge-ops.yml` and the other two host
files (same keys as prod's host_vars). Copy `secrets.example.yml` verbatim.

- [ ] **Step 8: Append `secrets.yml` to `.gitignore`**

Add to the repo-root `.gitignore`:
```
# ops: per-env operator + generated secrets (never commit)
ops/inventories/*/secrets.yml
```

- [ ] **Step 9: Run the assertion test — verify it passes**

```bash
cd ops
ansible-playbook -i inventories/prod/hosts.yml tests/test_inventory.yml
ansible-playbook -i inventories/staging/hosts.yml tests/test_inventory.yml
```
Expected: both PASS (all groups present, stack_env_vars complete).

- [ ] **Step 10: Lint + commit**

```bash
cd ops && yamllint . && ansible-lint .
cd .. && git add ops/inventories .gitignore ops/tests/test_inventory.yml
git commit -m "feat(ops): inventory, vars, and secrets template for prod + staging"
```

---

### Task 3: `validators.yaml` loader + derived facts

Derives `MINIO_USERS`, the per-label upper-case env prefixes, and validator DNS
records from `deployment/[staging/]bridges/default/operator/validators.yaml`.

**Files:**
- Create: `ops/roles/common/tasks/load_validators.yml`
- Create: `ops/tests/fixtures/validators.yaml`, `ops/tests/test_validators.yml`

- [ ] **Step 1: Write the fixture `ops/tests/fixtures/validators.yaml`**

```yaml
---
validators:
  - label: gorchain-primary
    chain: gorchain
    host: bridge-host-1
    privy_wallet_id: priv_aaa
    hostname: validator-gorchain.bridge.gorbagana.wtf
  - label: solana-primary
    chain: solana
    host: bridge-host-1
    privy_wallet_id: priv_bbb
    hostname: validator-solana.bridge.gorbagana.wtf
```

- [ ] **Step 2: Write the failing test `ops/tests/test_validators.yml`**

```yaml
---
- name: validators.yaml derivation assertions
  hosts: localhost
  connection: local
  gather_facts: false
  vars:
    validators_file: "{{ playbook_dir }}/fixtures/validators.yaml"
  tasks:
    - name: Load + derive
      ansible.builtin.include_tasks: ../roles/common/tasks/load_validators.yml

    - name: MINIO_USERS is the comma-joined label list
      ansible.builtin.assert:
        that: "minio_users == 'gorchain-primary,solana-primary'"
        fail_msg: "got '{{ minio_users }}'"

    - name: Per-label env prefixes are upper-snake
      ansible.builtin.assert:
        that:
          - "'GORCHAIN_PRIMARY' in validator_env_prefixes"
          - "'SOLANA_PRIMARY' in validator_env_prefixes"

    - name: Validator DNS records resolve label hostnames to the right host
      ansible.builtin.assert:
        that:
          - "validator_dns_records | length == 2"
          - "validator_dns_records[0].name == 'validator-gorchain'"
          - "validator_dns_records[0].host == 'bridge-host-1'"
```

- [ ] **Step 3: Run to verify it fails**

```bash
cd ops && ansible-playbook -i inventories/prod/hosts.yml tests/test_validators.yml
```
Expected: FAIL — `load_validators.yml` does not exist.

- [ ] **Step 4: Write `ops/roles/common/tasks/load_validators.yml`**

```yaml
---
# Loads validators.yaml and sets derived facts:
#   validators                -> raw list
#   minio_users               -> "label1,label2"
#   validator_env_prefixes    -> ["LABEL1", "LABEL2"] (upper-snake of label)
#   validator_dns_records     -> [{name, host}] for validator-* hostnames
# Input var: validators_file (path). Defaults to the env's operator file.
- name: Resolve validators_file default
  ansible.builtin.set_fact:
    validators_file: "{{ validators_file | default(deployment_root ~ '/bridges/' ~ bridge_name ~ '/operator/validators.yaml') }}"

- name: Read validators.yaml
  ansible.builtin.set_fact:
    validators: "{{ (lookup('ansible.builtin.file', validators_file) | from_yaml)['validators'] }}"

- name: Derive MINIO_USERS
  ansible.builtin.set_fact:
    minio_users: "{{ validators | map(attribute='label') | join(',') }}"

- name: Derive per-label env prefixes
  ansible.builtin.set_fact:
    validator_env_prefixes: "{{ validators | map(attribute='label') | map('upper') | map('replace', '-', '_') | list }}"

- name: Derive validator DNS records
  ansible.builtin.set_fact:
    validator_dns_records: >-
      {{ validators
         | map(attribute='hostname')
         | map('regex_replace', '\.' ~ (dns_zone | regex_escape) ~ '$', '')
         | zip(validators | map(attribute='host'))
         | map('zip', ['name', 'host'])
         | map('map', 'reverse')
         | map('community.general.dict')
         | list }}"
```

- [ ] **Step 5: Run to verify it passes**

```bash
cd ops && ansible-playbook -i inventories/prod/hosts.yml tests/test_validators.yml
```
Expected: PASS. If the `validator_dns_records` transform errors, replace the last
task with the explicit loop form:
```yaml
- name: Derive validator DNS records (loop form)
  ansible.builtin.set_fact:
    validator_dns_records: "{{ validator_dns_records | default([]) + [{'name': item.hostname | regex_replace('\\.' ~ dns_zone ~ '$', ''), 'host': item.host}] }}"
  loop: "{{ validators }}"
```

- [ ] **Step 6: Lint + commit**

```bash
cd ops && yamllint . && ansible-lint .
cd .. && git add ops/roles/common ops/tests/fixtures/validators.yaml ops/tests/test_validators.yml
git commit -m "feat(ops): validators.yaml loader with MINIO_USERS + DNS derivation"
```

---

### Task 4: `prerequisites_privileged` role

**Files:**
- Create: `ops/roles/prerequisites_privileged/tasks/main.yml`, `ops/roles/prerequisites_privileged/defaults/main.yml`

- [ ] **Step 1: Write `ops/roles/prerequisites_privileged/defaults/main.yml`**

```yaml
---
kind_version: v0.23.0
kubectl_version: v1.30.0
```

- [ ] **Step 2: Write `ops/roles/prerequisites_privileged/tasks/main.yml`**

```yaml
---
- name: Install prerequisite apt packages
  ansible.builtin.apt:
    name: [ca-certificates, curl, gnupg, git]
    state: present
    update_cache: true
  become: true

- name: Add Docker apt repository key
  ansible.builtin.get_url:
    url: https://download.docker.com/linux/ubuntu/gpg
    dest: /etc/apt/keyrings/docker.asc
    mode: "0644"
  become: true

- name: Add Docker apt repository
  ansible.builtin.apt_repository:
    repo: "deb [arch={{ 'amd64' if ansible_architecture == 'x86_64' else 'arm64' }} signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu {{ ansible_distribution_release }} stable"
    state: present
  become: true

- name: Install Docker engine
  ansible.builtin.apt:
    name: [docker-ce, docker-ce-cli, containerd.io, docker-compose-plugin]
    state: present
    update_cache: true
  become: true

- name: Add deploy_user to docker group
  ansible.builtin.user:
    name: "{{ deploy_user }}"
    groups: docker
    append: true
  become: true

- name: Install kind binary
  ansible.builtin.get_url:
    url: "https://kind.sigs.k8s.io/dl/{{ kind_version }}/kind-linux-{{ 'amd64' if ansible_architecture == 'x86_64' else 'arm64' }}"
    dest: /usr/local/bin/kind
    mode: "0755"
  become: true

- name: Install kubectl binary
  ansible.builtin.get_url:
    url: "https://dl.k8s.io/release/{{ kubectl_version }}/bin/linux/{{ 'amd64' if ansible_architecture == 'x86_64' else 'arm64' }}/kubectl"
    dest: /usr/local/bin/kubectl
    mode: "0755"
  become: true
```

- [ ] **Step 3: Lint + syntax via a temporary wrapper, then check-mode on localhost**

Create a throwaway `ops/tests/_role_smoke.yml` (deleted at end of step) to syntax-check the role:
```yaml
---
- hosts: localhost
  connection: local
  gather_facts: true
  roles:
    - prerequisites_privileged
```
Run:
```bash
cd ops
ansible-playbook --syntax-check tests/_role_smoke.yml
ansible-lint roles/prerequisites_privileged
rm tests/_role_smoke.yml
```
Expected: syntax-check OK, ansible-lint clean. (Actual install is verified on a VM in Task 16's Layer-1 run — see spec.)

- [ ] **Step 4: Commit**

```bash
git add ops/roles/prerequisites_privileged
git commit -m "feat(ops): prerequisites_privileged role (docker, kind, kubectl)"
```

---

### Task 5: `prerequisites_user` role

**Files:**
- Create: `ops/roles/prerequisites_user/tasks/main.yml`, `ops/roles/prerequisites_user/defaults/main.yml`

- [ ] **Step 1: Write `ops/roles/prerequisites_user/defaults/main.yml`**

```yaml
---
credentials_dir: "{{ ansible_env.HOME }}/.credentials/hyperlane"
```

- [ ] **Step 2: Write `ops/roles/prerequisites_user/tasks/main.yml`**

```yaml
---
- name: Install laconic-so release binary
  ansible.builtin.get_url:
    url: "{{ laconic_so_release_base_url }}/{{ laconic_so_version }}/laconic-so"
    dest: "{{ ansible_env.HOME }}/bin/laconic-so"
    mode: "0755"
  # Operator confirms the exact release source for the pinned SO build; the URL
  # template above matches cerc-io's release asset naming.

- name: Ensure ~/bin on PATH for non-interactive shells
  ansible.builtin.lineinfile:
    path: "{{ ansible_env.HOME }}/.profile"
    line: 'export PATH="$HOME/bin:$PATH"'
    create: true
    mode: "0644"

- name: Create credentials dir (0700)
  ansible.builtin.file:
    path: "{{ credentials_dir }}"
    state: directory
    mode: "0700"

- name: Create kind_mount_root owned by deploy_user
  ansible.builtin.file:
    path: "{{ kind_mount_root }}"
    state: directory
    mode: "0755"
    owner: "{{ deploy_user }}"
    group: "{{ deploy_user }}"
  become: true
```

- [ ] **Step 3: Syntax-check + lint (same throwaway-wrapper technique as Task 4)**

```bash
cd ops
printf -- '---\n- hosts: localhost\n  connection: local\n  roles:\n    - prerequisites_user\n' > tests/_role_smoke.yml
ansible-playbook --syntax-check tests/_role_smoke.yml
ansible-lint roles/prerequisites_user
rm tests/_role_smoke.yml
```
Expected: syntax OK, lint clean.

- [ ] **Step 4: Commit**

```bash
git add ops/roles/prerequisites_user
git commit -m "feat(ops): prerequisites_user role (laconic-so, credentials dir, mount root)"
```

---

### Task 6: `bootstrap-host.yml` playbook

**Files:**
- Create: `ops/playbooks/bootstrap-host.yml`

- [ ] **Step 1: Write `ops/playbooks/bootstrap-host.yml`**

```yaml
---
# Two plays: privileged host setup, then unprivileged deploy_user setup.
# Target a host/group with: -e target=<host-or-group>  (default: all)
- name: Privileged host prerequisites
  hosts: "{{ target | default('all') }}"
  become: true
  remote_user: "{{ privileged_user }}"
  roles:
    - prerequisites_privileged

- name: Unprivileged deploy_user prerequisites
  hosts: "{{ target | default('all') }}"
  remote_user: "{{ deploy_user }}"
  roles:
    - prerequisites_user
```

- [ ] **Step 2: Syntax-check**

```bash
cd ops && ansible-playbook --syntax-check playbooks/bootstrap-host.yml && ansible-lint playbooks/bootstrap-host.yml
```
Expected: OK / clean.

- [ ] **Step 3: Commit**

```bash
git add ops/playbooks/bootstrap-host.yml
git commit -m "feat(ops): bootstrap-host playbook (privileged + user plays)"
```

---

### Task 7: `dns_cloudflare` role + `configure-dns.yml` precursor

**Files:**
- Create: `ops/roles/dns_cloudflare/tasks/main.yml`, `ops/roles/dns_cloudflare/defaults/main.yml`
- Create: `ops/tests/test_dns_expansion.yml`

- [ ] **Step 1: Write the failing DNS-expansion test `ops/tests/test_dns_expansion.yml`**

```yaml
---
- name: DNS record expansion assertions
  hosts: localhost
  connection: local
  gather_facts: false
  vars:
    validators_file: "{{ playbook_dir }}/fixtures/validators.yaml"
    dns_zone: bridge.gorbagana.wtf
    dns_records:
      - { name: s3, host: bridge-host-1 }
      - { name: relayer, host: bridge-host-1 }
    host_ip_map:
      bridge-host-1: "203.0.113.10"
  tasks:
    - ansible.builtin.include_tasks: ../roles/common/tasks/load_validators.yml
    - name: Build the full record set (static + validators) with resolved IPs
      ansible.builtin.set_fact:
        all_records: >-
          {{ (dns_records + validator_dns_records)
             | map('combine', {'value_lookup': true}) | list }}
        resolved: >-
          {{ (dns_records + validator_dns_records)
             | map(attribute='host') | map('extract', host_ip_map) | list }}
    - name: Static + 2 validator records present, all IPs resolved
      ansible.builtin.assert:
        that:
          - "(dns_records + validator_dns_records) | length == 4"
          - "resolved | unique == ['203.0.113.10']"
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd ops && ansible-playbook -i inventories/prod/hosts.yml tests/test_dns_expansion.yml
```
Expected: FAIL (initially because `host_ip_map` extract / combine shapes need the
role's helper). If it passes immediately (pure-Jinja), proceed — the test still
guards the expansion contract the role depends on.

- [ ] **Step 3: Write `ops/roles/dns_cloudflare/defaults/main.yml`**

```yaml
---
dns_record_ttl: 300
dns_proxied: false
```

- [ ] **Step 4: Write `ops/roles/dns_cloudflare/tasks/main.yml`**

```yaml
---
- name: Require Cloudflare token
  ansible.builtin.assert:
    that: "cloudflare_api_token | default('') | length > 0"
    fail_msg: "cloudflare_api_token missing — set it in secrets.yml"

- name: Load validator-derived DNS records
  ansible.builtin.include_tasks: ../common/tasks/load_validators.yml

- name: Build host -> public_ip map from host_vars
  ansible.builtin.set_fact:
    host_ip_map: "{{ host_ip_map | default({}) | combine({item: hostvars[item].public_ip}) }}"
  loop: "{{ (dns_records + validator_dns_records) | map(attribute='host') | unique }}"

- name: Reconcile A records (additive)
  community.general.cloudflare_dns:
    api_token: "{{ cloudflare_api_token }}"
    zone: "{{ dns_zone }}"
    record: "{{ item.name }}"
    type: A
    value: "{{ host_ip_map[item.host] }}"
    ttl: "{{ dns_record_ttl }}"
    proxied: "{{ dns_proxied }}"
    state: present
  loop: "{{ dns_records + validator_dns_records }}"
  loop_control:
    label: "{{ item.name }}.{{ dns_zone }}"
  when: not ansible_check_mode
```

- [ ] **Step 5: Run the expansion test — verify pass**

```bash
cd ops && ansible-playbook -i inventories/prod/hosts.yml tests/test_dns_expansion.yml && ansible-lint roles/dns_cloudflare
```
Expected: PASS, lint clean.

- [ ] **Step 6: Commit**

```bash
git add ops/roles/dns_cloudflare ops/tests/test_dns_expansion.yml
git commit -m "feat(ops): dns_cloudflare role with additive A-record reconciliation"
```

---

### Task 8: `configure-dns.yml` playbook

**Files:**
- Create: `ops/playbooks/configure-dns.yml`

- [ ] **Step 1: Write `ops/playbooks/configure-dns.yml`**

```yaml
---
- name: Reconcile Cloudflare DNS from the controller
  hosts: controller
  gather_facts: false
  vars_files:
    - "{{ inventory_dir }}/secrets.yml"
  roles:
    - dns_cloudflare
```

> `inventory_dir` resolves to `ops/inventories/<env>/` because the playbook is run
> with `-i inventories/<env>/hosts.yml`. `host_vars` for the referenced hosts are
> read via `hostvars[...]` inside the role even though the play targets only
> `controller`.

- [ ] **Step 2: Syntax-check**

```bash
cd ops && ansible-playbook --syntax-check playbooks/configure-dns.yml && ansible-lint playbooks/configure-dns.yml
```
Expected: OK / clean.

- [ ] **Step 3: Commit**

```bash
git add ops/playbooks/configure-dns.yml
git commit -m "feat(ops): configure-dns playbook"
```

---

### Task 9: `credentials` role — generation half

**Files:**
- Create: `ops/roles/credentials/tasks/generate.yml`, `ops/roles/credentials/defaults/main.yml`
- Create: `ops/tests/test_credentials_idempotent.yml`

- [ ] **Step 1: Write `ops/roles/credentials/defaults/main.yml`**

```yaml
---
# Path to the gitignored secrets file for the active env.
secrets_file: "{{ inventory_dir }}/secrets.yml"
```

- [ ] **Step 2: Write the failing idempotency test `ops/tests/test_credentials_idempotent.yml`**

```yaml
---
- name: credentials generation is idempotent (no rotation on re-run)
  hosts: localhost
  connection: local
  gather_facts: false
  vars:
    secrets_file: "/tmp/ops-test-secrets.yml"
    validators_file: "{{ playbook_dir }}/fixtures/validators.yaml"
  tasks:
    - name: Start from empty secrets
      ansible.builtin.copy:
        content: "---\n{}\n"
        dest: "{{ secrets_file }}"
        mode: "0600"

    - name: First generation
      ansible.builtin.include_tasks: ../roles/credentials/tasks/generate.yml

    - name: Capture generated root password
      ansible.builtin.set_fact:
        first_pw: "{{ (lookup('ansible.builtin.file', secrets_file) | from_yaml).minio_root_password }}"

    - name: Second generation (should be a no-op)
      ansible.builtin.include_tasks: ../roles/credentials/tasks/generate.yml

    - name: Password unchanged across runs
      ansible.builtin.assert:
        that: "(lookup('ansible.builtin.file', secrets_file) | from_yaml).minio_root_password == first_pw"
        fail_msg: "credentials were rotated on re-run"

    - name: One IAM pair per validator label
      ansible.builtin.assert:
        that:
          - "(lookup('ansible.builtin.file', secrets_file) | from_yaml).minio_iam.keys() | list | sort == ['gorchain-primary', 'solana-primary']"
```

- [ ] **Step 3: Run to verify it fails**

```bash
cd ops && ansible-playbook -i inventories/prod/hosts.yml tests/test_credentials_idempotent.yml
```
Expected: FAIL — `generate.yml` does not exist.

- [ ] **Step 4: Write `ops/roles/credentials/tasks/generate.yml`**

```yaml
---
# Generates secrets we can, writing them into secrets_file ONLY if absent.
- name: Load current secrets
  ansible.builtin.set_fact:
    _secrets: "{{ lookup('ansible.builtin.file', secrets_file) | from_yaml | default({}, true) }}"

- name: Derive validator labels
  ansible.builtin.include_tasks: ../common/tasks/load_validators.yml

- name: Generate MinIO root user if absent
  ansible.builtin.set_fact:
    _secrets: "{{ _secrets | combine({'minio_root_user': 'hyperlane-root'}) }}"
  when: "'minio_root_user' not in _secrets"

- name: Generate MinIO root password if absent
  ansible.builtin.set_fact:
    _secrets: "{{ _secrets | combine({'minio_root_password': lookup('ansible.builtin.password', '/dev/null length=32 chars=ascii_letters,digits')}) }}"
  when: "'minio_root_password' not in _secrets"

- name: Generate per-validator IAM pairs if absent
  ansible.builtin.set_fact:
    _secrets: "{{ _secrets | combine({'minio_iam': (_secrets.minio_iam | default({})) | combine({item.label: {'key_id': lookup('ansible.builtin.password', '/dev/null length=20 chars=ascii_uppercase,digits'), 'secret': lookup('ansible.builtin.password', '/dev/null length=40 chars=ascii_letters,digits')}})}) }}"
  loop: "{{ validators }}"
  loop_control:
    label: "{{ item.label }}"
  when: "item.label not in (_secrets.minio_iam | default({}))"

- name: Persist secrets (0600)
  ansible.builtin.copy:
    content: "{{ _secrets | to_nice_yaml }}"
    dest: "{{ secrets_file }}"
    mode: "0600"
```

- [ ] **Step 5: Run to verify pass**

```bash
cd ops && ansible-playbook -i inventories/prod/hosts.yml tests/test_credentials_idempotent.yml
rm -f /tmp/ops-test-secrets.yml
```
Expected: PASS (password stable, one IAM pair per label).

- [ ] **Step 6: Lint + commit**

```bash
cd ops && ansible-lint roles/credentials
cd .. && git add ops/roles/credentials/defaults ops/roles/credentials/tasks/generate.yml ops/tests/test_credentials_idempotent.yml
git commit -m "feat(ops): credentials generation (idempotent MinIO root + IAM)"
```

---

### Task 10: `credentials` role — distribute half + required-key assertion

**Files:**
- Create: `ops/roles/credentials/tasks/distribute.yml`, `ops/roles/credentials/tasks/main.yml`
- Create: `ops/tests/test_credentials_required.yml`

- [ ] **Step 1: Write the failing required-keys test `ops/tests/test_credentials_required.yml`**

```yaml
---
- name: missing required external secret fails fast
  hosts: localhost
  connection: local
  gather_facts: false
  vars:
    secrets_file: "/tmp/ops-test-secrets-req.yml"
    validators_file: "{{ playbook_dir }}/fixtures/validators.yaml"
    distribute_only_assert: true
  tasks:
    - name: Secrets missing cloudflare_api_token
      ansible.builtin.copy:
        content: "privy_app_id: x\nprivy_app_secret: y\n"
        dest: "{{ secrets_file }}"
        mode: "0600"

    - name: Expect assertion failure
      block:
        - ansible.builtin.include_tasks: ../roles/credentials/tasks/distribute.yml
        - ansible.builtin.set_fact: { req_failed: false }
      rescue:
        - ansible.builtin.set_fact: { req_failed: true }

    - ansible.builtin.assert:
        that: "req_failed | bool"
        fail_msg: "distribute did not fail on missing cloudflare_api_token"
```

- [ ] **Step 2: Run to verify it fails**

```bash
cd ops && ansible-playbook -i inventories/prod/hosts.yml tests/test_credentials_required.yml
```
Expected: FAIL — `distribute.yml` missing.

- [ ] **Step 3: Write `ops/roles/credentials/tasks/distribute.yml`**

```yaml
---
- name: Load secrets
  ansible.builtin.set_fact:
    _secrets: "{{ lookup('ansible.builtin.file', secrets_file) | from_yaml | default({}, true) }}"

- name: Required operator-supplied secrets are present
  ansible.builtin.assert:
    that: "item in _secrets and (_secrets[item] | length > 0)"
    fail_msg: "Required secret '{{ item }}' missing from {{ secrets_file }}"
  loop:
    - cloudflare_api_token
    - privy_app_id
    - privy_app_secret

- name: Stop here when only asserting (used by tests)
  ansible.builtin.meta: end_play
  when: distribute_only_assert | default(false) | bool

- name: Derive labels
  ansible.builtin.include_tasks: ../common/tasks/load_validators.yml

- name: Drop per-validator MinIO cred files on each validator host
  ansible.builtin.copy:
    content: "{{ _secrets.minio_iam[item.label].key_id }}"
    dest: "{{ hostvars[item.host].kind_mount_root | default('/srv/kind/hyperlane') }}/../.credentials/hyperlane/{{ item.label }}-minio.key_id"
    mode: "0600"
  delegate_to: "{{ item.host }}"
  loop: "{{ validators }}"
  loop_control: { label: "{{ item.label }}" }
  when: not ansible_check_mode
```

> The MinIO secret-env assembly (`MINIO_USERS` + per-label `<PREFIX>_KEY_ID/_SECRET`)
> is consumed by `stack_deploy` for the MinIO stack via the `credentials` facts;
> Task 12 wires those names into the env map. This task only persists/distributes.

- [ ] **Step 4: Write `ops/roles/credentials/tasks/main.yml`**

```yaml
---
- ansible.builtin.import_tasks: generate.yml
- ansible.builtin.import_tasks: distribute.yml
```

- [ ] **Step 5: Run to verify pass**

```bash
cd ops && ansible-playbook -i inventories/prod/hosts.yml tests/test_credentials_required.yml
rm -f /tmp/ops-test-secrets-req.yml
ansible-lint roles/credentials
```
Expected: PASS (assertion fired), lint clean.

- [ ] **Step 6: Commit**

```bash
git add ops/roles/credentials/tasks/distribute.yml ops/roles/credentials/tasks/main.yml ops/tests/test_credentials_required.yml
git commit -m "feat(ops): credentials distribution + required-secret assertion"
```

---

### Task 11: `distribute-credentials.yml` playbook

**Files:**
- Create: `ops/playbooks/distribute-credentials.yml`

- [ ] **Step 1: Write `ops/playbooks/distribute-credentials.yml`**

```yaml
---
- name: Generate + distribute credentials
  hosts: controller
  gather_facts: false
  vars_files:
    - "{{ inventory_dir }}/secrets.yml"
  roles:
    - credentials
```

- [ ] **Step 2: Syntax-check + lint**

```bash
cd ops && ansible-playbook --syntax-check playbooks/distribute-credentials.yml && ansible-lint playbooks/distribute-credentials.yml
```
Expected: OK / clean.

- [ ] **Step 3: Commit**

```bash
git add ops/playbooks/distribute-credentials.yml
git commit -m "feat(ops): distribute-credentials playbook"
```

---

### Task 12: `stack_deploy` role — env assembly + preflight

**Files:**
- Create: `ops/roles/stack_deploy/tasks/preflight.yml`, `ops/roles/stack_deploy/defaults/main.yml`
- Create: `ops/tests/test_stack_env.yml`

- [ ] **Step 1: Write `ops/roles/stack_deploy/defaults/main.yml`**

```yaml
---
# Caller sets: stack_name, spec_file (absolute), stack_path (absolute).
deploy_base: "{{ kind_mount_root }}/deployments"
deployment_id: "{{ stack_name }}"
```

- [ ] **Step 2: Write the failing env-assembly test `ops/tests/test_stack_env.yml`**

```yaml
---
- name: stack env assembly resolves names to values
  hosts: localhost
  connection: local
  gather_facts: false
  vars:
    GORCHAIN_RPC_URL: "https://gorchain.example"
    SOLANA_RPC_URL: "https://solana.example"
    MINIO_ROOT_USER: "root"
    MINIO_ROOT_PASSWORD: "pw"
    MINIO_USERS: "a,b"
    stack_env_vars:
      hyperlane-minio: [MINIO_ROOT_USER, MINIO_ROOT_PASSWORD, MINIO_USERS]
      hyperlane-relayer: [GORCHAIN_RPC_URL, SOLANA_RPC_URL]
  tasks:
    - name: Assemble env for minio
      ansible.builtin.set_fact:
        stack_env: "{{ dict(stack_env_vars['hyperlane-minio'] | zip(stack_env_vars['hyperlane-minio'] | map('extract', vars) | list)) }}"
    - ansible.builtin.assert:
        that:
          - "stack_env.MINIO_ROOT_USER == 'root'"
          - "stack_env.MINIO_USERS == 'a,b'"
          - "stack_env | length == 3"
```

- [ ] **Step 3: Run to verify it passes (pure-Jinja contract check)**

```bash
cd ops && ansible-playbook -i inventories/prod/hosts.yml tests/test_stack_env.yml
```
Expected: PASS. This locks the `dict(names | zip(names | map('extract', vars)))`
idiom the role uses. (If it fails, the role must not ship with that idiom.)

- [ ] **Step 4: Write `ops/roles/stack_deploy/tasks/preflight.yml`**

```yaml
---
- name: laconic-so is installed
  ansible.builtin.command: laconic-so version
  changed_when: false
  when: not ansible_check_mode

- name: Expected hostnames resolve to this host's public IP
  ansible.builtin.command: "dig +short {{ item }}"
  register: _dig
  changed_when: false
  failed_when: "public_ip not in _dig.stdout"
  loop: "{{ stack_hostnames | default([]) }}"
  when:
    - not ansible_check_mode
    - stack_hostnames | default([]) | length > 0
```

- [ ] **Step 5: Run env test again + lint**

```bash
cd ops && ansible-playbook -i inventories/prod/hosts.yml tests/test_stack_env.yml && ansible-lint roles/stack_deploy
```
Expected: PASS / clean (role has defaults + preflight only so far).

- [ ] **Step 6: Commit**

```bash
git add ops/roles/stack_deploy/defaults ops/roles/stack_deploy/tasks/preflight.yml ops/tests/test_stack_env.yml
git commit -m "feat(ops): stack_deploy preflight + env-assembly contract"
```

---

### Task 13: `stack_deploy` role — deploy half

Mirrors the proven e2e sequence (`tests/e2e/lib/deploy.py`): `deploy init` →
overwrite spec → `deploy create` → patch `deployment-id` in `deployment.yml` →
`deployment start --perform-cluster-management`. Cluster sharing is automatic
because every spec sets `kind-cluster-name: hyperlane` (SO uses it as the cluster
name / `kind-hyperlane` context; verified at `deploy_k8s.py:144`). Idempotent:
skip init/create when `deployment.yml` already exists.

**Files:**
- Create: `ops/roles/stack_deploy/tasks/deploy.yml`, `ops/roles/stack_deploy/tasks/main.yml`

- [ ] **Step 1: Write `ops/roles/stack_deploy/tasks/deploy.yml`**

```yaml
---
- name: Resolve deploy dir
  ansible.builtin.set_fact:
    deploy_dir: "{{ deploy_base }}/{{ stack_name }}"
    init_spec: "{{ deploy_base }}/{{ stack_name }}-spec.yml"

- name: Already created?
  ansible.builtin.stat:
    path: "{{ deploy_dir }}/deployment.yml"
  register: _dep

- name: Assemble env from the per-stack map
  ansible.builtin.set_fact:
    stack_env: "{{ dict(stack_env_vars[stack_name] | zip(stack_env_vars[stack_name] | map('extract', vars) | list)) }}"

- name: Prepare (init + create) when not yet created
  when:
    - not _dep.stat.exists
    - not ansible_check_mode
  block:
    - name: Ensure deploy base exists
      ansible.builtin.file: { path: "{{ deploy_base }}", state: directory, mode: "0755" }

    - name: deploy init
      ansible.builtin.command:
        argv: [laconic-so, --stack, "{{ stack_path }}", deploy, init, --output, "{{ init_spec }}"]
      changed_when: true

    - name: Install our committed spec over the generated one
      ansible.builtin.copy:
        src: "{{ spec_file }}"
        dest: "{{ init_spec }}"
        remote_src: true
        mode: "0644"

    - name: deploy create
      ansible.builtin.command:
        argv: [laconic-so, --stack, "{{ stack_path }}", deploy, create, --spec-file, "{{ init_spec }}", --deployment-dir, "{{ deploy_dir }}"]
      changed_when: true

    - name: Patch human-readable deployment-id
      ansible.builtin.replace:
        path: "{{ deploy_dir }}/deployment.yml"
        regexp: '^(deployment-id:\s*).*$'
        replace: '\g<1>{{ deployment_id }}'

- name: deployment start (create-or-reuse cluster + Caddy)
  ansible.builtin.command:
    argv: [laconic-so, deployment, --dir, "{{ deploy_dir }}", start, --perform-cluster-management]
  changed_when: true
  when: not ansible_check_mode

- name: Wait for Job completion (deployer/warp-deployer specs only)
  ansible.builtin.command:
    argv: [kubectl, --context, kind-hyperlane, -n, "laconic-{{ stack_name }}", wait, --for=condition=complete, "job", --all, --timeout=1800s]
  changed_when: false
  when:
    - stack_is_job | default(false) | bool
    - not ansible_check_mode
```

- [ ] **Step 2: Write `ops/roles/stack_deploy/tasks/main.yml`**

```yaml
---
- ansible.builtin.import_tasks: preflight.yml
- ansible.builtin.import_tasks: deploy.yml
```

- [ ] **Step 3: Syntax-check via throwaway wrapper + lint**

```bash
cd ops
printf -- '---\n- hosts: localhost\n  connection: local\n  vars: {stack_name: hyperlane-minio, spec_file: /tmp/x.yml, stack_path: /tmp/s}\n  roles: [stack_deploy]\n' > tests/_role_smoke.yml
ansible-playbook --syntax-check tests/_role_smoke.yml
ansible-lint roles/stack_deploy
rm tests/_role_smoke.yml
```
Expected: syntax OK, lint clean. (Real deploy verified on a VM, Task 16 Layer 1.)

- [ ] **Step 4: Commit**

```bash
git add ops/roles/stack_deploy/tasks/deploy.yml ops/roles/stack_deploy/tasks/main.yml
git commit -m "feat(ops): stack_deploy deploy half (init/create/start, job-wait, idempotent)"
```

---

### Task 14: `state_distribute` role

**Files:**
- Create: `ops/roles/state_distribute/tasks/main.yml`, `ops/roles/state_distribute/defaults/main.yml`
- Create: `ops/tests/test_state_paths.yml`

- [ ] **Step 1: Write `ops/roles/state_distribute/defaults/main.yml`**

```yaml
---
state_repo_url: "git@github.com:gorbagana-dev/hyperlane-stacks.git"
state_repo_branch: main
state_repo_dir: "{{ ansible_env.HOME }}/hyperlane-stacks"
# Caller sets: stack_name, deploy_dir, configmap_names (list)
```

- [ ] **Step 2: Write the failing path-mapping test `ops/tests/test_state_paths.yml`**

```yaml
---
- name: generated-file -> configmap path mapping
  hosts: localhost
  connection: local
  gather_facts: false
  vars:
    deployment_root: "/repo/deployment"
    bridge_name: default
    deploy_dir: "/srv/deployments/hyperlane-relayer"
    configmap_names: [agent-config]
  tasks:
    - ansible.builtin.set_fact:
        src_dir: "{{ deployment_root }}/bridges/{{ bridge_name }}/generated"
        cm_targets: "{{ configmap_names | map('regex_replace', '^(.*)$', deploy_dir ~ '/configmaps/\\1') | list }}"
    - ansible.builtin.assert:
        that:
          - "src_dir == '/repo/deployment/bridges/default/generated'"
          - "cm_targets == ['/srv/deployments/hyperlane-relayer/configmaps/agent-config']"
```

- [ ] **Step 3: Run to verify pass (contract lock)**

```bash
cd ops && ansible-playbook -i inventories/prod/hosts.yml tests/test_state_paths.yml
```
Expected: PASS.

- [ ] **Step 4: Write `ops/roles/state_distribute/tasks/main.yml`**

```yaml
---
- name: git present on host
  ansible.builtin.command: git --version
  changed_when: false
  when: not ansible_check_mode

- name: Trust the git remote host key
  ansible.builtin.known_hosts:
    name: github.com
    key: "{{ lookup('ansible.builtin.pipe', 'ssh-keyscan github.com 2>/dev/null') }}"
    state: present
  when: not ansible_check_mode

- name: Pull bridge state repo (uses forwarded SSH agent)
  ansible.builtin.git:
    repo: "{{ state_repo_url }}"
    dest: "{{ state_repo_dir }}"
    version: "{{ state_repo_branch }}"
    accept_hostkey: true
  environment:
    GIT_SSH_COMMAND: "ssh -o ForwardAgent=yes"
  when: not ansible_check_mode

- name: Copy generated state into each configmap dir
  ansible.builtin.copy:
    src: "{{ state_repo_dir }}/{{ (deployment_root | regex_replace('^.*/deployment', 'deployment')) }}/bridges/{{ bridge_name }}/generated/"
    dest: "{{ deploy_dir }}/configmaps/{{ item }}/"
    remote_src: true
    mode: "0644"
  loop: "{{ configmap_names }}"
  when: not ansible_check_mode
```

- [ ] **Step 5: Lint + run path test again**

```bash
cd ops && ansible-lint roles/state_distribute && ansible-playbook -i inventories/prod/hosts.yml tests/test_state_paths.yml
```
Expected: clean / PASS.

- [ ] **Step 6: Commit**

```bash
git add ops/roles/state_distribute ops/tests/test_state_paths.yml
git commit -m "feat(ops): state_distribute role (agent-forwarded git pull -> configmaps)"
```

---

### Task 15: `commit-bridge-state.yml` playbook

**Files:**
- Create: `ops/playbooks/commit-bridge-state.yml`
- Create: `ops/tests/test_commit_scope.yml`

- [ ] **Step 1: Write the failing scope/skip test `ops/tests/test_commit_scope.yml`**

```yaml
---
- name: commit-bridge-state scopes add + skips when unchanged
  hosts: localhost
  connection: local
  gather_facts: false
  vars:
    work: /tmp/ops-commit-test
    generated_rel: "deployment/bridges/default/generated"
  tasks:
    - name: Fresh temp git repo with a generated file + an unrelated dirty file
      ansible.builtin.shell: |
        set -e
        rm -rf {{ work }} && mkdir -p {{ work }}/{{ generated_rel }}
        cd {{ work }} && git init -q && git config user.email t@t && git config user.name t
        echo '{"a":1}' > {{ generated_rel }}/program-ids.json
        echo unrelated > other.txt
        git add -A && git commit -qm init
        echo '{"a":2}' > {{ generated_rel }}/program-ids.json   # change generated
        echo dirty >> other.txt                                  # unrelated change
      changed_when: true

    - name: Scoped add + commit (mimics the playbook's core)
      ansible.builtin.shell: |
        cd {{ work }}
        git add {{ generated_rel }}
        git diff --cached --quiet && echo SKIP || git commit -qm "state update"
      register: _c
      changed_when: true

    - name: other.txt remains uncommitted (not swept in)
      ansible.builtin.shell: "cd {{ work }} && git status --porcelain other.txt"
      register: _st
      changed_when: false

    - ansible.builtin.assert:
        that:
          - "'other.txt' in _st.stdout"        # still dirty -> not committed
          - "'SKIP' not in _c.stdout"          # there WAS a generated change
```

- [ ] **Step 2: Run to verify pass (locks the scoping contract)**

```bash
cd ops && ansible-playbook -i inventories/prod/hosts.yml tests/test_commit_scope.yml
rm -rf /tmp/ops-commit-test
```
Expected: PASS.

- [ ] **Step 3: Write `ops/playbooks/commit-bridge-state.yml`**

```yaml
---
# Pull deployer-host state into the controller's working tree, then commit+push.
# Default: hands-off (auto commit + push). -e state_review=true to gate.
- name: Commit bridge state to git
  hosts: controller
  gather_facts: false
  vars:
    state_review: false
    generated_rel: "bridges/{{ bridge_name }}/generated"
  tasks:
    - name: Pull generated files from the deployer host into the working tree
      ansible.builtin.synchronize:
        src: "{{ hostvars[groups['deployer_hosts'][0]].kind_mount_root }}/deployments/hyperlane-svm-deployer/data/{{ generated_rel }}/"
        dest: "{{ deployment_root }}/{{ generated_rel }}/"
        mode: pull
      delegate_to: "{{ groups['deployer_hosts'][0] }}"

    - name: Stage only the generated paths
      ansible.builtin.command:
        argv: [git, -C, "{{ repo_root }}", add, "{{ (deployment_root ~ '/' ~ generated_rel) | realpath }}"]
      changed_when: true

    - name: Anything staged?
      ansible.builtin.command:
        argv: [git, -C, "{{ repo_root }}", diff, --cached, --quiet]
      register: _staged
      changed_when: false
      failed_when: false

    - name: Show the staged diff (always)
      ansible.builtin.command:
        argv: [git, -C, "{{ repo_root }}", diff, --cached]
      register: _diff
      changed_when: false
      when: _staged.rc != 0

    - ansible.builtin.debug: { var: _diff.stdout_lines }
      when: _staged.rc != 0

    - name: Review gate
      ansible.builtin.pause:
        prompt: "Press enter to commit + push the above, or Ctrl-C to abort"
      when:
        - _staged.rc != 0
        - state_review | bool

    - name: Commit
      ansible.builtin.command:
        argv: [git, -C, "{{ repo_root }}", commit, -m, "chore: update bridge state"]
      changed_when: true
      when: _staged.rc != 0

    - name: Push (current tracking branch, via forwarded agent)
      ansible.builtin.command:
        argv: [git, -C, "{{ repo_root }}", push]
      changed_when: true
      when: _staged.rc != 0
```

> The `synchronize` src path reflects the deployer stack's host-path layout
> (`{kind_mount_root}/deployments/hyperlane-svm-deployer/data/...`). Confirm the
> exact `generated/` location against `spec-deployer.yml`'s volume mounts on the
> first Layer-1 run and adjust the single `src:` line if needed.

- [ ] **Step 4: Syntax-check + lint + scope test**

```bash
cd ops && ansible-playbook --syntax-check playbooks/commit-bridge-state.yml && ansible-lint playbooks/commit-bridge-state.yml
ansible-playbook -i inventories/prod/hosts.yml tests/test_commit_scope.yml && rm -rf /tmp/ops-commit-test
```
Expected: OK / clean / PASS.

- [ ] **Step 5: Commit**

```bash
git add ops/playbooks/commit-bridge-state.yml ops/tests/test_commit_scope.yml
git commit -m "feat(ops): commit-bridge-state playbook (scoped add, skip-if-unchanged, flag-gated)"
```

---

### Task 16: Orchestration wrappers — `setup-all`, `deploy-all`, `stop-all`

**Files:**
- Create: `ops/playbooks/setup-all.yml`, `ops/playbooks/deploy-all.yml`, `ops/playbooks/stop-all.yml`

- [ ] **Step 1: Write `ops/playbooks/setup-all.yml`**

```yaml
---
# Phase 1 — provision the whole fleet. No stacks deployed.
- import_playbook: bootstrap-host.yml
- import_playbook: configure-dns.yml
- import_playbook: distribute-credentials.yml
```

- [ ] **Step 2: Write `ops/playbooks/deploy-all.yml`**

```yaml
---
# Phase 2 — deploy stacks (assumes setup-all has run). One play per stack so
# state_distribute precedes each consumer. Stack/host pairing comes from the
# inventory groups. spec_file / stack_path are derived from deployment_root + repo_root.
- name: Preflight — fleet is provisioned
  hosts: all:!controller
  gather_facts: false
  tasks:
    - name: laconic-so present (else: run setup-all first)
      ansible.builtin.command: laconic-so version
      changed_when: false

- name: MinIO
  hosts: minio_hosts
  gather_facts: true
  vars_files: ["{{ inventory_dir }}/secrets.yml"]
  vars:
    stack_name: hyperlane-minio
    spec_file: "{{ deployment_root }}/spec-minio.yml"
    stack_path: "{{ repo_root }}/stack_orchestrator/data/stacks/hyperlane-minio"
  pre_tasks:
    - ansible.builtin.include_tasks: ../roles/common/tasks/load_validators.yml
    - ansible.builtin.set_fact:
        MINIO_USERS: "{{ minio_users }}"
        MINIO_ROOT_USER: "{{ minio_root_user }}"
        MINIO_ROOT_PASSWORD: "{{ minio_root_password }}"
  roles: [stack_deploy]

- name: Deployer (Job)
  hosts: deployer_hosts
  gather_facts: true
  vars_files: ["{{ inventory_dir }}/secrets.yml"]
  vars:
    stack_name: hyperlane-svm-deployer
    spec_file: "{{ deployment_root }}/spec-deployer.yml"
    stack_path: "{{ repo_root }}/stack_orchestrator/data/stacks/hyperlane-svm-deployer"
    stack_is_job: true
    GORCHAIN_RPC_URL: "{{ gorchain_rpc_url }}"
    SOLANA_RPC_URL: "{{ solana_rpc_url }}"
    GORCHAIN_DOMAIN_ID: "{{ gorchain_domain_id }}"
    SOLANA_DOMAIN_ID: "{{ solana_domain_id }}"
    HARDWARE_WALLET_PUBKEY: "{{ hardware_wallet_pubkey }}"
  roles: [stack_deploy]

- import_playbook: commit-bridge-state.yml

- name: Long-running consumers
  hosts: relayer_hosts:gas_oracle_hosts:monitoring_hosts:warp_ui_hosts
  gather_facts: true
  vars_files: ["{{ inventory_dir }}/secrets.yml"]
  tasks:
    - name: "Deploy each consumer mapped to this host"
      ansible.builtin.debug:
        msg: "Per-stack state_distribute + stack_deploy are invoked via the per-stack tag blocks below"
```

> Step 2 note: the long-running consumers play is expanded in Step 3 into explicit
> per-stack blocks (relayer, gas-oracle, monitoring, warp-ui) each running
> `state_distribute` (with that stack's `configmap_names`) then `stack_deploy`.
> Validators loop over `validators.yaml` (host = `item.host`,
> `spec_file = spec-validator-<label>.yml`). Keep one block per stack so a single
> stack can be re-deployed by tag.

- [ ] **Step 3: Replace the placeholder consumers play with explicit per-stack blocks**

For each of relayer, gas-oracle, monitoring, warp-ui, add a play of this shape
(shown for relayer; repeat with the stack's own name/spec/configmaps):

```yaml
- name: Relayer
  hosts: relayer_hosts
  gather_facts: true
  vars_files: ["{{ inventory_dir }}/secrets.yml"]
  vars:
    stack_name: hyperlane-relayer
    spec_file: "{{ deployment_root }}/spec-relayer.yml"
    stack_path: "{{ repo_root }}/stack_orchestrator/data/stacks/hyperlane-relayer"
    deploy_dir: "{{ kind_mount_root }}/deployments/hyperlane-relayer"
    configmap_names: [agent-config]
    GORCHAIN_RPC_URL: "{{ gorchain_rpc_url }}"
    SOLANA_RPC_URL: "{{ solana_rpc_url }}"
  roles:
    - state_distribute
    - stack_deploy
```

And the validator loop play:

```yaml
- name: Validators
  hosts: controller
  gather_facts: false
  vars_files: ["{{ inventory_dir }}/secrets.yml"]
  tasks:
    - ansible.builtin.include_tasks: ../roles/common/tasks/load_validators.yml
    - name: Deploy each validator on its host
      ansible.builtin.include_role:
        name: stack_deploy
      vars:
        stack_name: "hyperlane-validator-{{ item.label }}"
        spec_file: "{{ deployment_root }}/spec-validator-{{ item.label }}.yml"
        stack_path: "{{ repo_root }}/stack_orchestrator/data/stacks/hyperlane-validator"
      delegate_to: "{{ item.host }}"
      loop: "{{ validators }}"
      loop_control: { label: "{{ item.label }}" }
```

> Note `spec-validator-<label>.yml`: v1 ships `spec-validator-gorchain.yml` and
> `spec-validator-solana.yml`; if `validators.yaml` labels differ from those
> filenames, align the labels to the committed spec filenames (or rename the
> specs) — template generation is sub-project 3.

- [ ] **Step 4: Write `ops/playbooks/stop-all.yml`**

```yaml
---
# Stop every stack on a host. Single-stack stops skip cluster management; pass
# -e destroy_cluster=true to tear the cluster down on the LAST stack.
- name: Stop stacks
  hosts: all:!controller
  gather_facts: false
  vars:
    destroy_cluster: false
    stacks: [hyperlane-warp-ui, hyperlane-monitoring, hyperlane-gas-oracle, hyperlane-relayer, hyperlane-svm-deployer, hyperlane-minio]
  tasks:
    - name: deployment stop (skip cluster mgmt)
      ansible.builtin.command:
        argv: [laconic-so, deployment, --dir, "{{ kind_mount_root }}/deployments/{{ item }}", stop, --skip-cluster-management]
      changed_when: true
      failed_when: false
      loop: "{{ stacks }}"

    - name: Destroy the host cluster on request
      ansible.builtin.command:
        argv: [kind, delete, cluster, --name, hyperlane]
      changed_when: true
      when: destroy_cluster | bool
```

- [ ] **Step 5: Syntax-check all three + lint**

```bash
cd ops
for p in setup-all deploy-all stop-all; do ansible-playbook --syntax-check playbooks/$p.yml; done
ansible-lint playbooks/setup-all.yml playbooks/deploy-all.yml playbooks/stop-all.yml
```
Expected: all OK, lint clean.

- [ ] **Step 6: Commit**

```bash
git add ops/playbooks/setup-all.yml ops/playbooks/deploy-all.yml ops/playbooks/stop-all.yml
git commit -m "feat(ops): setup-all, deploy-all, and stop-all orchestration playbooks"
```

---

### Task 17: Docs + keep-in-sync

**Files:**
- Create: `ops/README.md`
- Modify: `CLAUDE.md` (add an `ops/` keep-in-sync note)
- Modify: `docs/superpowers/specs/2026-05-29-staging-environment-design.md:141` (stale ref)

- [ ] **Step 1: Write `ops/README.md`**

Document: prerequisites (ansible + collections), the `-e env=` convention,
`secrets.yml` from `secrets.example.yml`, the two-phase run
(`ansible-playbook -i inventories/<env>/hosts.yml playbooks/setup-all.yml` then
`deploy-all.yml`), `state_review` flag, `stop-all.yml` with `destroy_cluster`,
and the Layer-0 lint commands. Reference the design spec.

- [ ] **Step 2: Add a keep-in-sync bullet to `CLAUDE.md`**

Under "Keep in sync — CRITICAL", add: when adding/renaming an env var in a
compose file or `deployment/spec-*.yml`, also update the `stack_env_vars` map in
`ops/inventories/*/group_vars/all.yml`.

- [ ] **Step 3: Fix the stale staging-doc reference**

In `docs/superpowers/specs/2026-05-29-staging-environment-design.md`, change the
`deployment/ops/group_vars/prod.yml` reference (line ~141) to
`ops/inventories/prod/group_vars/all.yml` (the `signer:` pin lands there under
sub-project 3).

- [ ] **Step 4: Lint + final whole-suite check**

```bash
cd ops && yamllint . && ansible-lint .
for p in playbooks/*.yml tests/test_*.yml; do ansible-playbook --syntax-check "$p"; done
for t in tests/test_*.yml; do ansible-playbook -i inventories/prod/hosts.yml "$t" || echo "FAIL $t"; done
```
Expected: lint clean; all syntax-checks OK; all assertion tests PASS.

- [ ] **Step 5: Commit**

```bash
git add ops/README.md CLAUDE.md docs/superpowers/specs/2026-05-29-staging-environment-design.md
git commit -m "docs(ops): README, keep-in-sync rule, and staging-doc path fix"
```

---

## Manual VM acceptance (post-implementation, per the spec's testing layers)

These are **not** plan tasks — they are the operator-run acceptance gates the
spec defines, listed here so the implementer knows what "done on a VM" means.

- **Layer 1 (single VM, own chains):** `setup-all.yml` then `deploy-all.yml` on
  one host with gorchain + a local Solana validator. Acceptance: all stacks
  `Running`; Caddy TLS per hostname; validators announce + write checkpoints;
  relayer delivers a test message; warp-ui reachable. Re-run both playbooks →
  no changes (idempotency).
- **Layer 2 (multi-VM):** split stacks across hosts by editing `hosts.yml` +
  `validators.yaml` only. Exercises `state_distribute` over agent forwarding,
  per-host DNS, multi-host credential distribution, cluster reuse per host.
- **Layer 3 (staging on devnet):** `-e env=staging`. Long-lived soak.

---

## Self-Review notes (author)

- **Spec coverage:** six roles (Tasks 4,5,7,9-10,12-13,14), provisioning vs
  deploy phases (Task 16), per-env inventory + secrets model (Task 2), generate-
  vs-supply split (Tasks 9-10), flag-gated commit (Task 15), cluster start/stop
  semantics (Tasks 13,16), Layer-0 CI (Task 1), manual-VM layers (acceptance
  section). All spec sections map to a task.
- **Known soft spots flagged inline** (confirm on first VM run, single line each
  to adjust): exact laconic-so release URL for the pinned SO build (Task 5);
  deployer host-path location of `generated/` (Task 15); validator label↔spec
  filename alignment (Task 16). These are environment facts, not logic — each is
  a one-line change, not a redesign.
- **Type/name consistency:** `stack_name`, `spec_file`, `stack_path`,
  `deploy_dir`, `stack_env_vars`, `minio_users`, `validator_dns_records`,
  `configmap_names`, `stack_is_job`, `state_review` are used consistently across
  tasks.
```
