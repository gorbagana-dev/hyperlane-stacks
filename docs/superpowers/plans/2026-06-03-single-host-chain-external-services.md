# Single-host chain external-services Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the single-host `local` topology reach the host-run SVM chains the way the e2e tests do — via `external-services` `ip:` mode → in-cluster `gorchain-rpc:8899` / `solana-rpc:18899` — instead of the current operator-set domains, which have no working in-cluster path.

**Architecture:** The chain RPC tokens in the local specs become topology-conditional (single → in-cluster service names; multi → operator domains). Every server-side chain consumer carries a single consolidated `# __SINGLE_HOST_EXTERNAL_SERVICES__` marker; `stack_deploy` renders it to that stack's full `external-services:` block (chains, chains+MinIO, or chains+Prom) on single-host and to `''` on multi-host. The chains' `ip:` is the kind-network gateway IP, detected at deploy time exactly as e2e's `get_host_ip()` does. warp-ui is browser-facing, so it keeps browser-reachable RPCs (single → `localhost` over the SSH tunnel; multi → domain) and gets no in-cluster block.

**Tech Stack:** Ansible (group_vars, `ansible.builtin.replace`, `set_fact`), laconic-so spec YAML, yamllint/ansible-lint.

---

## Background the implementer needs

**How chains are reached today (the gap):** Local specs set `GORCHAIN_RPC_URL: "__GORCHAIN_RPC_URL__"`, rendered from the operator-set `gorchain_rpc_url` (a domain). There is **no** `external-services` for chains — only for MinIO and Prometheus (single-host). So in single-host (chains on the host, agents in kind) there is no working in-cluster path to the chains.

**How e2e does it (the target):** `tests/e2e/fixtures/test-spec-validator-gorchain.yml:52-58`:
```yaml
external-services:
  gorchain-rpc: { ip: REPLACE_HOST_IP, port: 8899 }
  solana-rpc:   { ip: REPLACE_HOST_IP, port: 18899 }
```
Agents then use `GORCHAIN_RPC_URL=http://gorchain-rpc:8899`. `REPLACE_HOST_IP` is the **kind docker-network gateway IP** (`tests/e2e/lib/cluster.py:66` — `docker network inspect kind` gateway, e.g. `172.18.0.1`).

**Why validators need no RPC env change:** Validators read chain RPC from the `agent-config` configmap, which the **deployer** builds from `${GORCHAIN_RPC_URL}` (`stack_orchestrator/data/config/deployer-scripts-config/deploy.sh:366,390`). Make the deployer's token in-cluster and validators inherit it via agent-config — they just need the `gorchain-rpc`/`solana-rpc` Service resolvable in their own namespace (hence the chains+MinIO block).

**Why one consolidated marker:** A spec may have only one `external-services:` key. Validators/relayer already carry a single-host **MinIO** block; monitoring a **Prom** block. Chains must merge into the same block, and the set differs per stack — so one marker per spec, with `stack_deploy` choosing the block.

**Per-stack keying:** `deploy-all.yml` passes `stack_name` per play; validators pass `stack_env_map_key: hyperlane-validator` (`ops/playbooks/deploy-all.yml:166`). Key the external-services map by `stack_env_map_key | default(stack_name)` — both validators collapse to `hyperlane-validator`, every other stack is its own name.

**Marker / map ownership per stack:**

| Stack (`stack_env_map_key \| default(stack_name)`) | Block | Marker in spec? |
|---|---|---|
| `hyperlane-svm-deployer` | chains | yes |
| `hyperlane-svm-warp-deployer` | chains | yes |
| `hyperlane-gas-oracle` | chains | yes |
| `hyperlane-validator` (both validator specs) | chains + MinIO | yes |
| `hyperlane-relayer` | chains + MinIO | yes |
| `hyperlane-monitoring` | chains + Prom | yes |
| `hyperlane-warp-ui` | — (browser RPC, no block) | no |
| `hyperlane-minio` | — | no |

**Verification model:** No unit tests — this is config. Each task verifies with `yamllint`, `ansible-lint`, `ansible-playbook --syntax-check`, the `ops/tests/test_local_env.yml` assertion play, and a render-sanity simulation (apply the marker replacement for both topologies and eyeball the YAML). Run all ansible commands from `ops/` with the venv and locale:
```bash
cd ops
export PATH=/home/dev/.ops-ansible-venv/bin:$PATH LC_ALL=C.UTF-8 LANG=C.UTF-8
```

