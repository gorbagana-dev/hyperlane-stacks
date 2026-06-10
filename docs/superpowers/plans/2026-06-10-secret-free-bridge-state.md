# Secret-Free Generated Bridge State Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the deployer from embedding chain RPC URLs (the Helius URL carries an API key) in published bridge state, deliver real URLs to agents via env overrides, and gate `publish-bridge-state` so no secret can ever reach `deploy_branch`.

**Architecture:** Two layers (spec §6, `docs/superpowers/specs/2026-06-10-websocket-fast-bridging-design.md`). Layer 1: `deploy.sh` writes the placeholder `http://rpc-placeholder.invalid` into `agent-config.json` and the published `registry/metadata.yaml`; agents get real URLs via `HYP_CHAINS_<CHAIN>_CUSTOMRPCURLS` env vars (the settings parser prefers `customRpcUrls` over `rpcUrls` — verified at `hyperlane-base/src/settings/parser/mod.rs:594-619`). Layer 2: `publish-bridge-state.yml` scans the staged `generated/` tree for secret literals and fails closed.

**Tech Stack:** Bash (deploy scripts), docker-compose YAML (SO stacks), SO spec YAML (prod/local/e2e), Ansible (ops), pytest (e2e).

**Branch:** Work on the existing `fast-bridging-design` branch in `/home/dev/git_puller/repos/hyperlane-stacks`. Commit per task. NEVER push — the user pushes.

**Pebble:** `hyp-d34.1`. Mark `in_progress` at start (`pb update hyp-d34.1 --status in_progress`), close in Task 8.

---

## Verified facts the design rests on (do not re-derive)

1. **Parser override semantics** (fork checkout `/home/dev/git_puller/repos/hyperlane-monorepo`, branch `gorbagana`): `parse_base_and_override_urls(chain, "rpcUrls", "customRpcUrls", ...)` at `rust/main/hyperlane-base/src/settings/parser/mod.rs:594` — when the env override `HYP_CHAINS_<NAME>_CUSTOMRPCURLS` is present its URLs *replace* the config-file `rpcUrls` entirely. An **empty-string** override is a hard parse error (`"".parse::<Url>()` fails in `parse_custom_urls`, mod.rs:577-592), and `parse_chain` runs for *every* chain in `CONFIG_FILES`, so an empty override for an unused chain still crashes the agent. Consequence: **never let compose render an empty `CUSTOMRPCURLS`** — always supply a parseable default.
2. **SO compose-env rendering** (`stack-orchestrator/stack_orchestrator/deploy/k8s/helpers.py:1197-1230`): `${VAR}` with VAR missing from the spec's `config:` renders as **empty string** (an explicit pod `env:` entry, which *shadows* `envFrom` secrets). `${VAR:-default}` works (simple defaults only; nested `${A:-${B}}` does not). Consequence: a secret-sourced override must be delivered **only** via the spec's `secrets:` key name (envFrom), never also declared in compose.
3. **SO user secrets** (`deploy_k8s.py:656`): `secrets: <name>: keys: KEY: { env: VAR }` reads `VAR` from the operator's process env at `deployment start` and injects `KEY` as a pod env var via `envFrom.secretRef`. The e2e harness already uses this (`os.environ.update` before `deploy_start` in `tests/e2e/conftest.py`).
4. **Leak sites** (only two): the `agent-config.json` heredoc (`deployer-scripts-config/deploy.sh:357-411`, `rpcUrls` lines for gorchain and solana) and the published registry copy (`deploy.sh:446-452`, copies the *working* render that the hyperlane CLI needs with real URLs). The warp-deployer's registry render is `/tmp`-only (never published); its `deploy.log` already redacts `SOLANA_RPC_URL` (`warp-deployer-scripts-config/deploy.sh:113`); `warpRoutes.yaml`, `token-config.json`, `relayer-whitelist.json`, `program-ids.json`, `gas-oracle-config.json`, `multisig-config.json` contain no URLs (all checked 2026-06-10).
5. **No other consumer reads URLs from artifacts**: ops `render_spec.yml` reads only `program-ids.json` + `relayer-whitelist.json`; `build-warp-ui-config.sh` reads no URLs; gas-oracle/monitoring/warp-ui get RPC URLs from their own env/config. Validators/relayer are the only artifact-URL consumers — they are exactly who gets the env overrides.
6. **Who needs which real URL at runtime**: relayer → both chains; gorchain validator → gorchain only; solana validator → solana only. Placeholder URLs for *unused* chains parse fine (valid URL syntax) and are never dialed.

