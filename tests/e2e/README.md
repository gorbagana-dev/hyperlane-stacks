# E2E Tests

End-to-end tests for the Hyperlane SVM bridge stacks. Tests deploy contracts via `laconic-so` on a local kind cluster with real Solana and Gorchain nodes, then verify the deployment artifacts.

## What's tested

- **Core deployer** -- deploys Hyperlane core contracts (Mailbox, IGP, ISM, etc.) on both chains, verifies ConfigMap outputs (`hyperlane-program-ids`, `hyperlane-agent-config`, `hyperlane-gas-oracle-config`, `hyperlane-multisig-config`)
- **Warp deployer** -- deploys warp route contracts for a test SPL token, verifies ConfigMap outputs (`hyperlane-token-config`, `hyperlane-warp-deploy-outputs`)
- **MinIO** -- deploys the S3-compatible checkpoint storage, verifies pod health, bucket creation, and API accessibility
- **Validators** -- deploys gorchain and solana validators with a mock Privy server, verifies pod health, KMS proxy connectivity, metrics endpoint, log sanity, and checkpoint writing to MinIO
- **Relayer** -- deploys the hyperlane relayer, verifies pod health and metrics endpoint
- **Bridge transfers** -- executes cross-chain warp route transfers (Solana→Gorchain and reverse) via CLI, verifies on-chain balance changes
- **Warp UI (Tier 1)** -- deploys the warp-ui stack, verifies pod health, HTML serving, sentinel replacement, and chain config presence via HTTP
- **Warp UI (Tier 2)** -- drives the warp-ui in a Playwright browser with a mock Solana wallet, executes real bridge transfers through the UI

## Prerequisites

- Python 3.10+
- [kind](https://kind.sigs.k8s.io/)
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- [laconic-so](https://git.vdb.to/cerc-io/stack-orchestrator)
- [Solana CLI](https://docs.anza.xyz/cli/install) (`solana`, `solana-keygen`, `spl-token`)
- [Foundry](https://book.getfoundry.sh/getting-started/installation) (`cast`)
- Docker
- [Playwright](https://playwright.dev/python/) browser binary (for warp-ui browser tests): `playwright install chromium`
- Xvfb (for warp-ui browser tests without a display): `apt install xvfb` — use `xvfb-run` to wrap pytest

## Setup

```bash
cd tests/e2e
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium  # download browser binary for warp-ui tests
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
pytest -v --skip-cluster-setup --skip-chain-setup --skip-core-deploy --skip-warp-deploy --skip-minio-deploy --skip-validator-deploy --skip-relayer-deploy --skip-warp-ui-deploy

# Run only core deployer tests
pytest -v test_deployer.py

# Run only warp deployer tests
pytest -v test_warp_deployer.py

# Run only validator tests
pytest -v test_validator.py

# Run only warp UI smoke tests
pytest -v test_warp_ui.py

# Run only warp UI browser tests (headless via xvfb-run)
xvfb-run pytest -v test_warp_ui_bridge.py

# Run with visible browser window (on a desktop with $DISPLAY)
pytest -v test_warp_ui_bridge.py

# Exclude slow tests (validator checkpoint tests, bridge transfers, UI tests)
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
├── test_bridge.py                       # Cross-chain bridge transfer tests
├── test_warp_ui.py                      # Warp UI HTTP smoke tests (Tier 1)
├── test_warp_ui_bridge.py               # Warp UI browser bridge tests (Tier 2, Playwright)
├── lib/
│   ├── common.py                        # Logging, assertions, wait helpers
│   ├── cluster.py                       # Kind cluster lifecycle, cert-manager, RBAC
│   ├── chain.py                         # Solana/Gorchain node lifecycle
│   ├── deploy.py                        # laconic-so deployment helpers
│   ├── keygen.py                        # Keypair generation, funding, k8s secrets
│   ├── privy_mock.py                    # Mock Privy server for validator signing
│   └── backpack.py                      # Backpack wallet extension helpers for Playwright
└── fixtures/
    ├── kind-config.yaml                 # Kind cluster with ingress ports
    ├── cert-manager-issuer.yaml         # Self-signed TLS issuer
    ├── host-chain-services.yaml         # k8s Services pointing to host chain nodes + mock Privy
    ├── test-spec-deployer.yml           # laconic-so spec for core deployer
    ├── test-spec-warp-deployer.yml      # laconic-so spec for warp deployer
    ├── test-spec-minio.yml              # laconic-so spec for MinIO
    ├── test-spec-validator-gorchain.yml # laconic-so spec for gorchain validator
    ├── test-spec-validator-solana.yml   # laconic-so spec for solana validator
    ├── test-spec-relayer.yml            # laconic-so spec for relayer
    └── test-spec-warp-ui.yml            # laconic-so spec for warp UI
```

## Why custom cluster setup (not SO's `--skip-cluster-management`)

The tests manage the Kind cluster lifecycle directly instead of letting
`laconic-so` handle it via its built-in cluster management. All `deploy start`
and `deploy stop` calls use `--skip-cluster-management`. Here's why:

**Multiple stacks share one cluster.** The tests deploy 8+ stacks into a
single Kind cluster with a shared namespace. Without `--skip-cluster-management`,
`deploy stop` on any single stack would call `destroy_cluster()` and tear down
the entire cluster — destroying all other running stacks.

**Ordering constraints.** The tests need the namespace, secrets, RBAC, and
host-chain-services to exist *before* `deploy start`. SO only creates the
namespace during `deploy start`, which is too late for our setup sequence.

**Ingress with TLS on Kind.** SO suppresses TLS on Kind clusters
(`use_tls = not self.is_kind()`), so the Ingress it creates has no TLS config.
SO also installs Caddy without `hostNetwork`, so it can't bind to Kind's
mapped ports 80/443. The tests install nginx ingress controller (using the
Kind-specific manifest which includes hostNetwork) and create TLS-enabled
Ingress resources manually using cert-manager with a self-signed
`letsencrypt-prod` ClusterIssuer (named to match SO's hardcoded default,
so the same pattern works in production).

**Wrong cluster selection.** SO's `get_kind_cluster()` returns the first
cluster from `kind get clusters`, not by name. On machines with multiple Kind
clusters, this could select the wrong one.

**Image loading.** SO loads images listed in `stack.yml`'s `containers:` key
during `deploy start`. The tests pre-load images at specific points in the
fixture chain and the timing/naming may not match SO's expectations.

**Production note:** On a real k8s cluster (`deploy-to: k8s`), SO's cluster
management is a no-op. Pre-install an ingress controller and cert-manager with
a `letsencrypt-prod` ClusterIssuer, and SO handles TLS ingress automatically.

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
10. **Relayer deployment** deploys the hyperlane relayer with chain signer keys and IGP configuration (`relayer_deployment` fixture)
11. **Bridge transfers** executes cross-chain warp route transfers via CLI (`bridge_setup` fixture)
12. **Warp UI deployment** builds and deploys the warp-ui stack with resolved mailbox/warp addresses (`warp_ui_deployment` fixture)
13. **Warp UI browser tests** launches a Playwright browser with a mock Solana wallet injected (`warp_ui_browser` fixture)
14. **Test functions** verify deployment outputs: ConfigMaps, pod health, container readiness, metrics endpoints, log sanity, checkpoint files in MinIO, bridge transfers, UI rendering
15. **Teardown** is handled automatically by fixture finalizers -- stopping stacks, chain nodes, and destroying the kind cluster (unless `--skip-cleanup` is passed)
