# Deploy-Side Ansible Design

**Date:** 2026-06-01
**Status:** Implemented, then revised after a deploy-ansible review (2026-06-02):
config model split into static-committed / secret-env-injected / deployment-derived
publish-patched; per-env committed spec trees; `deploy init` dropped;
`commit-bridge-state` → `publish-bridge-state`. See the configuration model below.

This is **sub-project 2** of the ops-layer redesign
(`docs/superpowers/specs/2026-05-29-ops-layer-redesign-and-ledger-signing-design.md`).
It builds the ansible layer that gets a Hyperlane SVM bridge **fully running across
machines with zero on-chain signing**. Operator-attended signing/lifecycle
playbooks (kill-switch, restore, ism-update, teardown, …) are **sub-project 3**
and out of scope here.

Sub-project 2 is independent of sub-project 1 (the Ledger client fork): nothing
here invokes the Ledger.

---

## Goal

A single `deploy-all.yml` brings up every long-running stack of a bridge on its
target host(s), reading committed specs + bridge state, with all secrets either
generated on the controller or supplied in a gitignored per-env file. Validated
by **manual testing across real VMs** — own chains first, then staging on Solana
devnet.

## Scope

In scope: six roles, the provisioning/deploy playbooks, the inventory + vars +
secrets scaffolding for `prod` and `staging`, and a Layer-0 lint CI job.

Out of scope: anything operator-attended/on-chain (sub-project 3), the Ledger
client (sub-project 1), per-validator spec *generation* from a template (a
sub-project-3 lifecycle concern; v1 uses the committed `spec-validator-*.yml`).

---

## Repository layout

Ansible lives at a **top-level `ops/`** (sibling of `deployment/`), per the
locked redesign layout. Per-environment isolation uses the standard ansible
multi-env pattern (separate inventory trees), named `inventories/` by convention:

```
ops/
  ansible.cfg                      # ForwardAgent=yes; roles/collections paths
  requirements.yml                 # community.general, kubernetes.core
  playbooks/                       # env-agnostic
  roles/                           # env-agnostic (the six roles)
  inventories/
    prod/
      hosts.yml                    # YAML inventory (.yml extension required)
      group_vars/all.yml           # dns_zone, dns_records, chain config, per-stack env maps
      host_vars/<alias>.yml        # public_ip, deploy_user, privileged_user, kind_mount_root
      secrets.example.yml          # committed template
      secrets.yml                  # GITIGNORED — operator-supplied + generated secrets
    staging/                       # same shape, staging values (Solana devnet)
```

The env split mirrors the spec-tree split already in `deployment/`:

- `deployment/[staging/]` holds the **deploy inputs** — `spec-*.yml` and the
  bridge state/operator inputs under `bridges/default/{operator,generated}/`.
- `ops/inventories/{prod,staging}/` holds the **ansible control data** —
  inventory, host/group vars, secrets.

The environment is selected entirely by the inventory passed
(`-i inventories/<env>/hosts.yml`); there is **no** `-e env=` switch. Each
inventory's `group_vars/all.yml` sets `deployment_root` (→ `deployment/` for prod,
`deployment/staging/` for staging), so choosing the inventory picks both trees.

## Topology model (already locked)

