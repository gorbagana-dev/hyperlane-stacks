# ops/runbooks/ — per-environment operator guides

From-scratch, copy-pasteable guides for bringing a Hyperlane SVM bridge up in each
environment. One file per environment; they share the same two-phase flow
(`setup-all.yml` → `deploy-all.yml`) and differ only in inputs (inventory, chains,
DNS/TLS, secrets).

| Runbook | Environment | Chains | DNS / TLS | Status |
|---|---|---|---|---|
| [local-single-host.md](local-single-host.md) | Own-chains, one VM (Layer 1) | self-run gorchain + solana-test-validator, on the VM | mkcert (no DNS provider) | available |
| [local-multi-host.md](local-multi-host.md) | Own-chains, cross-host (Layer 2) | self-run gorchain + solana-test-validator, separate box | Cloudflare + Let's Encrypt | available |
| [staging.md](staging.md) | Devnet rehearsal (Layer 3), three VMs | persistent self-run gorchain + Helius devnet | Cloudflare + Let's Encrypt | available |
| prod.md | Production | mainnet gorchain + Helius mainnet | Cloudflare + Let's Encrypt | _to be added_ |

**Shared reference** (mechanics behind every runbook): `ops/README.md` — the
environment/inventory model, the secret-vs-config model, `fetch-stack`, and how a
stack gets deployed. Each runbook links into it rather than repeating it.

**Shared:** Privy server-wallet setup is the same for every runbook — see
[privy-wallets.md](privy-wallets.md). Each runbook links there and then says which vars
to fill.

**Adding a new environment runbook:** copy the structure of `local-single-host.md`
(Networking model → Prerequisites → Privy wallets → Inventory → Deployment config →
Chains & keys → Deploy → Access → Reset → Limitations) and call out only what that
environment changes. Add a row above.