**Keep-in-sync note:** This changes no compose env-var names and no secrets, so the CLAUDE.md compose↔spec↔fixture table is not triggered. It does change `deployment/local/spec-*.yml` rendering + ops group_vars + the design doc — update those (Tasks 1–6).

---

## Task 1: group_vars — RPC tokens + consolidated external-services map

**Files:**
- Modify: `ops/inventories/local/group_vars/all.yml` (the `# --- On-host spec token rendering ---` section, currently ~lines 54–109)

- [ ] **Step 1: Update the chain-RPC comment + vars**

Replace this block:
```yaml
# which commit concrete values. The chains are domain-routed out-of-band; set their
# full RPC URLs including scheme (http/https as the chain hosts serve them).
gorchain_rpc_url: "REPLACE_WITH_GORCHAIN_RPC_URL"
solana_rpc_url: "REPLACE_WITH_SOLANA_RPC_URL"
```
with:
```yaml
# which commit concrete values.
#
# Chain RPCs are topology-dependent (see spec_token_renders):
#   single-host: chains run on the VM; in-cluster pods reach them as
#     gorchain-rpc:8899 / solana-rpc:18899 (external-services ip: -> kind gateway),
#     and warp-ui's browser reaches them over the SSH tunnel at localhost. The two
#     vars below are UNUSED on single-host — leave them at the placeholder.
#   multi-host: chains run out-of-band on a separate box; set their full RPC URLs
#     (scheme + domain) here — used for the in-cluster agents AND warp-ui.
gorchain_rpc_url: "REPLACE_WITH_GORCHAIN_RPC_URL"
solana_rpc_url: "REPLACE_WITH_SOLANA_RPC_URL"
```

- [ ] **Step 2: Replace the two single-host XS block scalars + spec_token_renders**

Replace the entire block from `_single_host_minio_xs: |` through the final
`"# __SINGLE_HOST_PROM_XS__": ...` line with:

```yaml
# --- Single-host external-services (one block per stack) ---
# Single-host shares one kind cluster, so cross-stack and chain hops resolve
# in-cluster. external-services is ONE key per spec, so each stack's full block is
# defined here and rendered into the spec's `# __SINGLE_HOST_EXTERNAL_SERVICES__`
# marker by stack_deploy (multi-host renders ''). Chains run on the host, reached via
# the kind-network gateway IP (kind_gateway_ip, detected at deploy time) — the e2e
# `ip:` pattern; MinIO and the Prometheus scrape targets are in-cluster pods.
_xs_chains_only: |
  external-services:
    gorchain-rpc:
      ip: {{ kind_gateway_ip }}
      port: 8899
    solana-rpc:
      ip: {{ kind_gateway_ip }}
      port: 18899
_xs_chains_minio: |
  external-services:
    gorchain-rpc:
      ip: {{ kind_gateway_ip }}
      port: 8899
    solana-rpc:
      ip: {{ kind_gateway_ip }}
      port: 18899
    hyperlane-minio:
      selector:
        app.kubernetes.io/stack: hyperlane-minio
      namespace: laconic-hyperlane-minio
      port: 9000
_xs_chains_prom: |
  external-services:
    gorchain-rpc:
      ip: {{ kind_gateway_ip }}
      port: 8899
    solana-rpc:
      ip: {{ kind_gateway_ip }}
      port: 18899
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

# stack_env_map_key | default(stack_name) -> that stack's external-services block.
# Stacks absent here (minio, warp-ui) get no block.
single_host_external_services:
  hyperlane-svm-deployer: "{{ _xs_chains_only }}"
  hyperlane-svm-warp-deployer: "{{ _xs_chains_only }}"
  hyperlane-gas-oracle: "{{ _xs_chains_only }}"
  hyperlane-validator: "{{ _xs_chains_minio }}"
  hyperlane-relayer: "{{ _xs_chains_minio }}"
  hyperlane-monitoring: "{{ _xs_chains_prom }}"

