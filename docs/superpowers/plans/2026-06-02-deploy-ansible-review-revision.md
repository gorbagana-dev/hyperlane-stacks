# Deploy-Side Ansible — Review Revision Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve the deploy-ansible review by implementing the revised configuration
model (static-committed / secret-env-injected / deployment-derived publish-patched)
across the existing `ops/` tree and the `deployment/` prod specs.

**Architecture:** SO writes spec `config:` verbatim to `config.env` (no `${VAR}`
expansion — verified `deployment_create.py:_write_config_file`). Therefore: config
values are committed literals in per-env spec trees; secrets are env-injected via the
assembled `stack_env`; deployment-derived values are patched into the committed spec
by `publish-bridge-state`. See `docs/superpowers/specs/2026-06-01-deploy-side-ansible-design.md`
(§ Configuration & secret model).

**Tech Stack:** Ansible (community.general, kubernetes.core, ansible.posix), laconic-so,
yamllint/ansible-lint, localhost pytest-style assertion playbooks.

**Canonical IDs:** SVM chains, name-derived `u32` (`chainId==domainId`). gorchain
domain==chain==`1198486093` prod / `1198486095` devnet (`"Gor"` 0x476F72 + net byte);
Solana==`1399811149` mainnet / `1399811151` devnet.

---

### Task 1: Prod spec config/secret split + canonical IDs

**Files (all under `deployment/`):** `spec-deployer.yml`, `spec-warp-deployer.yml`,
`spec-gas-oracle.yml`, `spec-relayer.yml`, `spec-warp-ui.yml`,
`spec-validator-gorchain.yml`, `spec-validator-solana.yml`, `spec-minio.yml` (no change).

