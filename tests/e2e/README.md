# E2E Tests

End-to-end tests for the Hyperlane SVM bridge stacks. Tests deploy contracts via `laconic-so` on a local kind cluster with real Solana and Gorchain nodes, then verify the deployment artifacts.

## What's tested

- **Core deployer** -- deploys Hyperlane core contracts (Mailbox, IGP, ISM, etc.) on both chains, verifies ConfigMap outputs (`hyperlane-program-ids`, `hyperlane-agent-config`, `hyperlane-gas-oracle-config`, `hyperlane-multisig-config`)
- **Warp deployer** -- deploys warp route contracts for a test SPL token, verifies ConfigMap outputs (`hyperlane-token-config`, `hyperlane-warp-deploy-outputs`)
- **MinIO** -- deploys the S3-compatible checkpoint storage, verifies pod health, bucket creation, and API accessibility
- **Validators** -- deploys gorchain and solana validators with a mock Privy server, verifies pod health, KMS proxy connectivity, metrics endpoint, log sanity, and checkpoint writing to MinIO

## Prerequisites

- Python 3.10+
- [kind](https://kind.sigs.k8s.io/)
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- [laconic-so](https://git.vdb.to/cerc-io/stack-orchestrator)
- [Solana CLI](https://docs.anza.xyz/cli/install) (`solana`, `solana-keygen`, `spl-token`)
- [Foundry](https://book.getfoundry.sh/getting-started/installation) (`cast`)
- Docker

## Setup

```bash
cd tests/e2e
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Running locally

```bash
# Full run (builds images, creates cluster, starts chains, runs tests, tears down)
cd tests/e2e && pytest -v

# Skip cluster creation (reuse an existing kind cluster)
pytest -v --skip-cluster-setup

# Skip chain node startup (Solana/Gorchain already running externally)
pytest -v --skip-chain-setup

# Build images from source instead of using published images
pytest -v --build-from-source

# Keep everything running after tests (for debugging)
pytest -v --skip-cleanup

# Iterative development: reuse cluster + chains + existing deployments
pytest -v --skip-cluster-setup --skip-chain-setup --skip-cleanup
pytest -v --skip-cluster-setup --skip-chain-setup --skip-core-deploy --skip-warp-deploy --skip-minio-deploy --skip-validator-deploy

# Run only core deployer tests
pytest -v test_deployer.py

# Run only warp deployer tests
pytest -v test_warp_deployer.py

# Run only validator tests
pytest -v test_validator.py

# Exclude slow tests (validator checkpoint tests)
pytest -v -m "not slow"
```

## Structure

```
tests/e2e/
├── conftest.py                          # Session-scoped fixtures (setup/teardown)
├── pytest.ini                           # pytest configuration
├── requirements.txt                     # Python dependencies (use with venv)
├── test_deployer.py                     # Core deployer verification tests
├── test_warp_deployer.py                # Warp deployer verification tests
├── test_minio.py                        # MinIO stack tests
├── test_validator.py                    # Validator stack tests (gorchain + solana)
├── lib/
│   ├── common.py                        # Logging, assertions, wait helpers
│   ├── cluster.py                       # Kind cluster lifecycle, cert-manager, RBAC
│   ├── chain.py                         # Solana/Gorchain node lifecycle
│   ├── deploy.py                        # laconic-so deployment helpers
│   ├── keygen.py                        # Keypair generation, funding, k8s secrets
│   └── privy_mock.py                    # Mock Privy server for validator signing
└── fixtures/
    ├── kind-config.yaml                 # Kind cluster with ingress ports
    ├── cert-manager-issuer.yaml         # Self-signed TLS issuer
    ├── host-chain-services.yaml         # k8s Services pointing to host chain nodes + mock Privy
    ├── test-spec-deployer.yml           # laconic-so spec for core deployer
    ├── test-spec-warp-deployer.yml      # laconic-so spec for warp deployer
    ├── test-spec-minio.yml              # laconic-so spec for MinIO
    ├── test-spec-validator-gorchain.yml # laconic-so spec for gorchain validator
    └── test-spec-validator-solana.yml   # laconic-so spec for solana validator
```

## How it works

1. **Setup** creates a venv and installs Python dependencies (`requirements.txt`)
2. **Image setup** uses published container images by default; pass `--build-from-source` to build locally (`deployer_image` fixture)
3. **Keypair generation** creates test Ed25519 + secp256k1 keypairs (`keypairs` fixture)
4. **Kind cluster** creates the cluster and installs cert-manager for TLS (`kind_cluster` fixture)
5. **Chain nodes** start a Solana test validator on the host (`chain_nodes` fixture)
6. **MinIO deployment** deploys the checkpoint storage stack (`minio_deployment` fixture)
7. **Mock Privy server** starts a local HTTP server that implements the Privy wallet signing API, used by the KMS proxy sidecar in validator pods (`privy_mock` fixture)
8. **Deployer deployment** exposes host chain nodes to the cluster via k8s Service+Endpoints, funds wallets, creates secrets, and starts the deployer stack (`deployer_deployment` fixture)
9. **Validator deployment** deploys gorchain and solana validators, copies agent-config from deployer ConfigMap, creates per-chain secrets (`validator_gorchain`, `validator_solana` fixtures)
10. **Test functions** verify deployment outputs: ConfigMaps, pod health, container readiness, metrics endpoints, log sanity, checkpoint files in MinIO
11. **Teardown** is handled automatically by fixture finalizers -- stopping stacks, chain nodes, and destroying the kind cluster (unless `--skip-cleanup` is passed)