spec_token_renders:
  __DNS_ZONE__: "{{ dns_zone }}"
  # single: in-cluster service names (external-services); multi: operator domains
  __GORCHAIN_RPC_URL__: "{{ 'http://gorchain-rpc:8899' if topology == 'single' else gorchain_rpc_url }}"
  __SOLANA_RPC_URL__: "{{ 'http://solana-rpc:18899' if topology == 'single' else solana_rpc_url }}"
  # warp-ui is browser-facing: single -> chains over the SSH tunnel; multi -> domains
  __BROWSER_GORCHAIN_RPC_URL__: "{{ 'http://localhost:8899' if topology == 'single' else gorchain_rpc_url }}"
  __BROWSER_SOLANA_RPC_URL__: "{{ 'http://localhost:18899' if topology == 'single' else solana_rpc_url }}"
  __S3_ENDPOINT__: "{{ 'http://hyperlane-minio:9000' if topology == 'single' else 'https://s3.' ~ dns_zone }}"
  __PROM_SCRAPE_SCHEME__: "{{ 'http' if topology == 'single' else 'https' }}"
  __PROM_VALIDATOR_TARGETS__: >-
    {{ 'gorchain-primary=validator-gorchain:9090,solana-primary=validator-solana:9090'
       if topology == 'single'
       else 'gorchain-primary=validator-gorchain.' ~ dns_zone ~ ':443,'
            ~ 'solana-primary=validator-solana.' ~ dns_zone ~ ':443' }}
  __PROM_RELAYER_TARGETS__: >-
    {{ 'primary=relayer:9091' if topology == 'single'
       else 'primary=relayer.' ~ dns_zone ~ ':443' }}
```

Note: the old `"# __SINGLE_HOST_MINIO_XS__"` / `"# __SINGLE_HOST_PROM_XS__"` entries are
intentionally removed — the consolidated marker is rendered by a dedicated task (Task 2),
not the global token loop, because its value is per-stack.

- [ ] **Step 3: Verify yamllint + lint**

```bash
cd ops && export PATH=/home/dev/.ops-ansible-venv/bin:$PATH LC_ALL=C.UTF-8 LANG=C.UTF-8
yamllint inventories/local/group_vars/all.yml
ansible-lint inventories/local/group_vars/all.yml
```
Expected: no errors. (Block scalars containing `{{ kind_gateway_ip }}` are string content — not linted as Jinja.)

- [ ] **Step 4: Commit**

```bash
git add ops/inventories/local/group_vars/all.yml
git commit -m "feat(ops): single-host chain RPC via in-cluster external-services"
```

---

## Task 2: stack_deploy — gateway-IP detection + per-stack external-services render

**Files:**
- Modify: `ops/roles/stack_deploy/tasks/deploy.yml` (inside the `Create the deployment` block, after the `Render runtime tokens` task at lines 34–41)

- [ ] **Step 1: Add the gateway-IP detection + marker render tasks**

Immediately after the `Render runtime tokens into the spec (local only)` task and before
`Run deploy create`, insert:

```yaml
    # Single-host reaches the host-run chains via external-services ip: -> the kind
    # network gateway (the e2e get_host_ip pattern). Only chain-consumers need it;
    # MinIO (first stack, before any cluster exists) is not in the map and is skipped.
    - name: Detect kind-network gateway IP (single-host chain reachability)
      when:
        - topology | default('') == 'single'
        - (stack_env_map_key | default(stack_name)) in (single_host_external_services | default({}))
      block:
        - name: Inspect the kind network IPAM config
          ansible.builtin.command:
            argv:
              - docker
              - network
              - inspect
              - kind
              - -f
              - '{% raw %}{{json .IPAM.Config}}{% endraw %}'
          register: _kind_ipam
          changed_when: false

        - name: Set kind_gateway_ip from the first IPv4 gateway
          ansible.builtin.set_fact:
            kind_gateway_ip: >-
              {{ _kind_ipam.stdout | from_json
                 | selectattr('Gateway', 'defined')
                 | map(attribute='Gateway')
                 | select('match', '^[0-9.]+$') | first }}

    # One consolidated external-services block per spec. Single-host -> the stack's
    # block (chains / chains+MinIO / chains+Prom); multi-host or non-consumer -> ''.
    - name: Render the single-host external-services block (local only)
      ansible.builtin.replace:
        path: "{{ spec_file }}"
        regexp: '#\s*__SINGLE_HOST_EXTERNAL_SERVICES__'
        replace: >-
          {{ single_host_external_services[stack_env_map_key | default(stack_name)]
             if (topology | default('') == 'single'
                 and (stack_env_map_key | default(stack_name)) in (single_host_external_services | default({})))
             else '' }}
      when: spec_token_renders is defined
