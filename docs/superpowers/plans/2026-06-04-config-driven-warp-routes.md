# Config-Driven Warp Routes (Single Deployment) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the per-route warp-deployer deployments with one laconic-so deployment driven by a checked-in route menu, where the spec selects routes via `WARP_ROUTES`, the Job is idempotently re-runnable, and each route's deploy log is written to a scoped folder and committed with the bridge state.

**Architecture:** Routes become per-env YAML files under `deployment/<env>/bridges/default/warp-routes/`. One warp-deployer deployment mounts the whole menu as a ConfigMap; `deploy.sh` loops over the routes named in the spec's `WARP_ROUTES`, running the existing per-route deploy logic for each (it already writes per-route state to `/state/warp-routes/<name>/` and skips routes already deployed). An opt-in `laconic.recreate-job: "true"` compose label teaches stack-orchestrator to delete+recreate a completed Job on `deployment start`, so re-running picks up newly-selected routes while finished routes self-skip. Per-route logs ride the existing `publish-bridge-state.yml` flow into git.

**Tech Stack:** stack-orchestrator (Python), bash (`deploy.sh`), docker-compose specs, Ansible, pytest (e2e), `jq`.

**Spec:** `docs/superpowers/specs/2026-06-04-config-driven-warp-routes-design.md`

---

## Testing model (read first)

This is infrastructure. The project's integration harness is the pytest e2e suite, which runs **on the separate test machine** against a live Kind cluster — not on the dev host (no cluster/docker here). Verification is split:

- **Unit-testable on the dev host:** the SO Python change (mock the k8s batch API), `bash -n`/`jq` parsing.
- **Static checks on the dev host:** `ruff` (e2e), `ansible-lint` (ops), `bash -n`.
- **Integration-verified on the test machine:** `deploy.sh`, compose/spec wiring, ops, e2e fixtures — via the e2e suite (commands given per task).

Where a step says "(test machine)", it runs there.

## File Structure

**stack-orchestrator — `../stack-orchestrator/`**
- Modify `stack_orchestrator/deploy/k8s/cluster_info.py` — `get_jobs()` stamps a `laconic.recreate-job` Job annotation from the compose service label.
- Modify `stack_orchestrator/deploy/k8s/deploy_k8s.py` — `_create_jobs()` honors that annotation; add `_delete_job_and_wait()`.

**hyperlane-stacks**
- Modify `stack_orchestrator/data/config/warp-deployer-scripts-config/deploy.sh` — setup/loop split; `deploy_route` reads per-route JSON; scoped per-route log + redaction.
- Modify `stack_orchestrator/data/compose-jobs/docker-compose-hyperlane-svm-warp-deployer.yml` — drop per-route env, add `WARP_ROUTES`, mount `warp-routes-config`.
- Create `deployment/bridges/default/warp-routes/usdc.yml`; `deployment/staging/bridges/default/warp-routes/usdc.yml`; `deployment/local/bridges/default/warp-routes/{usdc,sol}.yml`.
- Rename/Modify `deployment/spec-warp-usdc.yml` → `deployment/spec-warp-deployer.yml`; `deployment/staging/spec-warp-*.yml`; `deployment/local/spec-warp-deployer.yml`.
- Modify `ops/playbooks/deploy-all.yml`; create `ops/roles/common/tasks/load_warp_routes.yml`; modify `ops/inventories/{prod,staging,local}/group_vars/all.yml`.
- Modify `tests/e2e/conftest.py`; replace `tests/e2e/fixtures/test-spec-warp-deployer-{usdc,native}.yml` with `test-spec-warp-deployer.yml`.
- Docs: `CLAUDE.md`, `specs/stack-specifications.md`, `stack_orchestrator/data/stacks/hyperlane-svm-warp-deployer/README.md`.

---

## Phase 1 — stack-orchestrator: idempotent Job re-run (foundational)

### Task 1: `laconic.recreate-job` label → delete+recreate the Job

**Files:**
- Modify: `../stack-orchestrator/stack_orchestrator/deploy/k8s/cluster_info.py` (`get_jobs` ~1162–1172: stamp the annotation from the service label)
- Modify: `../stack-orchestrator/stack_orchestrator/deploy/k8s/deploy_k8s.py` (`_create_jobs` ~847–874; new helper above it)

