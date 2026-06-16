# Deploying additional warp routes

A running bridge can gain (or drop) warp routes without a full redeploy. Routes are
config-driven: a checked-in **menu** of route files, a `WARP_ROUTES` selector in the
warp-deployer spec, and one playbook — `update-warp-routes.yml` — that applies the
current selection end to end. This guide is environment-agnostic; the worked example
at the end uses staging.

The flow is the same for prod, staging, and local — only the menu directory, the
inventory, and the deploy branch differ:

| Environment | Menu directory | Deploy branch |
|---|---|---|
| prod | `deployment/bridges/default/warp-routes/` | `main` (group_vars default) |
| staging | `deployment/staging/bridges/default/warp-routes/` | the throwaway deploy branch |
| local | `deployment/local/bridges/default/warp-routes/` | n/a (controller == host) |

## The model

- **Menu** — one YAML file per route under the env's menu directory. The file's
  stem (e.g. `usdc` → `usdc.yml`) is the route's selector name. Files are a menu:
  present in the directory ≠ deployed.
- **Selection** — `WARP_ROUTES` in `spec-warp-deployer.yml` (comma- or
  space-separated stems) is the single source of truth for what's deployed. Ops
  derives everything from it — there is no separate route list.
- **Idempotent** — already-deployed routes self-skip (the deployer checks
  `token-config.json` in `/state/warp-routes/<name>/`). Re-running with a longer
  `WARP_ROUTES` only deploys the new entries.

## Add a route

### 1. Add the route file to the menu

Create `<menu-dir>/<stem>.yml` (or reuse one already in the menu). Required fields,
validated by `load_warp_routes.yml` before the Job runs:

```yaml
name: <UNIQUE-ROUTE-NAME>          # unique across the selection
origin:
  chain: solana                    # solana | gorchain (must differ from remote)
  type: collateral                 # native | collateral | synthetic
  token: "<MINT>"                  # required only when type: collateral
  name: "USD Coin"
  symbol: "USDC"
  decimals: 6                      # YAML integer, not a quoted string
remote:
  chain: gorchain
  type: synthetic
  name: "USD Coin"
  symbol: "USDC"
  decimals: 6
metadataUri: "<URL or empty>"      # Token-2022 metadata for a synthetic side;
                                   # validated only when set (name/symbol must
                                   # match the synthetic side, image must serve)
logoURI: "<URL>"                   # optional, UI-only (read from warpRoutes.yaml)
```

Notes:
- **collateral with a runtime-deployed mint** (own-chains/local): set
  `token: "__WARP_TOKEN_MINT__"` — ops substitutes the mint that the chain box's SPL
  deploy printed (single-host: persisted by `prepare-chains.yml`; multi-host: the
  `local_warp_token_mint` in `deployment-config.yml`). Prod/staging use a committed
  real mint and carry no placeholder.
- No `REPLACE_WITH_*` placeholder may survive — the deploy gate rejects an unfilled
  `metadataUri`. Host the metadata JSON and commit its URL (see the USDC route for
  the pattern), or leave `metadataUri: ""` for a native↔synthetic route that needs
  none.

### 2. Select it

Add the stem to `WARP_ROUTES` in the env's `spec-warp-deployer.yml`:

```yaml
config:
  WARP_ROUTES: "usdc sol"          # was "usdc"
```

### 3. Commit + push to the deploy branch

The hosts fetch the repo on `deploy_branch`, and `publish-bridge-state` pushes the
regenerated state back to it. Both the route file and the spec change must be on that
branch before you run the playbook:

```bash
git add deployment/.../warp-routes/<stem>.yml deployment/.../spec-warp-deployer.yml
git commit -m "warp: add <stem> route"
git push           # prod: main; staging: your deploy branch
```

### 4. Make sure the deployer key is funded

The warp deployer signs the route deployment with the deployer key. `retire-keys.yml`
drains that key but **keeps the file**, so you only need to re-fund the same address —
no regeneration:

- **prod** — fund the deployer address (`solana-keygen pubkey
  ~/.credentials/hyperlane/deployer-keypair.json` on the deployer host) on both chains,
  enough to cover the route deploy.
- **staging** — re-run `ansible-playbook -i inventories/staging/hosts.yml playbooks/staging/prepare-gorchain.yml`:
  funding is balance-driven, so it tops the existing deployer back up (top up the devnet
  side by hand as in the staging runbook).

If you haven't retired keys yet (e.g. right after the initial deploy), the deployer is
already funded — skip this step.

### 5. Apply the selection

```bash
# prod (deploy_branch defaults to main):
ansible-playbook -i inventories/prod/hosts.yml playbooks/update-warp-routes.yml

# staging (deploy_branch required):
ansible-playbook -i inventories/staging/hosts.yml playbooks/update-warp-routes.yml \
  -e deploy_branch=<branch>
```

What it does:
1. **Re-runs the warp deployer** as a `restart` (so the deploy dir picks up the new
   `WARP_ROUTES` — a plain `start` would reuse the env captured at create time). New
   routes deploy; already-deployed ones self-skip.
2. **Publishes** the regenerated bridge state (per-route `token-config.json`,
   `warpRoutes.yaml`, and the `relayer-whitelist.json`).
3. **Restarts the relayer** — re-renders `HYP_WHITELIST` from the published state so
   it relays the new route's messages.
4. **Restarts the warp UI** — picks up the new route from `warpRoutes.yaml`.

The gas-oracle and validators are route-agnostic and are left running.

### 6. Verify

- The new token/route appears in the warp UI's selector.
- Run a small transfer across the new route in both directions (see the env runbook's
  "Try the bridge" section for wallet/RPC setup).
- The relayer delivers it (sending balance drops immediately; recipient credits in
  30–60s with a "Recipient has received funds" popup).

## Worked example — SOL ↔ gorchain on staging

The staging menu ships a native-SOL route (`deployment/staging/bridges/default/warp-routes/sol.yml`:
native SOL on Solana ↔ a synthetic on gorchain) that the default `WARP_ROUTES: "usdc"`
doesn't select. To add it to a live staging bridge:

```bash
BRANCH=<your-staging-deploy-branch>

# 1. select it (sol.yml is already in the menu)
#    edit deployment/staging/spec-warp-deployer.yml:  WARP_ROUTES: "usdc sol"

# 2. commit + push to the deploy branch
git add deployment/staging/spec-warp-deployer.yml
git commit -m "warp(staging): add sol route"
git push origin "$BRANCH"

# 3. if keys were retired, re-provision + fund the deployer
ansible-playbook -i inventories/staging/hosts.yml playbooks/staging/prepare-gorchain.yml

# 4. apply
ansible-playbook -i inventories/staging/hosts.yml playbooks/update-warp-routes.yml \
  -e deploy_branch="$BRANCH"
```

Then open `https://staging.gorbagana.wtf`, confirm **SOL** appears alongside USDC, and
transfer a little SOL solana→gorchain and back. Native SOL on the Solana side means no
collateral mint to manage; the gorchain side is a synthetic minted on delivery.