```

(Guarding the render on `spec_token_renders is defined` keeps it a no-op for prod/staging,
matching the existing token-render task. The Jinja `A if cond else ''` only evaluates the
map — and thus `kind_gateway_ip` — when the condition is true, so the detect task above
always runs first when needed.)

- [ ] **Step 2: Syntax-check both inventories**

```bash
cd ops && export PATH=/home/dev/.ops-ansible-venv/bin:$PATH LC_ALL=C.UTF-8 LANG=C.UTF-8
ansible-playbook -i inventories/local/hosts.yml playbooks/deploy-all.yml --syntax-check
ansible-playbook -i inventories/local/hosts-multihost.yml playbooks/deploy-all.yml --syntax-check \
  -e validators_file=$PWD/deployment/local/bridges/default/operator/validators-multihost.yaml
ansible-lint roles/stack_deploy/
```
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add ops/roles/stack_deploy/tasks/deploy.yml
git commit -m "feat(ops): detect kind gateway + render per-stack external-services"
```

---

## Task 3: Consolidate the external-services marker in the six server-side specs

**Files:**
- Modify: `deployment/local/spec-deployer.yml` (add marker at EOF)
- Modify: `deployment/local/spec-warp-deployer.yml` (add marker at EOF)
- Modify: `deployment/local/spec-gas-oracle.yml` (add marker at EOF)
- Modify: `deployment/local/spec-validator-gorchain.yml:55` (rename marker)
- Modify: `deployment/local/spec-validator-solana.yml:55` (rename marker)
- Modify: `deployment/local/spec-relayer.yml:60` (rename marker)
- Modify: `deployment/local/spec-monitoring.yml:57` (rename marker)

- [ ] **Step 1: Rename the existing markers**

In `spec-validator-gorchain.yml`, `spec-validator-solana.yml`, `spec-relayer.yml`,
replace the line `# __SINGLE_HOST_MINIO_XS__` with `# __SINGLE_HOST_EXTERNAL_SERVICES__`.
In `spec-monitoring.yml`, replace `# __SINGLE_HOST_PROM_XS__` with
`# __SINGLE_HOST_EXTERNAL_SERVICES__`. Leave the explanatory comment line above each
untouched.

- [ ] **Step 2: Add the marker to the three specs that lack one**

Append to the **end** of `spec-deployer.yml`, `spec-warp-deployer.yml`, and
`spec-gas-oracle.yml` (column 0, after a blank line):
```yaml

# Single-host only: this stack reaches the host-run chains in-cluster. Rendered to an
# external-services: block on single-host, removed on multi-host.
# __SINGLE_HOST_EXTERNAL_SERVICES__
```

- [ ] **Step 3: Confirm exactly one marker per server-side spec, none elsewhere**

```bash
cd /home/dev/git_puller/repos/hyperlane-stacks
grep -rl '__SINGLE_HOST_EXTERNAL_SERVICES__' deployment/local/   # 6 files
grep -rl '__SINGLE_HOST_MINIO_XS__\|__SINGLE_HOST_PROM_XS__' deployment/local/   # none
grep -L '__SINGLE_HOST_EXTERNAL_SERVICES__' deployment/local/spec-warp-ui.yml deployment/local/spec-minio.yml  # both (no marker)
```
Expected: deployer, warp-deployer, gas-oracle, validator-gorchain, validator-solana,
relayer, monitoring — wait, that is 7. Recount: deployer, warp-deployer, gas-oracle (3) +
validator-gorchain, validator-solana, relayer, monitoring (4) = **7 files**. warp-ui and
minio have none.

- [ ] **Step 4: Render-sanity (single + multi) for a representative spec**

