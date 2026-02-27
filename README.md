# Hyperlane SVM Bridge Stacks

Stack-orchestrator (`laconic-so`) stacks for deploying and operating a Hyperlane cross-chain token bridge between Gorchain and Solana. Includes contract deployers, validators, relayer, monitoring, and a browser-based bridge UI — all packaged for `k8s-kind` deployment.

## Stacks

Each stack has its own README with deployment instructions.

| Stack | Description |
|---|---|
| [hyperlane-minio](stack_orchestrator/data/stacks/hyperlane-minio/) | S3-compatible storage for validator checkpoints |
| [hyperlane-svm-deployer](stack_orchestrator/data/stacks/hyperlane-svm-deployer/) | Deploys Hyperlane core contracts on both chains |
| [hyperlane-svm-warp-deployer](stack_orchestrator/data/stacks/hyperlane-svm-warp-deployer/) | Deploys warp route contracts for a token pair |
| [hyperlane-validator](stack_orchestrator/data/stacks/hyperlane-validator/) | Validator + Privy KMS proxy (one deployment per chain) |
| [hyperlane-relayer](stack_orchestrator/data/stacks/hyperlane-relayer/) | Cross-chain message relayer with IGP fee claim sidecar |
| [hyperlane-gas-oracle](stack_orchestrator/data/stacks/hyperlane-gas-oracle/) | Periodically updates IGP gas oracle configs |
| [hyperlane-monitoring](stack_orchestrator/data/stacks/hyperlane-monitoring/) | Prometheus, Grafana, pushgateway, balance monitor |
| [hyperlane-warp-ui](stack_orchestrator/data/stacks/hyperlane-warp-ui/) | Browser-based bridge UI |

### Deployment order

1. `hyperlane-minio` — checkpoint storage
2. `hyperlane-svm-deployer` — core contracts (writes ConfigMaps consumed by all downstream stacks)
3. `hyperlane-svm-warp-deployer` — warp route contracts
4. `hyperlane-validator` (gorchain) + `hyperlane-validator` (solana) — one deployment per chain
5. `hyperlane-relayer` — message delivery
6. `hyperlane-gas-oracle`, `hyperlane-monitoring`, `hyperlane-warp-ui` — no ordering constraint

## Repository Structure

- `stack_orchestrator/` — Stack definitions, compose files, config, and container builds
- `deployment/` — Reference spec files and ops playbooks
- `specs/` — Stack specifications and ops design docs
- `hyperlane-kms-proxy/` — Privy KMS proxy sidecar (Go)
- `hyperlane-gas-oracle/` — Gas oracle service (Node.js)
- `docs/` — Architecture decisions

## Documentation

- [Stack Specifications](specs/stack-specifications.md) — Detailed per-stack specs
- [Ansible Ops Spec](specs/ansible-spec.md) — Ops job playbook design
- [Architecture Decisions](docs/architecture-decisions.md) — Design rationale and build strategy
