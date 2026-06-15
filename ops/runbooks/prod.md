# Production — from-zero deployment

> **Placeholder — a proper prod runbook is still to be written.**
> Don't follow this page for a real production bring-up yet.

Production runs the bridge against **mainnet gorchain + Helius mainnet**, with
Cloudflare DNS + Let's Encrypt TLS, signing from **operator-provisioned** key
files (not the generated throwaway keys staging uses).

Until the dedicated guide lands, the closest references are:

- [staging.md](staging.md) — the same two-phase flow (`setup-all.yml` →
  `deploy-all.yml`) end-to-end; prod differs mainly in inventory
  (`inventories/prod/`), mainnet chain/domain IDs (already committed in the prod
  specs), operator-provisioned signer keys, and a mainnet Helius key.
- [ops/README.md](../README.md) — the mechanics reference (environment/inventory
  model, secret-vs-config model, how a stack gets deployed).
- [privy-wallets.md](privy-wallets.md) — Privy server-wallet setup (shared).

Key prod-only differences to capture when this is written: operator-provisioned
signer keyfiles, mainnet funding (no faucet), the prod Cloudflare zone, and the
mainnet Helius key.
