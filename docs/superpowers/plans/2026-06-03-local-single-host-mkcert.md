# Local single-host mkcert (no Cloudflare) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the single-host `local` ops topology bring up the whole bridge with self-trusted mkcert certs and in-cluster MinIO/scrape — no Cloudflare, no public DNS — while multi-host keeps Let's Encrypt + Cloudflare, both from one committed spec tree.

**Architecture:** A derived `topology` var (`single`|`multi`) selects everything. All spec divergence rides the existing `spec_token_renders` replace step in `ops/roles/stack_deploy/tasks/deploy.yml` — value tokens for the S3 endpoint / Prometheus targets, and comment-marker tokens that expand to `external-services:` blocks (single) or empty (multi). A new host-only `local_tls` role generates an mkcert cert, pre-seeds it into Caddy's restore path, trusts the CA + writes `/etc/hosts` on the host, and publishes `rootCA.pem` for the operator to fetch. SO's existing `_restore_caddy_certs` does the Caddy side with no SO change.

**Tech Stack:** Ansible (YAML, Jinja2), laconic-so / stack-orchestrator (k8s-kind, caddy-ingress), mkcert, yamllint + ansible-lint.

**Spec:** `docs/superpowers/specs/2026-06-03-local-single-host-mkcert-design.md`

**Branch:** Work continues on the current `local-own-chains-env` branch. Never push (the user pushes). Commit per task. End commit messages with the `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` trailer.

---

## File Structure

- `ops/inventories/local/group_vars/all.yml` — add `topology` + `manage_dns` derivations, the topology-conditional `spec_token_renders` (value tokens + two structural-block scalars), and a topology-conditional `required_operator_secrets`.
- `deployment/local/spec-validator-gorchain.yml`, `deployment/local/spec-validator-solana.yml` — `AWS_ENDPOINT_URL_S3` becomes `__S3_ENDPOINT__`; append a `# __SINGLE_HOST_MINIO_XS__` comment marker at column 0.
- `deployment/local/spec-monitoring.yml` — Prometheus target/scheme tokens; append `# __SINGLE_HOST_PROM_XS__`.
- `ops/roles/local_tls/tasks/main.yml` — new host-only role.
- `ops/roles/local_tls/templates/caddy-secrets.yaml.j2` — new Caddy pre-seed template.
- `ops/roles/local_tls/defaults/main.yml` — new role defaults.
- `ops/playbooks/local-tls.yml` — new play, imported by `setup-all.yml`.
- `ops/playbooks/setup-all.yml` — import the new play.
- `ops/tests/test_local_env.yml` — extend assertions for both topologies.
- `ops/runbooks/local.md` — single-host needs no DNS provider; tunnel-based browsing; drop the local-ACME fallback; hairpin gone for single-host.
- `docs/superpowers/specs/2026-06-01-deploy-side-ansible-design.md` — own-chains section reflects the single/multi split.

A note on the test harness: `ops/tests/test_local_env.yml` runs `hosts: localhost` and `include_vars` the local `group_vars/all.yml`. Because `topology` is defined there as a Jinja expression over `groups[...]` (absent in a localhost play), every task that reads a topology-dependent var **must** be preceded by a `set_fact: topology=...` (set_fact outranks the group_var, and Ansible vars are lazy, so the override is what gets evaluated). The tasks below always set `topology` first.

---

## Task 1: Derive topology + manage_dns; topology-gate the Cloudflare secret

**Files:**
- Modify: `ops/inventories/local/group_vars/all.yml`
- Test: `ops/tests/test_local_env.yml`

- [ ] **Step 1: Write the failing assertions**

Replace the two existing Cloudflare-secret assertions in `ops/tests/test_local_env.yml` (the lines asserting `'cloudflare_api_token' in required_operator_secrets` and `'helius_api_key' not in ...`) by splitting into two plays — one per topology. Replace the whole file with:

```yaml
---
- name: Local single-host wiring (mkcert, no Cloudflare, in-cluster MinIO/scrape)
  hosts: localhost
  connection: local
  gather_facts: false
  vars:
    ansible_env:
      HOME: /home/op
  tasks:
    - name: Load local env wiring
      ansible.builtin.include_vars:
        file: "{{ playbook_dir }}/../inventories/local/group_vars/all.yml"

    - name: Stand in operator-supplied values + single-host topology
      ansible.builtin.set_fact:
        dns_zone: "hyperlane.local"
        gorchain_rpc_url: "https://gorchain-rpc.example"
        solana_rpc_url: "https://solana-rpc.example"
        topology: "single"
        validators:
          - { label: gorchain-primary, chain: gorchain }
          - { label: solana-primary, chain: solana }

    - name: Single-host wiring resolves as designed
      ansible.builtin.assert:
        that:
          - "deployment_root == '/home/op/deployments/hyperlane-stacks/deployment/local'"
          - "manage_dns | bool == false"
          # in-cluster MinIO + HTTP scrape
          - "spec_token_renders['__S3_ENDPOINT__'] == 'http://hyperlane-minio:9000'"
          - "spec_token_renders['__PROM_SCRAPE_SCHEME__'] == 'http'"
          - "'validator-gorchain:9090' in spec_token_renders['__PROM_VALIDATOR_TARGETS__']"
          - "spec_token_renders['__PROM_RELAYER_TARGETS__'] == 'primary=relayer:9091'"
          # structural blocks expand for single-host
          - "'laconic-hyperlane-minio' in spec_token_renders['# __SINGLE_HOST_MINIO_XS__']"
          - "'hyperlane-relayer' in spec_token_renders['# __SINGLE_HOST_PROM_XS__']"
          # single-host needs no Cloudflare token
          - "'cloudflare_api_token' not in required_operator_secrets"
          - "'privy_app_id' in required_operator_secrets"
          - "'ghcr_pat' in required_operator_secrets"
          # chain RPCs are rendered config: literals, never secret-env names
          - "'SOLANA_RPC_URL' not in (stack_env_vars.values() | map('list') | flatten | unique)"

- name: Local multi-host wiring (Let's Encrypt + Cloudflare, Caddy-fronted MinIO/scrape)
  hosts: localhost
  connection: local
  gather_facts: false
  vars:
    ansible_env:
      HOME: /home/op
  tasks:
    - name: Load local env wiring
      ansible.builtin.include_vars:
        file: "{{ playbook_dir }}/../inventories/local/group_vars/all.yml"

    - name: Stand in operator-supplied values + multi-host topology
      ansible.builtin.set_fact:
        dns_zone: "staging.gorbagana.wtf"
        gorchain_rpc_url: "https://gorchain-rpc.example"
        solana_rpc_url: "https://solana-rpc.example"
        topology: "multi"

    - name: Multi-host wiring resolves as designed
      ansible.builtin.assert:
        that:
          - "manage_dns | bool == true"
          - "spec_token_renders['__S3_ENDPOINT__'] == 'https://s3.staging.gorbagana.wtf'"
          - "spec_token_renders['__PROM_SCRAPE_SCHEME__'] == 'https'"
          - "'validator-gorchain.staging.gorbagana.wtf:443' in spec_token_renders['__PROM_VALIDATOR_TARGETS__']"
          - "spec_token_renders['__PROM_RELAYER_TARGETS__'] == 'primary=relayer.staging.gorbagana.wtf:443'"
          # structural markers render to empty for multi-host
          - "spec_token_renders['# __SINGLE_HOST_MINIO_XS__'] == ''"
          - "spec_token_renders['# __SINGLE_HOST_PROM_XS__'] == ''"
          # multi-host needs Cloudflare
          - "'cloudflare_api_token' in required_operator_secrets"
          # derived DNS map unchanged
          - "dns_record_map.s3 == 'minio_hosts'"
          - "dns_record_map.relayer == 'relayer_hosts'"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd ops && ansible-playbook -i inventories/local/hosts.yml tests/test_local_env.yml`
Expected: FAIL — `topology`/`manage_dns`/the new `spec_token_renders` keys don't exist yet (undefined-variable or assertion error).

- [ ] **Step 3: Add the topology + manage_dns derivations**

In `ops/inventories/local/group_vars/all.yml`, under the `# --- DNS / TLS ---` section, immediately after the `# --- DNS / TLS ---` comment block and before `dns_zone:`, add:

```yaml
# Topology is DERIVED from inventory group membership: in single-host MinIO and the
# agents (relayer + validators) share a host = one kind cluster, the precondition for
# the in-cluster MinIO/scrape paths; in multi-host they don't. No flag, no per-inventory
# var — the shared group_vars serves both hosts.yml and hosts-multihost.yml.
topology: "{{ 'single' if (groups['minio_hosts'][0] == groups['relayer_hosts'][0]) else 'multi' }}"
# Single-host uses mkcert (local_tls role) and needs no DNS provider; multi-host
# reconciles Cloudflare. configure-dns.yml is gated on this.
manage_dns: "{{ topology == 'multi' }}"
```

- [ ] **Step 4: Make required_operator_secrets topology-conditional**

In the same file, replace the existing block:

```yaml
# Local needs no Helius; it does need Cloudflare (DNS) like prod/staging.
required_operator_secrets:
  - cloudflare_api_token
  - privy_app_id
  - privy_app_secret
  - privy_oracle_wallet_id
  - ghcr_pat
```

with:

```yaml
# Local needs no Helius. Cloudflare is multi-host only (single-host uses mkcert).
required_operator_secrets: >-
  {{ (['cloudflare_api_token'] if manage_dns | bool else [])
     + ['privy_app_id', 'privy_app_secret', 'privy_oracle_wallet_id', 'ghcr_pat'] }}
```

- [ ] **Step 5: Run the test to verify the topology/manage_dns/secrets assertions pass**

Run: `cd ops && ansible-playbook -i inventories/local/hosts.yml tests/test_local_env.yml`
Expected: still FAILS, but now only on the `spec_token_renders['__S3_ENDPOINT__']` etc. assertions (Task 2). The `manage_dns`, `required_operator_secrets`, and `deployment_root` assertions pass. Confirm the failure is the token assertions, not the secrets ones.

- [ ] **Step 6: Commit**

```bash
git add ops/inventories/local/group_vars/all.yml ops/tests/test_local_env.yml
git commit -m "$(printf 'feat(ops): derive local topology; gate Cloudflare to multi-host\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

## Task 2: Topology-conditional spec_token_renders (value tokens + structural blocks)

**Files:**
- Modify: `ops/inventories/local/group_vars/all.yml`
- Test: `ops/tests/test_local_env.yml` (already written in Task 1)

- [ ] **Step 1: Replace the spec_token_renders block**

In `ops/inventories/local/group_vars/all.yml`, replace the existing block:

```yaml
gorchain_rpc_url: "REPLACE_WITH_GORCHAIN_RPC_URL"
solana_rpc_url: "REPLACE_WITH_SOLANA_RPC_URL"
spec_token_renders:
  __DNS_ZONE__: "{{ dns_zone }}"
  __GORCHAIN_RPC_URL__: "{{ gorchain_rpc_url }}"
  __SOLANA_RPC_URL__: "{{ solana_rpc_url }}"
```

with (note the two block scalars defined first, then the map):

```yaml
gorchain_rpc_url: "REPLACE_WITH_GORCHAIN_RPC_URL"
solana_rpc_url: "REPLACE_WITH_SOLANA_RPC_URL"

# Single-host external-services blocks. Rendered into the validator/monitoring specs in
# place of a comment marker; multi-host renders the marker to ''. Column-0 top-level keys
# (the marker comment sits at column 0). MinIO leg: validator dials the MinIO pod
# cross-NS over plain HTTP (aws-sdk-rust can't trust a non-public CA). Monitoring leg:
# Prometheus scrapes validators/relayer in-cluster.
_single_host_minio_xs: |
  external-services:
    hyperlane-minio:
      selector:
        app.kubernetes.io/stack: hyperlane-minio
      namespace: laconic-hyperlane-minio
      port: 9000
_single_host_prom_xs: |
  external-services:
    validator-gorchain:
      selector:
        app.kubernetes.io/stack: hyperlane-validator
      namespace: laconic-hyperlane-validator-gorchain
      port: 9090
    validator-solana:
      selector:
        app.kubernetes.io/stack: hyperlane-validator
      namespace: laconic-hyperlane-validator-solana
      port: 9090
    relayer:
      selector:
        app.kubernetes.io/stack: hyperlane-relayer
      namespace: laconic-hyperlane-relayer
      port: 9091

spec_token_renders:
  __DNS_ZONE__: "{{ dns_zone }}"
  __GORCHAIN_RPC_URL__: "{{ gorchain_rpc_url }}"
  __SOLANA_RPC_URL__: "{{ solana_rpc_url }}"
  # single: validator dials MinIO in-cluster; multi: via the Caddy-fronted LE endpoint
  __S3_ENDPOINT__: "{{ 'http://hyperlane-minio:9000' if topology == 'single' else 'https://s3.' ~ dns_zone }}"
  __PROM_SCRAPE_SCHEME__: "{{ 'http' if topology == 'single' else 'https' }}"
  __PROM_VALIDATOR_TARGETS__: >-
    {{ 'gorchain-primary=validator-gorchain:9090,solana-primary=validator-solana:9090'
       if topology == 'single'
       else 'gorchain-primary=validator-gorchain.' ~ dns_zone ~ ':443,solana-primary=validator-solana.' ~ dns_zone ~ ':443' }}
  __PROM_RELAYER_TARGETS__: "{{ 'primary=relayer:9091' if topology == 'single' else 'primary=relayer.' ~ dns_zone ~ ':443' }}"
  "# __SINGLE_HOST_MINIO_XS__": "{{ _single_host_minio_xs if topology == 'single' else '' }}"
  "# __SINGLE_HOST_PROM_XS__": "{{ _single_host_prom_xs if topology == 'single' else '' }}"