Simulate the replace the way `stack_deploy` will, for the relayer (chains+MinIO):
```bash
cd /home/dev/git_puller/repos/hyperlane-stacks
# single-host block (gateway IP stubbed as 172.18.0.1):
python3 - <<'PY'
block = """external-services:
  gorchain-rpc:
    ip: 172.18.0.1
    port: 8899
  solana-rpc:
    ip: 172.18.0.1
    port: 18899
  hyperlane-minio:
    selector:
      app.kubernetes.io/stack: hyperlane-minio
    namespace: laconic-hyperlane-minio
    port: 9000
"""
import re, yaml
spec = open("deployment/local/spec-relayer.yml").read()
single = re.sub(r'#\s*__SINGLE_HOST_EXTERNAL_SERVICES__', block, spec)
multi  = re.sub(r'#\s*__SINGLE_HOST_EXTERNAL_SERVICES__', '', spec)
# Strip remaining __TOKENS__ so yaml can parse, then assert validity
for name, txt in (("single", single), ("multi", multi)):
    t = re.sub(r'__[A-Z0-9_]+__', 'x', txt)
    yaml.safe_load(t)
    print(name, "parses OK; has external-services:", 'external-services:' in txt)
PY
```
Expected: `single parses OK; has external-services: True` and
`multi parses OK; has external-services: False`.

- [ ] **Step 5: Commit**

```bash
git add deployment/local/spec-deployer.yml deployment/local/spec-warp-deployer.yml \
  deployment/local/spec-gas-oracle.yml deployment/local/spec-validator-gorchain.yml \
  deployment/local/spec-validator-solana.yml deployment/local/spec-relayer.yml \
  deployment/local/spec-monitoring.yml
git commit -m "feat(local): consolidate single-host external-services marker across chain consumers"
```

---

## Task 4: warp-ui — browser-reachable chain RPCs

**Files:**
- Modify: `deployment/local/spec-warp-ui.yml:11-12`

- [ ] **Step 1: Point warp-ui at the browser RPC tokens**

Replace:
```yaml
  GORCHAIN_RPC_URL: "__GORCHAIN_RPC_URL__"
  SOLANA_RPC_URL: "__SOLANA_RPC_URL__"
```
with:
```yaml
  # warp-ui is browser-facing — the user's browser talks to these RPCs, so they must
  # be browser-reachable (single-host: chains over the SSH tunnel at localhost;
  # multi-host: the public chain domains), NOT the in-cluster gorchain-rpc names.
  GORCHAIN_RPC_URL: "__BROWSER_GORCHAIN_RPC_URL__"
  SOLANA_RPC_URL: "__BROWSER_SOLANA_RPC_URL__"
```

- [ ] **Step 2: Confirm warp-ui uses only the browser tokens**

```bash
grep -nE '__(BROWSER_)?(GORCHAIN|SOLANA)_RPC_URL__' deployment/local/spec-warp-ui.yml
```
Expected: only `__BROWSER_GORCHAIN_RPC_URL__` and `__BROWSER_SOLANA_RPC_URL__`.

- [ ] **Step 3: Commit**

```bash
git add deployment/local/spec-warp-ui.yml
git commit -m "fix(local): warp-ui uses browser-reachable chain RPCs per topology"
```

---

## Task 5: Update the topology assertion test

**Files:**
- Modify: `ops/tests/test_local_env.yml`

- [ ] **Step 1: Update the token assertions for both topology plays**

The test currently asserts `__GORCHAIN_RPC_URL__`/`__SOLANA_RPC_URL__` resolve to the
operator vars and that the old `# __SINGLE_HOST_MINIO_XS__` / `# __SINGLE_HOST_PROM_XS__`
tokens render per topology. Update so:

For the **single-host** play, assert:
```yaml
    - name: Single-host chain RPCs are in-cluster service names
      ansible.builtin.assert:
        that:
          - spec_token_renders['__GORCHAIN_RPC_URL__'] == 'http://gorchain-rpc:8899'
          - spec_token_renders['__SOLANA_RPC_URL__'] == 'http://solana-rpc:18899'
          - spec_token_renders['__BROWSER_GORCHAIN_RPC_URL__'] == 'http://localhost:8899'
          - spec_token_renders['__BROWSER_SOLANA_RPC_URL__'] == 'http://localhost:18899'
          - "'hyperlane-validator' in single_host_external_services"
          - "'hyperlane-svm-deployer' in single_host_external_services"
          - "'hyperlane-warp-ui' not in single_host_external_services"
          - "'hyperlane-minio' not in single_host_external_services"
```

