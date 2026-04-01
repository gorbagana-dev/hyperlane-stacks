# E2E Tests

End-to-end tests for the Hyperlane SVM bridge stacks. Tests deploy contracts via `laconic-so` on a local kind cluster with real Solana and Gorchain nodes, then verify the deployment artifacts.

## What's tested

- **Core deployer** -- deploys Hyperlane core contracts (Mailbox, IGP, ISM, etc.) on both chains, verifies ConfigMap outputs (`hyperlane-program-ids`, `hyperlane-agent-config`, `hyperlane-gas-oracle-config`, `hyperlane-multisig-config`)
- **Warp deployer** -- deploys warp route contracts for a test SPL token, verifies ConfigMap outputs (`hyperlane-token-config`, `hyperlane-warp-deploy-outputs`)
- **MinIO** -- deploys the S3-compatible checkpoint storage, verifies pod health, bucket creation, and API accessibility
- **Validators** -- deploys gorchain and solana validators with a mock Privy server, verifies pod health, KMS proxy connectivity, metrics endpoint, log sanity, and checkpoint writing to MinIO
- **Relayer** -- deploys the hyperlane relayer, verifies pod health and metrics endpoint
- **Gas oracle** -- deploys the gas oracle service with mock Privy signing, waits for first price update, verifies on-chain IGP gas oracle configs match oracle output
- **Bridge transfers** -- executes cross-chain warp route transfers (Solana→Gorchain and reverse) via CLI, verifies on-chain balance changes
- **Fee claims** -- claims accumulated IGP fees on both chains, verifies beneficiary balance increases (skips with warning if no fees to claim)
- **Warp UI (Tier 1)** -- deploys the warp-ui stack, verifies pod health, HTML serving, sentinel replacement, and chain config presence via HTTP
- **Warp UI (Tier 2)** -- drives the warp-ui in a Playwright browser with a mock Solana wallet, executes real bridge transfers through the UI

## Prerequisites

