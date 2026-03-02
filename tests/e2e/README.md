# E2E Tests

End-to-end tests for the Hyperlane SVM bridge stacks. Tests deploy contracts via `laconic-so` on a local kind cluster with real Solana and Gorchain nodes, then verify the deployment artifacts.

## What's tested

- **Core deployer** -- deploys Hyperlane core contracts (Mailbox, IGP, ISM, etc.) on both chains, verifies ConfigMap outputs (`hyperlane-program-ids`, `hyperlane-agent-config`, `hyperlane-gas-oracle-config`, `hyperlane-multisig-config`)
- **Warp deployer** -- deploys warp route contracts for a test SPL token, verifies ConfigMap outputs (`hyperlane-token-config`, `hyperlane-warp-deploy-outputs`)

## Prerequisites

- Python 3.10+
- [pytest](https://docs.pytest.org/) (`pip install pytest`)
- [kind](https://kind.sigs.k8s.io/)
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- [laconic-so](https://git.vdb.to/cerc-io/stack-orchestrator) (`pip install laconic-stack-orchestrator`)
- [Solana CLI](https://docs.anza.xyz/cli/install) (`solana`, `solana-keygen`, `spl-token`)
- [Foundry](https://book.getfoundry.sh/getting-started/installation) (`cast`)
- Docker

## Running locally

```bash
# Full run (creates cluster, starts chains, builds images, runs tests, tears down)
cd tests/e2e && pytest -v

# Skip cluster creation (reuse an existing kind cluster)
pytest -v --skip-cluster-setup

# Skip chain node startup (Solana/Gorchain already running externally)
pytest -v --skip-chain-setup

# Skip image builds (use cached Docker images)
pytest -v --skip-build

# Keep everything running after tests (for debugging)
pytest -v --skip-cleanup

# Iterative development: reuse cluster + chains + images
pytest -v --skip-cluster-setup --skip-chain-setup --skip-build --skip-cleanup

# Run only core deployer tests
pytest -v test_deployer.py

# Run only warp deployer tests
pytest -v test_warp_deployer.py

# Exclude slow tests
pytest -v -m "not slow"
```

## Structure

```
tests/e2e/
├── conftest.py                     # Session-scoped fixtures (setup/teardown)
├── pytest.ini                      # pytest configuration
├── test_deployer.py                # Core deployer verification tests
├── test_warp_deployer.py           # Warp deployer verification tests
├── lib/
│   ├── common.py                   # Logging, assertions, wait helpers
│   ├── cluster.py                  # Kind cluster lifecycle, cert-manager, RBAC
│   ├── chain.py                    # Solana/Gorchain node lifecycle
│   ├── deploy.py                   # laconic-so deployment helpers
│   └── keygen.py                   # Keypair generation, funding, k8s secrets
└── fixtures/
    ├── kind-config.yaml            # Kind cluster with ingress ports
    ├── cert-manager-issuer.yaml    # Self-signed TLS issuer
    ├── host-chain-services.yaml    # k8s Services pointing to host chain nodes
    ├── test-spec-deployer.yml      # laconic-so spec for core deployer
    └── test-spec-warp-deployer.yml # laconic-so spec for warp deployer
```

## How it works

1. **Session fixtures** create a kind cluster and install cert-manager for TLS (`kind_cluster` fixture)
2. **Chain nodes** start a Solana test validator on the host (`chain_nodes` fixture)
3. **Deployer deployment** exposes host chain nodes to the cluster via k8s Service+Endpoints, generates test keypairs, funds wallets, and starts the deployer stack (`deployer_deployment` fixture)
4. **Test functions** wait for deployer pods to complete and verify ConfigMap outputs
5. **Teardown** is handled automatically by fixture finalizers -- stopping stacks, chain nodes, and destroying the kind cluster (unless `--skip-cleanup` is passed)