```

- [ ] **Step 2: Run the test to verify both topologies pass**

Run: `cd ops && ansible-playbook -i inventories/local/hosts.yml tests/test_local_env.yml`
Expected: PASS — both plays (single + multi) green. (`>-` folds the multi-line target expression to a single line with no trailing newline, so the `==` and `in` assertions match exactly.)

- [ ] **Step 3: yamllint the group_vars**

Run: `cd ops && yamllint inventories/local/group_vars/all.yml`
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add ops/inventories/local/group_vars/all.yml
git commit -m "$(printf 'feat(ops): topology-conditional spec token renders for local\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

## Task 3: Token the validator + monitoring specs

**Files:**
- Modify: `deployment/local/spec-validator-gorchain.yml`
- Modify: `deployment/local/spec-validator-solana.yml`
- Modify: `deployment/local/spec-monitoring.yml`

- [ ] **Step 1: Validator (gorchain) — S3 endpoint token + MinIO marker**

In `deployment/local/spec-validator-gorchain.yml`, change the `AWS_ENDPOINT_URL_S3` line under `config:` from:

```yaml
  AWS_ENDPOINT_URL_S3: "https://s3.__DNS_ZONE__"
```

to:

```yaml
  # Rendered per topology: single -> http://hyperlane-minio:9000 (in-cluster, plain
  # HTTP); multi -> https://s3.<zone> (Caddy + LE). See ops group_vars spec_token_renders.
  AWS_ENDPOINT_URL_S3: "__S3_ENDPOINT__"
```

Then append, as the **last lines of the file** (column 0, after the `network:` block):

```yaml
# Single-host only: validator reaches the MinIO pod cross-NS in-cluster. Rendered to an
# external-services: block on single-host, removed on multi-host.
# __SINGLE_HOST_MINIO_XS__
```

- [ ] **Step 2: Validator (solana) — same two edits**

In `deployment/local/spec-validator-solana.yml`, apply the identical change to `AWS_ENDPOINT_URL_S3`:

```yaml
  # Rendered per topology: single -> http://hyperlane-minio:9000 (in-cluster, plain
  # HTTP); multi -> https://s3.<zone> (Caddy + LE). See ops group_vars spec_token_renders.
  AWS_ENDPOINT_URL_S3: "__S3_ENDPOINT__"
```

and append the identical marker block as the last lines of the file:

```yaml
# Single-host only: validator reaches the MinIO pod cross-NS in-cluster. Rendered to an
# external-services: block on single-host, removed on multi-host.
# __SINGLE_HOST_MINIO_XS__
```

- [ ] **Step 3: Monitoring — target/scheme tokens + Prometheus marker**

In `deployment/local/spec-monitoring.yml`, replace the two target lines under `config:`:

```yaml
  PROMETHEUS_VALIDATOR_TARGETS: "gorchain-primary=validator-gorchain.__DNS_ZONE__:443,solana-primary=validator-solana.__DNS_ZONE__:443"
  PROMETHEUS_RELAYER_TARGETS: "primary=relayer.__DNS_ZONE__:443"
```

with:

```yaml
  # Rendered per topology: single -> in-cluster names + http; multi -> <zone>:443 + https.
  PROMETHEUS_VALIDATOR_TARGETS: "__PROM_VALIDATOR_TARGETS__"
  PROMETHEUS_RELAYER_TARGETS: "__PROM_RELAYER_TARGETS__"
  PROMETHEUS_SCRAPE_SCHEME: "__PROM_SCRAPE_SCHEME__"
