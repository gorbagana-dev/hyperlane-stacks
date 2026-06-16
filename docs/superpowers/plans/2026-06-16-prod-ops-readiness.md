# Production Ops Readiness + Repo Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the prod environment deployable from zero on a single host (with multi-host still supported), and clean up the repo structure for handoff.

**Architecture:** Reuse the existing ansible ops layer end-to-end. Topology-aware MinIO addressing is achieved by porting the local inventory's `__S3_ENDPOINT__` render-token + `single_host_external_services` selector mechanism into the prod inventory (MinIO only — chains stay public literals). New prod key-lifecycle playbooks (`prepare-prod`, funding gate, `retire-deployer-key`) mirror the staging `prepare-gorchain` patterns. Cleanup is mechanical file moves/deletes plus reference fixes.

**Tech Stack:** Ansible (>=9), Jinja2, bash, laconic-so (stack-orchestrator), pytest-style ansible assertion tests under `ops/tests/`, `check-spec-parity.py`, `ansible-lint`, `yamllint`, `shellcheck`.

**Source spec:** `docs/superpowers/specs/2026-06-16-prod-ops-readiness-design.md`

**Branch:** `prod-ops-readiness` (already created; design doc committed at `f879778`).

---

## Conventions for every task

- All commit messages end with the trailer:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
- **Never push.** Commit only.
- **Safe to run on this machine** (static, no cluster/chain): `yamllint`, `ansible-lint`, `shellcheck`, `python3 ops/scripts/check-spec-parity.py`, `ansible-playbook … --syntax-check`, and the localhost `ops/tests/test_*.yml` assertion playbooks (they `connection: local` and read files directly).
- **Hand off to the operator** (NOT run here): any `ansible-playbook` run that targets real hosts, and anything touching a kind cluster or a chain. Where a task's only real validation is a live run, the step says "HAND OFF" and gives the exact command.
- Lint commands run from `ops/`: `cd ops && yamllint . && ansible-lint .`. Parity runs from repo root: `python3 ops/scripts/check-spec-parity.py` (expect `Spec shape parity OK: 9 specs match.`).

---

# Phase 1 — Repo cleanup (independent; lands first)

### Task 1: Move `specs/` → `docs/` and fix live references

**Files:**
- Move: `specs/ansible-spec.md` → `docs/ansible-spec.md`
- Move: `specs/e2e-test-spec.md` → `docs/e2e-test-spec.md`
- Move: `specs/stack-specifications.md` → `docs/stack-specifications.md`
- Modify: `README.md`, `CLAUDE.md`, `ops/README.md`, `docs/architecture-decisions.md`

- [ ] **Step 1: Move the three files with git**

```bash
cd /home/dev/git_puller/repos/hyperlane-stacks
git mv specs/ansible-spec.md docs/ansible-spec.md
git mv specs/e2e-test-spec.md docs/e2e-test-spec.md
git mv specs/stack-specifications.md docs/stack-specifications.md
rmdir specs 2>/dev/null || true
```

- [ ] **Step 2: Find every live reference**

```bash
grep -rn "specs/stack-specifications\|specs/e2e-test-spec\|specs/ansible-spec" \
  README.md CLAUDE.md ops/README.md docs/architecture-decisions.md
```
Expected: matches in all four files (the `docs/superpowers/**` historical files are intentionally left unchanged).

- [ ] **Step 3: Rewrite the references**

In each of `README.md`, `CLAUDE.md`, `ops/README.md`, `docs/architecture-decisions.md`, replace every `specs/<name>.md` with `docs/<name>.md`. Specifically:
- `README.md`: the Repository Structure line `- \`specs/\` — Stack specifications` → fold into the `docs/` line; and the Documentation link `[Stack Specifications](specs/stack-specifications.md)` → `[Stack Specifications](docs/stack-specifications.md)`.
- `CLAUDE.md`: the repo-layout block (`specs/ # Detailed specifications`), and every keep-in-sync / docs mention: `specs/stack-specifications.md`, `specs/e2e-test-spec.md` → `docs/…`.
- `ops/README.md` and `docs/architecture-decisions.md`: same path swap.

- [ ] **Step 4: Verify no live references remain**

```bash
grep -rn "specs/stack-specifications\|specs/e2e-test-spec\|specs/ansible-spec" \
  README.md CLAUDE.md ops/README.md docs/architecture-decisions.md
```
Expected: no output.

- [ ] **Step 5: Verify the README repo-structure block no longer lists a root `specs/`**

```bash
test ! -d specs && echo "specs/ removed"
grep -n "specs/" README.md || echo "no root specs/ refs in README"
```
Expected: `specs/ removed` and no stray `specs/` path in README structure.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
docs: move specs/ into docs/

Flattens the top-level specs/ dir into docs/ to match the existing docs
layout. Updates the live references in README, CLAUDE.md, ops/README and
architecture-decisions; historical docs/superpowers/** keep their
point-in-time paths.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Delete `deployment/ops-archive/`

**Files:**
- Delete: `deployment/ops-archive/` (whole tree)

- [ ] **Step 1: Confirm nothing live references it**

```bash
grep -rn "ops-archive" . --include='*.md' --include='*.yml' --include='*.yaml' --include='*.py' --include='*.sh' \
  | grep -v '.git/' | grep -v 'docs/superpowers/' | grep -v '.pebbles/'
```
Expected: no output (only historical/pebble references, which are fine).

- [ ] **Step 2: Remove the tree**

```bash
git rm -r deployment/ops-archive
```

- [ ] **Step 3: Verify**

```bash
test ! -d deployment/ops-archive && echo "ops-archive removed"
```
Expected: `ops-archive removed`.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
chore: remove the Ledger-era ops-archive playbooks

Superseded by the Privy server-wallet model. The replacement maintenance
ops (kill-switch/restore/teardown/verify-ownership) are tracked under epic
hyp-564 with self-contained descriptions.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Remove vestigial `inventories/*/secrets.yml`

**Files:**
- Delete: `ops/inventories/prod/secrets.yml`, `ops/inventories/staging/secrets.yml`, `ops/inventories/local/secrets.yml`

- [ ] **Step 1: Confirm nothing reads them**

```bash
grep -rn "secrets.yml" ops/roles ops/playbooks ops/tests | grep -v collections
```
Expected: no output (the loader reads `deployment-config.yml`, not `secrets.yml`).

- [ ] **Step 2: Remove the three files**

```bash
git rm ops/inventories/prod/secrets.yml ops/inventories/staging/secrets.yml ops/inventories/local/secrets.yml
```

- [ ] **Step 3: Verify the migration hint still stands**

```bash
grep -n "secrets.yml" ops/roles/common/tasks/load_deployment_config.yml
```
Expected: the fail-msg line that tells operators to `mv` an old `secrets.yml` to `deployment-config.yml` (kept — it's guidance, not a read).

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
chore: drop vestigial inventories/*/secrets.yml

Nothing reads these; deployment-config.yml is the single operator file and
load_deployment_config already points migrators at it.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

# Phase 2 — Make `deploy-all` reach a healthy prod bridge

This phase introduces `ops/tests/test_prod_env.yml`, a localhost assertion test
that is the fail-first gate for Tasks 4 and 5. Write it first (Task 4 Step 1).

### Task 4: Prod validator set + placement (hyp-fda)

**Files:**
- Create: `deployment/bridges/default/operator/validators.yaml`
- Modify: `ops/inventories/prod/hosts.yml`
- Create: `ops/tests/test_prod_env.yml` (shared with Task 5; created here, extended there)

- [ ] **Step 1: Write the failing test** (`ops/tests/test_prod_env.yml`)

```yaml
---
# Layer-0: prod wiring — single-host Cloudflare env, external mainnet gorchain
# (no chain host), specs under deployment/. Reads the prod files directly. Run
# with -i inventories/prod/hosts.yml so the derived topology sees prod groups.
- name: Prod wiring
  hosts: localhost
  connection: local
  gather_facts: false
  vars:
    ansible_env:
      HOME: /home/op
  tasks:
    - name: Load prod env wiring
      ansible.builtin.include_vars:
        file: "{{ playbook_dir }}/../inventories/prod/group_vars/all.yml"

    - name: Parse the prod hosts file
      ansible.builtin.set_fact:
        _prod_inv: >-
          {{ lookup('ansible.builtin.file',
                    playbook_dir ~ '/../inventories/prod/hosts.yml') | from_yaml }}

    # topology + S3 endpoint are asserted in Task 5 (they depend on the group_vars
    # derivation added there). Task 4 only asserts the static prod wiring it adds.
    - name: Prod env resolves as designed (external chains, real DNS, validators inventoried)
      ansible.builtin.assert:
        that:
          - "manage_dns | bool"
          - "deployment_subdir == 'deployment'"
          - "base_domain == 'bridge.gorbagana.wtf'"
          # prod runs no chain host (gorchain is external mainnet)
          - "'chain_hosts' not in _prod_inv.all.children"
          # validators are inventoried so bootstrap covers them
          - "_prod_inv.all.children.validator_hosts.hosts.keys() | list == ['bridge-host-1']"
        quiet: true

    - name: Parse the prod validators file
      ansible.builtin.set_fact:
        _prod_validators: >-
          {{ (lookup('ansible.builtin.file',
                     playbook_dir ~ '/../../deployment/bridges/default/operator/validators.yaml')
              | from_yaml)['validators'] }}

    - name: Prod validators are the prod-shaped 1-of-1 pair on the single host
      ansible.builtin.assert:
        that:
          - "_prod_validators | map(attribute='label') | list == ['gorchain-primary', 'solana-primary']"
          - "_prod_validators | map(attribute='host') | unique | list == ['bridge-host-1']"
          - >-
            _prod_validators | map(attribute='hostname')
            | select('search', '\.bridge\.gorbagana\.wtf$') | list | length == 2
        quiet: true