## Delivery design (the one decision everything implements)

| Env var | Mechanism | Why |
|---|---|---|
| `HYP_CHAINS_GORCHAIN_CUSTOMRPCURLS` | compose: `${GORCHAIN_RPC_URL:-http://rpc-placeholder.invalid}` | gorchain URL is non-secret `config:` everywhere; the `:-` default keeps the var parseable in the solana validator (whose spec has no `GORCHAIN_RPC_URL`) — see fact 1 |
| `HYP_CHAINS_SOLANA_CUSTOMRPCURLS` | spec `secrets:` key `{ env: SOLANA_RPC_URL }`, **not** in compose | solana URL is a secret in prod; compose would render `""` there and shadow the secret — see fact 2 |

---

### Task 1: Sanitize the deployer's published artifacts

**Files:**
- Modify: `stack_orchestrator/data/config/deployer-scripts-config/deploy.sh`

- [ ] **Step 1: Define the placeholder once, before the agent-config heredoc**

At `deploy.sh` line ~352 (just under the `=== Building agent-config.json ===` echo, before the `cat > ... <<AGENT_EOF`), add:

```bash
# Published artifacts carry no real RPC URLs (the Helius URL embeds an API
# key). Agents receive real URLs via HYP_CHAINS_<CHAIN>_CUSTOMRPCURLS.
PLACEHOLDER_RPC_URL="http://rpc-placeholder.invalid"
```

- [ ] **Step 2: Placeholder the two rpcUrls lines in the heredoc**

In the `AGENT_EOF` heredoc, change the gorchain line (~deploy.sh:369):

```bash
      "rpcUrls": [{"http": "${GORCHAIN_RPC_URL}"}],
```
to
```bash
      "rpcUrls": [{"http": "${PLACEHOLDER_RPC_URL}"}],
```

and the solana line (~deploy.sh:393) identically:

```bash
      "rpcUrls": [{"http": "${PLACEHOLDER_RPC_URL}"}],
```

(The deployer itself keeps using the real `${RPC_URL}`/`${SOLANA_RPC_URL}` env vars for its own CLI calls — only the artifact changes.)

- [ ] **Step 3: Publish a sanitized registry render instead of the working copy**

Replace the registry-copy block at deploy.sh:445-452:

```bash
# registry/: copy the rendered chain-metadata directory
if [ -d "${RENDERED_REGISTRY_DIR}/chains" ]; then
  rm -rf "${STATE_DIR}/registry"
  mkdir -p "${STATE_DIR}/registry"
  cp -a "${RENDERED_REGISTRY_DIR}/chains/." "${STATE_DIR}/registry/"
else
  echo "WARNING: Registry config not found at ${RENDERED_REGISTRY_DIR}/chains; not written"
fi
```

with:

```bash
# registry/: publish a sanitized render — placeholder URLs only. The working
# copy at ${RENDERED_REGISTRY_DIR} keeps real URLs for the CLI's own use.
if [ -f "${REGISTRY_DIR}/metadata.yaml.tmpl" ]; then
  rm -rf "${STATE_DIR}/registry"
  mkdir -p "${STATE_DIR}/registry"
  GORCHAIN_RPC_URL="${PLACEHOLDER_RPC_URL}" SOLANA_RPC_URL="${PLACEHOLDER_RPC_URL}" \
    envsubst < "${REGISTRY_DIR}/metadata.yaml.tmpl" > "${STATE_DIR}/registry/metadata.yaml"
elif [ -d "${RENDERED_REGISTRY_DIR}/chains" ]; then
  rm -rf "${STATE_DIR}/registry"
  mkdir -p "${STATE_DIR}/registry"
  cp -a "${RENDERED_REGISTRY_DIR}/chains/." "${STATE_DIR}/registry/"
else
  echo "WARNING: Registry config not found at ${RENDERED_REGISTRY_DIR}/chains; not written"
fi
```