```

Then append, as the **last lines of the file** (column 0):

```yaml
# Single-host only: Prometheus scrapes validators/relayer cross-NS in-cluster. Rendered
# to an external-services: block on single-host, removed on multi-host.
# __SINGLE_HOST_PROM_XS__
```

- [ ] **Step 4: yamllint the three specs**

Run: `cd /home/dev/git_puller/repos/hyperlane-stacks && yamllint deployment/local/spec-validator-gorchain.yml deployment/local/spec-validator-solana.yml deployment/local/spec-monitoring.yml`
Expected: no errors (the markers are valid YAML comments; the tokens are quoted string values).

- [ ] **Step 5: Verify a single-host render produces valid YAML**

This proves the comment-marker → block substitution yields a parseable spec. Run:

```bash
cd /home/dev/git_puller/repos/hyperlane-stacks && \
cp deployment/local/spec-validator-gorchain.yml /tmp/render-test.yml && \
python3 - <<'PY'
import re, yaml
block = """external-services:
  hyperlane-minio:
    selector:
      app.kubernetes.io/stack: hyperlane-minio
    namespace: laconic-hyperlane-minio
    port: 9000
"""
t = open("/tmp/render-test.yml").read()
t = t.replace("__S3_ENDPOINT__", "http://hyperlane-minio:9000")
t = t.replace("__DNS_ZONE__", "hyperlane.local")
t = t.replace("# __SINGLE_HOST_MINIO_XS__", block)
doc = yaml.safe_load(t)
assert doc["config"]["AWS_ENDPOINT_URL_S3"] == "http://hyperlane-minio:9000"
assert doc["external-services"]["hyperlane-minio"]["namespace"] == "laconic-hyperlane-minio"
print("single-host validator render OK")
PY
```

Expected output: `single-host validator render OK`. Then `rm /tmp/render-test.yml`.

- [ ] **Step 6: Commit**

```bash
git add deployment/local/spec-validator-gorchain.yml deployment/local/spec-validator-solana.yml deployment/local/spec-monitoring.yml
git commit -m "$(printf 'feat(local): token the MinIO/scrape legs for per-topology render\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

## Task 4: The `local_tls` role (host-only mkcert + Caddy pre-seed + /etc/hosts + CA)

**Files:**
- Create: `ops/roles/local_tls/defaults/main.yml`
- Create: `ops/roles/local_tls/templates/caddy-secrets.yaml.j2`
- Create: `ops/roles/local_tls/tasks/main.yml`

- [ ] **Step 1: Role defaults**

Create `ops/roles/local_tls/defaults/main.yml`:

```yaml
---
# Where mkcert writes the leaf cert/key and where we publish rootCA.pem for the operator
# to scp down for workstation browser trust.
local_tls_cert_dir: "{{ credentials_dir }}/local-certs"
local_tls_published_ca: "{{ credentials_dir }}/local-rootCA.pem"
# mkcert release pinned for reproducibility.
mkcert_version: "v1.4.4"
mkcert_url: "https://github.com/FiloSottile/mkcert/releases/download/{{ mkcert_version }}/mkcert-{{ mkcert_version }}-linux-amd64"
```

- [ ] **Step 2: Caddy pre-seed template**

Create `ops/roles/local_tls/templates/caddy-secrets.yaml.j2`. This must match `tests/e2e/lib/cluster.py:103-152` (3 Secrets per host, `data.value` = base64 content, `manager: caddy` label, `certmagic.io/storage-key` annotation under the LE issuer path) so SO's `_restore_caddy_certs` (`../stack-orchestrator/.../helpers.py:129-198`) restores them:

```yaml
{% set issuer = "certificates/acme-v02.api.letsencrypt.org-directory" %}
{% set prefix = "caddy.ingress--certificates.acme-v02.api.letsencrypt.org-directory" %}
apiVersion: v1
kind: List
items:
{% for host in mkcert_hostnames %}
{% for ext, value in [('crt', cert_b64), ('key', key_b64), ('json', json_b64)] %}
  - apiVersion: v1
    kind: Secret
    metadata:
      name: "{{ prefix }}.{{ host }}.{{ host }}.{{ ext }}"
      namespace: caddy-system
      labels:
        manager: caddy
      annotations:
        certmagic.io/storage-key: "{{ issuer }}/{{ host }}/{{ host }}.{{ ext }}"
    type: Opaque
    data:
      value: "{{ value }}"
{% endfor %}
{% endfor %}
```

- [ ] **Step 3: Role tasks**

Create `ops/roles/local_tls/tasks/main.yml`. Runs on the single host; `become: true` for the system-trust + /etc/hosts steps. Derives the hostname list from `dns_record_map` + `validators` (same sources the specs use):