```

- [ ] **Step 2: Run it to confirm it fails**

```bash
cd ops && ansible-playbook -i inventories/prod/hosts.yml tests/test_prod_env.yml
```
Expected: FAIL — `validators.yaml` does not exist (lookup error) and `validator_hosts` is missing from prod `hosts.yml`. (The Task-5 assertions about `__S3_ENDPOINT__` are not in this file yet.)

- [ ] **Step 3: Create the prod validator set** (`deployment/bridges/default/operator/validators.yaml`)

```yaml
---
# Prod validator set — 1-of-1 per chain, single-host default (both on bridge-host-1;
# gorchain is external mainnet, so there is no chain-host 80/443 conflict to dodge).
# Labels are load-bearing: the committed validator specs hardcode the derived MinIO
# IAM env names (GORCHAIN_PRIMARY_KEY_ID, SOLANA_PRIMARY_KEY_ID, ...). Pure topology:
# each validator's Privy wallet id comes from the operator's deployment-config
# (privy_validator_wallet_ids, keyed by label). For multi-host prod, move a
# validator's `host:` to a second host and add that host to the inventory.
validators:
  - label: gorchain-primary
    chain: gorchain
    host: bridge-host-1
    hostname: validator-gorchain.bridge.gorbagana.wtf
  - label: solana-primary
    chain: solana
    host: bridge-host-1
    hostname: validator-solana.bridge.gorbagana.wtf
```

- [ ] **Step 4: Add the `validator_hosts` group to prod `hosts.yml`**

Append under `all.children` in `ops/inventories/prod/hosts.yml` (after `warp_ui_hosts`):

```yaml
    # Both hyperlane validators (from validators.yaml). No playbook targets this
    # group directly; membership puts the host in `all` so bootstrap-host.yml
    # provisions it and the validator loop can delegate to it. Single-host default:
    # the same bridge-host-1. For multi-host, point a validator's host: at a
    # second host and add it here.
    validator_hosts:
      hosts:
        bridge-host-1:
```

- [ ] **Step 5: Run the test — validator assertions pass**

```bash
cd ops && ansible-playbook -i inventories/prod/hosts.yml tests/test_prod_env.yml
```
Expected: PASS (Task-5 S3 assertions still absent — add them in Task 5).

- [ ] **Step 6: Lint**

```bash
cd ops && yamllint . && ansible-lint .
```
Expected: no new errors.

- [ ] **Step 7: Commit**

```bash
cd /home/dev/git_puller/repos/hyperlane-stacks
git add deployment/bridges/default/operator/validators.yaml ops/inventories/prod/hosts.yml ops/tests/test_prod_env.yml
git commit -m "$(cat <<'EOF'
ops(prod): commit prod validator set + placement (hyp-fda)

deploy-all failed at load_validators because prod shipped no
operator/validators.yaml. Adds the prod-shaped 1-of-1 pair on the single
host and a validator_hosts group so bootstrap covers them, with a
localhost wiring test.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Topology-aware MinIO addressing for prod

**Files:**
- Modify: `ops/inventories/prod/group_vars/all.yml`
- Modify: `deployment/spec-validator-gorchain.yml`
- Modify: `deployment/spec-validator-solana.yml`
- Modify: `deployment/spec-relayer.yml`
- Modify: `ops/tests/test_prod_env.yml` (extend with S3/external-services assertions)

- [ ] **Step 1: Extend the failing test** — append these tasks to `ops/tests/test_prod_env.yml`

```yaml
    - name: Topology + S3 endpoint + external-services are topology-aware (MinIO only)
      ansible.builtin.assert:
        that:
          # prod single-host is derived from minio/relayer co-location
          - "topology == 'single'"
          # single-host prod reaches MinIO in-cluster, not via the public Caddy URL
          - "spec_token_renders.__S3_ENDPOINT__ == 'http://hyperlane-minio:9000'"
          # validators + relayer get a single-host external-services block...
          - "'hyperlane-validator' in single_host_external_services"
          - "'hyperlane-relayer' in single_host_external_services"
          # ...and that block is MinIO-only (chains stay external/public — never tunneled)
          - "'hyperlane-minio' in single_host_external_services['hyperlane-relayer']"
          - "'gorchain-rpc' not in single_host_external_services['hyperlane-relayer']"
          - "'solana-rpc' not in single_host_external_services['hyperlane-relayer']"
        quiet: true
```

- [ ] **Step 2: Run it — confirm the new assertions fail**

```bash
cd ops && ansible-playbook -i inventories/prod/hosts.yml tests/test_prod_env.yml
```
Expected: FAIL — `spec_token_renders.__S3_ENDPOINT__` is undefined and `single_host_external_services` is undefined in prod group_vars.

- [ ] **Step 3: Edit prod `group_vars/all.yml` — derive topology, keep DNS, add MinIO render + selector**

In `ops/inventories/prod/group_vars/all.yml`, replace the topology/DNS block:

```yaml
topology: multi
manage_dns: true
```
with:
```yaml
# Single-host by default (all bridge services co-located); add a separate
# relayer/minio host to flip to multi. Unlike local, prod's topology switches ONLY
# the MinIO endpoint — chains are external mainnet/Helius and DNS is always real.
topology: "{{ 'single' if (groups['minio_hosts'][0] == groups['relayer_hosts'][0]) else 'multi' }}"
manage_dns: true
```

Extend `spec_token_renders` (the existing block currently holds only the
WalletConnect render) to:
```yaml
spec_token_renders:
  # from deployment-config.yml ("" disables WalletConnect)
  REPLACE_WITH_WALLETCONNECT_PROJECT_ID: "{{ wallet_connect_id }}"
  # single-host reaches MinIO in-cluster (no Caddy hairpin / 308); multi uses the
  # public S3 Caddy front. Chains are NOT tokenized — they stay public literals.
  __S3_ENDPOINT__: "{{ 'http://hyperlane-minio:9000' if topology == 'single' else 'https://s3.' ~ base_domain }}"
```

