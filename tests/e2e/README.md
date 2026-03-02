# E2E Tests

End-to-end tests for the Hyperlane SVM bridge stacks. Tests deploy contracts via `laconic-so` on a local kind cluster with real Solana and Gorchain nodes, then verify the deployment artifacts.

## What's tested

- **Core deployer** — deploys Hyperlane core contracts (Mailbox, IGP, ISM, etc.) on both chains, verifies ConfigMap outputs (`hyperlane-program-ids`, `hyperlane-agent-config`, `hyperlane-gas-oracle-config`, `hyperlane-multisig-config`)
- **Warp deployer** — deploys warp route contracts for a test SPL token, verifies ConfigMap outputs (`hyperlane-token-config`, `hyperlane-warp-deploy-outputs`)

## Prerequisites

- [kind](https://kind.sigs.k8s.io/)
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- [laconic-so](https://git.vdb.to/cerc-io/stack-orchestrator) (`pip install laconic-stack-orchestrator`)
- [Solana CLI](https://docs.anza.xyz/cli/install) (`solana`, `solana-keygen`, `spl-token`)
- [Foundry](https://book.getfoundry.sh/getting-started/installation) (`cast`)
- Docker

## Running locally

```bash
# Full run (creates cluster, starts chains, builds images, runs tests, tears down)
./tests/e2e/run.sh

# Skip cluster creation (reuse an existing kind cluster)
./tests/e2e/run.sh --skip-cluster-setup

# Skip chain node startup (Solana/Gorchain already running externally)
./tests/e2e/run.sh --skip-chain-setup

# Skip image builds (use cached Docker images)
./tests/e2e/run.sh --skip-build

# Keep everything running after tests (for debugging)
./tests/e2e/run.sh --skip-cleanup

# Iterative development: reuse cluster + chains + images
./tests/e2e/run.sh --skip-cluster-setup --skip-chain-setup --skip-build --skip-cleanup
```

## Structure

```
tests/e2e/
├── run.sh                          # Main orchestrator
├── lib/
│   ├── common.sh                   # Logging, assertions, wait helpers
│   ├── cluster.sh                  # Kind cluster lifecycle, cert-manager, RBAC
│   ├── chain.sh                    # Solana/Gorchain node lifecycle
│   ├── deploy.sh                   # laconic-so deployment helpers
│   └── keygen.sh                   # Keypair generation, funding, k8s secrets
├── tests/
│   ├── test-deployer.sh            # Core deployer verification
│   └── test-warp-deployer.sh       # Warp deployer verification
└── fixtures/
    ├── kind-config.yaml            # Kind cluster with ingress ports
    ├── cert-manager-issuer.yaml    # Self-signed TLS issuer
    ├── host-chain-services.yaml    # k8s Services pointing to host chain nodes
    ├── test-spec-deployer.yml      # laconic-so spec for core deployer
    └── test-spec-warp-deployer.yml # laconic-so spec for warp deployer
```

## How it works

1. Creates a kind cluster and installs cert-manager for TLS
2. Starts a Solana test validator on the host (port 18899)
3. Exposes host chain nodes to the cluster via k8s Service+Endpoints
4. Generates test keypairs (Ed25519 for Solana, secp256k1 for validators)
5. Deploys stacks via `laconic-so`, sharing a single kind cluster across deployments
6. Runs test scripts that wait for deployer pods to complete and verify ConfigMap outputs