- [ ] **Move `SOLANA_RPC_URL` from `config:` to `secrets:`** in every spec that has it
  (deployer, warp-deployer, gas-oracle, relayer, warp-ui). Add to the spec's existing
  `secrets:` block as `SOLANA_RPC_URL: { env: SOLANA_RPC_URL }`. Remove its `config:` line.
  (Validator specs don't list RPC in config — leave them.)
- [ ] **Set gorchain RPC to the real public endpoint** (non-secret, committed):
  `GORCHAIN_RPC_URL: "https://rpc.gorbagana.wtf"` wherever it was `REPLACE_WITH_GORCHAIN_RPC_URL`.
- [ ] **Set Solana domain/chain IDs to canonical mainnet** everywhere they appear:
  `SOLANA_DOMAIN_ID: "1399811149"`, `SOLANA_CHAIN_ID: "1399811149"` (was `99998`).
  In `spec-warp-deployer.yml`, `COLLATERAL_DOMAIN_ID: "1399811149"` (collateral=Solana);
  `SYNTHETIC_DOMAIN_ID` (synthetic=gorchain) and the gorchain domain/chain IDs are the
  name-derived `1198486093` (prod) / `1198486095` (devnet).
- [ ] **Verify** `yamllint deployment/spec-*.yml` passes; each spec still has a valid
  `secrets:` block.
- [ ] **Commit:** `git commit -m "fix(specs): canonical Solana IDs + move Helius RPC to secrets"`

### Task 2: Compose ↔ spec ↔ fixture sync for the SOLANA_RPC_URL move

**Files:** `stack_orchestrator/data/compose/docker-compose-hyperlane-{relayer,gas-oracle,warp-ui}.yml`,
`compose-jobs/docker-compose-hyperlane-svm-{deployer,warp-deployer}.yml`,
`tests/e2e/fixtures/test-spec-deployer.yml`, `test-spec-warp-deployer.yml`, `CLAUDE.md`.

- [ ] **Confirm** each affected compose service still lists `SOLANA_RPC_URL` in its
  `environment:` (a k8s Secret key is injected as an env var just like a config var, so
  no compose change is expected — but verify; if a compose used `${SOLANA_RPC_URL:-...}`
  default tied to config, keep it working).
- [ ] **Update e2e fixtures** `test-spec-deployer.yml` / `test-spec-warp-deployer.yml`:
  if they mirror `SOLANA_RPC_URL` under `config:`, move it under `secrets:` to match,
  keeping the `REPLACE_AT_RUNTIME` placeholder the test patches.
- [ ] **Update CLAUDE.md** "Chain-specific vars" note: `SOLANA_RPC_URL` is now a secret
  (Helius URL embeds the API key); domain/chain IDs are committed per-env spec literals.
- [ ] **Commit:** `git commit -m "fix(e2e,docs): SOLANA_RPC_URL is a secret; sync fixtures"`

### Task 3: group_vars — secrets-only env, helius/ghcr, credentials_dir, dead-var cleanup

**Files:** `ops/inventories/prod/group_vars/all.yml`, `ops/inventories/staging/group_vars/all.yml`,
`ops/inventories/prod/secrets.example.yml`, `ops/inventories/staging/secrets.example.yml`.

- [ ] **Build `solana_rpc_url` from the Helius secret** (this var feeds the secret env, not
  config): prod `solana_rpc_url: "https://mainnet.helius-rpc.com/?api-key={{ helius_api_key }}"`;
  staging `"https://devnet.helius-rpc.com/?api-key={{ helius_api_key }}"`.
- [ ] **prod `gorchain_rpc_url`** is no longer consumed by ansible (it's committed in the
  spec) — remove it and the `*_domain_id`/`*_chain_id`/`hardware_wallet_pubkey` vars that
  only fed `config:`. Keep only what the secret env or DNS needs. Remove the dead
  `solana_chain_id: 101`.
- [ ] **staging `dns_zone`** → placeholder `"REPLACE_WITH_STAGING_DNS_ZONE"`; staging
  `gorchain_rpc_url` is committed in the staging spec (deferred, Task 11) — drop here too.
- [ ] **Add `credentials_dir: "{{ ansible_env.HOME }}/.credentials/hyperlane"`** to
  `group_vars/all.yml` (both envs) with a comment that it must match the `file:` paths in
  `deployment/spec-*.yml`. Remove it from `prerequisites_user/defaults/main.yml`.
- [ ] **Add `helius_api_key` and `ghcr_pat`** to both `secrets.example.yml` under the
  operator-supplied (required) section.
- [ ] **Commit:** `git commit -m "feat(ops): build Helius URL from secret; centralize credentials_dir"`

### Task 4: Shrink `stack_env_vars` to secrets-only + add SOLANA_RPC_URL/GHCR_PAT

**Files:** `ops/inventories/{prod,staging}/group_vars/all.yml`.

- [ ] **Remove config-type vars** from every `stack_env_vars` entry: `GORCHAIN_RPC_URL`,
  `GORCHAIN_DOMAIN_ID`, `SOLANA_DOMAIN_ID`, `HARDWARE_WALLET_PUBKEY` (those are committed
  in specs now).
- [ ] **Each stack's list now contains only its `secrets:` env keys.** Add `SOLANA_RPC_URL`
  to every stack whose spec consumes it (deployer, warp-deployer, gas-oracle, relayer,
  warp-ui). Add `GHCR_PAT` to every stack with an `image-pull-secret: token-env: GHCR_PAT`
  (all private-image stacks). Keep `MINIO_*`, `PRIVY_*` where present.
- [ ] **Commit:** `git commit -m "refactor(ops): stack_env_vars carries secrets only"`

### Task 5: `stack_deploy` — drop init, apply secret env, home-based deploy_base

**Files:** `ops/roles/stack_deploy/tasks/deploy.yml`, `ops/roles/stack_deploy/defaults/main.yml`.

- [ ] **`deploy_base`** default → `"{{ ansible_env.HOME }}/deployments"`.
- [ ] **Drop the `deploy init` and "install committed spec" tasks** and the `init_spec`
  fact. Pass the committed spec directly: `deploy create --spec-file {{ spec_file }}
  --deployment-dir {{ deploy_dir }}`.
- [ ] **Build `GHCR_PAT` into the env**: extend the `stack_env` assembly so `GHCR_PAT`
  resolves to `{{ ghcr_pat }}` and `SOLANA_RPC_URL` to `{{ solana_rpc_url }}` (they're in
  the per-stack secret list). Keep `no_log: true`.
- [ ] **Apply the env**: add `environment: "{{ stack_env }}"` to **both** the `deploy create`
  and `deployment start` tasks (the bug: it was assembled but never applied).
- [ ] **Verify** `ansible-playbook --syntax-check` on a playbook using the role passes.
- [ ] **Commit:** `git commit -m "fix(ops): apply assembled secret env; drop redundant deploy init"`

### Task 6: `deploy-all.yml` — home deploy dirs, drop state_distribute for gas-oracle/warp-ui, publish rename, per-stack vars to group_vars

**Files:** `ops/playbooks/deploy-all.yml`, `ops/inventories/{prod,staging}/group_vars/all.yml`.

- [ ] **Remove the `deploy_dir: "{{ kind_mount_root }}/deployments/..."` overrides** from
  the relayer, gas-oracle, warp-ui plays and the validator loop — they inherit the
  home-based `deploy_base` default.
- [ ] **Drop `state_distribute`** from the gas-oracle and warp-ui plays (and their
  `configmap_names`). Keep it for relayer (`agent-config`) and the validator loop.
- [ ] **Rename** the `commit-bridge-state.yml` import to `publish-bridge-state.yml`.
- [ ] **Refactor per-play `vars:`** into a `stacks` dict in `group_vars/all.yml` keyed by
  stack (`spec_file`, `stack_path`, `is_job`, `env_map_key`, `configmap_names`); each play
  reads from it. Remove per-play config env aliases (RPC/domain) now that those aren't
  passed.
- [ ] **Fix the validator spec path**: the loop builds `spec-validator-{{ item.label }}.yml`,
  but committed specs are `spec-validator-{gorchain,solana}.yml` (per chain). Either key the
  spec off `item.chain` or rename committed specs per label. Use `item.chain` →
  `spec-validator-{{ item.chain }}.yml` (matches existing files); document that two
  validators on one chain need per-label specs (sub-project-3 concern).
- [ ] **Verify** `ansible-playbook --syntax-check playbooks/deploy-all.yml` passes.
- [ ] **Commit:** `git commit -m "refactor(ops): home deploy dirs, stacks map, drop dead state_distribute"`

### Task 7: `publish-bridge-state.yml` — rename + spec-patch step

**Files:** rename `ops/playbooks/commit-bridge-state.yml` → `ops/playbooks/publish-bridge-state.yml`;
references in `ops/playbooks/setup-all.yml`/`deploy-all.yml`/`README.md`.

- [ ] **Rename the file** and update all `import_playbook`/doc references.
- [ ] **Add a patch step** after the `generated/` is pulled into the working tree: read
  `{deployment_root}/bridges/{bridge}/generated/program-ids.json` and patch these committed
  `config:` keys in the per-env specs (only when the key currently differs):
  - `deployment[/staging]/spec-relayer.yml`: `GORCHAIN_IGP_PROGRAM_ID`←`.gorchain.igp_program_id`,
    `SOLANA_IGP_PROGRAM_ID`←`.solana.igp_program_id`, `GORCHAIN_IGP_ACCOUNT`←`.gorchain.igp_account`,
    `SOLANA_IGP_ACCOUNT`←`.solana.igp_account`.
  - `spec-gas-oracle.yml`: the two `*_IGP_PROGRAM_ID` keys.
  - `spec-warp-ui.yml`: `GORCHAIN_MAILBOX`←`.gorchain.mailbox`, `SOLANA_MAILBOX`←`.solana.mailbox`.
  Use a key-scoped replace (e.g. `ansible.builtin.replace` per key, or `lineinfile` on
  `  KEY: "..."`), so re-running with identical artifacts is a no-op.
- [ ] **Scope `git add`** to the `generated/` paths **and** the patched `spec-*.yml` only.
  Keep the skip-if-unchanged and `state_review` gate.
- [ ] **Leave the warp-ui `WARP_*` keys as placeholders** with a comment: filled once the
  warp-deployer flow is wired (existing follow-up; `token-config.json` lacks the deployed
  addresses/synthetic mint).
- [ ] **Verify** `--syntax-check` passes.
- [ ] **Commit:** `git commit -m "feat(ops): publish-bridge-state patches derived config into specs"`

### Task 8: `state_repo_dir`, `distribute.yml` de-hardcode, bootstrap target, stack_hostnames

**Files:** `ops/roles/state_distribute/defaults/main.yml`, `ops/roles/credentials/tasks/distribute.yml`,
`ops/playbooks/bootstrap-host.yml`, `ops/roles/stack_deploy/tasks/preflight.yml`,
`ops/playbooks/deploy-all.yml` (+ group_vars `stacks` for hostnames).

- [ ] **`state_repo_dir`** → `"{{ ansible_env.HOME }}/deployments/state-repo"`.
- [ ] **Replace the three hardcoded `~/.credentials/hyperlane`** in `distribute.yml` with
  `{{ credentials_dir }}`.
- [ ] **`bootstrap-host.yml`** default target → `all:!controller` (both plays), so the
  controller/localhost isn't apt-provisioned.
- [ ] **`stack_hostnames`**: make the preflight real — derive expected hostnames per stack
  (from the spec's `http-proxy` `host-name`s / the validator hostname) and pass via the
  `stacks` map; or, if deriving is awkward, delete the dead dig check. Prefer deriving.
- [ ] **Verify** `--syntax-check` on bootstrap + deploy-all.
- [ ] **Commit:** `git commit -m "fix(ops): scoped state repo + credentials_dir, real preflight, bootstrap excludes controller"`

### Task 9: Update localhost assertion tests + README

**Files:** `ops/tests/test_stack_env.yml`, `test_state_paths.yml`, `test_commit_scope.yml`
(rename→`test_publish_scope.yml`), other affected tests; `ops/README.md`.

- [ ] **Update `test_stack_env.yml`** to assert the secrets-only map (no RPC/domain config
  vars; `SOLANA_RPC_URL`/`GHCR_PAT` present where expected).
- [ ] **Update `test_state_paths.yml`** for `~/deployments` deploy_base and the new
  `state_repo_dir`.
- [ ] **Rename/adjust the commit-scope test** for `publish-bridge-state` and the spec-patch
  git-add scope.
- [ ] **Update `README.md`**: publish-bridge-state name, the config model summary, the
  secrets list (helius_api_key, ghcr_pat), deploy dirs under `~/deployments`, and trim the
  resolved follow-ups (gas-oracle/warp-ui conftest is gone; deployer state path; multi-host
  validators).
- [ ] **Run all gates:** `yamllint .`, `ansible-lint .`, `--syntax-check` on every playbook,
  and `for t in tests/test_*.yml; do ansible-playbook -i inventories/prod/hosts.yml "$t"; done`
  — all green.
- [ ] **Commit:** `git commit -m "test,docs(ops): sync tests + README to revised config model"`

### Task 10 (DEFERRED — Layer-3 stand-up): create `deployment/staging/` spec tree

Not built in this pass: the staging `dns_zone` is an undecided placeholder, staging is the
Layer-3 target (after Layers 1–2), and the prod specs should settle first. When standing up
staging, copy each `deployment/spec-*.yml` to `deployment/staging/spec-*.yml` and substitute:
Solana domain/chain `1399811151`, `*_IS_TESTNET: "true"`, gorchain RPC
`https://gorchain-rpc.{staging-zone}`, hostnames/`acme-email` under the staging zone, and
add `deployment/staging/bridges/default/operator/validators.yaml`. Configmap source dirs are
shared (relative `./configmaps/...` resolve to the stack's `data/config`) — no duplication.
The publish-patch and `deployment_root` switch already target `deployment/staging/` when the
staging inventory is used.

---

## Notes / out of scope
- **warp-deployer → warp-ui `WARP_*` patching** stays a follow-up (warp-deployer not in
  `deploy-all`; `token-config.json` lacks deployed warp addresses + synthetic mint).
- **Per-label validator specs** (two validators on one chain) is a sub-project-3 lifecycle
  concern; v1 keys specs off `item.chain`.