Add, immediately after `spec_token_renders` (MinIO-only — no chain blocks):
```yaml
# Single-host external-services: each consumer gets a headless Service backed by
# the MinIO pods' Endpoints (cross-namespace, plain HTTP) so it reaches MinIO at
# http://hyperlane-minio:9000 without Caddy/public DNS. render_spec injects this
# at the `# __SINGLE_HOST_EXTERNAL_SERVICES__` marker on single-host and strips it
# otherwise. MinIO only: prod chains are external in every topology.
_xs_minio: |
  external-services:
    hyperlane-minio:
      selector:
        app.kubernetes.io/stack: hyperlane-minio
      namespace: laconic-hyperlane-minio

single_host_external_services:
  hyperlane-validator: "{{ _xs_minio }}"
  hyperlane-relayer:   "{{ _xs_minio }}"
```

- [ ] **Step 4: Tokenize the S3 endpoint + add the marker in the three consumer specs**

In `deployment/spec-validator-gorchain.yml` and `deployment/spec-validator-solana.yml`, replace:
```yaml
  # MinIO via Caddy — public DNS resolves to the Caddy ingress.
  AWS_ENDPOINT_URL_S3: "https://s3.bridge.gorbagana.wtf"
```
with:
```yaml
  # MinIO endpoint is topology-aware (ops __S3_ENDPOINT__): in-cluster on
  # single-host, the public Caddy URL on multi-host.
  AWS_ENDPOINT_URL_S3: "__S3_ENDPOINT__"
# __SINGLE_HOST_EXTERNAL_SERVICES__
```
(The marker is a top-level comment line at column 0, not under `config:`.)

In `deployment/spec-relayer.yml`, replace:
```yaml
  # MinIO via Caddy — public DNS resolves to the Caddy ingress.
  AWS_ENDPOINT_URL_S3: "https://s3.bridge.gorbagana.wtf"
```
with:
```yaml
  # MinIO endpoint is topology-aware (ops __S3_ENDPOINT__): in-cluster on
  # single-host, the public Caddy URL on multi-host.
  AWS_ENDPOINT_URL_S3: "__S3_ENDPOINT__"
# __SINGLE_HOST_EXTERNAL_SERVICES__
```
Place the marker as a top-level comment line (column 0) — a sibling of `config:`,
`configmaps:`, `secrets:`. Put it right after the `config:` block ends in each file.

- [ ] **Step 5: Run the prod env test — all assertions pass**

```bash
cd ops && ansible-playbook -i inventories/prod/hosts.yml tests/test_prod_env.yml
```
Expected: PASS.

- [ ] **Step 6: Confirm spec parity is unaffected (values + comments are exempt)**

```bash
cd /home/dev/git_puller/repos/hyperlane-stacks && python3 ops/scripts/check-spec-parity.py
```
Expected: `Spec shape parity OK: 9 specs match.`

- [ ] **Step 7: Lint**

```bash
cd ops && yamllint . && ansible-lint .
```
Expected: no new errors.

- [ ] **Step 8: HAND OFF — multi-host render sanity (operator/CI, needs hosts)**

The single-host path is asserted above; the multi-host strip path is exercised by
staging/local deploys already. No local action. Note in the PR that a multi-host
prod render yields `AWS_ENDPOINT_URL_S3: https://s3.bridge.gorbagana.wtf` and no
`external-services:` block.

- [ ] **Step 9: Commit**

```bash
cd /home/dev/git_puller/repos/hyperlane-stacks
git add ops/inventories/prod/group_vars/all.yml deployment/spec-validator-gorchain.yml \
        deployment/spec-validator-solana.yml deployment/spec-relayer.yml ops/tests/test_prod_env.yml
git commit -m "$(cat <<'EOF'
ops(prod): topology-aware MinIO addressing

Single-host prod reached MinIO via the public S3 Caddy URL, which hairpins
to the host's own public IP (often unroutable on cloud VMs) and breaks the
S3 SDK through Caddy's 308. Derive topology from co-location and render the
MinIO endpoint in-cluster (selector-mode external-services) on single-host,
public on multi-host. Chains stay public literals; DNS stays managed.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Warp token metadata URI (hyp-646)

**Files:**
- Modify: `deployment/bridges/default/warp-routes/usdc.yml`

- [ ] **Step 1: Replace the sentinel with the hosted gist URL**

In `deployment/bridges/default/warp-routes/usdc.yml`, replace:
```yaml
metadataUri: "REPLACE_WITH_TOKEN_METADATA_URI"
```
with:
```yaml
metadataUri: "https://gist.githubusercontent.com/prathamesh0/685734f8aac9dd22c9eeb3d1e7f8e407/raw/c2c4abdc3d7b05866c00d95379983bca64d864a7/token-metadata.json"
```

- [ ] **Step 2: Verify the URL serves the expected metadata**

```bash
curl -s "https://gist.githubusercontent.com/prathamesh0/685734f8aac9dd22c9eeb3d1e7f8e407/raw/c2c4abdc3d7b05866c00d95379983bca64d864a7/token-metadata.json" | python3 -c "import json,sys; d=json.load(sys.stdin); assert d['name']=='USD Coin' and d['symbol']=='USDC' and d['image'].startswith('http'); print('metadata OK')"
```
Expected: `metadata OK`.

- [ ] **Step 3: Confirm no sentinel remains**

```bash
grep -n "REPLACE_WITH_TOKEN_METADATA_URI" deployment/bridges/default/warp-routes/usdc.yml || echo "sentinel gone"
```
Expected: `sentinel gone`.

- [ ] **Step 4: Commit**

```bash
git add deployment/bridges/default/warp-routes/usdc.yml
git commit -m "$(cat <<'EOF'
ops(prod): set the USDC warp token metadata URI (hyp-646)

Replaces the REPLACE_WITH_TOKEN_METADATA_URI sentinel with the operator's
hosted gist (name/symbol match remote USD Coin/USDC; image serves), so the
prod warp deployer's metadata validation passes.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

# Phase 3 — Hardening

### Task 7: Env-contract gates — reject placeholder `public_ip`, assert `wallet_connect_id` (hyp-c7d)

**Files:**
- Modify: `ops/roles/stack_deploy/tasks/preflight.yml`
- Modify: `ops/roles/common/tasks/load_deployment_config.yml`
- Create: `ops/tests/fixtures/config-no-wcid.yml`
- Modify: `ops/tests/test_env_contract.yml`

The `wallet_connect_id` gate lives in `load_deployment_config.yml`, which is cleanly
includable on localhost (it stats + `include_vars` a file path), so it gets a real
red→green test via a fixture. The `public_ip` guard lives in `preflight.yml`, which
is not includable standalone (it runs `laconic-so version` + slurps the on-host
spec); it is verified by `ansible-lint`/`--syntax-check` here and exercised by the
operator's deploy.

- [ ] **Step 1: Create the fixture config missing `wallet_connect_id`** (`ops/tests/fixtures/config-no-wcid.yml`)

```yaml
---
# Fixture: a deployment-config that fills the usual secrets but OMITS
# wallet_connect_id, to prove load_deployment_config's presence gate fires.
cloudflare_api_token: "x"
privy_app_id: "x"
privy_app_secret: "x"
privy_oracle_wallet_id: "x"
helius_api_key: "x"
ghcr_pat: "x"
bridge_owner_pubkey: "x"
igp_oracle_pubkey: "x"
gorchain_validator_address: "0x0"
solana_validator_address: "0x0"
```

- [ ] **Step 2: Add the failing test play** — append to `ops/tests/test_env_contract.yml`

