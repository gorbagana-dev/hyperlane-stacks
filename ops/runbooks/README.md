# ops/runbooks/ — per-environment operator guides

From-scratch, copy-pasteable guides for bringing a Hyperlane SVM bridge up in each
environment. One file per environment; they share the same two-phase flow
(`setup-all.yml` → `deploy-all.yml`) and differ only in inputs (inventory, chains,
DNS/TLS, secrets).

| Runbook | Environment | Chains | DNS / TLS | Status |
|---|---|---|---|---|
| [local.md](local.md) | Own-chains (testing Layers 1-2) | self-run gorchain + local solana-test-validator | none (plain HTTP) | available |
| staging.md | Devnet rehearsal (Layer 3) | self-run gorchain + Helius devnet | Cloudflare + Let's Encrypt | _to be added_ |
| prod.md | Production | mainnet gorchain + Helius mainnet | Cloudflare + Let's Encrypt | _to be added_ |

**Shared reference** (mechanics behind every runbook): `ops/README.md` — the
environment/inventory model, the secret-vs-config model, `fetch-stack`, and how a
stack gets deployed. Each runbook links into it rather than repeating it.

**Adding a new environment runbook:** copy the structure of `local.md`
(Prerequisites → Chains → Inventory → Secrets → Keyfiles → Run → Access → Reset →
Limitations) and call out only what that environment changes. Add a row above.
