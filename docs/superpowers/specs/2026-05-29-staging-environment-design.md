# Staging Environment Design

> **2026-06-10:** implementation-facing parts superseded by
> `2026-06-10-staging-ops-design.md` (the ops layer landed with a different
> layout: `ops/inventories/`, `publish-bridge-state.yml`). This doc remains
> the source for staging's purpose, lifecycle, and rehearsal surface.

## Goal

A long-lived **staging deployment** of the Hyperlane SVM bridge that mirrors
production's shape closely enough to serve as the rehearsal ground for every
production ops procedure (bootstrap, kill-switch, restore, ISM update,
validator add/remove, MinIO resync, warp redeploy, hardware-wallet-attended
signing). Every procedure that touches prod is exercised against staging
first.

This spec captures **what staging is** and **how it differs from prod**. It
does not produce a standalone implementation plan — most of staging's
deliverables are spec/config files and procedures that the ops playbook PR
(separate, upcoming) executes. Standalone work is listed in §13.

## Non-goals

- **Synthetic load on staging.** Driving fake bridge traffic for throughput
  testing is useful eventually but does not shape the v1 staging design.
- **Chaos drills beyond what ops playbooks already cover.** Kill-switch +
  restore are in scope as playbook rehearsals. Deliberate fault injection
  (network partitions, RPC blackholes, byzantine validator behavior) is a
  follow-up.
- **Production-grade gorchain consensus on staging.** Staging runs a
  single-node gorchain validator. Multi-validator gorchain consensus testing
  is gorchain's concern, not the bridge's.
- **Multi-bridge sharding.** "Bridge" here means staging-vs-prod, not
  multiple chain pairs. The current setup is single-chain-pair (gorchain ↔
  solana); the multi-route concern is tracked separately in
  `docs/production-readiness-gaps.md` §5.4.

## Layout

> Reconciled 2026-05-29 with the ops-layer redesign
> (`2026-05-29-ops-layer-redesign-and-ledger-signing-design.md`): ansible moved
> to a top-level `ops/` with per-env isolation; bridge state + operator inputs
> sit under `bridges/<bridge>/`.

```
deployment/
  spec-*.yml             # prod specs (flat at env root, unchanged)
  bridges/<bridge>/operator/validators.yaml   # prod operator inputs
  bridges/<bridge>/generated/                 # prod bridge state, committed
  staging/
    spec-*.yml           # staging — same shape as prod, different values
    bridges/<bridge>/operator/validators.yaml
    bridges/<bridge>/generated/

ops/                     # top-level, sibling of deployment/
  playbooks/  roles/
  envs/{prod,staging}/{inventory.yml,host_vars/,group_vars/}
```

Prod specs are not relocated (flat at the env root). Staging mirrors the same
shape under `deployment/staging/`. v1 bridge name is `default`. Ops playbooks
take the target environment (`deployment/` or `deployment/staging/`) and the
`ops/envs/<env>/` directory as inputs; they are otherwise identical between
environments.

## Topology

Staging is **prod-shaped multi-VM** (the "(b)" topology from brainstorming),
subject to later collapse toward a hybrid "(c)" shape after operating it.
The grouping is parameterized so collapsing is a spec edit, not a redesign.

Initial staging VM grouping:

| Host | Stacks |
|---|---|
| `staging-gorchain` | single-node gorchain chain validator, hyperlane-validator (gorchain), gorbagana RPC |
| `staging-solana-validator` | hyperlane-validator (solana) — points at Helius devnet |
| `staging-bridge-ops` | relayer, gas-oracle, monitoring, MinIO, warp-ui |

Three VMs minimum. Cross-machine seams that get exercised: relayer ↔
gorchain RPC over Cloudflare-managed DNS, validator → MinIO checkpoint
writes over the same path, and the hardware-wallet signing seam (see §7).

## Chain endpoints

### Gorchain
Single-node gorchain validator co-located with the gorchain hyperlane
validator on the `staging-gorchain` host. Persistent state in
`/srv/kind/hyperlane/gorchain/` (host-path), exposed to in-cluster pods
via Service. No public RPC.

### Solana
**Solana devnet**, accessed via **Helius**. Separate Helius API key from
prod (different project, mirrored quota and billing surface, devnet
endpoint). The staging spec consumes this key the same way prod's spec
consumes the prod key — env-injected secret, no code change.

## Tokens

Warp route collateral is **Circle's devnet USDC mint**
(`4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU`). Same Circle-issued
metadata, faucetable, real shape. Staging's warp-deployer spec pins
`WARP_TOKEN_MINT` to this address; everything else in `spec-warp-deployer.yml`
keeps its prod shape.