```yaml
- name: load_deployment_config rejects a config missing wallet_connect_id
  hosts: localhost
  connection: local
  gather_facts: false
  vars:
    deployment_config_file: "{{ playbook_dir }}/fixtures/config-no-wcid.yml"
  tasks:
    - name: Include the loader (must fail once the presence gate exists)
      block:
        - name: Load the config
          ansible.builtin.include_tasks: ../roles/common/tasks/load_deployment_config.yml
        - name: Loader succeeded
          ansible.builtin.set_fact:
            _loaded_ok: true
      rescue:
        - name: Loader failed (expected)
          ansible.builtin.set_fact:
            _loaded_ok: false
    - name: The wallet_connect_id presence gate fired
      ansible.builtin.assert:
        that: "not (_loaded_ok | default(true))"
        fail_msg: "load_deployment_config did not reject a config missing wallet_connect_id"
```

- [ ] **Step 2b: Run it to confirm it fails**

```bash
cd ops && ansible-playbook -i inventories/prod/hosts.yml tests/test_env_contract.yml
```
Expected: FAIL at "The wallet_connect_id presence gate fired" — the loader currently
succeeds (no gate yet), so `_loaded_ok` is true.

- [ ] **Step 3: Add the placeholder `public_ip` guard to `preflight.yml`**

Insert at the top of `ops/roles/stack_deploy/tasks/preflight.yml` (after the
`laconic-so` check), gated like the existing public-DNS check (non-single only,
where `public_ip` is actually used for DNS/SSH):

```yaml
- name: Reject an unfilled public_ip (host_vars not configured)
  ansible.builtin.assert:
    that: "public_ip is not defined or (public_ip is string and 'REPLACE_WITH' not in public_ip)"
    fail_msg: >-
      public_ip for {{ inventory_hostname }} is still the REPLACE_WITH placeholder —
      set it in inventories/<env>/host_vars/{{ inventory_hostname }}.yml.
    quiet: true
  when:
    - not ansible_check_mode
    - topology | default('') != 'single'
```

- [ ] **Step 4: Add the `wallet_connect_id` presence assert to `load_deployment_config.yml`**

Append to `ops/roles/common/tasks/load_deployment_config.yml` (after the
`include_vars` that loads the config):

```yaml
- name: wallet_connect_id must be present (empty disables WalletConnect; missing is a gap)
  ansible.builtin.assert:
    that: wallet_connect_id is defined
    fail_msg: >-
      wallet_connect_id is not set in deployment-config.yml — add it (an empty
      string "" is valid and disables WalletConnect; a missing key dies later as
      an undefined-var at the warp-ui spec render).
    quiet: true
```

- [ ] **Step 5: Run the env-contract test — passes**

```bash
cd ops && ansible-playbook -i inventories/prod/hosts.yml tests/test_env_contract.yml
```
Expected: PASS.

- [ ] **Step 6: Lint**

```bash
cd ops && yamllint . && ansible-lint .
```
Expected: no new errors.

- [ ] **Step 7: Commit**

```bash
cd /home/dev/git_puller/repos/hyperlane-stacks
git add ops/roles/stack_deploy/tasks/preflight.yml ops/roles/common/tasks/load_deployment_config.yml \
        ops/tests/test_env_contract.yml ops/tests/fixtures/config-no-wcid.yml
git commit -m "$(cat <<'EOF'
ops: fail fast on unfilled public_ip and missing wallet_connect_id (hyp-c7d)

A REPLACE_WITH public_ip previously surfaced as an opaque SSH/DNS failure;
a missing wallet_connect_id died as an undefined-var at the warp-ui render.
Add a placeholder guard in preflight and a presence assert in
load_deployment_config.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Redact the Helius URL in the core deployer log (hyp-534 residual)

**Files:**
- Modify: `stack_orchestrator/data/config/deployer-scripts-config/deploy.sh:28`

- [ ] **Step 1: Confirm the leak is present**

```bash
sed -n '28p' stack_orchestrator/data/config/deployer-scripts-config/deploy.sh
```
Expected: `exec > >(tee -a "${LOG_FILE}") 2>&1` (no redaction).

- [ ] **Step 2: Add the redaction stage to the exec pipeline**

Replace line 28 of `stack_orchestrator/data/config/deployer-scripts-config/deploy.sh`:
```bash
exec > >(tee -a "${LOG_FILE}") 2>&1
```
with (mirrors the warp-deployer's per-route redaction at `warp-deployer-scripts-config/deploy.sh:113`, but scrubs any `api-key=` query credential so it holds even before `SOLANA_RPC_URL` is exported):
```bash
exec > >(stdbuf -o0 sed -E "s/api-key=[A-Za-z0-9_-]+/api-key=<REDACTED>/g" | tee -a "${LOG_FILE}") 2>&1
```

- [ ] **Step 3: Verify the redaction logic with a smoke test**

```bash
printf 'Running command: ... --url https://mainnet.helius-rpc.com/?api-key=abc123-DEF_456 ...\n' \
  | stdbuf -o0 sed -E "s/api-key=[A-Za-z0-9_-]+/api-key=<REDACTED>/g"
```
Expected: the line prints with `api-key=<REDACTED>` and no `abc123-DEF_456`.

- [ ] **Step 4: Shellcheck the script**

```bash
shellcheck -e SC1090,SC2086 stack_orchestrator/data/config/deployer-scripts-config/deploy.sh
```
Expected: no new errors introduced by the change (pre-existing findings, if any, unchanged).

- [ ] **Step 5: Commit**

```bash
git add stack_orchestrator/data/config/deployer-scripts-config/deploy.sh
git commit -m "$(cat <<'EOF'
fix(deployer): redact api-key in the core deployer log (hyp-534)

The core deployer's main log teed the fork CLI's verbatim command echo,
leaking the Helius api-key to the host log and kubectl logs. Adds the same
sed redaction the warp-deployer already applies, scrubbing any api-key
query credential in the exec pipeline.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

# Phase 4 — Prod key-lifecycle playbooks

### Task 9: Funding-verification script + gate (C2)

**Files:**
- Create: `ops/scripts/verify-signer-funding.sh`
- Create: `ops/playbooks/verify-funding.yml`
- Modify: `ops/playbooks/deploy-all.yml` (add a prod-only funding-gate play)

- [ ] **Step 1: Create `ops/scripts/verify-signer-funding.sh`** (report-only; no airdrops)