- [ ] **Step 1: Add the delete-and-wait helper** (above `_create_jobs` in `deploy_k8s.py`)

```python
def _delete_job_and_wait(self, job_name, timeout=120):
    """Delete a Job (cascading to its pods) and block until it's gone.

    Jobs are one-shot/immutable; to re-run one we must delete then recreate.
    """
    import time
    try:
        self.batch_api.delete_namespaced_job(
            name=job_name,
            namespace=self.k8s_namespace,
            body=client.V1DeleteOptions(propagation_policy="Background"),
        )
        print(f"Deleting Job {job_name} for recreate")
    except ApiException as e:
        if e.status == 404:
            return
        raise
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            self.batch_api.read_namespaced_job(
                name=job_name, namespace=self.k8s_namespace
            )
        except ApiException as e:
            if e.status == 404:
                return
            raise
        time.sleep(2)
    raise TimeoutError(f"Job {job_name} not deleted within {timeout}s")
```

- [ ] **Step 2: Stamp the annotation in `get_jobs`** (`cluster_info.py`)

`_build_containers` already returns the job file's services dict (the 3rd tuple element, currently `_services`). Read the recreate label from it and set a Job annotation. In the per-job-file loop, before building the `V1Job`:

```python
            recreate = False
            for svc in (_services or {}).values():
                svc_labels = svc.get("labels", {})
                if isinstance(svc_labels, list):
                    svc_labels = dict(item.split("=", 1) for item in svc_labels)
                if str(svc_labels.get("laconic.recreate-job", "")).lower() in (
                    "true", "1", "yes",
                ):
                    recreate = True
                    break
            job_annotations = {"laconic.recreate-job": "true"} if recreate else None
```

and pass it to the existing `V1ObjectMeta(...)`:

```python
                metadata=client.V1ObjectMeta(
                    name=f"{self.app_name}-job-{job_name}",
                    labels=job_labels,
                    annotations=job_annotations,
                ),
```

- [ ] **Step 3: Honor the annotation in `_create_jobs`** (`deploy_k8s.py`)

Replace the body so a Job annotated for recreate is deleted before create; others keep skip-on-409:

```python
def _create_jobs(self):
    job_pull_policy = "IfNotPresent" if self.is_kind() else "Always"
    jobs = self.cluster_info.get_jobs(image_pull_policy=job_pull_policy)
    for job in jobs:
        if opts.o.debug:
            print(f"Sending this job: {job}")
        if not opts.o.dry_run:
            job_name = job.metadata.name
            anns = job.metadata.annotations or {}
            recreate = str(anns.get("laconic.recreate-job", "")).lower() in (
                "true", "1", "yes",
            )
            if recreate:
                self._delete_job_and_wait(job_name)
            try:
                job_resp = self.batch_api.create_namespaced_job(
                    body=job, namespace=self.k8s_namespace
                )
                if opts.o.debug and job_resp.metadata:
                    print(
                        f"Job created: {job_resp.metadata.namespace} "
                        f"{job_resp.metadata.name}"
                    )
            except ApiException as e:
                if e.status == 409:
                    print(f"Job {job_name} already exists, skipping")
                else:
                    raise
```

