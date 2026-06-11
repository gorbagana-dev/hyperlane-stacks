# Staging Ops Standup Design

> Supersedes the implementation-facing parts of
> `2026-05-29-staging-environment-design.md`, which predates the ops layer
> (PRs #30/#33/#34/#36) and still references `ops/envs/` and
> `commit-bridge-state.yml`. That doc remains the source for staging's
> *purpose* (rehearsal ground, lifecycle, validator-set drills); this one
> defines what makes staging actually deployable through the implemented
> ops layer — correct by construction.

## Goal

Make `setup-all.yml` + `deploy-all.yml` bring the staging bridge up
end-to-end on real hardware, with the gaps that bit the local rollout
(silent `default()` fallbacks, unfilled placeholders, spec drift) closed by
machine-enforced checks instead of operator care.

## Decisions

- **Scope**: deployable staging only. The hyp-564 maintenance/rehearsal
  playbooks (kill-switch, restore, ISM update) stay deferred.
- **Gorchain chain**: ops-managed via an *isolated* playbook — not part of
  the `setup-all`/`deploy-all` composites, mirroring how local keeps
  `prepare-chains.yml` outside them.
- **Topology**: 3 VMs per the staging design — `staging-bridge-ops`
  (singleton stacks), `staging-gorchain` (chain + RPC Caddy front only),
  `staging-hyperlane-validators` (BOTH hyperlane validators). (Revised
  2026-06-11: the gorchain validator moved off the chain host — the host
  Caddy RPC front owns 80/443 there, and the validators' kind ingress
  needs the same ports.)
- **DNS zone**: `staging.gorbagana.wtf` (same Cloudflare account as prod).
  Subject to change; a zone change is mechanical — edit `base_domain` in the
  staging `group_vars` and the hostname literals in
  `deployment/staging/spec-*.yml`, re-run `configure-dns.yml`.
- **Spec model**: staging specs are committed literals exactly prod-shaped
  (no generation, no `__TOKENS__`). Parity is *checked*, not generated —
  `publish-bridge-state.yml` patches deployment-derived `config:` values
  into the committed specs post-deploy, and generated files would fight
  that patcher. Staging also exists to rehearse the prod procedure, which
  is a spec edit.

## 1. Inventory and env contract

### `ops/inventories/staging/hosts.yml`

- `chain_hosts` shrinks to **only** `staging-gorchain` — the only host
  running a chain. Solana is Helius devnet; nothing chain-like runs on
  `staging-hyperlane-validators`.
- `staging-hyperlane-validators` moves to a new `validator_hosts` group. No
  playbook targets the group; it exists so `bootstrap-host.yml`
  (`all:!controller`) provisions the host and the validator loop in
  `deploy-all.yml` can delegate to it. A comment in the file says exactly
  that.

### `ops/inventories/staging/group_vars/all.yml`

Every var the roles branch on becomes explicit:

```yaml
ansible_host: "{{ public_ip | default(omit) }}"   # same as local; currently missing → SSH fails
topology: multi          # literal, not local's group-comparison derivation
manage_dns: true         # literal
base_domain: staging.gorbagana.wtf
```

`dns_records` gains `rpc → staging-gorchain` (the cross-host gorchain RPC
seam). Validator records keep coming from the `validators.yaml` auto-append
in `dns_cloudflare`.

### Env contract assertion

New `ops/roles/common/tasks/assert_env_contract.yml`: asserts that the
inventory defines `topology`, `manage_dns`, `deployment_subdir`,
`base_domain`, `bridge_name`, and that `stack_env_vars` has
an entry for every key in `stacks`. Fails fast naming each missing var.

Wired in two places:

- first play of `setup-all.yml` and `deploy-all.yml` (controller, no
  facts) — a real run can never start against an under-specified inventory;
- Layer-0 test `ops/tests/test_env_contract.yml`, run against **all three**
  inventories in CI (see §5).

This kills the failure class where a role's `when: topology == 'single'`
silently evaluates against an undefined var.

## 2. Staging specs — `deployment/staging/spec-*.yml`

Nine files mirroring `deployment/spec-*.yml` exactly in shape (same
`config:` keys, `secrets:` blocks, `configmaps:`, `volumes:`, annotations,
http-proxy route shape), with staging values:

| Knob | Staging value |
|---|---|
| Domain/chain IDs | gorchain `1198486095`, solana `1399811151` (devnet derivations, see `ops/README.md`) |
| Testnet flags | `*_IS_TESTNET: "true"` |
| `GORCHAIN_RPC_URL` | `https://rpc.staging.gorbagana.wtf` |
| Public hostnames | `{s3,minio-console,grafana,prometheus,relayer,validator-gorchain,validator-solana}.staging.gorbagana.wtf`; warp-ui at `staging.gorbagana.wtf` itself (like prod's `bridge.gorbagana.wtf`) |
| `AWS_ENDPOINT_URL_S3` | `https://s3.staging.gorbagana.wtf` |
| Buckets, secrets, configmaps | identical to prod (incl. the `HYP_CHAINS_SOLANA_CUSTOMRPCURLS: { env: SOLANA_RPC_URL }` secret key) |
| `PRIVY_WALLET_ID` | the `REPLACE_WITH_WALLET_ID` sentinel, rendered per validator from `validators.yaml` by the deploy loop (existing mechanism) |
| Deployment-derived keys (IGP IDs/accounts, mailboxes, warp addrs) | same pre-deploy sentinels as prod; `publish-bridge-state.yml` patches them after the deployer Job, unchanged |

`SOLANA_RPC_URL` stays a secret (Helius devnet URL embeds the API key),
built in staging `group_vars` from `helius_api_key` — already wired.

`deployment/staging/bridges/default/warp-routes/usdc.yml`: the origin
token becomes the Circle devnet USDC literal
`4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU` (replacing
`REPLACE_WITH_STAGING_USDC_MINT_ADDRESS`). No SPL deploy on staging — the
collateral mint already exists on devnet.

## 3. `deployment/staging/bridges/default/operator/validators.yaml`

```yaml
# pure topology — wallet ids come from deployment-config's
# privy_validator_wallet_ids (consolidation, 2026-06-11)
validators:
  - label: gorchain-primary
    chain: gorchain
    host: staging-hyperlane-validators
    hostname: validator-gorchain.staging.gorbagana.wtf
  - label: solana-primary
    chain: solana
    host: staging-hyperlane-validators
    hostname: validator-solana.staging.gorbagana.wtf
```

Labels must stay `gorchain-primary`/`solana-primary` — the committed
validator specs hardcode the derived MinIO IAM env names
(`GORCHAIN_PRIMARY_KEY_ID`, `SOLANA_PRIMARY_KEY_ID`, …). The operator
fills the Privy IDs per `runbooks/privy-wallets.md`; whether staging uses
a separate Privy app from prod is an operator choice the file doesn't
encode. This one file feeds the validator deploy loop, per-validator MinIO
IAM generation, and DNS auto-append — its absence today is three failure
points.

## 4. Gorchain chain play — `ops/playbooks/prepare-gorchain.yml`

Isolated staging sibling of `prepare-chains.yml` (which stays untouched,
local-only). Run explicitly after `bootstrap-host.yml`, never imported by
the composites:

```bash
ansible-playbook -i inventories/staging/hosts.yml playbooks/prepare-gorchain.yml
```

Targets `chain_hosts` (= `staging-gorchain`). Three parts:

1. **Chain standup** — reuses `ops/scripts/setup-chains.sh` with its
   existing `SKIP_SOLANA` knob: gorchain comes up via `gorchain-stacks`
   through laconic-so exactly as local does, state under
   `~/chains/gorchain`. State is persistent across rehearsals — only an
   explicit destroy resets it (the staging lifecycle requirement).
2. **TLS/DNS exposure** — a host-level Caddy container reverse-proxying
   `rpc.staging.gorbagana.wtf` → `localhost:8899` with Let's Encrypt
   (cert volume persisted across restarts). This makes the committed
   `GORCHAIN_RPC_URL` literal work cross-host and exercises the
   relayer↔gorchain-over-DNS seam. The gorchain faucet port is **not**
   proxied — faucet stays on-host only.
3. **Keys + funding** — reuses `gen-local-keys.sh`: generates the
   deployer/relayer/validator hot keyfiles (the bridge owner is NOT a
   keyfile — it is the Privy bridge-owner wallet, decision 2026-06-11)
   and funds the gorchain side from the chain's own faucet. The solana
   side funds via `solana airdrop` on devnet; the runbook documents the
   rate limits and manual top-up fallback. No SPL token deploy step.

## 5. Correct-by-construction checks

### Shape-parity checker — `ops/scripts/check-spec-parity.py`

Compares `deployment/spec-*.yml` against `deployment/staging/spec-*.yml`:

- same set of spec files on both sides;
- per file: identical key paths under `config:`, identical `secrets:`
  structure (secret names and their key maps), identical `configmaps:`,
  `volumes:`, and http-proxy route shape (path/proxy-to pairs; hostname
  *values* may differ).

Values are exempt — only shape is compared. Exits non-zero listing every
divergence. Runs in the `ops-lint` CI workflow and locally. This is the
permanent drift tripwire for promotions: "staging grew an env var prod
doesn't have" fails CI, not a prod deploy.

### Placeholder gate — `stack_deploy` preflight

After token rendering, immediately before the spec is handed to
`laconic-so`, fail if it still matches `REPLACE_WITH` or `__[A-Z_]+__`,
listing the offenders. Catches an unfilled `validators.yaml`, a forgotten
zone, an unrendered token. Audit item for the plan: any sentinel that
survives to deploy time *today* (e.g. `REPLACE_WITH_GITHUB_USERNAME` in
the specs' `image-pull-secret:` blocks) is a latent bug this gate will
surface — fix those, don't allowlist them.

### Tests and CI

- `ops/tests/test_env_contract.yml` — the §1 contract, parameterized over
  the inventory it's invoked with.
- `ops/tests/test_staging_env.yml` — sibling of `test_local_env.yml`:
  asserts `topology == 'multi'`, `manage_dns`, `deployment_subdir ==
  'deployment/staging'`, `chain_hosts == ['staging-gorchain']`, the `rpc`
  DNS record exists, staging `validators.yaml` parses with the two
  expected labels/hosts.
- `ops-lint` CI runs the Layer-0 suite against **all three** inventories
  (today's examples/docs only exercise prod), plus the parity checker.
  Tests that need `deployment-config.yml` values keep working the way they
  do today (the suite is designed to run secrets-free); anything that can't is
  noted in the test header, not silently skipped.

## 6. Runbook and docs

- **`ops/runbooks/staging.md`** — from-zero operator guide: 3-VM
  prerequisites and `host_vars` IPs; Cloudflare token scoped to the
  staging zone; `deployment-config.yml` fill (devnet Helius key, Privy app
  credentials + IDs/addresses, GHCR PAT); Privy wallet setup (cross-ref
  `privy-wallets.md`); then `prepare-gorchain` → `setup-all` →
  `deploy-all` → `publish-bridge-state -e state_review=true` for the
  first publish; verification checklist (MinIO checkpoint objects, relayer
  logs, a warp-ui transfer on devnet); reset/destroy procedure.
- **`ops/README.md`** — staging section updated: chain play, env
  contract, all-inventories CI.
- **`2026-05-29-staging-environment-design.md`** — header note pointing
  here for the implementation-facing parts.

## Out of scope / follow-ups

- hyp-564 maintenance playbooks (kill-switch, restore, ISM update,
  validator add/remove) — deferred; staging is their rehearsal ground once
  it's up.
- CODEOWNERS split for prod vs staging review gates — GitHub-side config,
  handled by the operator.
- Synthetic-token `metadataUri` for staging warp-ui display — stays empty
  as today.
- WebSocket exposure on the staging gorchain RPC — lands with the deferred
  fast-bridging work (hyp-d34); the Caddy front gains the ws route then.

## Operator-supplied values (collected by the runbook)

(Consolidated 2026-06-11: everything below except `host_vars` lives in the
single gitignored `inventories/staging/deployment-config.yml`, read at
runtime — committed files keep sentinels.)

- `host_vars` `public_ip` for the three VMs
- `deployment-config.yml`: `cloudflare_api_token`, `helius_api_key`
  (devnet project), `privy_app_id`/`privy_app_secret`/
  `privy_oracle_wallet_id`, `ghcr_pat`, the four identity values
  (`bridge_owner_pubkey`, `igp_oracle_pubkey`, the two validator
  addresses), `privy_validator_wallet_ids` (by label), and
  `wallet_connect_id`