```bash
#!/usr/bin/env bash
# Verify the prod hot signers are funded to their per-chain TARGET BALANCES,
# reading addresses from the addresses.env that gen-local-keys.sh wrote. REPORT
# ONLY — never airdrops (mainnet has no faucet). Exits non-zero listing every
# shortfall so the calling play fails visibly; the operator funds from a treasury
# and re-runs (balance-driven: re-runs just re-check).
#
# Env: CRED_DIR       [~/.credentials/hyperlane]  holds addresses.env
#      GORCHAIN_RPC   [https://rpc.gorbagana.wtf]
#      SOLANA_RPC     (required) the Helius mainnet RPC URL
#      ORACLE_PUBKEY  (required) the Privy IGP oracle's Solana pubkey
set -uo pipefail

CRED_DIR="${CRED_DIR:-$HOME/.credentials/hyperlane}"
GORCHAIN_RPC="${GORCHAIN_RPC:-https://rpc.gorbagana.wtf}"
SOLANA_RPC="${SOLANA_RPC:?set SOLANA_RPC (the Helius mainnet RPC URL)}"
ORACLE_PUBKEY="${ORACLE_PUBKEY:?set ORACLE_PUBKEY (the Privy IGP oracle pubkey)}"

command -v solana >/dev/null || { echo "ERROR: solana CLI not found"; exit 1; }
ADDR_FILE="$CRED_DIR/addresses.env"
[ -f "$ADDR_FILE" ] || { echo "ERROR: $ADDR_FILE not found — run prepare-prod.yml first"; exit 1; }
# shellcheck disable=SC1090
source "$ADDR_FILE"
for v in DEPLOYER_KEYPAIR_ADDR VALIDATOR_GORCHAIN_ADDR VALIDATOR_SOLANA_ADDR \
         RELAYER_GORCHAIN_ADDR RELAYER_SOLANA_ADDR RELAYER_FEE_CLAIM_ADDR; do
  [ -n "${!v:-}" ] || { echo "ERROR: $v missing from $ADDR_FILE — regenerate that key and re-run"; exit 1; }
done

balance_sol() {  # <addr> <rpc> — whole SOL rounded down; 0 if account absent
  local out
  out=$(solana balance "$1" --url "$2" 2>/dev/null) || { echo 0; return; }
  echo "$out" | awk '{print int($1)}'
}

SHORTFALLS=()
check() {  # <label> <addr> <target_sol> <rpc>
  local label=$1 addr=$2 target=$3 rpc=$4 have
  have=$(balance_sol "$addr" "$rpc")
  if [ "$have" -ge "$target" ]; then
    echo "  ✓ $label $addr: $have SOL (>= $target)"
  else
    echo "  ✗ $label $addr: have $have SOL, want $target"
    SHORTFALLS+=("$addr needs $(( target - have )) more SOL ($label)")
  fi
}

echo "Checking funding on gorchain ($GORCHAIN_RPC)..."
solana cluster-version --url "$GORCHAIN_RPC" >/dev/null 2>&1 \
  || { echo "ERROR: gorchain RPC unreachable"; exit 1; }
check "deployer"           "$DEPLOYER_KEYPAIR_ADDR"  100 "$GORCHAIN_RPC"
check "gorchain validator" "$VALIDATOR_GORCHAIN_ADDR"  1 "$GORCHAIN_RPC"
check "relayer gorchain"   "$RELAYER_GORCHAIN_ADDR"    1 "$GORCHAIN_RPC"
check "IGP fee-claim"      "$RELAYER_FEE_CLAIM_ADDR"   1 "$GORCHAIN_RPC"
check "Privy IGP oracle"   "$ORACLE_PUBKEY"            1 "$GORCHAIN_RPC"

echo "Checking funding on solana mainnet..."
solana cluster-version --url "$SOLANA_RPC" >/dev/null 2>&1 \
  || { echo "ERROR: solana RPC unreachable"; exit 1; }
check "deployer"           "$DEPLOYER_KEYPAIR_ADDR"   10 "$SOLANA_RPC"
check "solana validator"   "$VALIDATOR_SOLANA_ADDR"    1 "$SOLANA_RPC"
check "relayer solana"     "$RELAYER_SOLANA_ADDR"      1 "$SOLANA_RPC"
check "IGP fee-claim"      "$RELAYER_FEE_CLAIM_ADDR"   1 "$SOLANA_RPC"
check "Privy IGP oracle"   "$ORACLE_PUBKEY"            1 "$SOLANA_RPC"

if [ "${#SHORTFALLS[@]}" -ne 0 ]; then
  echo ""
  echo "Underfunded (mainnet has no faucet — fund from a treasury wallet, then re-run):"
  printf '  %s\n' "${SHORTFALLS[@]}"
  exit 1
fi
echo "All signers funded to target."
exit 0
```

- [ ] **Step 2: Shellcheck it**

```bash
shellcheck ops/scripts/verify-signer-funding.sh
```
Expected: clean (SC1090 is suppressed inline as in the staging funder).

- [ ] **Step 3: Create `ops/playbooks/verify-funding.yml`** (standalone wrapper)

```yaml
---
# Standalone prod funding gate — verify every hot signer is funded to its target on
# both chains. Report-only (mainnet has no faucet): fails listing shortfalls. Run
# after prepare-prod.yml (which generates keys + addresses.env), before deploy-all.
# Also imported as a pre-deploy gate by deploy-all.yml on prod.
#
#   ansible-playbook -i inventories/prod/hosts.yml playbooks/verify-funding.yml
- name: Verify prod signer funding
  hosts: deployer_hosts
  gather_facts: true
  vars:
    solana_bin: "{{ ansible_env.HOME }}/.local/share/solana/install/active_release/bin"
    tool_path: "{{ ansible_env.HOME }}/bin:{{ solana_bin }}:{{ ansible_env.PATH }}"
    scripts_dst: "{{ ansible_env.HOME }}/.bridge-setup-scripts"
  pre_tasks:
    - name: Load the deployment config
      ansible.builtin.include_tasks: ../roles/common/tasks/load_deployment_config.yml
  tasks:
    - name: Ensure the setup-scripts dir exists
      ansible.builtin.file:
        path: "{{ scripts_dst }}"
        state: directory
        mode: "0755"
    - name: Ship the funding-verification script
      ansible.builtin.copy:
        src: "../scripts/verify-signer-funding.sh"
        dest: "{{ scripts_dst }}/verify-signer-funding.sh"
        mode: "0755"
    - name: Verify funding (report-only; fails listing shortfalls)
      ansible.builtin.command:
        cmd: "{{ scripts_dst }}/verify-signer-funding.sh"
      environment:
        PATH: "{{ tool_path }}"
        CRED_DIR: "{{ credentials_dir }}"
        GORCHAIN_RPC: "{{ gorchain_rpc_url | default('https://rpc.gorbagana.wtf') }}"
        SOLANA_RPC: "{{ SOLANA_RPC_URL }}"
        ORACLE_PUBKEY: "{{ igp_oracle_pubkey }}"
      register: _funding
      changed_when: false
      failed_when: _funding.rc != 0
```

- [ ] **Step 4: Add the prod-only funding-gate play to `deploy-all.yml`**

Insert a new play in `ops/playbooks/deploy-all.yml` immediately **after** the
"Preflight — fleet is provisioned" play and **before** the "MinIO" play:

```yaml
- name: Prod funding gate
  hosts: deployer_hosts
  gather_facts: true
  # Prod only (deployment_subdir == 'deployment'). staging/local fund via their own
  # prepare plays and faucets; this gate guards the no-faucet mainnet path.
  vars:
    solana_bin: "{{ ansible_env.HOME }}/.local/share/solana/install/active_release/bin"
    tool_path: "{{ ansible_env.HOME }}/bin:{{ solana_bin }}:{{ ansible_env.PATH }}"
    scripts_dst: "{{ ansible_env.HOME }}/.bridge-setup-scripts"
  pre_tasks:
    - name: Load the deployment config
      ansible.builtin.include_tasks: ../roles/common/tasks/load_deployment_config.yml
      when: deployment_subdir == 'deployment'
  tasks:
    - name: Ship + run the funding-verification gate
      when: deployment_subdir == 'deployment'
      block:
        - name: Ensure the setup-scripts dir exists
          ansible.builtin.file:
            path: "{{ scripts_dst }}"
            state: directory
            mode: "0755"
        - name: Ship the funding-verification script
          ansible.builtin.copy:
            src: "../scripts/verify-signer-funding.sh"
            dest: "{{ scripts_dst }}/verify-signer-funding.sh"
            mode: "0755"
        - name: Funding gate (fails the deploy if any signer is underfunded)
          ansible.builtin.command:
            cmd: "{{ scripts_dst }}/verify-signer-funding.sh"
          environment:
            PATH: "{{ tool_path }}"
            CRED_DIR: "{{ credentials_dir }}"
            GORCHAIN_RPC: "{{ gorchain_rpc_url | default('https://rpc.gorbagana.wtf') }}"
            SOLANA_RPC: "{{ SOLANA_RPC_URL }}"
            ORACLE_PUBKEY: "{{ igp_oracle_pubkey }}"
          register: _funding_gate
          changed_when: false
          failed_when: _funding_gate.rc != 0
```

- [ ] **Step 5: Syntax-check both playbooks**

```bash
cd ops
ansible-playbook -i inventories/prod/hosts.yml playbooks/verify-funding.yml --syntax-check
ansible-playbook -i inventories/prod/hosts.yml playbooks/deploy-all.yml --syntax-check
```
Expected: both report no syntax errors.

- [ ] **Step 6: Lint**

```bash
cd ops && yamllint . && ansible-lint .
```
Expected: no new errors.