```yaml
---
- name: Load validators (for cert SANs)
  ansible.builtin.include_tasks: ../roles/common/tasks/load_validators.yml

- name: Derive the cert hostname list from dns_record_map + validators
  ansible.builtin.set_fact:
    mkcert_hostnames: "{{ _base_hosts + _validator_hosts }}"
  vars:
    # regex_replace on '$' appends, on '^' prepends — no backreferences, no escaping.
    _base_hosts: "{{ dns_record_map.keys() | map('regex_replace', '$', '.' ~ dns_zone) | list }}"
    _validator_hosts: >-
      {{ validators | map(attribute='chain')
         | map('regex_replace', '^', 'validator-')
         | map('regex_replace', '$', '.' ~ dns_zone) | list }}

- name: Install mkcert binary
  become: true
  ansible.builtin.get_url:
    url: "{{ mkcert_url }}"
    dest: /usr/local/bin/mkcert
    mode: "0755"

- name: Find the mkcert CAROOT
  ansible.builtin.command: mkcert -CAROOT
  register: _caroot
  changed_when: false

- name: Generate the mkcert root CA and trust it in the host system store
  become: true
  ansible.builtin.command: mkcert -install
  args:
    creates: "{{ _caroot.stdout }}/rootCA.pem"

- name: Ensure the cert dir exists
  ansible.builtin.file:
    path: "{{ local_tls_cert_dir }}"
    state: directory
    mode: "0755"

- name: Generate the multi-SAN leaf cert
  # Each hostname must be its own argv element — concatenate the list, don't join.
  ansible.builtin.command:
    argv: "{{ ['mkcert', '-cert-file', local_tls_cert_dir ~ '/bridge.crt', '-key-file', local_tls_cert_dir ~ '/bridge.key'] + mkcert_hostnames }}"
  args:
    creates: "{{ local_tls_cert_dir }}/bridge.crt"

- name: Slurp cert + key (slurp returns base64-encoded content)
  ansible.builtin.slurp:
    src: "{{ item }}"
  register: _slurped
  loop:
    - "{{ local_tls_cert_dir }}/bridge.crt"
    - "{{ local_tls_cert_dir }}/bridge.key"

- name: Build the Caddy pre-seed backup
  become: true
  ansible.builtin.template:
    src: caddy-secrets.yaml.j2
    dest: "{{ kind_mount_root }}/caddy-cert-backup/caddy-secrets.yaml"
    mode: "0644"
  vars:
    cert_b64: "{{ _slurped.results[0].content }}"
    key_b64: "{{ _slurped.results[1].content }}"
    json_b64: "{{ '{}' | b64encode }}"

- name: Publish rootCA.pem for the operator to fetch (workstation browser trust)
  become: true
  ansible.builtin.copy:
    src: "{{ _caroot.stdout }}/rootCA.pem"
    dest: "{{ local_tls_published_ca }}"
    remote_src: true
    mode: "0644"

- name: Point the host /etc/hosts at loopback for every bridge hostname
  become: true
  ansible.builtin.blockinfile:
    path: /etc/hosts
    marker: "# {mark} hyperlane local_tls"
    block: |
      {% for host in mkcert_hostnames %}
      127.0.0.1 {{ host }}
      {% endfor %}
```

Note: `kind_mount_root` resolves from the spec value `/srv/kind/hyperlane` — set it in the role play (Task 5) since it is not in `group_vars`.

- [ ] **Step 4: Lint the role**

Run: `cd ops && yamllint roles/local_tls/ && ansible-lint roles/local_tls/`
Expected: no errors. (If ansible-lint flags `command`/`get_url` rules, the `creates:`/`changed_when:` guards already make them idempotent; resolve any remaining lint by following its suggestion, do not silence with `# noqa` unless the rule is a false positive for an idempotent guarded command.)

- [ ] **Step 5: Commit**

```bash
git add ops/roles/local_tls/
git commit -m "$(printf 'feat(ops): local_tls role — host-only mkcert + Caddy pre-seed\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

## Task 5: Wire local-tls into setup-all

**Files:**
- Create: `ops/playbooks/local-tls.yml`
- Modify: `ops/playbooks/setup-all.yml`

- [ ] **Step 1: New play**

Create `ops/playbooks/local-tls.yml`. Targets the single host (the MinIO host, which in single-host equals every host); gated on `topology == 'single'`:

```yaml
---
# Single-host local only: generate a self-trusted mkcert cert, pre-seed it into Caddy's
# restore path, trust the CA + write /etc/hosts on the host, and publish rootCA.pem for
# the operator to fetch. No-op for multi-host (which uses configure-dns.yml + LE).
- name: Provision self-trusted TLS for single-host local
  hosts: minio_hosts
  gather_facts: true
  vars:
    kind_mount_root: /srv/kind/hyperlane
  roles:
    - role: local_tls
      when: topology == 'single'
```

- [ ] **Step 2: Import it from setup-all**

In `ops/playbooks/setup-all.yml`, add the import after the Configure DNS import:

```yaml
- name: Provision single-host TLS (mkcert)
  ansible.builtin.import_playbook: local-tls.yml
```

The full file becomes:

```yaml
---
# Phase 1 — provision the whole fleet. No stacks deployed.
- name: Bootstrap hosts
  ansible.builtin.import_playbook: bootstrap-host.yml

- name: Configure DNS
  ansible.builtin.import_playbook: configure-dns.yml