For the **multi-host** play, assert (operator vars are the test's set values — reuse
whatever `gorchain_rpc_url`/`solana_rpc_url` the multi-host play already sets):
```yaml
    - name: Multi-host chain RPCs are the operator domains
      ansible.builtin.assert:
        that:
          - spec_token_renders['__GORCHAIN_RPC_URL__'] == gorchain_rpc_url
          - spec_token_renders['__SOLANA_RPC_URL__'] == solana_rpc_url
          - spec_token_renders['__BROWSER_GORCHAIN_RPC_URL__'] == gorchain_rpc_url
          - spec_token_renders['__BROWSER_SOLANA_RPC_URL__'] == solana_rpc_url
```

Remove any assertions referencing `__SINGLE_HOST_MINIO_XS__` or `__SINGLE_HOST_PROM_XS__`.
Both plays must define `kind_gateway_ip` (e.g. `kind_gateway_ip: 172.18.0.1`) in their
vars so the single-host play can evaluate `single_host_external_services` values without
the runtime fact.

- [ ] **Step 2: Run the assertion play for both topologies**

```bash
cd ops && export PATH=/home/dev/.ops-ansible-venv/bin:$PATH LC_ALL=C.UTF-8 LANG=C.UTF-8
ansible-playbook tests/test_local_env.yml
```
Expected: all asserts pass, no failed tasks.

- [ ] **Step 3: Commit**

```bash
git add ops/tests/test_local_env.yml
git commit -m "test(ops): assert single-host chain external-services + browser RPCs"
```

---

## Task 6: Docs — design doc, runbook split, README index

**Files:**
- Modify: `docs/superpowers/specs/2026-06-03-local-single-host-mkcert-design.md`
- Create: `ops/runbooks/local-single-host.md`
- Create: `ops/runbooks/local-multi-host.md`
- Delete: `ops/runbooks/local.md`
- Modify: `ops/runbooks/README.md`
- (already created: `ops/runbooks/privy-wallets.md`)

- [ ] **Step 1: Extend the design doc**

Add a "Chain reachability" subsection documenting: single-host reaches the host-run chains
via `external-services` `ip:` → kind-gateway IP → in-cluster `gorchain-rpc:8899` /
`solana-rpc:18899` (the e2e pattern); validators inherit the in-cluster URL through
`agent-config` (deployer-built); warp-ui is browser-facing and uses browser-reachable RPCs
(localhost over the tunnel / domain); multi-host uses operator chain domains and no chain
external-services. Note the consolidated `# __SINGLE_HOST_EXTERNAL_SERVICES__` marker
replaced the per-concern MinIO/Prom markers. Update "Specs touched" to list all seven
server-side specs + warp-ui.

- [ ] **Step 2: Write `ops/runbooks/local-single-host.md`**

Self-contained single-host runbook with sections: Networking model (mkcert, no DNS
provider, chains + agents on one VM) → Prerequisites (controller; Privy; GHCR; **no**
Cloudflare, **no** public 80/443) → **Privy wallets** (link to `privy-wallets.md`; set
`privy_wallet_id` in `validators.yaml`, `privy_oracle_wallet_id` in `secrets.yml`, the
`*_VALIDATOR_ADDRESS` + `IGP_ORACLE_PUBKEY` in `group_vars/all.yml`) → **Chains on the VM**
with real commands:
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

# fund deployer + the Privy oracle wallet on both chains
for rpc in http://localhost:8899 http://localhost:18899; do
  solana airdrop 100 <deployer-pubkey> --url "$rpc"
  solana airdrop 1   <oracle-base58-pubkey> --url "$rpc"
done