- [ ] **Step 7: HAND OFF — live funding check (operator, needs a prod host + chains)**

```bash
# (operator) after prepare-prod.yml and funding the addresses:
ansible-playbook -i inventories/prod/hosts.yml playbooks/verify-funding.yml
```

- [ ] **Step 8: Commit**

```bash
cd /home/dev/git_puller/repos/hyperlane-stacks
git add ops/scripts/verify-signer-funding.sh ops/playbooks/verify-funding.yml ops/playbooks/deploy-all.yml
git commit -m "$(cat <<'EOF'
ops(prod): funding-verification gate before deploy

Report-only balance check (mainnet has no faucet) of every hot signer
against per-chain targets. Exposed standalone (verify-funding.yml) and as a
prod-only pre-deploy gate in deploy-all so a deploy can't start against
underfunded signers.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 10: `prepare-prod.yml` + allow prod key generation (C1)

**Files:**
- Create: `ops/playbooks/prepare-prod.yml`
- Modify: `ops/scripts/gen-local-keys.sh` (soften the prod prohibition in the header/banner)

- [ ] **Step 1: Soften `gen-local-keys.sh` so prod generation is sanctioned**

In `ops/scripts/gen-local-keys.sh`, update the top comment and the banner.
Replace the header lines:
```bash
# Generate hot signing keys for the hot-key-signer environments: `local` and
# `staging` (per the staging design, staging signs from key files for fast
# iteration). NEVER run this against a prod credentials dir.
```
with:
```bash
# Generate hot signing keys for the hot-key-signer environments (local, staging,
# and prod). Prod hot signers (deployer, relayer x3, validator announce x2) are
# generated on the deploy host by prepare-prod.yml — they are hot keys in every
# env and must live in the on-host credentials dir for laconic-so to read them.
# It refuses to overwrite existing files, so it cannot clobber funded keys.
```
And replace the banner line:
```bash
 Hot keys only. Do NOT use for prod.
```
with:
```bash
 Hot signing keys (local / staging / prod). Existing files are never touched.
```

- [ ] **Step 2: Create `ops/playbooks/prepare-prod.yml`**

```yaml
---
# Prod key prep — generate the hot signing keys on the deploy host(s), distribute
# each keyfile to the host whose spec reads it, then run the funding gate (report
# only; mainnet has no faucet). Run AFTER setup-all.yml, BEFORE deploy-all.yml.
# Idempotent: existing keyfiles are never overwritten; funding is balance-checked.
#
#   ansible-playbook -i inventories/prod/hosts.yml playbooks/prepare-prod.yml
#
# Unlike staging's prepare-gorchain, prod stands up NO chain (gorchain is external
# mainnet) and fronts NO RPC — it only mints + places keys and checks funding.
- name: Prepare prod hot signing keys + funding gate
  hosts: deployer_hosts
  gather_facts: true
  vars:
    scripts_dst: "{{ ansible_env.HOME }}/.bridge-setup-scripts"
    solana_bin: "{{ ansible_env.HOME }}/.local/share/solana/install/active_release/bin"
    tool_path: "{{ ansible_env.HOME }}/bin:{{ solana_bin }}:{{ ansible_env.PATH }}"
    solana_version: "v3.1.9"
  pre_tasks:
    - name: Load the deployment config
      ansible.builtin.include_tasks: ../roles/common/tasks/load_deployment_config.yml
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

    - name: Ship the key-gen + funding-verification scripts
      ansible.builtin.copy:
        src: "../scripts/{{ item }}"
        dest: "{{ scripts_dst }}/{{ item }}"
        mode: "0755"
      loop:
        - gen-local-keys.sh
        - verify-signer-funding.sh
        - fund-test-wallets.sh

    - name: Generate the hot signing keys (existing files are never overwritten)
      ansible.builtin.command:
        cmd: "{{ scripts_dst }}/gen-local-keys.sh --yes"
      environment:
        PATH: "{{ tool_path }}"
        CRED_DIR: "{{ credentials_dir }}"
      register: keys_out
      changed_when: true

    # Carry each keyfile to the host whose spec reads it (the `file:` refs in
    # deployment/spec-*.yml). On single-host every consumer IS this host, so
    # _key_dist is empty and the copy loop is a no-op.
    - name: Derive the validator hosts (validators.yaml)
      ansible.builtin.include_tasks: ../roles/common/tasks/load_validators.yml

    - name: Map each keyfile to its consumer host
      ansible.builtin.set_fact:
        _key_dist: >-
          {%- set pairs = [] -%}
          {%- for grp, files in _group_keyfiles.items() -%}
            {%- for host in groups[grp] | default([]) | unique -%}
              {%- for f in files -%}
                {%- if pairs.append({'host': host, 'file': f}) -%}{%- endif -%}
              {%- endfor -%}
            {%- endfor -%}
          {%- endfor -%}
          {%- for v in validators -%}
            {%- if pairs.append({'host': v.host, 'file': 'validator-' ~ v.chain ~ '.key'}) -%}{%- endif -%}
          {%- endfor -%}
          {{ pairs | rejectattr('host', 'eq', inventory_hostname) | list }}
      vars:
        _group_keyfiles:
          relayer_hosts: [relayer-gorchain.key, relayer-solana.key, relayer-fee-claim.json]

    - name: Gather the consumer hosts' facts (credentials_dir derives from their HOME)
      ansible.builtin.setup:
      delegate_to: "{{ item }}"
      delegate_facts: true
      loop: "{{ _key_dist | map(attribute='host') | unique | list }}"

    - name: Ensure the credentials dir on each consumer host
      ansible.builtin.file:
        path: "{{ hostvars[item].credentials_dir }}"
        state: directory
        mode: "0700"
      delegate_to: "{{ item }}"
      loop: "{{ _key_dist | map(attribute='host') | unique | list }}"

    - name: Read the generated keyfiles
      ansible.builtin.slurp:
        src: "{{ credentials_dir }}/{{ item.file }}"
      register: _key_blobs
      loop: "{{ _key_dist }}"
      no_log: true

    - name: Carry each keyfile to its consumer host
      ansible.builtin.copy:
        content: "{{ item.content | b64decode }}"
        dest: "{{ hostvars[item.item.host].credentials_dir }}/{{ item.item.file }}"
        mode: "0600"
      delegate_to: "{{ item.item.host }}"
      loop: "{{ _key_blobs.results }}"
      no_log: true

    - name: Verify funding (report-only; fails listing shortfalls)
      ansible.builtin.command:
        cmd: "{{ scripts_dst }}/verify-signer-funding.sh"
      environment:
        PATH: "{{ tool_path }}"
        CRED_DIR: "{{ credentials_dir }}"
        GORCHAIN_RPC: "{{ gorchain_rpc_url | default('https://rpc.gorbagana.wtf') }}"
        SOLANA_RPC: "{{ SOLANA_RPC_URL }}"
        ORACLE_PUBKEY: "{{ igp_oracle_pubkey }}"
      register: fund_out
      changed_when: false
      failed_when: false

    - name: Show generated key addresses + funding results
      ansible.builtin.debug:
        msg: "{{ keys_out.stdout_lines + fund_out.stdout_lines }}"

    - name: Every signer reached its target balance
      ansible.builtin.assert:
        that: fund_out.rc == 0
        fail_msg: >-
          Some signers are underfunded (listed above) — fund them from a treasury
          wallet (mainnet has no faucet), then re-run; funding is balance-checked.
        quiet: true
