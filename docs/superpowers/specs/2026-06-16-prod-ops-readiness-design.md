# Production ops readiness + repo cleanup — design

**Date:** 2026-06-16
**Status:** approved, pending spec review
**Branch:** `prod-ops-readiness` (off `main`)

## Problem

The prod environment can't be brought up from zero today. `ops/runbooks/prod.md`
is a placeholder, `deploy-all` hard-fails before it starts (no committed prod
validator set), the specs are wired for multi-host only (a single-host prod would
hit a MinIO loopback), and there is no prod key-provisioning / funding story. The
repo also carries stale structure (root `specs/`, a Ledger-era `ops-archive`,
vestigial `secrets.yml` files) that muddies the handoff.

Staging is the working reference: the same two-phase flow (`setup-all.yml` →
`deploy-all.yml`), real Cloudflare DNS + Let's Encrypt, prod-shaped specs. Prod
differs in four ways that drive this design:

1. **gorchain is external mainnet** (`https://rpc.gorbagana.wtf`) — the operator
   does **not** run the chain. No chain host, no `prepare-gorchain` step. Solana
   is Helius **mainnet**.
2. **Likely single remote host** for the whole bridge — which introduces a MinIO
   loopback the multi-host specs don't have.
3. **Operator-provisioned hot keys** (not generated throwaways) and **manual
   mainnet funding** (no faucet).
4. **Real public surface** under `bridge.gorbagana.wtf` regardless of host count.

## Goals

- `setup-all` → key prep → funding gate → `deploy-all` reaches a healthy prod
  bridge on a **single host by default**, with multi-host still supported.
- No MinIO loopback on single-host prod.
- A from-zero `prod.md` runbook mirroring staging, calling out only prod deltas.
- A cleaner repo for handoff.

## Non-goals

- The hyp-564 maintenance-ops playbooks (kill-switch/teardown/etc.) — tracked
  separately; only their stale Ledger-era source is removed here.
- Fork/agent changes. The ed25519-via-Privy gap (relayer + validator announce
  must use local HexKey signers because the kms-proxy is secp256k1/AWS-KMS only
  and the agent supports only HexKey for SVM tx signing) is documented, not fixed.
- hyp-a4b: the stacks-side proxy wiring is already in `main`; only the fork image
  release + staging verify remain (out of scope here).

---

## Workstream G — repo cleanup (independent; land first)

Decoupled from the prod-ops feature work and low-risk, so it lands first to keep
the feature diff clean. Three moves, each its own commit:

1. **Move `specs/` → `docs/`** (flat, matching the existing docs layout):
   `ansible-spec.md`, `e2e-test-spec.md`, `stack-specifications.md` →
   `docs/`. Fix the 4 live references:
   - `README.md` (Repo Structure + Documentation links)
   - `CLAUDE.md` (repo-layout block + every `specs/…` mention in the keep-in-sync
     and docs sections)
   - `ops/README.md`
   - `docs/architecture-decisions.md`
   Historical `docs/superpowers/**` plans/specs keep their point-in-time `specs/…`
   references unchanged (they describe state at their write time).
2. **Delete `deployment/ops-archive/`** — the Ledger-based teardown / kill-switch /
   restore / verify-ownership playbooks, superseded by the Privy model. Their
   replacements are tracked under epic **hyp-564** (descriptions self-contained in
   pebbles). Closes the hyp-7f4 `ops-archive` HARDWARE_WALLET nit.
3. **Remove vestigial `inventories/{prod,staging,local}/secrets.yml`** — nothing
   reads them; `load_deployment_config` reads `deployment-config.yml` and its
   fail-msg already tells operators to migrate (`mv secrets.yml → deployment-config.yml`).

---

## Workstream A — topology-aware MinIO addressing for prod

### Why

The prod validator/relayer specs hardcode `AWS_ENDPOINT_URL_S3:
https://s3.bridge.gorbagana.wtf` (a multi-host choice — a validator on another
host can only reach MinIO over public DNS). On a single host that FQDN resolves to
the host's own public IP, so a consumer pod must hairpin out to the cloud edge and
back, through Caddy TLS, to `minio:9000`. Two failure modes:

- NAT hairpin to your own public IP commonly fails on cloud VMs (DO/AWS 1:1 NAT) →
  validators/relayer can't reach MinIO at all.
- Even when it routes, Caddy's 308 HTTP→HTTPS redirect mangles the S3 SDK's signed
  requests (the documented reason local single-host bypasses Caddy,
  `docs/architecture-decisions.md`).

MinIO is the **only** self-referential server-to-server flow. Chains are external
mainnet/Helius in every topology (no hairpin); the warp-UI's RPC calls originate
from remote browsers (no hairpin).

### Key distinction from local single-host

Local's `topology` knob couples three things: in-cluster chains, MinIO, **and**
`manage_dns: false` (mkcert, no DNS). **Prod is different** — single-host prod
still has external chains and a real public surface. So for prod, `topology` must
switch **only the MinIO endpoint**:

| Concern | local single | **prod single** | prod/staging multi |
|---|---|---|---|
| Chain RPCs | in-cluster (`gorchain-rpc:8899`) | **public literal** (mainnet/Helius) | public literal |
| MinIO | `http://hyperlane-minio:9000` | **`http://hyperlane-minio:9000`** | `https://s3.<base_domain>` |
| `manage_dns` | false | **true** | true |

### Changes

In `ops/inventories/prod/group_vars/all.yml`:

- Derive topology from co-location, the same predicate local uses:
  ```yaml
  topology: "{{ 'single' if (groups['minio_hosts'][0] == groups['relayer_hosts'][0]) else 'multi' }}"
  ```
  **Keep `manage_dns: true`** (do NOT tie it to topology). Chains stay committed
  public literals in the specs — **not** tokenized, no chain `external-services`.
- Add the S3 render token and a **MinIO-only** single-host external-services map:
  ```yaml
  spec_token_renders:
    REPLACE_WITH_WALLETCONNECT_PROJECT_ID: "{{ wallet_connect_id }}"
    __S3_ENDPOINT__: "{{ 'http://hyperlane-minio:9000' if topology == 'single' else 'https://s3.' ~ base_domain }}"

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
  (No `__KIND_GATEWAY_IP__` substitution needed — selector mode discovers MinIO
  pod IPs; that part of `render_spec.yml` is a no-op for a chain-free block.)

In the 3 prod consumer specs
(`deployment/spec-validator-gorchain.yml`, `…-solana.yml`, `spec-relayer.yml`):

- Replace the hardcoded `AWS_ENDPOINT_URL_S3: "https://s3.bridge.gorbagana.wtf"`
  with `AWS_ENDPOINT_URL_S3: "__S3_ENDPOINT__"`.
- Add a `# __SINGLE_HOST_EXTERNAL_SERVICES__` marker line where the
  `external-services:` block should be injected (validators + relayer). On
  single-host `render_spec.yml` replaces it with `_xs_minio`; on multi-host it's
  stripped.

`render_spec.yml` already handles both the token substitution and the marker
inject/strip — no role change required.

---

## Workstream B — validator placement, unblock `deploy-all` (hyp-fda)

`deploy-all` fails at `load_validators` because prod has no
`deployment/bridges/default/operator/validators.yaml`.

