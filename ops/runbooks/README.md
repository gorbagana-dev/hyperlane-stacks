# ops/runbooks/ — per-environment operator guides

From-scratch, copy-pasteable guides for bringing a Hyperlane SVM bridge up in each
environment. One file per environment; they share the same two-phase flow
(`setup-all.yml` → `deploy-all.yml`) and differ only in inputs (inventory, chains,
DNS/TLS, secrets).

| Runbook | Environment | Chains | DNS / TLS |
|---|---|---|---|
| [local-single-host.md](local-single-host.md) | Own-chains, one VM | self-run gorchain + solana-test-validator, on the VM | mkcert (no DNS provider) |
| [staging.md](staging.md) | Devnet rehearsal, three VMs | persistent self-run gorchain + Helius devnet | Cloudflare + Let's Encrypt |
| [prod.md](prod.md) | Production (mainnet) | mainnet gorchain (external) + Helius mainnet | Cloudflare + Let's Encrypt |

**Shared reference** (mechanics behind every runbook): `ops/README.md` — the
environment/inventory model, the secret-vs-config model, `fetch-stack`, and how a
stack gets deployed. Each runbook links into it rather than repeating it.

**Shared:** Privy server-wallet setup is the same for every runbook — see
[privy-wallets.md](privy-wallets.md). Each runbook links there and then says which vars
to fill.

**Shared:** Adding a warp route to a running bridge is the same flow in every
environment — see [warp-routes.md](warp-routes.md) (edit `WARP_ROUTES`, then
`update-warp-routes.yml`). Each runbook links there from its "Adding a warp route" section.