```

- [ ] **Step 3: Shellcheck the (edited) key-gen script**

```bash
shellcheck ops/scripts/gen-local-keys.sh
```
Expected: clean (the edits are comment-only).

- [ ] **Step 4: Syntax-check the playbook**

```bash
cd ops && ansible-playbook -i inventories/prod/hosts.yml playbooks/prepare-prod.yml --syntax-check
```
Expected: no syntax errors.

- [ ] **Step 5: Lint**

```bash
cd ops && yamllint . && ansible-lint .
```
Expected: no new errors.

- [ ] **Step 6: HAND OFF — live key prep (operator, needs a prod host)**

```bash
# (operator)
ansible-playbook -i inventories/prod/hosts.yml playbooks/prepare-prod.yml
```

- [ ] **Step 7: Commit**

```bash
cd /home/dev/git_puller/repos/hyperlane-stacks
git add ops/playbooks/prepare-prod.yml ops/scripts/gen-local-keys.sh
git commit -m "$(cat <<'EOF'
ops(prod): prepare-prod key generation + funding gate

Generates the hot signing keys on the deploy host, distributes each keyfile
to the host whose spec reads it, and runs the report-only funding gate. No
chain bring-up (gorchain is external mainnet). Sanctions prod key
generation in gen-local-keys.sh (hot keys must live on-host regardless).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 11: `retire-deployer-key.yml` — drain + archive the one-shot deployer key (C3)

**Files:**
- Create: `ops/scripts/drain-deployer-key.sh`
- Create: `ops/playbooks/retire-deployer-key.yml`

- [ ] **Step 1: Create `ops/scripts/drain-deployer-key.sh`**

```bash
#!/usr/bin/env bash
# Drain the deployer key's balance on both chains to a treasury address, leaving a
# small rent/fee buffer. The deployer key is one-shot: after deploy + ownership
# handoff no running pod needs it. REQUIRES an explicit treasury address.
#
# Env: DEPLOYER_KEYFILE  (required) path to deployer-keypair.json
#      TREASURY_ADDRESS  (required) base58 destination
#      GORCHAIN_RPC      [https://rpc.gorbagana.wtf]
#      SOLANA_RPC        (required) Helius mainnet RPC URL
#      RENT_BUFFER_SOL   [0.01] left behind on each chain
set -euo pipefail

DEPLOYER_KEYFILE="${DEPLOYER_KEYFILE:?set DEPLOYER_KEYFILE}"
TREASURY_ADDRESS="${TREASURY_ADDRESS:?set TREASURY_ADDRESS}"
GORCHAIN_RPC="${GORCHAIN_RPC:-https://rpc.gorbagana.wtf}"
SOLANA_RPC="${SOLANA_RPC:?set SOLANA_RPC (the Helius mainnet RPC URL)}"
RENT_BUFFER_SOL="${RENT_BUFFER_SOL:-0.01}"

command -v solana >/dev/null || { echo "ERROR: solana CLI not found"; exit 1; }
[ -f "$DEPLOYER_KEYFILE" ] || { echo "ERROR: $DEPLOYER_KEYFILE not found"; exit 1; }

drain() {  # <rpc> <label>
  local rpc=$1 label=$2 bal
  bal=$(solana balance "$DEPLOYER_KEYFILE" --url "$rpc" 2>/dev/null | awk '{print $1}') || bal=0
  echo "$label: deployer balance ${bal} SOL"
  # solana transfer with ALL leaves the account empty; keep a buffer by transferring
  # (balance - buffer) only when there is something worth moving.
  awk -v b="$bal" -v r="$RENT_BUFFER_SOL" 'BEGIN{exit !(b>r+0.001)}' || { echo "  nothing to drain"; return 0; }
  local amount
  amount=$(awk -v b="$bal" -v r="$RENT_BUFFER_SOL" 'BEGIN{printf "%.9f", b-r}')
  echo "  transferring ${amount} SOL to ${TREASURY_ADDRESS}"
  solana transfer "$TREASURY_ADDRESS" "$amount" \
    --from "$DEPLOYER_KEYFILE" --fee-payer "$DEPLOYER_KEYFILE" \
    --url "$rpc" --allow-unfunded-recipient --no-wait
}

drain "$GORCHAIN_RPC" "gorchain"
drain "$SOLANA_RPC"   "solana"
echo "Drain submitted on both chains."
```

- [ ] **Step 2: Shellcheck it**

```bash
shellcheck ops/scripts/drain-deployer-key.sh
```
Expected: clean.

- [ ] **Step 3: Create `ops/playbooks/retire-deployer-key.yml`**

```yaml
---
# Post-deploy: retire the one-shot deployer key. Drains its balance to a treasury,
# archives the keyfile to the operator's machine, and removes it on-box. The
# deployer is a completed Job, so removing its keyfile does not affect running pods
# (relayer/validator keyfiles MUST stay — deployment restart re-reads them).
# Re-deploying additional warp routes later needs a funded deployer key again, so
# re-import the archived file first.
#
#   ansible-playbook -i inventories/prod/hosts.yml playbooks/retire-deployer-key.yml \
#     -e treasury_address=<BASE58> -e confirm_retire=true
- name: Retire the deployer key
  hosts: deployer_hosts
  gather_facts: true
  vars:
    scripts_dst: "{{ ansible_env.HOME }}/.bridge-setup-scripts"
    solana_bin: "{{ ansible_env.HOME }}/.local/share/solana/install/active_release/bin"
    tool_path: "{{ ansible_env.HOME }}/bin:{{ solana_bin }}:{{ ansible_env.PATH }}"
    archive_dir: "{{ playbook_dir }}/../.deployer-key-archive"
  pre_tasks:
    - name: Load the deployment config
      ansible.builtin.include_tasks: ../roles/common/tasks/load_deployment_config.yml
    - name: Require an explicit treasury + confirm
      ansible.builtin.assert:
        that:
          - treasury_address is defined and (treasury_address | length) > 0
          - confirm_retire | default(false) | bool
        fail_msg: >-
          Pass -e treasury_address=<BASE58> -e confirm_retire=true. This drains the
          deployer key and removes it on-box.
        quiet: true
  tasks:
    - name: Ship the drain script
      ansible.builtin.copy:
        src: "../scripts/drain-deployer-key.sh"
        dest: "{{ scripts_dst }}/drain-deployer-key.sh"
        mode: "0755"
    - name: Drain the deployer balance to the treasury
      ansible.builtin.command:
        cmd: "{{ scripts_dst }}/drain-deployer-key.sh"
      environment:
        PATH: "{{ tool_path }}"
        DEPLOYER_KEYFILE: "{{ credentials_dir }}/deployer-keypair.json"
        TREASURY_ADDRESS: "{{ treasury_address }}"
        GORCHAIN_RPC: "{{ gorchain_rpc_url | default('https://rpc.gorbagana.wtf') }}"
        SOLANA_RPC: "{{ SOLANA_RPC_URL }}"
      register: _drain
      changed_when: true
    - name: Show the drain result
      ansible.builtin.debug:
        var: _drain.stdout_lines
    - name: Ensure the local archive dir exists (gitignored)
      ansible.builtin.file:
        path: "{{ archive_dir }}"
        state: directory
        mode: "0700"
      delegate_to: localhost
    - name: Archive the deployer keyfile to the operator machine
      ansible.builtin.fetch:
        src: "{{ credentials_dir }}/deployer-keypair.json"
        dest: "{{ archive_dir }}/deployer-keypair.json"
        flat: true
    - name: Remove the deployer keyfile on-box (Job is complete; pods unaffected)
      ansible.builtin.file:
        path: "{{ credentials_dir }}/deployer-keypair.json"
        state: absent
```

- [ ] **Step 4: Gitignore the archive dir**

Append to `ops/.gitignore` (create if absent) the line `.deployer-key-archive/`.
Verify:
```bash
grep -q ".deployer-key-archive" ops/.gitignore && echo "ignored"
```
Expected: `ignored`.

- [ ] **Step 5: Syntax-check + lint**

```bash
cd ops && ansible-playbook -i inventories/prod/hosts.yml playbooks/retire-deployer-key.yml --syntax-check
cd ops && yamllint . && ansible-lint .
```
Expected: no syntax errors; no new lint errors.

- [ ] **Step 6: HAND OFF — live retirement (operator, post-deploy)**