- **Create `deployment/bridges/default/operator/validators.yaml`** (prod-shaped,
  1-of-1 per chain, mirroring staging). Single-host default → both validators on
  `bridge-host-1`; hostnames under `base_domain`:
  ```yaml
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
  Wallet ids come from the operator's `deployment-config.yml`
  (`privy_validator_wallet_ids`, keyed by label) — already modeled.
- **Add a `validator_hosts` group → `bridge-host-1`** in
  `ops/inventories/prod/hosts.yml` so `bootstrap-host` provisions it and the
  validator loop targets it. (For multi-host prod, the operator moves a validator's
  `host:` and adds the second host — documented in the runbook, not committed.)

---

## Workstream C — prod key lifecycle playbooks

Prod hot signers are operator keyfiles in `credentials_dir`
(`~/.credentials/hyperlane/`): `deployer-keypair.json`, `relayer-gorchain.key`,
`relayer-solana.key`, `relayer-fee-claim.json`, `validator-gorchain.key`,
`validator-solana.key`. Bridge owner + IGP oracle are Privy wallets (no keyfiles).

**Keyfile lifecycle decision:** relayer + validator keyfiles **stay on-box** (0600,
dir 0700) for the deployment lifetime — `deployment restart` re-reads every `file:`
secret via `up()` → `_create_user_secrets()` and fails if any is missing
(`deploy_k8s.py:697-703`). `update-envs` does not re-read files; `restart`
(image bumps, recreate) does. Only the **deployer key** is genuinely one-shot.

### C1. `playbooks/prepare-prod.yml`

Idempotent, prod-targeted:
1. Generate any missing hot keyfile into `credentials_dir` on the host that reads
   it (ed25519 for SVM signers/announce; the validator checkpoint key is Privy, not
   a file). Skip existing files (never overwrite a funded key).
2. Derive each signer's on-chain address.
3. **Run the funding check** (C2) and report gaps — never auto-fund mainnet.

### C2. funding check (reusable, 3 entry points)

A report-only balance check (mirrors staging's balance-driven
`fund-staging-signers.sh`, minus the faucet/airdrop legs): per signer, derive
address from its keyfile, query on-chain balance vs a target table, **fail listing
the underfunded addresses** so the operator funds them from a treasury and re-runs.

Per the operator's request, exposed three ways:
- a standalone `playbooks/verify-funding.yml`,
- imported into `prepare-prod.yml` (step 3),
- imported as a **pre-task gate in `deploy-all.yml`** (prod only) so a deploy can't
  start against underfunded signers.

Target table (prod, GOR on mainnet gorchain / SOL on Solana mainnet) — operator-
tunable; starting values mirror staging shape:

| Signer | gorchain | solana |
|---|---|---|
| deployer | 100 | 10 |
| gorchain validator (announce) | 1 | — |
| solana validator (announce) | — | 1 |
| relayer gorchain | 1 | — |
| relayer solana | — | 1 |
| IGP fee-claim | 1 | 1 |
| Privy IGP oracle | 1 | 1 |
| Privy bridge owner | — | — (transfer target + default fee beneficiary) |

### C3. `playbooks/retire-deployer-key.yml` (post-deploy)

The deployer key is one-shot (after core/warp deploy + ownership/beneficiary
handoff to the Privy bridge owner, no running pod needs it). Post-deploy:
1. Drain its gorchain + solana balances to a treasury address (leaving rent),
   guarded by an explicit confirm flag. The treasury address is a **runtime `-e`
   var** (e.g. `-e treasury_address=<addr>`), not committed.
2. `fetch` the keyfile to the operator's machine (archive off-box).
3. Remove the on-box keyfile (the deployer is a Job; relayer/validator files stay).

Re-deploying additional warp routes later re-runs the deployer Job and needs a
funded deployer key again — so this is "retire," not "delete forever"; the runbook
notes the re-import path.

---

## Workstream D — prod runbook (`ops/runbooks/prod.md`)

Replace the placeholder with a from-zero guide mirroring staging's section
structure (Prereqs → Privy → VMs/inventory → deployment-config → key prep + funding
→ deploy → verify → try the bridge → reset), calling out only the prod deltas:

- **External mainnet gorchain** — no chain host, no `prepare-gorchain`; gorchain
  RPC is `https://rpc.gorbagana.wtf`.
- **Helius mainnet** key (distinct project from staging devnet).
- **Single-host by default** (everything incl. both validators on `bridge-host-1`);
  note the in-cluster MinIO behavior and the multi-host opt-out (move a validator's
  `host:` + add a host).
