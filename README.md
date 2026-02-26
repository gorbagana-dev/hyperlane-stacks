# Hyperlane SVM Bridge Stacks

Stack-orchestrator (`laconic-so`) stacks for deploying and operating a Hyperlane cross-chain token bridge between Gorchain and Solana. Includes contract deployers, validators, relayer, monitoring, and a browser-based bridge UI — all packaged for `k8s-kind` deployment.

## Repository Structure

- `stack_orchestrator/` — Stack definitions, compose files, config, and container builds
- `deployment/` — Example spec files and ops job manifests
- `hyperlane-kms-proxy/` — Privy KMS proxy sidecar (Go)
- `hyperlane-gas-oracle/` — Gas oracle service (Node.js)
- `docs/` — Architecture decisions and stack specifications

## Documentation

- [Stack Specifications](docs/stack-specifications.md) — Detailed per-stack specs
- [Architecture Decisions](docs/architecture-decisions.md) — Design rationale and build strategy