```bash
# (operator) after a successful deploy + ownership handoff:
ansible-playbook -i inventories/prod/hosts.yml playbooks/retire-deployer-key.yml \
  -e treasury_address=<BASE58> -e confirm_retire=true
```

- [ ] **Step 7: Commit**

```bash
cd /home/dev/git_puller/repos/hyperlane-stacks
git add ops/scripts/drain-deployer-key.sh ops/playbooks/retire-deployer-key.yml ops/.gitignore
git commit -m "$(cat <<'EOF'
ops(prod): retire-deployer-key — drain + archive the one-shot deployer key

Post-deploy, drains the deployer balance to a treasury (runtime -e var),
archives the keyfile to the operator machine, and removes it on-box. The
deployer is a completed Job, so this doesn't touch running pods; relayer and
validator keyfiles stay on-box because deployment restart re-reads them.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

# Phase 5 — Documentation

### Task 12: Prod runbook + index updates

**Files:**
- Rewrite: `ops/runbooks/prod.md`
- Modify: `ops/runbooks/README.md` (prod row)
- Modify: `README.md` (prod row — drop "in progress")
- Modify: `docs/stack-specifications.md` (note the topology-aware S3 endpoint under the validator/relayer/MinIO stacks)

- [ ] **Step 1: Rewrite `ops/runbooks/prod.md`** as a from-zero guide mirroring `staging.md`'s structure (Prereqs → Privy → host/inventory → deployment-config → key prep + funding → deploy → verify → try the bridge → reset), with ONLY the prod deltas. It must cover, in order:

  1. **Overview** — prod runs against **external mainnet gorchain** (`https://rpc.gorbagana.wtf`) + **Helius mainnet**, single host by default under `bridge.gorbagana.wtf` (Cloudflare + Let's Encrypt). No chain host, no `prepare-gorchain`.
  2. **Prerequisites** — controller setup (same as `ops/README.md`); a Helius **mainnet** project; a Cloudflare token for the `gorbagana.wtf` zone; Privy app with oracle + bridge-owner + per-validator wallets (link `privy-wallets.md`).
  3. **The single host** — one VM running everything incl. both validators; note the multi-host opt-out (move a validator's `host:` in `validators.yaml`, add the host to `hosts.yml` + `host_vars`).
  4. **Inventory + secrets** — fill `host_vars/bridge-host-1.yml` (`public_ip`) and copy `deployment-config.example.yml` → `deployment-config.yml`; call out `helius_api_key` is the **mainnet** key, `igp_beneficiary_pubkey` defaults to the bridge owner.
  5. **Provision** — `setup-all.yml` (reconciles real DNS).
  6. **Key prep + funding** — `prepare-prod.yml` generates keys, lists addresses + funding gaps; **fund each listed address from a treasury** (mainnet — no faucet); re-run until the gate passes. Warp collateral is mainnet USDC (`EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`).
  7. **Deploy** — deploy off a dedicated branch (never `main`); `deploy-all.yml -e deploy_branch=<branch> -e state_review=true`; note the prod funding gate runs first.
  8. **Verify** — warp-UI at `https://bridge.gorbagana.wtf`, Grafana/MinIO consoles, relayer; explorer at `https://explorer.bridge.gorbagana.wtf`.
  9. **Try the bridge** — Backpack against mainnet (point its RPC per direction as in staging).
  10. **Retire the deployer key** — `retire-deployer-key.yml -e treasury_address=<addr> -e confirm_retire=true`; relayer/validator keyfiles stay (restart re-reads them).
  11. **Reset** — `stop-all.yml`; note chain state is external (nothing chain-side to reset).

  Remove the "Placeholder — do not follow" banner entirely.

- [ ] **Step 2: Verify the runbook has no leftover placeholder banner and links resolve**

```bash
grep -ni "placeholder\|still to be written\|in progress" ops/runbooks/prod.md || echo "no placeholder text"
grep -oE "\]\([^)]+\.md[^)]*\)" ops/runbooks/prod.md | sed -E 's/.*\(([^)]+)\)/\1/' | sed 's/#.*//' \
  | while read -r f; do [ -e "ops/runbooks/$f" ] || [ -e "$f" ] || echo "BROKEN LINK: $f"; done
echo "link check done"
```
Expected: `no placeholder text` and no `BROKEN LINK` lines.

- [ ] **Step 3: Update `ops/runbooks/README.md` prod row**

Replace the prod table row's "placeholder — guide still to be written" with a real
description: `Production (mainnet) | mainnet gorchain (external) + Helius mainnet | Cloudflare + Let's Encrypt`.

- [ ] **Step 4: Update the root `README.md` prod row**

Replace `| **prod** | [ops/runbooks/prod.md](ops/runbooks/prod.md) | Mainnet (runbook in progress) |`
with `| **prod** | [ops/runbooks/prod.md](ops/runbooks/prod.md) | Mainnet: external gorchain + Helius mainnet, single host under bridge.gorbagana.wtf |`.

- [ ] **Step 5: Note the topology-aware S3 endpoint in `docs/stack-specifications.md`**

In the validator/relayer/MinIO sections of `docs/stack-specifications.md`, add a
sentence: the `AWS_ENDPOINT_URL_S3` is topology-aware in prod (rendered to the
in-cluster `http://hyperlane-minio:9000` on single-host via selector-mode
`external-services:`, the public `https://s3.<base_domain>` on multi-host), so a
single-host prod avoids the public-URL MinIO loopback.

- [ ] **Step 6: Markdown lints clean (no tooling required) — re-read for correctness**

Read the rewritten `prod.md` top-to-bottom once and confirm every command matches
the playbook/var names introduced in Tasks 9–11 (`prepare-prod.yml`,
`verify-funding.yml`, `retire-deployer-key.yml`, `-e treasury_address`,
`-e confirm_retire`).

- [ ] **Step 7: Commit**

```bash
git add ops/runbooks/prod.md ops/runbooks/README.md README.md docs/stack-specifications.md
git commit -m "$(cat <<'EOF'
docs(prod): from-zero production runbook

Replaces the prod.md placeholder with a full guide mirroring staging:
external mainnet gorchain, Helius mainnet, single-host default, operator
key prep + manual funding gate, deploy, verify, deployer-key retirement.
Updates the runbook index + README rows and notes the topology-aware S3
endpoint in stack-specifications.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

# Final verification (after all tasks)

- [ ] **Static suite is green**

```bash
cd /home/dev/git_puller/repos/hyperlane-stacks
python3 ops/scripts/check-spec-parity.py          # Spec shape parity OK: 9 specs match.
cd ops && yamllint . && ansible-lint .             # clean
# Run the FULL ops test suite — the wallet_connect_id gate touches the shared
# load_deployment_config, so confirm no other test regressed.
cd ops && for t in tests/test_*.yml; do \
  echo "== $t =="; ansible-playbook -i inventories/prod/hosts.yml "$t" || break; done   # all PASS
shellcheck ops/scripts/verify-signer-funding.sh ops/scripts/drain-deployer-key.sh ops/scripts/gen-local-keys.sh
```

- [ ] **Dispatch the final code reviewer** for the whole branch, then use `superpowers:finishing-a-development-branch`.

- [ ] **HAND OFF the live bring-up** to the operator following `ops/runbooks/prod.md` (this machine does not run stacks/chains).

---

## Notes for the executor

- **Order matters only across phases, not within.** Phase 1 (cleanup) is fully
  independent; Phases 2–5 build on each other (Task 5 reads the validator set from
  Task 4; Tasks 9–11 are referenced by the runbook in Task 12).
- **Do not run any `ansible-playbook` against real hosts**, and do not start kind
  clusters or chains here. Static checks + localhost assertion tests only.
- The `topology` derivation makes `ops/tests/test_prod_env.yml` inventory-sensitive:
  always run it with `-i inventories/prod/hosts.yml` so `groups` reflects prod.