- **Operator key prep** via `prepare-prod.yml`, **manual mainnet funding** (no
  faucet — fund the listed addresses from treasury, re-run the gate).
- **Cloudflare prod zone** `bridge.gorbagana.wtf`; Let's Encrypt.
- **Deployer-key retirement** step post-deploy.
- **Explorer** at `https://explorer.bridge.gorbagana.wtf` (warp-UI link already
  wired).
- Update the `ops/runbooks/README.md` prod row and the root `README.md` prod row
  (drop "in progress").

---

## Workstream E — warp token metadata (hyp-646)

Resolve `metadataUri: "REPLACE_WITH_TOKEN_METADATA_URI"` in
`deployment/bridges/default/warp-routes/usdc.yml` by committing the operator's
hosted gist raw URL **literally** (same as staging — no render token, no extra
config):

```
metadataUri: "https://gist.githubusercontent.com/prathamesh0/685734f8aac9dd22c9eeb3d1e7f8e407/raw/c2c4abdc3d7b05866c00d95379983bca64d864a7/token-metadata.json"
```

The metadata JSON it serves (verified 200; image 200 PNG) is:

```json
{
  "name": "USD Coin",
  "symbol": "USDC",
  "description": "Hyperlane-bridged Circle USDC on Gorchain.",
  "image": "https://raw.githubusercontent.com/solana-labs/token-list/main/assets/mainnet/EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v/logo.png"
}
```

The deployer validates `name`/`symbol` against `remote.name`/`remote.symbol`
(USD Coin / USDC) and that the image URL serves — both hold.

---

## Workstream F — hardening

- **hyp-c7d (env-contract gates):** in the env-contract assertions, reject a
  `REPLACE_WITH…` / unset `public_ip` and assert `wallet_connect_id` is defined, so
  a half-filled prod inventory fails up front instead of mid-deploy.
- **hyp-534 residual (core-deployer log redaction):** the core deployer's main log
  still leaks the Helius URL — `deployer-scripts-config/deploy.sh:28` is a bare
  `tee` with no redaction (the warp-deployer already redacts at `deploy.sh:113`).
  Mirror that one-line `sed` redaction in the core deployer's exec pipeline.

---

## Keep-in-sync impact

- **A:** specs (×3 consumers) ↔ prod `group_vars` (`spec_token_renders`,
  `single_host_external_services`, `topology`). Staging/local specs unchanged
  (staging stays multi → public S3; local already tokenized).
  `check-spec-parity.py` compares only structural skeletons (mapping keys, list
  lengths, nesting) — scalar leaf values are exempt and YAML comments are invisible
  to it. So tokenizing the S3 value (`__S3_ENDPOINT__`) and adding the
  `# __SINGLE_HOST_EXTERNAL_SERVICES__` comment marker leave the prod↔staging
  skeletons identical; no staging change is needed for parity.
- **B:** new `deployment/bridges/default/operator/validators.yaml` ↔ prod
  `hosts.yml` validator group ↔ runbook.
- **D:** `prod.md` ↔ `runbooks/README.md` ↔ root `README.md`.
- **G:** `specs/` move ↔ README/CLAUDE.md/ops-README/architecture-decisions.

## Testing

E2E runs on kind with local chains (operator/CI, not this session) and exercises
the **local single-host** MinIO selector path already — the prod change reuses the
same `render_spec` mechanism, so the regression surface is the rendered prod spec
shape (assert `__S3_ENDPOINT__` and the marker render correctly for single vs multi
via a spec-render check / `check-spec-parity.py`). The prod bring-up itself is
validated by the operator following `prod.md` on a real host. C2's funding gate and
C3's retirement are operator-run playbooks (hand off with commands).

## Out of scope

- hyp-564 maintenance playbooks (only stale source removed here).
- hyp-a4b fork image release + staging verify (stacks wiring already in `main`).
- ed25519-via-Privy for relayer/validator-announce (structural fork+agent change).
- Setting the beneficiary / any change on the already-deployed staging bridge.