- name: Provision single-host TLS (mkcert)
  ansible.builtin.import_playbook: local-tls.yml

- name: Generate + distribute credentials
  ansible.builtin.import_playbook: distribute-credentials.yml
```

- [ ] **Step 3: Syntax-check both inventories**

Run:
```bash
cd ops && \
ansible-playbook -i inventories/local/hosts.yml playbooks/setup-all.yml --syntax-check && \
ansible-playbook -i inventories/local/hosts-multihost.yml playbooks/setup-all.yml --syntax-check
```
Expected: both report no syntax errors.

- [ ] **Step 4: Confirm the role is skipped for multi-host**

Run a check-mode listing of tasks/tags to confirm gating (multi-host has no `minio_hosts`-vs-`relayer_hosts` collision so `topology` is `multi`):
```bash
cd ops && ansible-playbook -i inventories/local/hosts-multihost.yml playbooks/local-tls.yml --list-hosts
```
Expected: the play lists `local-services` (the multi-host `minio_hosts`), but with `topology == multi` the `local_tls` role `when:` is false at run time — note this is a static list; the real gate is the role `when:`. (No run is performed.)

- [ ] **Step 5: Commit**

```bash
git add ops/playbooks/local-tls.yml ops/playbooks/setup-all.yml
git commit -m "$(printf 'feat(ops): wire single-host local_tls into setup-all\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

## Task 6: Docs — runbook + deploy-side design

**Files:**
- Modify: `ops/runbooks/local.md`
- Modify: `docs/superpowers/specs/2026-06-01-deploy-side-ansible-design.md`

- [ ] **Step 1: Runbook — single-host no longer needs a DNS provider**

In `ops/runbooks/local.md`, update the **Networking model** section and **Prerequisites** so single-host is mkcert-based:

Replace the `> **No public DNS?** Fall back to a local ACME server ...` callout (lines ~31-34) with:

```markdown
> **Single-host needs no DNS provider.** It uses mkcert: the `local_tls` role generates
> a self-trusted cert, pre-seeds it into Caddy (no ACME), and the validator→MinIO and
> Prometheus→validator/relayer legs go in-cluster over HTTP. `dns_zone` is just a label
> the cert covers (e.g. `hyperlane.local`) — it does not need to be a real Cloudflare
> zone. Multi-host still uses Cloudflare + Let's Encrypt (cross-host routing needs real
> DNS + a cert the Rust S3 client trusts).
```

Under **Prerequisites → Accounts / access**, change the Cloudflare bullet to note it is multi-host only:

```markdown
- **Multi-host only:** a public DNS zone on Cloudflare (`dns_zone`) and a Cloudflare API
  token scoped to it. Single-host needs neither — `dns_zone` is any label mkcert signs.
```

- [ ] **Step 2: Runbook — browse-via-tunnel for single-host**

Replace the **7. Access the stacks** section with:

```markdown
## 7. Access the stacks

**Multi-host** (public DNS + LE): browse the hostnames directly once DNS propagates —
`https://warp-ui.<zone>`, `https://grafana.<zone>`, `https://prometheus.<zone>`,
`https://minio-console.<zone>`.

**Single-host** (mkcert, no public DNS): tunnel and trust the CA from your workstation.
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
```

- [ ] **Step 3: Runbook — drop the local-ACME fallback, fix the hairpin note**

Delete the entire **"## Fallback — no public DNS (local ACME)"** section at the bottom of `ops/runbooks/local.md`.

In **9. Known limitations / notes**, replace the **"Single-host relies on NAT hairpin."** bullet with:

```markdown
- **Single-host no longer hairpins.** Because the validator→MinIO and Prometheus scrape
  legs run in-cluster (pod-to-pod) under mkcert, single-host does not loop traffic out to
  the host's public IP and back. The earlier NAT-hairpin caveat applied only to the old
  all-LE single-host model and is gone. Multi-host hosts are genuinely separate, so they
  don't hairpin either.
```

- [ ] **Step 4: Deploy-side design doc — record the single/multi split**

In `docs/superpowers/specs/2026-06-01-deploy-side-ansible-design.md`, find the "Own-chains environment for Layers 1–2" section and add a subsection after the Topologies subsection:

```markdown
#### Networking by topology

Single-host and multi-host diverge only in networking, driven by a derived `topology`
var (`minio_hosts[0] == relayer_hosts[0]`):

- **Single-host:** mkcert self-trusted certs (the `local_tls` role pre-seeds Caddy; SO's
  `_restore_caddy_certs` loads them, no ACME). The validator→MinIO and Prometheus scrape
  legs go in-cluster over HTTP via `external-services: selector:` blocks. No Cloudflare,
  no public DNS. See `docs/superpowers/specs/2026-06-03-local-single-host-mkcert-design.md`.