(`REGISTRY_DIR` is the ConfigMap mount already used at deploy.sh:70; the `elif` keeps the existing static-file path, which by construction has no env-expanded secrets.)

- [ ] **Step 4: Syntax-check and simulate the sanitized render**

Run:
```bash
bash -n stack_orchestrator/data/config/deployer-scripts-config/deploy.sh && echo SYNTAX_OK
```
Expected: `SYNTAX_OK`

Run (proves the env-prefixed envsubst yields placeholders while other vars still expand):
```bash
GORCHAIN_CHAIN_ID=1 GORCHAIN_DOMAIN_ID=2 GORCHAIN_IS_TESTNET=true \
SOLANA_CHAIN_ID=3 SOLANA_DOMAIN_ID=4 SOLANA_IS_TESTNET=false \
GORCHAIN_RPC_URL="http://rpc-placeholder.invalid" SOLANA_RPC_URL="http://rpc-placeholder.invalid" \
envsubst < stack_orchestrator/data/config/deployer-registry-config/metadata.yaml.tmpl
```
Expected: both `http:` lines show `http://rpc-placeholder.invalid`; `chainId`/`domainId` show 1/2/3/4.

- [ ] **Step 5: Commit**

```bash
git add stack_orchestrator/data/config/deployer-scripts-config/deploy.sh
git commit -m "fix(deployer): publish placeholder RPC URLs in agent-config and registry

The Helius SOLANA_RPC_URL embeds an API key; published state must not
carry it. Agents receive real URLs via HYP_CHAINS_<CHAIN>_CUSTOMRPCURLS.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Deliver real URLs to the relayer

**Files:**
- Modify: `stack_orchestrator/data/compose/docker-compose-hyperlane-relayer.yml` (relayer service `environment:`)
- Modify: `deployment/spec-relayer.yml:36-42` (secrets)
- Modify: `deployment/local/spec-relayer.yml:41-46` (secrets)
- Modify: `tests/e2e/fixtures/test-spec-relayer.yml` (secrets block)
- Modify: `tests/e2e/conftest.py:1237-1242` (relayer fixture env export)

- [ ] **Step 1: Add the gorchain override to the relayer compose service**

In `docker-compose-hyperlane-relayer.yml`, in the `relayer:` service `environment:` block, directly after the `CONFIG_FILES: /config/agent-config.json` line, add:

```yaml
      # agent-config.json carries placeholder rpcUrls; real URLs arrive here.
      # Solana (secret) is injected via secrets: as HYP_CHAINS_SOLANA_CUSTOMRPCURLS.
      HYP_CHAINS_GORCHAIN_CUSTOMRPCURLS: ${GORCHAIN_RPC_URL:-http://rpc-placeholder.invalid}
```

- [ ] **Step 2: Add the solana override key to all three relayer specs**

`deployment/spec-relayer.yml` — in `secrets: hyperlane-relayer-secrets: keys:`, after the existing `SOLANA_RPC_URL` line (line 42; keep it — `claim-fees.sh` consumes it):

```yaml
      HYP_CHAINS_SOLANA_CUSTOMRPCURLS: { env: SOLANA_RPC_URL }
```

`deployment/local/spec-relayer.yml` — in `secrets: hyperlane-relayer-secrets: keys:` (after line 46):

```yaml
      HYP_CHAINS_SOLANA_CUSTOMRPCURLS: { env: SOLANA_RPC_URL }
```

`tests/e2e/fixtures/test-spec-relayer.yml` — in `secrets: hyperlane-relayer-secrets: keys:`:

```yaml
      HYP_CHAINS_SOLANA_CUSTOMRPCURLS: { env: SOLANA_RPC_URL }
```

- [ ] **Step 3: Export SOLANA_RPC_URL in the e2e relayer fixture**

`tests/e2e/conftest.py` — in the relayer fixture's `os.environ.update({...})` (line ~1237, the one with `HYP_CHAINS_GORCHAIN_SIGNER_KEY`), add:

```python
        "SOLANA_RPC_URL":                 "http://solana-rpc:18899",
```

- [ ] **Step 4: Validate YAML + lint**

```bash
python3 - <<'EOF'
import yaml
for f in ("stack_orchestrator/data/compose/docker-compose-hyperlane-relayer.yml",
          "deployment/spec-relayer.yml",
          "deployment/local/spec-relayer.yml",
          "tests/e2e/fixtures/test-spec-relayer.yml"):
    yaml.safe_load(open(f))
    print("OK", f)
EOF
ruff check tests/e2e/conftest.py
```
Expected: four `OK` lines, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add stack_orchestrator/data/compose/docker-compose-hyperlane-relayer.yml \
        deployment/spec-relayer.yml deployment/local/spec-relayer.yml \
        tests/e2e/fixtures/test-spec-relayer.yml tests/e2e/conftest.py
git commit -m "feat(relayer): deliver real chain RPC URLs via env overrides

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Deliver real URLs to the validators

**Files:**
- Modify: `stack_orchestrator/data/compose/docker-compose-hyperlane-validator.yml` (validator service `environment:`)
- Modify: `deployment/spec-validator-gorchain.yml` (`config:` + nothing in secrets)
- Modify: `deployment/spec-validator-solana.yml` (secrets)
- Modify: `deployment/local/spec-validator-gorchain.yml` (`config:`)
- Modify: `deployment/local/spec-validator-solana.yml` (secrets)
- Modify: `tests/e2e/fixtures/test-spec-validator-gorchain.yml` (`config:`)
- Modify: `tests/e2e/fixtures/test-spec-validator-solana.yml` (secrets)
- Modify: `tests/e2e/conftest.py:1064-1070` (validator fixture env export)

- [ ] **Step 1: Add the gorchain override to the validator compose service**

In `docker-compose-hyperlane-validator.yml`, in the `validator:` service `environment:` block, directly after `CONFIG_FILES: /config/agent-config.json`, add:

```yaml
      # agent-config.json carries placeholder rpcUrls; the origin chain's real
      # URL arrives via override. The :- default keeps the var parseable on the
      # solana validator (gorchain unused there); solana (secret) is injected
      # via secrets: as HYP_CHAINS_SOLANA_CUSTOMRPCURLS on its own stack.
      HYP_CHAINS_GORCHAIN_CUSTOMRPCURLS: ${GORCHAIN_RPC_URL:-http://rpc-placeholder.invalid}
```

- [ ] **Step 2: Give the gorchain validator specs the real gorchain URL**

`deployment/spec-validator-gorchain.yml` — in `config:` (after line 9 `CHECKPOINT_BUCKET`):

```yaml
  GORCHAIN_RPC_URL: "https://rpc.gorbagana.wtf"
```

`deployment/local/spec-validator-gorchain.yml` — in `config:`:

```yaml
  GORCHAIN_RPC_URL: "__GORCHAIN_RPC_URL__"
```

`tests/e2e/fixtures/test-spec-validator-gorchain.yml` — in `config:`:

```yaml
  GORCHAIN_RPC_URL: "http://gorchain-rpc:8899"
```

- [ ] **Step 3: Add the solana override key to the three solana validator specs**

In each of `deployment/spec-validator-solana.yml`, `deployment/local/spec-validator-solana.yml`, `tests/e2e/fixtures/test-spec-validator-solana.yml`, add to `secrets: hyperlane-validator-solana-secrets: keys:`:

```yaml
      HYP_CHAINS_SOLANA_CUSTOMRPCURLS: { env: SOLANA_RPC_URL }
```

- [ ] **Step 4: Export SOLANA_RPC_URL in the e2e validator fixture**

`tests/e2e/conftest.py` — in the shared validator fixture's `os.environ.update({...})` (line ~1064, the one with `PRIVY_APP_ID` / `HYP_DEFAULTSIGNER_KEY`), add (unconditionally — harmless for the gorchain validator, whose spec doesn't reference it):

```python
        "SOLANA_RPC_URL":          "http://solana-rpc:18899",
```

- [ ] **Step 5: Validate YAML + lint**

```bash
python3 - <<'EOF'
import yaml
for f in ("stack_orchestrator/data/compose/docker-compose-hyperlane-validator.yml",
          "deployment/spec-validator-gorchain.yml",
          "deployment/spec-validator-solana.yml",
          "deployment/local/spec-validator-gorchain.yml",
          "deployment/local/spec-validator-solana.yml",
          "tests/e2e/fixtures/test-spec-validator-gorchain.yml",
          "tests/e2e/fixtures/test-spec-validator-solana.yml"):
    yaml.safe_load(open(f))
    print("OK", f)
EOF
ruff check tests/e2e/conftest.py
```
Expected: seven `OK` lines, ruff clean.

- [ ] **Step 6: Commit**

```bash
git add stack_orchestrator/data/compose/docker-compose-hyperlane-validator.yml \
        deployment/spec-validator-gorchain.yml deployment/spec-validator-solana.yml \
        deployment/local/spec-validator-gorchain.yml deployment/local/spec-validator-solana.yml \
        tests/e2e/fixtures/test-spec-validator-gorchain.yml \
        tests/e2e/fixtures/test-spec-validator-solana.yml tests/e2e/conftest.py
git commit -m "feat(validator): deliver the origin chain's real RPC URL via env override

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Ops inventories — export the env the new secret keys resolve from

**Files:**
- Modify: `ops/inventories/prod/group_vars/all.yml` (`stack_env_vars.hyperlane-validator`)
- Modify: `ops/inventories/staging/group_vars/all.yml` (`stack_env_vars.hyperlane-validator`)
- Modify: `ops/inventories/local/group_vars/all.yml` (secret-env values + `stack_env_vars`)

Context: `stack_env_vars` lists the env var NAMES each stack's spec injects via `secrets: { env: NAME }`; the ansible deploy layer exports them for `laconic-so`. The relayer entries in prod/staging already export `SOLANA_RPC_URL`; the validator entries don't (validators had no RPC env until now). Local has no `SOLANA_RPC_URL` ansible var at all yet.

- [ ] **Step 1: prod + staging — validator gains SOLANA_RPC_URL**

In both `ops/inventories/prod/group_vars/all.yml` and `ops/inventories/staging/group_vars/all.yml`, in `stack_env_vars: hyperlane-validator:`, add:

```yaml
    - SOLANA_RPC_URL
```

(The relayer entry already lists it. Only the solana validator's spec references it; exporting for both validator deploys is export-side only and harmless.)

- [ ] **Step 2: local — define SOLANA_RPC_URL and export it for relayer + validator**

In `ops/inventories/local/group_vars/all.yml`:

(a) In the `# --- Secret-env values ---` block (after the `MINIO_ROOT_PASSWORD` line), add:

```yaml
# Not a secret locally (own Solana chain) — exported only so the specs'
# HYP_CHAINS_SOLANA_CUSTOMRPCURLS: { env: SOLANA_RPC_URL } keys resolve.
SOLANA_RPC_URL: "{{ 'http://solana-rpc:18899' if topology == 'single' else solana_rpc_url }}"
```

(b) In `stack_env_vars:`, add `- SOLANA_RPC_URL` to both `hyperlane-relayer:` and `hyperlane-validator:` lists.

(c) Update the stale comment above `stack_env_vars` — change

```yaml
# Lists only the SECRET env-var NAMES each spec injects via secrets: { env: NAME }.
# Local has no SOLANA_RPC_URL/COLLATERAL_CHAIN_RPC_URL here — those are rendered
# config: literals (see spec_token_renders). Keep in sync with the secrets: blocks
# in deployment/local/spec-*.yml.
```
to
```yaml
# Lists the env-var NAMES each spec injects via secrets: { env: NAME }.
# COLLATERAL_CHAIN_RPC_URL stays a rendered config: literal (see
# spec_token_renders); SOLANA_RPC_URL is listed for the CUSTOMRPCURLS override
# keys. Keep in sync with the secrets: blocks in deployment/local/spec-*.yml.
```

- [ ] **Step 3: Validate YAML**

```bash
python3 - <<'EOF'
import yaml
for f in ("ops/inventories/prod/group_vars/all.yml",
          "ops/inventories/staging/group_vars/all.yml",
          "ops/inventories/local/group_vars/all.yml"):
    yaml.safe_load(open(f))
    print("OK", f)
EOF
```
Expected: three `OK` lines.

- [ ] **Step 4: Commit**

```bash
git add ops/inventories/prod/group_vars/all.yml \
        ops/inventories/staging/group_vars/all.yml \
        ops/inventories/local/group_vars/all.yml
git commit -m "feat(ops): export SOLANA_RPC_URL for the CUSTOMRPCURLS secret keys

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Publish-time secret-scan gate

**Files:**
- Modify: `ops/playbooks/publish-bridge-state.yml` (new task between "Copy the deployer's generated state into the on-host clone" and "Stage the generated paths (scoped)")

- [ ] **Step 1: Add the gate task**

Insert after the copy task and before "Stage the generated paths (scoped)":

```yaml
    # Fail-closed gate: no secret material may reach deploy_branch even if a
    # future artifact regresses. Needles are passed via environment so the
    # rendered command never contains a secret; output names files only.
    - name: Secret-scan the generated state before staging
      ansible.builtin.shell:
        executable: /bin/bash
        cmd: |
          set -eu
          dir="{{ deployment_root }}/{{ generated_rel }}"
          hits=""
          for var in SCAN_NEEDLE_1 SCAN_NEEDLE_2 SCAN_NEEDLE_3 SCAN_NEEDLE_4; do
            val="${!var:-}"
            if [ -n "$val" ]; then
              found=$(grep -rlF -- "$val" "$dir" || true)
              if [ -n "$found" ]; then hits="$hits$found"$'\n'; fi
            fi
          done
          found=$(grep -rli -- 'api-key=' "$dir" || true)
          if [ -n "$found" ]; then hits="$hits$found"$'\n'; fi
          if [ -n "$hits" ]; then
            echo "ERROR: secret material found in files staged for publish:" >&2
            printf '%s' "$hits" | sort -u >&2
            exit 1
          fi
      environment:
        SCAN_NEEDLE_1: "{{ SOLANA_RPC_URL | default('') }}"
        SCAN_NEEDLE_2: "{{ SOLANA_WS_URL | default('') }}"
        SCAN_NEEDLE_3: "{{ helius_api_key | default('') }}"
        SCAN_NEEDLE_4: "{{ solana_rpc_url | default('') }}"
      changed_when: false
      when: not ansible_check_mode
```

Notes for the implementer:
- `SOLANA_WS_URL` doesn't exist yet (lands with the WS work, `hyp-d34.5`); `default('')` makes the gate forward-compatible.
- `solana_rpc_url` (lowercase) is the local-inventory spelling.
- Empty needles are skipped in the loop; the `api-key=` substring scan is unconditional (catches any Helius-style URL regardless of which var carried it).
- Use explicit `if` statements, not `[ ... ] && ...` — `set -e` exits on a failing test that terminates an AND-list.

- [ ] **Step 2: Syntax-check the playbook**

```bash
cd ops && ansible-playbook playbooks/publish-bridge-state.yml --syntax-check -i inventories/local/hosts.yml 2>&1 | tail -3; cd ..
```
Expected: `playbook: playbooks/publish-bridge-state.yml` (no error). If the local inventory hosts file has a different name, use whatever `ls ops/inventories/local/` shows.

Also run ansible-lint if available:
```bash
cd ops && (command -v ansible-lint >/dev/null && ansible-lint playbooks/publish-bridge-state.yml || echo "ansible-lint not installed — skipped"); cd ..
```
Expected: no new violations (existing `# noqa` style in the file is the baseline).

- [ ] **Step 3: Unit-exercise the gate logic standalone**

```bash
tmp=$(mktemp -d)
mkdir -p "$tmp/generated"
echo 'rpcUrl: http://rpc-placeholder.invalid' > "$tmp/generated/clean.yaml"
echo 'url: https://mainnet.helius-rpc.com/?api-key=SECRET123' > "$tmp/generated/dirty.yaml"
SCAN_NEEDLE_1="https://mainnet.helius-rpc.com/?api-key=SECRET123" bash -c '
  set -eu
  dir="'"$tmp"'/generated"
  hits=""
  for var in SCAN_NEEDLE_1; do
    val="${!var:-}"
    if [ -n "$val" ]; then
      found=$(grep -rlF -- "$val" "$dir" || true)
      if [ -n "$found" ]; then hits="$hits$found"$'\n'; fi
    fi
  done
  found=$(grep -rli -- "api-key=" "$dir" || true)
  if [ -n "$found" ]; then hits="$hits$found"$'\n'; fi
  if [ -n "$hits" ]; then echo "BLOCKED:"; printf "%s" "$hits" | sort -u; exit 1; fi
  echo PASSED
'; echo "exit=$?"
rm -rf "$tmp"
```
Expected: `BLOCKED:` listing only `dirty.yaml`, then `exit=1`. Re-run after deleting `dirty.yaml`'s secret line to see `PASSED` / `exit=0` if you want the green case.

- [ ] **Step 4: Commit**

```bash
git add ops/playbooks/publish-bridge-state.yml
git commit -m "feat(ops): fail-closed secret scan before publishing bridge state

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: E2E assertions — published state is secret-free

**Files:**
- Modify: `tests/e2e/test_01_deployer.py` (`test_agent_config_configmap` ~line 99, registry test ~line 244, one new test)

Note: these asserts encode the *new* contract; they go red against pre-Task-1 artifacts and green after. A dedicated red run is impractical (full e2e is ~an hour); the green run happens in Task 8.

- [ ] **Step 1: Pin agent-config rpcUrls to the placeholder**

In `test_agent_config_configmap`, replace (lines ~98-100):

```python
            # RPC URL present
            rpc_urls = chain.get("rpcUrls", [])
            assert rpc_urls, f"{chain_name}: rpcUrls is empty"
```
with:
```python
            # rpcUrls must be placeholders — real URLs reach agents only via
            # HYP_CHAINS_<CHAIN>_CUSTOMRPCURLS env overrides (hyp-d34.1)
            rpc_urls = chain.get("rpcUrls", [])
            assert rpc_urls == [{"http": "http://rpc-placeholder.invalid"}], (
                f"{chain_name}: expected placeholder rpcUrls, got {rpc_urls}"
            )
```

- [ ] **Step 2: Registry must carry placeholders and no real URLs**

In the registry test (the one asserting `"rpcUrls:" in raw`, ~line 244), after the existing per-chain asserts, add:

```python
        # Published registry is sanitized (hyp-d34.1)
        assert "http://rpc-placeholder.invalid" in raw, (
            "registry missing the placeholder rpcUrl"
        )
        for real_url in ("http://gorchain-rpc:8899", "http://solana-rpc:18899"):
            assert real_url not in raw, (
                f"registry leaks a real RPC URL: {real_url}"
            )
```

- [ ] **Step 3: Whole-state scan mirroring the publish gate**

Add a new test method to the same class as `test_agent_config_configmap`:

```python
    def test_state_contains_no_solana_rpc_url(
        self,
        deployer_deployment: DeploymentInfo,
        bridge_state_loader: BridgeStateLoader,
    ) -> None:
        """No published state file may embed the Solana RPC URL (the prod
        equivalent carries a Helius API key); mirrors the publish-time gate."""
        secret = "http://solana-rpc:18899"
        offenders = [
            str(p)
            for p in bridge_state_loader.state_dir.rglob("*")
            if p.is_file() and secret in p.read_text(errors="ignore")
        ]
        assert not offenders, f"state files embed the Solana RPC URL: {offenders}"
```

(Reuse the exact fixture parameter names/types the neighbouring tests in that class use — match the file's existing imports; `DeploymentInfo` and `BridgeStateLoader` are already imported there.)

- [ ] **Step 4: Lint**

```bash
ruff check tests/e2e/test_01_deployer.py
```
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/test_01_deployer.py
git commit -m "test(e2e): assert published bridge state carries no real RPC URLs

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: Documentation sync

**Files:**
- Modify: `CLAUDE.md` (Config patterns → Environment variables)
- Modify: `specs/stack-specifications.md` (deployer outputs + validator/relayer config sections)

- [ ] **Step 1: CLAUDE.md**

In `## Config patterns` → `### Environment variables`, after the `SOLANA_RPC_URL is a secret` bullet, add:

```markdown
- **Generated state is secret-free**: `agent-config.json` and the published
  `registry/metadata.yaml` carry the placeholder `http://rpc-placeholder.invalid`
  instead of real RPC URLs. Agents get real URLs via
  `HYP_CHAINS_<CHAIN>_CUSTOMRPCURLS` env overrides — gorchain from compose
  `${GORCHAIN_RPC_URL:-placeholder}`, solana via each spec's `secrets:` key
  `{ env: SOLANA_RPC_URL }` (never via compose: a missing var renders as empty
  string, which both crashes the agent's URL parser and shadows the envFrom
  secret). `publish-bridge-state.yml` secret-scans `generated/` before staging.
```

- [ ] **Step 2: stack-specifications.md**

(a) Near line 111 (`hyperlane-agent-config — agent-config.json for validators/relayer`), extend the line:

```markdown
- `hyperlane-agent-config` — agent-config.json for validators/relayer
  (`rpcUrls` are placeholders; real URLs are env-injected — see below)
```

(b) In the validator section's env list (~line 303-306 region, where `CONFIG_FILES=/config/agent-config.json` is described), add:

```markdown
- `HYP_CHAINS_GORCHAIN_CUSTOMRPCURLS` — real gorchain RPC URL (compose default
  keeps it parseable on the solana validator); the solana validator gets
  `HYP_CHAINS_SOLANA_CUSTOMRPCURLS` via `secrets:` instead
```

(c) In the relayer section's env list (~line 338, `GORCHAIN_RPC_URL`, `SOLANA_RPC_URL`), add:

```markdown
- `HYP_CHAINS_GORCHAIN_CUSTOMRPCURLS` / `HYP_CHAINS_SOLANA_CUSTOMRPCURLS` —
  real chain RPC URLs overriding the placeholder `rpcUrls` in agent-config.json
  (solana via `secrets:` — the Helius URL embeds an API key)
```

Read the surrounding lines first and match the file's exact list style.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md specs/stack-specifications.md
git commit -m "docs: secret-free generated state and CUSTOMRPCURLS delivery

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8: Full e2e verification + close the pebble

**This machine is a shared dev box — do NOT run the e2e suite or any stacks here. Hand the run off to the user.**

- [ ] **Step 1: Hand the e2e run to the user**

Give the user the exact command to run where/when they choose:

```bash
cd tests/e2e && python3 -m pytest -x -q 2>&1 | tail -30
```
Expected: all tests pass (suite takes on the order of an hour; it deploys the full bridge into kind). The critical proof points:
- `test_01_deployer.py` — new placeholder/scan asserts green (artifacts sanitized)
- `test_05_validator.py` + `test_06_relayer.py` — agents start and stay healthy with placeholder agent-config + env overrides (an empty/missing override would crash them at settings parse)
- `test_09_bridge.py` / `test_13_warp_ui_bridge.py` — messages actually relay end-to-end (overrides point at the right chains)

If validators/relayer crash-loop: `kubectl logs` will show a settings parse error naming the chain — check the override env var made it into the pod (`kubectl exec ... env | grep CUSTOMRPC`) before touching code.

- [ ] **Step 2: Close the pebble (only after the user reports a green run)**

```bash
pb update hyp-d34.1 --status closed
NO_COLOR=1 pb show hyp-d34.1
```
Then commit the pebble state change:
```bash
git add .pebbles/events.jsonl
git commit -m "chore: close hyp-d34.1 — secret-free generated bridge state

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

- [ ] **Step 3: Report**

Summarize for the user: commits on `fast-bridging-design`, e2e result, and that the user pushes when ready. Do NOT push.