- [ ] **Step 4: Write the unit test** (follow the SO repo's existing test layout under `tests/`)

```python
from unittest.mock import MagicMock
from kubernetes.client.exceptions import ApiException

def test_create_jobs_recreate_deletes_then_creates():
    d = make_deployer()  # minimal K8sDeployer with mocked batch_api + cluster_info
    job = MagicMock()
    job.metadata.name = "warp-deployer-job"
    job.metadata.annotations = {"laconic.recreate-job": "true"}
    d.cluster_info.get_jobs = lambda image_pull_policy: [job]
    # read returns 404 immediately after delete -> _delete_job_and_wait returns
    d.batch_api.read_namespaced_job.side_effect = ApiException(status=404)
    d._create_jobs()
    d.batch_api.delete_namespaced_job.assert_called_once()
    d.batch_api.create_namespaced_job.assert_called_once()
```

- [ ] **Step 5: Run the unit test**

Run: `python -m pytest tests/ -k recreate -q` (in the SO repo)
Expected: PASS. (If SO's test harness can't construct the deployer cheaply, delete this test and rely on Step 6.)

- [ ] **Step 6: Integration check (test machine)**

Deploy a warp spec whose compose service has `laconic.recreate-job: "true"`, then `laconic-so deployment --dir <dir> start` again. Expected: the second start prints "Deleting Job … for recreate" and a new pod runs (not "already exists, skipping").

- [ ] **Step 7: Commit (SO repo)**

```bash
git -C ../stack-orchestrator add stack_orchestrator/deploy/k8s/cluster_info.py stack_orchestrator/deploy/k8s/deploy_k8s.py tests/
git -C ../stack-orchestrator commit -m "feat(k8s): honor laconic.recreate-job label to re-run completed Jobs"
```

---

## Phase 2 — Route menu + `deploy.sh` loop

### Task 2: Checked-in route menu files

**Files:** Create `deployment/bridges/default/warp-routes/usdc.yml`, `deployment/staging/bridges/default/warp-routes/usdc.yml`, `deployment/local/bridges/default/warp-routes/usdc.yml`, `deployment/local/bridges/default/warp-routes/sol.yml`

- [ ] **Step 1: Prod USDC** — `deployment/bridges/default/warp-routes/usdc.yml`

```yaml
# Warp route: USDC (Solana collateral) <-> gorchain synthetic.
# Chain endpoints live in spec-warp-deployer.yml, not here.
name: USDC-solana-gorchain
origin:
  chain: solana
  type: collateral
  token: "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
  name: "USD Coin"
  symbol: "USDC"
  decimals: 6
remote:
  chain: gorchain
  type: synthetic
  name: "USD Coin"
  symbol: "USDC"
  decimals: 6
metadataUri: "REPLACE_WITH_TOKEN_METADATA_URI"
```

- [ ] **Step 2: Staging USDC** — same as Step 1 with staging's testnet USDC mint in `origin.token` and `metadataUri: ""`.

- [ ] **Step 3: Local USDC** — `deployment/local/bridges/default/warp-routes/usdc.yml`: Step 1 shape with `token: "REPLACE_WITH_USDC_MINT_ADDRESS"` and `metadataUri: ""`.

- [ ] **Step 4: Local SOL (native)** — `deployment/local/bridges/default/warp-routes/sol.yml`

```yaml
name: SOL-solana-gorchain
origin:
  chain: solana
  type: native
  name: "Solana"
  symbol: "SOL"
  decimals: 9
remote:
  chain: gorchain
  type: synthetic
  name: "Solana"
  symbol: "SOL"
  decimals: 9
metadataUri: ""
```

- [ ] **Step 5: Validate YAML parses**

Run: `python3 -c "import yaml,glob; [yaml.safe_load(open(f)) for f in glob.glob('deployment/**/warp-routes/*.yml', recursive=True)]; print('ok')"`
Expected: `ok`

- [ ] **Step 6: Commit**

```bash
git add deployment/bridges deployment/staging/bridges deployment/local/bridges/default/warp-routes
git commit -m "feat(warp): checked-in route menu (usdc all envs, sol local)"
```

### Task 3: `deploy.sh` loops over selected routes

**Files:** Modify `stack_orchestrator/data/config/warp-deployer-scripts-config/deploy.sh`

- [ ] **Step 1: Keep one-time setup outside the loop**

Leave lines `:1-48` (state/log dirs, core check, `chain_var`) and `:66-107` (deployer keypair, Solana CLI config, registry render) at top level, running once.

- [ ] **Step 2: Wrap the per-route body in `deploy_route()` reading JSON**

Move the current per-route work (`:50-64` and `:109-331`) into a function whose fields come from the route's JSON file instead of env:

```bash
deploy_route() {  # $1 = /config/warp-routes/<name>.json
  cfg="$1"
  WARP_ROUTE_NAME=$(jq -r '.name' "$cfg")
  WARP_ORIGIN_CHAIN=$(jq -r '.origin.chain' "$cfg")
  WARP_ORIGIN_TYPE=$(jq -r '.origin.type' "$cfg")
  WARP_ORIGIN_TOKEN=$(jq -r '.origin.token // ""' "$cfg")
  WARP_ORIGIN_NAME=$(jq -r '.origin.name' "$cfg")
  WARP_ORIGIN_SYMBOL=$(jq -r '.origin.symbol' "$cfg")
  WARP_ORIGIN_DECIMALS=$(jq -r '.origin.decimals' "$cfg")
  WARP_REMOTE_CHAIN=$(jq -r '.remote.chain' "$cfg")
  WARP_REMOTE_TYPE=$(jq -r '.remote.type' "$cfg")
  WARP_REMOTE_NAME=$(jq -r '.remote.name' "$cfg")
  WARP_REMOTE_SYMBOL=$(jq -r '.remote.symbol' "$cfg")
  WARP_REMOTE_DECIMALS=$(jq -r '.remote.decimals' "$cfg")
  WARP_TOKEN_METADATA_URI=$(jq -r '.metadataUri // ""' "$cfg")

  ROUTE_STATE_DIR="${STATE_DIR}/warp-routes/${WARP_ROUTE_NAME}"
  mkdir -p "${ROUTE_STATE_DIR}"

  ROUTE_LOG="${ROUTE_STATE_DIR}/deploy.log"
  exec > >(sed "s#${SOLANA_RPC_URL:-__no_solana_rpc__}#<REDACTED>#g" | tee "${ROUTE_LOG}") 2>&1

  # ---- existing per-route body, unchanged, using the WARP_* vars above ----
  # idempotency skip (:53-64); token-config build (:109-136); deploy (:144-155);
  # outputs (:163-195); ownership transfer (:200-238); synthetic mint (:246-277);
  # write state (:287-309); cleanup keypair; preflight (:319-331)
}
```

- [ ] **Step 3: Replace the single-route invocation with the loop**

```bash
echo "=== Deploying warp routes: ${WARP_ROUTES} ==="
for route in ${WARP_ROUTES}; do
  cfg="/config/warp-routes/${route}.json"
  if [ ! -s "$cfg" ]; then
    echo "ERROR: route config $cfg not found for selected route '${route}'"
    exit 1
  fi
  ( deploy_route "$cfg" )   # subshell: per-route exec redirect + vars stay scoped
done
echo "=== All selected warp routes processed ==="
```

- [ ] **Step 4: Syntax check**

Run: `bash -n stack_orchestrator/data/config/warp-deployer-scripts-config/deploy.sh`
Expected: no output.

- [ ] **Step 5: Commit**

```bash
git add stack_orchestrator/data/config/warp-deployer-scripts-config/deploy.sh
git commit -m "feat(warp): deploy.sh loops over selected routes; scoped per-route logs"
```

---

## Phase 3 — Single deployment: compose + spec

### Task 4: Compose — selection + menu mount

**Files:** Modify `stack_orchestrator/data/compose-jobs/docker-compose-hyperlane-svm-warp-deployer.yml`

- [ ] **Step 1: Mark the Job re-runnable + replace per-route env with `WARP_ROUTES`**

Add the recreate label to the service (sibling of `environment:`), then the shared config:

```yaml
    labels:
      laconic.recreate-job: "true"
    environment:
      WARP_ROUTES: ${WARP_ROUTES}
      GORCHAIN_RPC_URL: ${GORCHAIN_RPC_URL}
      GORCHAIN_DOMAIN_ID: ${GORCHAIN_DOMAIN_ID}
      GORCHAIN_CHAIN_ID: ${GORCHAIN_CHAIN_ID}
      SOLANA_DOMAIN_ID: ${SOLANA_DOMAIN_ID}
      SOLANA_CHAIN_ID: ${SOLANA_CHAIN_ID}
      GORCHAIN_IS_TESTNET: ${GORCHAIN_IS_TESTNET:-true}
      SOLANA_IS_TESTNET: ${SOLANA_IS_TESTNET:-true}
      FORCE_REDEPLOY: ${FORCE_REDEPLOY:-false}
```

- [ ] **Step 2: Mount the menu ConfigMap**

```yaml
    volumes:
      - warp-deployer-scripts-config:/opt/scripts:ro
      - warp-deployer-registry-config:/config/registry:ro
      - warp-routes-config:/config/warp-routes:ro
      - bridge-state:/state
      - bridge-logs:/logs
volumes:
  warp-deployer-scripts-config:
  warp-deployer-registry-config:
  warp-routes-config:
  bridge-state:
  bridge-logs:
```

- [ ] **Step 3: Commit**

```bash
git add stack_orchestrator/data/compose-jobs/docker-compose-hyperlane-svm-warp-deployer.yml
git commit -m "feat(warp): compose selects routes via WARP_ROUTES + mounts route menu"
```

### Task 5: Single warp-deployer spec per env

**Files:** `git mv deployment/spec-warp-usdc.yml deployment/spec-warp-deployer.yml`; modify `deployment/staging/spec-warp-*.yml`, `deployment/local/spec-warp-deployer.yml`

- [ ] **Step 1: Prod spec config block** (replace the per-route `WARP_*` keys)

```yaml
namespace: laconic-hyperlane-warp-deployer
# volumes (bridge-state, bridge-logs), network: unchanged
# recreate behavior comes from the compose service label (Task 4), not the spec
config:
  WARP_ROUTES: "usdc"
  GORCHAIN_RPC_URL: "https://rpc.gorbagana.wtf"
  GORCHAIN_DOMAIN_ID: "1198486093"
  GORCHAIN_CHAIN_ID: "1198486093"
  SOLANA_DOMAIN_ID: "1399811149"
  SOLANA_CHAIN_ID: "1399811149"
  GORCHAIN_IS_TESTNET: "false"
  SOLANA_IS_TESTNET: "false"
  FORCE_REDEPLOY: "false"
configmaps:
  warp-deployer-scripts-config: ./configmaps/warp-deployer-scripts-config
  warp-deployer-registry-config: ./configmaps/warp-deployer-registry-config
  warp-routes-config: ./configmaps/warp-routes-config
secrets:
  hyperlane-warp-deployer-secrets:
    keys:
      DEPLOYER_KEYPAIR:       { file: ~/.credentials/hyperlane/deployer-keypair.json }
      HARDWARE_WALLET_PUBKEY: { env: HARDWARE_WALLET_PUBKEY }
      SOLANA_RPC_URL:         { env: SOLANA_RPC_URL }
```

(`WARP_TOKEN_METADATA_URI` is removed — it lives in each route file's `metadataUri`.)

- [ ] **Step 2: Staging + local** — mirror Step 1 with each env's chain values; `local` (already named `spec-warp-deployer.yml`) sets `WARP_ROUTES: "usdc sol"`, testnet IDs, and adds the `warp-routes-config` configmap. Staging: rename its per-route spec, `WARP_ROUTES: "usdc"`. (No `recreate-jobs` key anywhere — it's the compose label.)

- [ ] **Step 3: Commit**

```bash
git add deployment/spec-warp-deployer.yml deployment/staging deployment/local/spec-warp-deployer.yml
git rm deployment/spec-warp-usdc.yml 2>/dev/null || true
git commit -m "feat(warp): single warp-deployer spec per env (WARP_ROUTES selection)"
```

---

## Phase 4 — Ops: single deployment + menu population

### Task 6: Populate menu ConfigMap; replace the per-route loop

**Files:** Create `ops/roles/common/tasks/load_warp_routes.yml`; modify `ops/playbooks/deploy-all.yml`, `ops/inventories/{prod,staging,local}/group_vars/all.yml`

- [ ] **Step 1: `load_warp_routes.yml` (YAML→JSON into the deploy dir's configmap)**

```yaml
---
# Render the SELECTED route files from the env's menu into the warp-deployer
# deployment's configmap dir as JSON. Inputs: warp_routes (list), deploy_dir.
- name: Ensure warp-routes-config dir exists
  ansible.builtin.file:
    path: "{{ deploy_dir }}/configmaps/warp-routes-config"
    state: directory
    mode: "0755"

- name: Render each selected route file to JSON
  ansible.builtin.copy:
    dest: "{{ deploy_dir }}/configmaps/warp-routes-config/{{ item }}.json"
    content: >-
      {{ lookup('ansible.builtin.file',
         deployment_root ~ '/bridges/' ~ bridge_name ~ '/warp-routes/' ~ item ~ '.yml')
         | from_yaml | to_nice_json }}
    mode: "0644"
  loop: "{{ warp_routes }}"
  loop_control:
    label: "{{ item }}"
```

- [ ] **Step 2: Replace the warp play in `deploy-all.yml` (`:49-67`)**

The populate MUST run after `deploy create` (configmaps dir exists) and before `deployment start`. Confirm against `ops/roles/stack_deploy/tasks/deploy.yml:95-127` during implementation; the least-invasive wiring is to add an optional pre-start include hook in `stack_deploy/deploy.yml`:

```yaml
# in stack_deploy/deploy.yml, between "Patch human-readable deployment-id" and
# "Start deployment":
- name: Pre-start hook (optional per-stack include)
  ansible.builtin.include_tasks: "{{ stack_pre_start_tasks }}"
  when: stack_pre_start_tasks is defined
```

Then the warp play:

```yaml
- name: Warp route deployer (single Job — routes from the menu)
  hosts: deployer_hosts
  gather_facts: true
  vars_files:
    - "{{ inventory_dir }}/secrets.yml"
  vars:
    stack_name: hyperlane-warp-deployer
    spec_file: "{{ deployment_root }}/spec-warp-deployer.yml"
    stack_path: "{{ repo_root }}/{{ stacks['hyperlane-svm-warp-deployer'].path }}"
    stack_is_job: true
    stack_env_map_key: hyperlane-svm-warp-deployer
    deploy_dir: "{{ repo_base_dir }}/hyperlane-warp-deployer"
    stack_pre_start_tasks: "{{ playbook_dir }}/../roles/common/tasks/load_warp_routes.yml"
  roles:
    - fetch_stack
    - stack_deploy
```

- [ ] **Step 3: `group_vars` — selection list stays, drives `WARP_ROUTES`**

Keep `warp_routes` (e.g. `["usdc"]`) in each env's `group_vars/all.yml`. Set the spec literal `WARP_ROUTES: "usdc"` to match; add an assertion in the warp play that `spec WARP_ROUTES == warp_routes | join(' ')` if you want them enforced equal (optional).

- [ ] **Step 4: Lint**

Run: `ansible-lint ops/playbooks/deploy-all.yml ops/roles/common/tasks/load_warp_routes.yml ops/roles/stack_deploy/tasks/deploy.yml`
Expected: clean (production profile).

- [ ] **Step 5: Commit**

```bash
git add ops/playbooks/deploy-all.yml ops/roles/common/tasks/load_warp_routes.yml ops/roles/stack_deploy/tasks/deploy.yml ops/inventories/*/group_vars/all.yml
git commit -m "feat(ops): single warp-deployer deployment driven by the route menu"
```

---

## Phase 5 — e2e: single deployment fixture

### Task 7: Collapse `warp_deployment` to one deployment

**Files:** Modify `tests/e2e/conftest.py` (`WARP_ROUTES` ~111; `warp_deployment` :800-915); create `tests/e2e/fixtures/test-spec-warp-deployer.yml`; remove `test-spec-warp-deployer-{usdc,native}.yml`

- [ ] **Step 1: One test spec** — `tests/e2e/fixtures/test-spec-warp-deployer.yml`: `namespace: laconic-hyperlane-warp-deployer`, `WARP_ROUTES: "usdc sol"`, testnet chain config, the three configmaps incl. `warp-routes-config`, secrets (model on `deployment/local/spec-warp-deployer.yml`). The recreate behavior comes from the compose label (Task 4), so the fixture inherits it — no spec key needed.

- [ ] **Step 2: Rewrite `warp_deployment`** — one deployment; write the menu JSONs into the deploy dir (usdc with the runtime test mint, sol as-is); deploy once; wait for the single Job; capture its log once. Keep the yield shape `{"routes": {name: {...}}}`:

```python
DEPLOY_DIR_WARP = DEPLOY_DIR / "hyperlane-warp-deployer"
WARP_NS = "laconic-hyperlane-warp-deployer"
# JOB_NAME: confirm the name SO assigns; e2e deployment_id="warp-deployer" =>
#   f"{deployment_id}-job-hyperlane-svm-warp-deployer"

def _write_menu(deploy_dir, test_mint):
    cmdir = deploy_dir / "configmaps" / "warp-routes-config"
    cmdir.mkdir(parents=True, exist_ok=True)
    import yaml, json
    for stem in ("usdc", "sol"):
        src = REPO_ROOT / "deployment/local/bridges/default/warp-routes" / f"{stem}.yml"
        cfg = yaml.safe_load(src.read_text())
        if stem == "usdc":
            cfg["origin"]["token"] = test_mint
        (cmdir / f"{stem}.json").write_text(json.dumps(cfg))
```

Then `deploy_prepare("hyperlane-svm-warp-deployer", TEST_SPEC, deploy_dir=DEPLOY_DIR_WARP, namespace=WARP_NS, deployment_id="warp-deployer")`, `_write_menu(...)`, `bridge_state_loader.populate(...)`, `deploy_start(...)`, `wait_for_job_complete(WARP_NS, JOB_NAME, 1800)`, `save_job_logs(WARP_NS, JOB_NAME)`. Build `routes` from `bridge_state_loader.discover_routes()` + `read_route_token_config(name)` (origin token = the side carrying `token`). Reuse path (`--skip-warp-deploy`): recover `routes` from `discover_routes()` exactly like the current reuse branch (`conftest.py:824-857`) but over all discovered routes.

- [ ] **Step 3: `WARP_ROUTES` constant** — becomes the test selection (name + `needs_spl_mint`), no per-route `namespace`/`deployment_id`. `warp_ui_deployment` + `_read_route_synthetic_mint` already key off route name + state, so only namespace/job references change.

- [ ] **Step 4: Static check** — `ruff check tests/e2e/conftest.py` → All checks passed.

- [ ] **Step 5: Integration (test machine)**

```
pytest -v --skip-cleanup test_02_warp_deployer.py test_08_bridge.py
```
Expected: both routes deploy via the single Job; forward+reverse bridges pass. Re-run with `--skip-*` to confirm the Job recreates and finished routes skip.

- [ ] **Step 6: Commit**

```bash
git add tests/e2e/conftest.py tests/e2e/fixtures/test-spec-warp-deployer.yml
git rm tests/e2e/fixtures/test-spec-warp-deployer-usdc.yml tests/e2e/fixtures/test-spec-warp-deployer-native.yml
git commit -m "test(e2e): single warp-deployer deployment driven by the route menu"
```

---

## Phase 6 — Docs

### Task 8: Keep-in-sync + specs + README

**Files:** `CLAUDE.md`, `specs/stack-specifications.md`, `stack_orchestrator/data/stacks/hyperlane-svm-warp-deployer/README.md`

- [ ] **Step 1:** `CLAUDE.md` keep-in-sync table → single `spec-warp-deployer.yml` + `test-spec-warp-deployer.yml`; add the `warp-routes-config` configmap + the `bridges/default/warp-routes/` menu.
- [ ] **Step 2:** `stack-specifications.md` warp section → menu, `WARP_ROUTES` selection, the `laconic.recreate-job` label, scoped `deploy.log`.
- [ ] **Step 3:** Warp-deployer `README.md` → single deployment + menu workflow.
- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md specs/stack-specifications.md stack_orchestrator/data/stacks/hyperlane-svm-warp-deployer/README.md
git commit -m "docs: config-driven warp routes (single deployment, menu, recreate label)"
```

---

## Self-Review

**Spec coverage:**
- Checked-in menu (validators pattern) → Task 2 + Task 6 (`load_warp_routes`). ✓
- Single deployment → Tasks 4–6. ✓
- Spec selects routes (`WARP_ROUTES`) → Task 5. ✓
- USDC in all envs → Task 2 Steps 1–3. ✓
- Idempotent → Task 1 (`laconic.recreate-job` label) + existing per-route skip (Task 3). ✓
- Scoped logs exported + checked in → Task 3 Step 2 (`warp-routes/<name>/deploy.log`) + existing publish (no change). ✓

**Placeholder scan:** Task 6 Step 2 (pre-start hook location) and Task 7 Step 2 (fixture body) reference exact existing line ranges to follow rather than re-printing the whole surrounding file — flagged, with the precise insertion points (`deploy.yml:95-127`, `conftest.py:824-857`). No "TBD"/"add error handling"/"similar to" placeholders.

**Type/name consistency:** `WARP_ROUTES` (selection) consistent across compose (Task 4), spec (Task 5), `deploy.sh` loop (Task 3). Route file stems (`usdc`,`sol`) match `WARP_ROUTES` entries and `<name>.json` lookups. The `laconic.recreate-job` label (compose, Task 4) → Job annotation (`get_jobs`, Task 1 Step 2) → read in `_create_jobs` (Task 1 Step 3) use the same string. `warp-routes-config` configmap name matches across compose mount, spec configmaps, and the populate dir.

**Open verification items (resolve during implementation):** exact SO-generated Job name for the single deployment (Task 1 Step 5 / Task 7 Step 2); the create→populate→start ordering hook (Task 6 Step 2).

---

## Execution Handoff

Offered in chat after save.