The synthetic USDC on the gorchain side is minted by the warp deployer the
same way prod mints it. Synthetic metadata URI points at a
staging-versioned metadata JSON so warp-UI shows the right token info per
environment.

Faucet rate limits are an operational nuisance, not a blocker. If Circle
ever deprecates or rotates the devnet mint, falling back to a self-minted
SPL is straightforward (covered as a contingency in §15, not v1 design).

## Validator set

Both prod and staging run a **1-of-1** validator set in steady state. The
prod spec's threshold and validator count are matched exactly in staging.

The add/remove and ISM-update playbooks are rehearsed by **transiently
bringing up a second validator on staging**, running the threshold update
to 2-of-2, exercising relayer behavior at the new threshold, then
running the remove flow to return staging to 1-of-1. The "scratch validator
slot" is a fourth VM (or a fourth pod, depending on the final
host-collapse decision) available on demand during these rehearsals; it
does not run permanently.

This shape lets us rehearse the validator-set procedures fully despite
prod's small steady-state size.

## Hardware-wallet flow

Two signer paths exist in playbook code:

- **`signer: ledger`** — the real hardware-wallet path. Used by prod for
  every privileged operation (warp redeploy, ISM update, upgrade-authority
  transfer, multisig-owner signing). Validated end-to-end through the
  `@ledgerhq` / `solana-ledger` toolchain on the operator's machine.
- **`signer: hot-key-file`** — a software fallback that signs from a
  keypair file. Same playbook codepath up to the signing call. Used by
  staging for fast iteration so that day-to-day staging rehearsals are not
  gated on a human walking to a physical device.

The prod spec **must reject `signer: hot-key-file`** at validation time.
The simplest way is an assertion in the prod environment's ansible variables
file (`ops/inventories/prod/group_vars/all.yml`) that pins `signer: ledger` and
fails the playbook if any operation requests otherwise. (This `signer:` pin lands
with the sub-project-3 ops playbooks; the deploy-side layer has no signing.)

**Real-Ledger rehearsal cadence:** Before every prod promotion, the most
recent staging release-candidate run is repeated end-to-end with
`signer: ledger` on staging, using a secondary Ledger device kept for that
purpose. This catches Ledger-specific issues (firmware changes, derivation
path drift, prompt-text mismatches in operator runbooks) without making
every staging run wait on the device.

## Lifecycle

Staging is **long-lived** by default. State (MinIO checkpoint history,
validator merkle trees, gas oracle price history, accumulated warp
balances) persists across rehearsals so that state-dependent procedures
(restore-from-MinIO, checkpoint-replay) can actually be tested.

A **scheduled "destroy and bootstrap from zero" rehearsal** is run before
every prod release promotion. The rebuild exercises the full ansible
bootstrap path on staging hardware — `prerequisites_privileged`,
`prerequisites_user`, `stack_deploy`, `state_distribute`, DNS
reconciliation — from an empty host. Successful rebuild is the gate for
promoting the same release to prod.

Between rebuilds, staging accumulates real state. The rebuild rehearsal
is the only thing that resets it.

## State distribution and GitOps

Staging and prod share the same repository. Each environment keeps its bridge
state under `bridges/<bridge>/generated/`:

- `deployment/bridges/<bridge>/generated/` — prod bridge state
  (program-ids.json, token-config.json, agent-config snippets per chain)
- `deployment/staging/bridges/<bridge>/generated/` — staging bridge state, same shape

State files are committed by the `state_distribute` role after deployer
job runs. Day-to-day, these files are stable — commits only happen on
bootstrap, ISM update, validator add/remove, and warp redeploy. The
"noise" concern from sharing a repo with prod is minimal in practice.

**CODEOWNERS strategy:**

```
# prod specs + state — stricter review
/deployment/spec-*.yml          @ops-lead @security-reviewer
/deployment/bridges/            @ops-lead @security-reviewer

# deployment/staging/ — staging, lighter review
/deployment/staging/            @ops-lead
```

Blast-radius separation comes from the review gate, not from repo or
branch boundaries. A misconfigured staging playbook can produce a noisy
PR but cannot land prod changes without the prod reviewers.

## DNS and TLS

The staging DNS zone is **not yet decided** — it is an operator-supplied
value (`dns_zone` in the staging `group_vars`, currently a placeholder), not
a fixed subdomain of the prod zone. Hostnames under it follow the same
pattern as prod's. The `dns_cloudflare` role takes the zone and its API
token as variables and behaves identically across environments, so the zone
choice is settled at staging standup without code changes.

Same cert-manager / ACME setup as prod (the MinIO PR2 will land the TLS
strategy; staging follows it). Real Let's Encrypt certs, not a fake CA —
that is part of what staging is rehearsing.