- **Multi-host:** Caddy + Cloudflare DNS + real Let's Encrypt, as prod. The MinIO leg
  crosses a host boundary, so the Rust S3 client needs a publicly-trusted cert.

Both ship from one `deployment/local/` spec tree; the per-topology values and the
in-cluster `external-services` blocks render via `spec_token_renders`.
```

- [ ] **Step 5: yamllint markdown is N/A; sanity-check the runbook renders**

Run: `cd /home/dev/git_puller/repos/hyperlane-stacks && grep -n "local ACME" ops/runbooks/local.md || echo "fallback removed OK"`
Expected: `fallback removed OK` (the local-ACME section is gone).

- [ ] **Step 6: Commit**

```bash
git add ops/runbooks/local.md docs/superpowers/specs/2026-06-01-deploy-side-ansible-design.md
git commit -m "$(printf 'docs(ops): single-host local is mkcert + tunnel; drop local-ACME fallback\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

## Task 7: Full gate run

**Files:** none (verification only)

- [ ] **Step 1: yamllint the touched trees**

Run:
```bash
cd /home/dev/git_puller/repos/hyperlane-stacks && \
yamllint ops/inventories/local/ ops/roles/local_tls/ ops/playbooks/local-tls.yml ops/playbooks/setup-all.yml \
  deployment/local/spec-validator-gorchain.yml deployment/local/spec-validator-solana.yml deployment/local/spec-monitoring.yml
```
Expected: no errors.

- [ ] **Step 2: ansible-lint (production profile)**

Run: `cd ops && ansible-lint`
Expected: 0 failures (matches the pre-existing baseline).

- [ ] **Step 3: Syntax-check both inventories, both phases**

Run:
```bash
cd ops && for inv in hosts hosts-multihost; do \
  ansible-playbook -i inventories/local/$inv.yml playbooks/setup-all.yml --syntax-check && \
  ansible-playbook -i inventories/local/$inv.yml playbooks/deploy-all.yml --syntax-check; \
done
```
Expected: all four report no syntax errors.

- [ ] **Step 4: Run the local env test**

Run: `cd ops && ansible-playbook -i inventories/local/hosts.yml tests/test_local_env.yml`
Expected: PASS — both the single-host and multi-host plays green.

- [ ] **Step 5: Multi-host render sanity (markers must vanish, LE shape intact)**

Confirm the multi-host render leaves no marker and no external-services in the validator spec:
```bash
cd /home/dev/git_puller/repos/hyperlane-stacks && \
python3 - <<'PY'
import yaml
t = open("deployment/local/spec-validator-gorchain.yml").read()
t = t.replace("__S3_ENDPOINT__", "https://s3.staging.gorbagana.wtf")
t = t.replace("__DNS_ZONE__", "staging.gorbagana.wtf")
t = t.replace("# __SINGLE_HOST_MINIO_XS__", "")   # multi-host renders marker to ''
doc = yaml.safe_load(t)
assert doc["config"]["AWS_ENDPOINT_URL_S3"] == "https://s3.staging.gorbagana.wtf"
assert "external-services" not in doc, "multi-host must NOT have a MinIO external-service"
print("multi-host validator render OK")
PY
```
Expected: `multi-host validator render OK`.

- [ ] **Step 6: Final commit (only if any lint auto-fixes were applied; otherwise skip)**

```bash
git status --short
# if lint reformatted anything:
git add -A && git commit -m "$(printf 'chore(ops): lint fixups for single-host local mkcert\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

## Notes for the implementer

- **Never push.** Commit only; the user pushes. Before any `git commit --amend`, run `git branch -vv` and confirm the commit is local-only — if it shows in-sync with the remote, add a new commit instead.
- **Real-VM validation is deferred** — bring-up on an actual single-host VM (mkcert pre-seed → Caddy serves trusted certs with no ACME calls → validator writes checkpoints to MinIO in-cluster → Prometheus scrapes in-cluster) lands in the same follow-up bucket as the multi-host real-VM run. Not part of this plan.
- **load_validators** (`ops/roles/common/tasks/load_validators.yml`) sets `validators` (list of `{label, chain, host, ...}`) from `bridges/default/operator/validators.yaml`. The `local_tls` role and the test both rely on `item.chain` ∈ {`gorchain`, `solana`}.
- If `ansible.builtin.template` rejects the `kind: List` because a host list is empty, that means `mkcert_hostnames` derivation failed — check `dns_record_map` and `validators` are both populated before the template step.