# create the collateral USDC SPL mint on Solana (-> WARP_TOKEN_MINT)
spl-token --url http://localhost:18899 create-token --decimals 6
spl-token --url http://localhost:18899 create-account <mint>
spl-token --url http://localhost:18899 mint <mint> 1000000
```
Then: note that **the in-cluster bridge reaches these via `gorchain-rpc:8899` /
`solana-rpc:18899` automatically — you do NOT set `gorchain_rpc_url`/`solana_rpc_url`**
(the chains must bind `0.0.0.0`, which the commands above do, so the kind-gateway can
reach them). → Inventory & zone (`hosts.yml`, `host_vars/local-1.yml` `public_ip`,
`dns_zone` = any mkcert label) → Secrets (`secrets.yml`, **no** `cloudflare_api_token`) →
Keyfiles & group_vars (signing keys list; pubkeys/addresses; `WARP_TOKEN_MINT`;
`REPLACE_WITH_GITHUB_USERNAME`) → Run (`setup-all.yml` then `deploy-all.yml` with the venv
+ locale) → Access (tunnel + trust CA; **also** `-L 8899:localhost:8899 -L
18899:localhost:18899` if using warp-ui, since the browser talks to the chains) → Reset →
Limitations (cert pinned to hostname list; no hairpin).

- [ ] **Step 3: Write `ops/runbooks/local-multi-host.md`**

Self-contained multi-host runbook, same section order. Differences to spell out:
Networking (Caddy + Cloudflare + Let's Encrypt; public DNS; inbound 80/443) →
Prerequisites add the Cloudflare zone + token → Chains on a **separate beefy box**
out-of-band, exposed at domains/IPs; **set `gorchain_rpc_url`/`solana_rpc_url`** in
`group_vars/all.yml` to those reachable URLs (used by both the in-cluster agents and
warp-ui) → Inventory uses `hosts-multihost.yml` + `host_vars/local-services.yml` &
`local-agents.yml` `public_ip`; validators use `validators-multihost.yaml` (the `-e
validators_file=...` flag on both playbooks) → Secrets include `cloudflare_api_token` →
Run (the two playbooks with `-e validators_file=...`) → Access (browse `https://<sub>.<zone>`
directly) → Reset → Limitations.

- [ ] **Step 4: Update the README index and remove `local.md`**

In `ops/runbooks/README.md`, replace the single `local.md` row with two rows
(`local-single-host.md`, `local-multi-host.md`), add a "shared: `privy-wallets.md`" note,
and update the "Adding a new environment runbook" guidance to reference the new section
order. Then `git rm ops/runbooks/local.md`.

- [ ] **Step 5: Verify links + lint docs**

```bash
cd /home/dev/git_puller/repos/hyperlane-stacks
grep -rn 'local\.md' ops/ docs/   # no stale references to the deleted file
yamllint ops/runbooks/*.md 2>/dev/null || true   # markdown, not yaml — informational
```
Expected: no references to the removed `local.md`.

- [ ] **Step 6: Commit**

```bash
git add docs/superpowers/specs/2026-06-03-local-single-host-mkcert-design.md \
  ops/runbooks/local-single-host.md ops/runbooks/local-multi-host.md ops/runbooks/README.md
git rm ops/runbooks/local.md
git commit -m "docs(ops): split local runbook by topology; document chain reachability"
```

---

## Self-Review

**Spec coverage:** chain RPC in-cluster (Task 1 tokens) ✓; external-services blocks
(Task 1 map + Task 2 render) ✓; gateway IP (Task 2) ✓; consolidated marker (Tasks 1–3) ✓;
warp-ui browser RPC (Tasks 1, 4) ✓; tests (Task 5) ✓; docs incl. runbook split + Privy +
chains commands (Task 6) ✓.

**Placeholder scan:** all code blocks are concrete; `<deployer-pubkey>` / `<mint>` are
runtime values the operator substitutes, called out as such — not plan placeholders.

**Type/name consistency:** `single_host_external_services`, `kind_gateway_ip`,
`# __SINGLE_HOST_EXTERNAL_SERVICES__`, `__BROWSER_*_RPC_URL__`, and the
`stack_env_map_key | default(stack_name)` key all match across Tasks 1, 2, 3, 4, 5. The
map keys equal the values `deploy-all.yml` passes (`hyperlane-validator` for both
validators via `stack_env_map_key`).

**Note** the file-count fix in Task 3 Step 3: **7** specs carry the marker (3 added + 4
renamed), warp-ui and minio carry none.