## Image promotion

Same container image tag binary flows staging → prod. Promotion is
mechanically a `image-overrides:` edit:

1. New release candidate `v1.3.0-rc1` is published to the registry.
2. `deployment/staging/spec-*.yml` `image-overrides:` is bumped to
   `v1.3.0-rc1`. Staging is restarted via `laconic-so deployment restart
   --image`. Rehearsals run.
3. If rehearsals pass, the same tag (or its promoted alias `v1.3.0`) is
   pinned in `deployment/spec-*.yml` via `image-overrides:`, PR'd, merged,
   prod is restarted.

No staging-specific images exist. The image binary does not know what
environment it is in.

## Procedures rehearsed on staging

Every prod ops playbook has a "ran successfully on staging" gate before
it touches prod. This list defines the v1 rehearsal surface:

- `bootstrap-host.yml` — privileged + user setup on a fresh host
- `deploy-all.yml` — full bridge bootstrap from empty hosts
- `deploy-validator.yml` — add a new validator host
- `generate-validator-spec.yml` — produce a validator spec for a new key
- `configure-dns.yml` — additive Cloudflare DNS reconciliation
- `commit-bridge-state.yml` — commit `generated/` files after deployer run
- `kill-switch.yml` — pause the bridge
- `restore.yml` — restore from MinIO checkpoint history
- `ism-update.yml` — update the multisig ISM threshold or set
- `add-validator.yml` — transient `n=1 → n=2` flow on staging
- `remove-validator.yml` — transient `n=2 → n=1` flow on staging
- `submit-signed-tx.yml` — hardware-wallet-attended transaction submission
- `minio-resync.yml` — MinIO user/bucket resync
- `remove-dns.yml` — Cloudflare DNS deprovisioning
- `teardown.yml` — full bridge teardown

Each playbook accepts the target environment (`deployment/` for prod,
`deployment/staging/` for staging) as a variable.

## Standalone implementation work

The bulk of staging's execution lives in the ops playbook PR. Standalone
items that can be done before, in parallel with, or after that PR:

1. **Create `deployment/staging/spec-*.yml`** — copies of the prod specs
   with staging values (devnet endpoints, devnet USDC mint, staging
   Cloudflare hostnames, staging image tags). Done once, edited per
   release.
2. **`CODEOWNERS` entry** — gate `deployment/spec-*.yml` and
   `deployment/bridges/` more strictly than `deployment/staging/`.
3. **Software-signer fallback in the signing role** (part of the ops PR,
   not standalone). Listed here because this spec is what justifies it.
4. **Prod spec validation that rejects `signer: hot-key-file`** (also part
   of the ops PR).
5. **Optional: `make diff-bridges` script** that compares the *shape* of
   `deployment/spec-*.yml` vs `deployment/staging/spec-*.yml` (same keys
   present, same configmap names, same stack-list) without comparing
   values. Catches "staging has a new env var that prod doesn't" before
   promotion. P2, deferrable.

## Open questions and risks

- **Circle devnet USDC durability.** If Circle deprecates or rotates the
  devnet USDC mint, staging warp deployer needs to point at a new mint.
  Fallback: self-mint an SPL with `name/symbol/decimals` matching prod
  USDC. Tracked as a contingency, not v1 work.
- **Helius devnet feature parity.** This spec assumes Helius devnet
  supports the same RPC + websocket surface (including `programSubscribe`)
  as Helius mainnet-beta. Verify before staging deployer runs hit it.
  If a feature is missing on devnet, document and decide per case.
- **Ledger device availability.** The real-Ledger rehearsal cadence
  depends on a physically-accessible secondary Ledger. If the team is
  distributed across timezones, this rehearsal becomes a scheduling
  problem. Operational concern, not a design blocker.
- **State drift between staging and prod.** Even with same playbooks,
  spec values inevitably diverge over time. The `make diff-bridges`
  shape-comparator (§13.5) is the long-term mitigation but is not v1.
- **Host-spread rehearsal cadence.** The brainstorm noted that collapsing
  validators onto fewer staging hosts (topology "(c)") may miss bugs that
  only appear on truly separated hosts. Mitigate with an occasional
  "spread out" staging rebuild on the same calendar slot as the Ledger
  rehearsal.

## Relationship to other docs

- `docs/architecture-decisions.md` — production topology, DNS, multi-machine
  principle. This spec is consistent with all of it.
- `docs/ops-decisions.md` — atomic ops + composite playbook structure. This
  spec is what those playbooks rehearse against.
- `docs/production-readiness-gaps.md` — §5.4 (multiple warp routes) is the
  related gap not addressed by staging.
- The ops playbook PR (upcoming) consumes this spec as a requirements
  input.
