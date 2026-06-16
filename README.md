# Hyperlane SVM Bridge Stacks

Stack-orchestrator (`laconic-so`) stacks for deploying and operating a Hyperlane cross-chain token bridge between Gorchain and Solana. Includes contract deployers, validators, relayer, monitoring, and a browser-based bridge UI — all packaged for `k8s-kind` deployment.

## Deploying the bridge

The supported way to stand up a full bridge is the **ops/ ansible layer**, which
provisions the hosts and deploys every stack in order across one or more machines.
Start with the operator runbook for your environment — each is a from-zero,
copy-runnable guide:

| Environment | Runbook | What it is |
|---|---|---|
| **staging** | [ops/runbooks/staging.md](ops/runbooks/staging.md) | Prod rehearsal: Solana devnet + a persistent single-node gorchain, real DNS/TLS, on three VMs |
| **local (single-host)** | [ops/runbooks/local-single-host.md](ops/runbooks/local-single-host.md) | Whole bridge + both chains on one VM, self-trusted certs |
| **prod** | [ops/runbooks/prod.md](ops/runbooks/prod.md) | Mainnet (runbook in progress) |

[**ops/README.md**](ops/README.md) is the mechanics reference behind the runbooks
(configuration model, inventory/topology, how a stack gets deployed). The
per-stack tables below are for understanding the components; you don't deploy them
by hand.

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
- `deployment/` — Per-environment deployment specs and warp-route menus (prod at the root, `staging/`, `local/`)
- `ops/` — Ansible deploy layer (inventories, playbooks, roles) and operator [`runbooks/`](ops/runbooks/)
- `tests/` — Test suites (`e2e/` pytest, `unit/`)
- `docs/` — Architecture decisions, stack specifications, ops/e2e specs
- `hyperlane-kms-proxy/` — Privy KMS proxy sidecar (Go)
- `hyperlane-gas-oracle/` — Gas oracle service (Node.js)

## Documentation

- [Operator runbooks](ops/runbooks/) — from-zero deployment guides per environment
- [ops/ deploy layer](ops/README.md) — ansible mechanics reference
- [Stack Specifications](docs/stack-specifications.md) — detailed per-stack specs
- [Architecture Decisions](docs/architecture-decisions.md) — design rationale and build strategy