Hybrid stack→host mapping (`docs/architecture-decisions.md` "Production Topology
Model"). Seven singleton inventory groups — `controller`, `deployer_hosts`,
`minio_hosts`, `relayer_hosts`, `gas_oracle_hosts`, `monitoring_hosts`,
`warp_ui_hosts` — plus `validator_hosts` computed at runtime from
`deployment/bridges/default/operator/validators.yaml`. In a single-host run every
group contains the same alias. Moving a singleton to another host = inventory
edit only; no spec or playbook change.

---

## The six roles

Each role has one responsibility, explicit inputs, and is re-run-safe.

### 1. `prerequisites_privileged`
Runs as `privileged_user` (`become: yes`). Installs host-level dependencies:
Docker, kind, kubectl. The only role needing root. Idempotent via package state +
version checks.

### 2. `prerequisites_user`
Runs as `deploy_user` (no sudo). Installs the pinned laconic-so release binary
(cerc-io upstream), creates `~/.credentials/hyperlane/` (mode 0700), and creates
the host's `kind_mount_root` (e.g. `/srv/kind/hyperlane/`) owned by `deploy_user`.
Inputs: `laconic_so_version`, `kind_mount_root` (host_vars).

### 3. `dns_cloudflare`
Reconciles A records under `dns_zone` against Cloudflare via
`community.general.cloudflare_dns`. Reads `dns_records:` from `group_vars/all.yml`
plus validator hostnames auto-appended from `validators.yaml`; resolves each
record's `host` alias → `public_ip` via host_vars. **Additive** — declared
records ensured, others untouched. TTL 300. Inputs: `CLOUDFLARE_API_TOKEN`
(secrets), `dns_zone`, `dns_records`, `validators.yaml`.

### 4. `credentials`
Two halves:
- **Generate** (idempotent): MinIO root user/password, per-validator MinIO IAM
  key/secret pairs (`openssl rand`), and any relayer hot key. Written once to the
  gitignored `secrets.yml`; **never regenerated if already present** (re-running
  provisioning does not rotate live creds).
- **Distribute**: drops cred files into `~/.credentials/hyperlane/<...>` on each
  consumer host and assembles the MinIO secret env (`MINIO_ROOT_*`,
  `MINIO_USERS`, per-label `<LABEL>_KEY_ID/_SECRET`). Operator-supplied secrets
  pass through untouched. Asserts required external keys are present and fails
  fast naming any missing.

Inputs: `secrets.yml`, `validators.yaml`. The generate-vs-distribute split lets
the sub-project-3 add-validator flow call just a `-e validator_label=` slice.

### 5. `stack_deploy`
The workhorse. For one spec on its target host:
- **Pre-flight**: the spec's hostnames resolve to the host's `public_ip` (`dig`
  check, fails fast with a clear message); host has docker + laconic-so.
- **Assemble env**: build the **secrets-only** env the spec's `secrets:` declares
  from `secrets.yml`, driven by a per-stack env-var map (`no_log`). Config values
  are already literal in the committed spec — see the configuration model; the
  process env cannot set them (SO writes `config:` verbatim).
- **Deploy**: `laconic-so deploy create --spec-file <committed per-env spec>` if
  the deploy_dir is absent (skip if present → idempotent) — **no `deploy init`**,
  since `deploy create` reads the committed spec directly; patch a readable
  `deployment-id`; then `laconic-so deployment start
  --perform-cluster-management`, with the assembled secret env applied to **both**
  create and start.
- **Job specs** (deployer, warp-deployer): detect job-type and wait for
  completion (mirrors the e2e `_wait_for_job_complete`), not pod-ready.

The deployment dir lives under `~/deployments/<stack>` (home, ext4), **not** under
`kind_mount_root` (reserved for runtime host-path data); runtime volumes stay
pinned to their absolute `/srv/kind/...` paths inside the spec. Mirrors the
`woodburn_deployer` pattern.

Inputs: `spec_file` (committed), target host, assembled secret env.

### 6. `state_distribute`
On each consumer host, git-pulls the repo via **ansible SSH agent forwarding**
(no at-rest creds on the host), then copies the relevant
`deployment/bridges/default/generated/*` files into
`{deploy_dir}/configmaps/<cm-name>/` so SO mounts them as ConfigMaps at start.
Ensures git is installed and the remote host key is in `known_hosts`. Runs
immediately before each consumer's `stack_deploy`. Inputs: repo URL/branch,
`bridge` name, deploy_dir.

Composition: roles 1–2 are bootstrap-only; 3 and 6 are shared with sub-project-3
flows; 5 is used by every deploy.

---

## Cluster lifecycle (SO semantics)

SO owns the single Kind cluster per host (verified in stack-orchestrator source):

- `helpers.py:create_cluster` is **create-or-reuse** (single cluster per host,
  `check_mounts_compatible` on reuse). First stack on a host creates it + Caddy;
  later stacks reuse it.
- `deploy_k8s.py:down` destroys the cluster **only** when not skipping cluster
  management; stack resources are cleaned per `app.kubernetes.io/stack` label.

Rule the roles encode:
- **start** → `--perform-cluster-management` (create-or-reuse).
- **single-stack stop** → `--skip-cluster-management` (default) — never tears
  down the shared host cluster.
- **full-host teardown** → `--perform-cluster-management` on the final stop.

---

## Configuration & secret model

Every value a spec's `config:`/`secrets:` consumes falls into one of three
categories, each resolved by a different mechanism. This split is **forced by
SO**: `deployment_create.py:_write_config_file` writes spec `config:` values
**verbatim** to `config.env` — it does *not* expand `${VAR}` from the process
environment. So the process env (assembled by `stack_deploy`) can only feed the
`secrets:` path; `config:` values must already be literal in the committed spec.

**1. Static, non-secret — committed in the spec (per env).**
gorchain RPC URL (public), domain IDs, chain IDs, `*_IS_TESTNET`,
`AWS_ENDPOINT_URL_S3`, `NEXT_PUBLIC_WALLET_CONNECT_ID`, GHCR username, and the
per-validator `PRIVY_WALLET_ID`. The operator replaces each `REPLACE_WITH_*`
placeholder once the value is known and commits it. Because prod and staging
differ, these live in **two committed spec trees** — `deployment/` (prod) and
`deployment/staging/` (devnet) — selected by `deployment_root`.

Canonical IDs: both chains are SVM (no EIP-155 chainId — agave/Solana identify by
genesis hash, not a number), so Hyperlane assigns a `u32` domain derived from the
chain name (`0x536F6C` = ASCII `"Sol"` + a network byte) and sets `chainId ==
domainId` (verified in Hyperlane's own agent config). Solana = the canonical value
**`1399811149`** mainnet (prod) / **`1399811151`** devnet (staging). gorchain has no
canonical Hyperlane domain (we deploy our own core on it), so we derive one the same
way from `"Gor"` (`0x476F72`) + the matching network byte: domain == chain ==
**`1198486093`** (prod) / **`1198486095`** (staging devnet). Collision-free against
Hyperlane's known-domains enum.

**2. Secret — never committed, env-injected via `secrets:`.**
`SOLANA_RPC_URL` (the Helius URL embeds an API key → it is a secret, **moved out
of `config:` into `secrets:`**), `PRIVY_APP_ID/SECRET` and `PRIVY_*_WALLET_ID`,
MinIO root + per-validator IAM, `GHCR_PAT`, and the `~/.credentials` keyfiles.
`stack_deploy` assembles these into the process environment for `deploy
create`/`start`. The Helius URL is built from one operator secret
`helius_api_key`: `https://{mainnet,devnet}.helius-rpc.com/?api-key={{ helius_api_key }}`.

- *Generated on the controller* (no operator input; written once to the gitignored
  `secrets.yml`, never rotated on re-run): MinIO root creds, per-validator IAM.
- *Operator-supplied* (external): `cloudflare_api_token`, `privy_app_id`,
  `privy_app_secret`, `helius_api_key`, `ghcr_pat`. The `credentials` role fails
  fast naming any missing required key.

**3. Deployment-derived — produced by a Job, publish-patched into the spec.**
Core values (IGP program IDs/accounts, mailbox addresses) from the deployer's
`generated/program-ids.json`; warp-route values from the warp-deployer's
`token-config.json` + `warp-deploy-outputs/program-ids.json`. `publish-bridge-state`
reads those artifacts and patches the matching `config:` keys in the committed
per-env spec, then commits (see flow). Mapping:

| Spec | `config:` key | Source |
|---|---|---|
| relayer, gas-oracle | `GORCHAIN/SOLANA_IGP_PROGRAM_ID` | `program-ids.json .<chain>.igp_program_id` |
| relayer | `GORCHAIN/SOLANA_IGP_ACCOUNT` | `.<chain>.igp_account` |
| warp-ui | `GORCHAIN/SOLANA_MAILBOX` | `.<chain>.mailbox` |
| warp-ui | `WARP_COLLATERAL/SYNTHETIC_ADDRESS` | `warp-deploy-outputs/program-ids.json .<chain>.base58` |
| warp-ui | `WARP_TOKEN_MINT` | `token-config.json .warpRoute.tokenMint` |

`WARP_SYNTHETIC_MINT` is the one warp-ui value the warp-deployer doesn't emit yet —
left empty in the committed spec (warp-ui tolerates it).

The rich `agent-config.json` (validators + relayer) stays a **ConfigMap**
distributed by `state_distribute`; only the scalar env values above are
publish-patched. gas-oracle and warp-ui therefore need **no** `state_distribute` —
their derived values arrive via publish-patched `config:`.

`secrets.yml` is gitignored per env; `secrets.example.yml` is committed alongside.

---

## Playbooks and flow

Environment chosen by inventory (`-i inventories/<env>/hosts.yml`). Granular
playbooks stay independently runnable; `setup-all.yml` and `deploy-all.yml` are
the two top-level phase wrappers. The
phases are deliberately separate so the whole fleet is provisioned before any
stack comes up (a half-provisioned host can't block a deploy midway).

### Phase 1 — provisioning: `setup-all.yml`
1. `bootstrap-host.yml` across **all** hosts — two plays: privileged
   (`prerequisites_privileged`) then unprivileged (`prerequisites_user`).
2. `configure-dns.yml` — `dns_cloudflare` from the controller (records live
   before any Caddy/LE).
3. `distribute-credentials.yml` — `credentials` role (generate + distribute;
   MinIO secret env assembled).

After Phase 1 the fleet is fully provisioned: binaries installed, mount roots
created, DNS resolving, creds in place. Nothing deployed.

### Phase 2 — deployment: `deploy-all.yml`
Starts with a **per-host preflight assert** (laconic-so present, DNS resolves to
the right IP, required creds present) that fails fast with "run `setup-all.yml`
first" rather than failing deep inside a stack. Then, per the locked order:

4. **MinIO** — `stack_deploy spec-minio.yml` (per-validator IAM auto-provisioned
   by the `minio-provision` CronJob from `MINIO_USERS`).
5. **core deployer** Job — `stack_deploy spec-deployer.yml`, wait-for-job-complete
   (writes mailbox/IGP/ISM program-ids to `generated/`).
6. **warp-deployer** Job — **required** (this deploys the USDC collateral↔synthetic
   route that actually moves tokens; the core deployer only lays down message
   passing). Same host as the core deployer; reads its `program-ids.json` from the
   shared bridge-state volume, writes `token-config.json` + `warp-deploy-outputs/`.
7. **publish-bridge-state** — commits `generated/` **and** patches the
   deployment-derived `config:` keys (core IGP/mailbox + warp route addresses/mint)
   into the committed per-env consumer specs (see gate below).
8. **long-running stacks** — relayer + validators run `state_distribute` (for the
   `agent-config` ConfigMap) → `stack_deploy`; gas-oracle, monitoring, and warp-ui
   run `stack_deploy` only (no `state_distribute` — their derived values are
   publish-patched into `config:`). Validators loop over `validators.yaml` →
   `spec-validator-<label>.yml` on each validator's host.

(Step 10, on-chain ownership/ISM setup, is sub-project 3. `deploy-all` stops
after the stacks are running.) Steps 4–8 are idempotent; the only attended point
is the optional state-review gate.

### `publish-bridge-state.yml` (flag-gated)
Pulls deployer-host state into the controller's working tree under
`deployment/[staging/]bridges/default/generated/`, **patches** the
deployment-derived `config:` keys into the committed per-env consumer specs (per
the mapping in the configuration model), then commits + pushes via
**agent-forwarded SSH**. (Renamed from `commit-bridge-state` — it both commits and
publishes.)

- **Default** (`state_review=false`): commits and pushes automatically —
  `deploy-all.yml` runs hands-off end to end.
- **`-e state_review=true`**: shows the diff and pauses for operator approval
  before commit/push.

Safety regardless of flag: `git add` is **scoped to the `generated/` paths and the
patched `spec-*.yml` only** (never sweeps unrelated working-tree changes); each
spec patch only rewrites the listed keys, and re-running with the same artifacts
is a no-op; the commit is **skipped if nothing changed** (idempotent); the diff is
**always printed**; push targets the current tracking branch.

State only flows deployer-host → git → consumer-hosts (the multi-machine
principle: consumers never read peer namespaces).

### `stop-all.yml` (test-iteration helper)
Pure `laconic-so deployment stop` per stack — single-stack stops use
`--skip-cluster-management`; the final stop destroys the cluster with
`--perform-cluster-management`. No on-chain actions (so it is deploy-side, not
the sub-project-3 `teardown.yml`). Used to reset a VM between manual test runs.

---

## Testing approach

Manual, across real VMs, staged cheapest-first. No automated multi-node harness
is built.

**Layer 0 — static, no VMs** (also a small CI job on PRs): `yamllint` +
`ansible-lint` on `ops/`; `ansible-playbook --syntax-check` on every playbook;
`--check --diff` where modules support it. Shell-out tasks (laconic-so, kind,
docker) are written **check-mode-aware** (skip/no-op under `ansible_check_mode`)
so a `--check` run still exercises the rest.

**Layer 1 — single VM, own chains.** All groups → one host; gorchain + a local
Solana validator reachable. Run `setup-all.yml` then `deploy-all.yml`
end-to-end — the ansible-driven equivalent of the existing Kind e2e.

**Layer 2 — multi-VM split, own chains.** Spread stacks across hosts by editing
only `hosts.yml` + `validators.yaml`. Exercises the cross-host paths Layer 1
can't: `state_distribute` git-pull over agent forwarding, per-host DNS,
credential distribution to multiple hosts, `check_mounts_compatible` cluster
reuse per host.

**Layer 3 — staging on Solana devnet + single-node gorchain.** Same `ops/`,
selected by inventory (`-i inventories/staging/hosts.yml`). The long-lived
rehearsal/soak ground once Layers 1–2 are stable.

**Acceptance per layer** (verified by hand, mirroring the e2e assertions): all
stacks `Running`; Caddy serving TLS per hostname; validators announcing + writing
checkpoints to their MinIO buckets; relayer delivering a test message end-to-end;
warp-ui reachable. Plus **idempotency**: re-running `setup-all.yml` and
`deploy-all.yml` reports no changes and breaks nothing (no cred rotation, no
duplicate commits, cluster reused).

### Layer 3 staging environment requirements

What the staging rehearsal ground needs, per the staging topology
(`2026-05-29-staging-environment-design.md`). Two scoping facts:

- **No AWS account.** The validator "AWS KMS" signer is fronted by the
  `hyperlane-kms-proxy` sidecar, which calls **Privy** (`main.go` reads only
  `privyAppID/Secret/WalletID`). Validator signing = a Privy project.
- **Only gorchain runs a local chain node.** The Solana side points at **Helius
  devnet** (remote), so that host stays light.

**VMs — 3 required:**

| Host | Stacks | Starting spec |
|---|---|---|
| `staging-gorchain` | single-node agave/gorchain validator + gorbagana RPC + hyperlane-validator (gorchain) | 8 vCPU / 32 GB / 500 GB NVMe SSD |
| `staging-solana-validator` | hyperlane-validator (solana) → Helius devnet | 2 vCPU / 4 GB / 40 GB SSD |
| `staging-bridge-ops` | relayer, gas-oracle, monitoring, MinIO, warp-ui | 4 vCPU / 8 GB / 80 GB SSD |

- **Scratch (second) validator** for the add/remove + 2-of-2 ISM rehearsals does
  **not** need a 4th VM. A `hyperlane-validator` is a lightweight agent pod
  (~1 GB RAM, 5 Gi RocksDB), placement is independent of which chain it validates
  (it reaches the chain over RPC), and adding one is just a `validators.yaml` +
  DNS entry on its host's existing shared Kind cluster. Co-locate it transiently
  on `staging-gorchain` (the most RAM headroom) for the drill, then tear it down.

- **gorchain is the host to get right** — agave is RAM/IO-heavy and prunes its
  blockstore to ~100–400 GB; NVMe is effectively mandatory. Defer to
  `gorchain-stacks`' own single-validator recommendation; the row above is a safe
  starting point.
- `staging-bridge-ops` storage sums the spec reservations (MinIO 10Gi +
  monitoring 12Gi + relayer 5Gi + overhead). Each VM also runs Docker + a single
  Kind node + Caddy + laconic-so, accounted for above.

**Networking (per VM):** public IPv4 with inbound 80 + 443 open from the internet
(real Let's Encrypt HTTP-01); inbound 22 from the controller; outbound to Helius
devnet, Cloudflare API, GHCR, cerc-io (laconic-so binary), Circle devnet faucet.

**DNS:** a Cloudflare-managed zone, operator-supplied via `dns_zone` (not
necessarily a subdomain of the prod zone), and a Cloudflare API token scoped to
that zone.

**External accounts/keys:** Helius devnet API key (separate project from prod);
a Privy project (validator keys + gas-oracle wallet); GHCR pull access for the
private `gorbagana-dev/*` images; Circle devnet USDC mint
`4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU` (faucet, no account).

**Controller (operator's machine):** ansible + `community.general` /
`kubernetes.core` collections, git, ssh (agent forwarding), `dig`, kubectl; SSH
to all VMs.

**Hardware wallet — not needed for this sub-project.** `setup-all.yml` +
`deploy-all.yml` do zero signing, and day-to-day staging ops use the
`signer: hot-key-file` fallback. A physical **Ledger** (Nano S Plus / Nano X /
Flex; Solana app, blind signing enabled, udev rules on the controller; pubkey
funded on both chains) is only required for the periodic `signer: ledger`
rehearsal before a prod promotion — a **sub-project-3** concern, using a
secondary device kept for staging.

---

## Relationship to other docs

- `docs/architecture-decisions.md` — "Production Topology Model", "Production
  bootstrap workflow", "DNS Prerequisites", "Multi-Machine Prod Principle" are
  the locked inputs this spec implements.
- `docs/ops-decisions.md` — the operator-attended playbooks it describes are
  sub-project 3; this spec covers the deploy-side (no-signing) half.
- `docs/superpowers/specs/2026-05-29-ops-layer-redesign-and-ledger-signing-design.md`
  — parent decomposition; this is its sub-project 2.
- `docs/superpowers/specs/2026-05-29-staging-environment-design.md` — Layer 3
  target; consistent with this layout.