- Python 3.10+
- [kind](https://kind.sigs.k8s.io/)
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- [laconic-so](https://git.vdb.to/cerc-io/stack-orchestrator)
- [Solana CLI](https://docs.anza.xyz/cli/install) (`solana`, `solana-keygen`, `spl-token`)
- [Foundry](https://book.getfoundry.sh/getting-started/installation) (`cast`)
- Docker — logged in to ghcr.io for pulling published images (see below)
- [Playwright](https://playwright.dev/python/) browser binary (for warp-ui browser tests): `playwright install chromium`
- Xvfb (for warp-ui browser tests without a display): `apt install xvfb` — use `xvfb-run` to wrap pytest

### Firewall (UFW)

If UFW is enabled on the host, Kind pods won't be able to reach host services
(Gorchain/Solana RPC nodes) via the Docker bridge network. The pods connect to
the host's Docker bridge IP (e.g. `172.18.0.1`), which hits the INPUT chain —
UFW's default `deny (incoming)` drops this traffic even though the services are
listening on `0.0.0.0`.

Allow the Kind/Docker network to reach the host:

```bash
# Check if UFW is active and blocking
sudo ufw status verbose  # look for "deny (incoming)"

# Allow the Docker bridge subnet
sudo ufw allow from 172.18.0.0/16
```

Symptoms of this issue: deployer jobs fail with `ConnectionRefused` when trying
to reach `gorchain-rpc:8899` or `solana-rpc:18899`, even though `curl
localhost:8899/health` works fine on the host and the k8s Service/Endpoints
exist with the correct IP. Ping from inside the Kind node to the host IP works
(UFW allows ICMP by default), but TCP connections hang or are refused.

### Docker registry login

Published container images are hosted on ghcr.io as private packages. You need a GitHub Personal Access Token (PAT) with `packages:read` scope:

```bash
echo $GHCR_PAT | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin
```

Without this, image pulls will fail with `unauthorized`. To build images locally instead, use the `--build-from-source` flag.

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
# -x stops on first failure (tests are sequential — downstream tests can't pass if earlier ones fail)
cd tests/e2e && xvfb-run pytest -v -x

# Skip cluster creation (reuse an existing kind cluster)
pytest -v -x --skip-cluster-setup

# Skip chain node startup (Solana/Gorchain already running externally)
pytest -v -x --skip-chain-setup

# Build images from source instead of using published images
pytest -v -x --build-from-source

# Keep everything running after tests (for debugging)
pytest -v -x --skip-cleanup

# Iterative development: reuse cluster + chains + existing deployments
pytest -v -x --skip-cluster-setup --skip-chain-setup --skip-cleanup
pytest -v -x --skip-cluster-setup --skip-chain-setup --skip-core-deploy --skip-warp-deploy --skip-minio-deploy --skip-validator-deploy --skip-relayer-deploy --skip-gas-oracle-deploy --skip-warp-ui-deploy

# Run only core deployer tests
pytest -v -x test_01_deployer.py

# Run only warp deployer tests
pytest -v -x test_02_warp_deployer.py

# Run only validator tests
pytest -v -x test_04_validator.py

# Run only warp UI smoke tests
pytest -v -x test_10_warp_ui.py

# Run only warp UI browser tests (headless via xvfb-run)
xvfb-run pytest -v -x test_11_warp_ui_bridge.py

# Run with visible browser window (on a desktop with $DISPLAY)
pytest -v -x test_11_warp_ui_bridge.py

# Exclude slow tests (validator checkpoint tests, bridge transfers, UI tests)
pytest -v -x -m "not slow"
```

## Structure

```
tests/e2e/
├── conftest.py                          # Session-scoped fixtures (setup/teardown)
├── pytest.ini                           # pytest configuration
├── requirements.txt                     # Python dependencies (use with venv)
├── test_01_deployer.py                  # Core deployer verification tests
├── test_02_warp_deployer.py             # Warp deployer verification tests
├── test_03_minio.py                     # MinIO stack tests
├── test_04_validator.py                 # Validator stack tests (gorchain + solana)
├── test_05_relayer.py                   # Relayer stack tests
├── test_06_gas_oracle.py                # Gas oracle stack tests
├── test_07_monitoring.py                # Monitoring stack tests (Prometheus, Grafana, balance monitor)
├── test_08_bridge.py                    # Cross-chain bridge transfer tests
├── test_09_fee_claim.py                 # IGP fee claim tests
├── test_10_warp_ui.py                   # Warp UI HTTP smoke tests (Tier 1)
├── test_11_warp_ui_bridge.py            # Warp UI browser bridge tests (Tier 2, Playwright)
├── .logs/                               # k8s logs captured during test runs (gitignored)
├── lib/
│   ├── common.py                        # Logging, assertions, wait helpers, log capture
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
    ├── test-spec-gas-oracle.yml         # laconic-so spec for gas oracle
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
11. **Gas oracle deployment** deploys the gas oracle with mock Privy signing, waits for first price update, captures computed exchange rates and gas prices (`gas_oracle_deployment` fixture)
12. **Bridge transfers** executes cross-chain warp route transfers via CLI (`bridge_setup` fixture)
13. **Warp UI deployment** builds and deploys the warp-ui stack with resolved mailbox/warp addresses (`warp_ui_deployment` fixture)
14. **Warp UI browser tests** launches a Playwright browser with a mock Solana wallet injected (`warp_ui_browser` fixture)
15. **Test functions** verify deployment outputs: ConfigMaps, pod health, container readiness, metrics endpoints, log sanity, checkpoint files in MinIO, bridge transfers, UI rendering
16. **Teardown** is handled automatically by fixture finalizers -- stopping stacks, chain nodes, and destroying the kind cluster (unless `--skip-cleanup` is passed)

## Logs

Kubernetes logs are automatically captured to `tests/e2e/.logs/` during test runs:

- **Job logs** (deployer, warp-deployer, minio-init) are saved immediately after the job completes
- **Pod logs** (validators, relayer, warp-ui) are saved during fixture teardown, before the stack is stopped

Each log file is named by stack and container, e.g. `job_hyperlane-svm-deployer.log`, `relayer_relayer.log`. The `.logs/` directory is gitignored. In CI, logs are uploaded as artifacts alongside `.deployments/` and the Solana validator log.
