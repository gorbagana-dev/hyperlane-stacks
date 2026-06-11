# E2E Tests

End-to-end tests for the Hyperlane SVM bridge stacks. Tests deploy contracts via `laconic-so` on a local kind cluster with real Solana and Gorchain nodes, then verify the deployment artifacts.

> Pure unit tests that need no cluster, chains, or `laconic-so` live in `tests/unit/` (see `tests/unit/README.md`) and run with a plain `pytest tests/unit/`.

## What's tested

- **Core deployer** -- deploys Hyperlane core contracts (Mailbox, IGP, ISM, etc.) on both chains, verifies state file outputs (`agent-config.json`, `program-ids.json`, `gas-oracle-config.json`, `multisig-config.json`) via `BridgeStateLoader` reads
- **Warp deployer** -- one deployment that deploys the routes selected via `WARP_ROUTES` (a test SPL collateral route and a native route), verifies per-route state file outputs (`token-config.json`, `warp-deploy-outputs/`) via `BridgeStateLoader` reads
- **MinIO** -- deploys the S3-compatible checkpoint storage, verifies pod health, bucket creation, and API accessibility
- **Validators** -- deploys gorchain and solana validators with a mock Privy server, verifies pod health, KMS proxy connectivity, metrics endpoint, log sanity, and checkpoint writing to MinIO
- **Ownership & relay authorization** -- right after the warp deploy, asserts the multisig-ISM and each warp route's app-level owner were transferred to the bridge owner, and that the relayer's `HYP_WHITELIST` covers exactly the deployed warp programs (`test_03_ownership_whitelist.py`)
- **Relayer** -- deploys the hyperlane relayer, verifies pod health and metrics endpoint
- **Gas oracle** -- deploys the gas oracle service with mock Privy signing, waits for first price update, verifies on-chain IGP gas oracle configs match oracle output
- **Bridge transfers** -- executes cross-chain warp route transfers (Solana→Gorchain and reverse) via CLI, verifies on-chain balance changes
- **Fee claims** -- claims accumulated IGP fees on both chains, verifies beneficiary balance increases (skips with warning if no fees to claim)
- **Warp UI (Tier 1)** -- deploys the warp-ui stack, verifies pod health, HTML serving, runtime route/chain config serving, and chain config presence via HTTP
- **Warp UI (Tier 2)** -- drives the warp-ui in a Playwright browser with a mock Solana wallet, executes real bridge transfers through the UI
- **Ledger signing (dormant fork feature)** -- signs a real on-chain op (a Solana mailbox ownership round-trip) with a physically connected Ledger via the native `hyperlane-sealevel-client`, verifying the fork's built-in Ledger signing end to end. The architecture no longer uses a hardware wallet (ownership goes to the Privy bridge-owner wallet), so this is kept only as a fork-feature test. Skipped by default; see [Ledger signing test](#ledger-signing-test)

## Prerequisites

- Python 3.10+
- [kind](https://kind.sigs.k8s.io/)
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- [laconic-so](https://github.com/cerc-io/stack-orchestrator) `v1.1.0-a181281-202606050713`
- [Solana CLI](https://docs.anza.xyz/cli/install) (`solana`, `solana-keygen`, `spl-token`)
- [Foundry](https://book.getfoundry.sh/getting-started/installation) (`cast`)
- Docker — logged in to ghcr.io for pulling published images (see below)
- [Playwright](https://playwright.dev/python/) browser binary and system dependencies (for warp-ui browser tests): `playwright install chromium && playwright install-deps chromium`
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

## First-time machine setup

These tests need `mkcert` to generate browser-trusted TLS certificates for the
`*.test` hostnames Caddy serves. `mkcert -install` installs a root CA into your
system + browser trust stores so `curl` / Playwright / Python `requests` don't
need to disable cert verification.

```bash
# Linux (Ubuntu/Debian):
sudo apt-get install -y libnss3-tools
curl -L -o /tmp/mkcert https://github.com/FiloSottile/mkcert/releases/download/v1.4.4/mkcert-v1.4.4-linux-amd64
sudo install /tmp/mkcert /usr/local/bin/mkcert

# macOS:
brew install mkcert nss

# One-time CA install (both platforms):
mkcert -install
```

Remove with `mkcert -uninstall` later if needed; the generated cert files persist
under the test state directory and are wiped on full teardown.

## Setup

```bash
cd tests/e2e
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium       # download browser binary for warp-ui tests
playwright install-deps chromium  # install system libraries (libatk, libgdk, etc.)
```

## Running locally

```bash
# Full run (builds images, creates cluster, starts chains, runs tests, tears down)
# -x stops on first failure (tests are sequential — downstream tests can't pass if earlier ones fail)
xvfb-run pytest -v -x

# Skip cluster creation (reuse an existing kind cluster)
pytest -v -x --skip-cluster-setup

# Skip chain node startup (Solana/Gorchain already running externally)
pytest -v -x --skip-chain-setup

# Build images from source instead of using published images
pytest -v -x --build-from-source

# Keep everything running after tests (for debugging)
pytest -v -x --skip-cleanup

# Run only a specific test file
pytest -v -x test_01_deployer.py
pytest -v -x test_02_warp_deployer.py
pytest -v -x test_05_validator.py
pytest -v -x test_11_warp_ui.py

# Run only warp UI browser tests (headless via xvfb-run)
xvfb-run pytest -v -x test_13_warp_ui_bridge.py

# Run with visible browser window (on a desktop with $DISPLAY)
pytest -v -x test_13_warp_ui_bridge.py

# Exclude slow tests (validator checkpoint tests, bridge transfers, UI tests)
pytest -v -x -m "not slow"
```

## Ledger signing test

`test_14_ledger_signing.py` exercises built-in Ledger signing in the
`hyperlane-sealevel-client`: it transfers Solana mailbox ownership to the
Ledger's key and back, with the transfer-back signed **on the device**, then
checks ownership is restored. It needs a physically attached Ledger, so it is
**skipped by default** — a normal run is never blocked by missing hardware.

### Prerequisites

- A Ledger with the **Solana app** installed and open (device unlocked).
- **Blind signing enabled** in the Solana app (Settings → Blind signing).
  Hyperlane's instructions aren't a plain transfer the app can decode, so
  without this the device rejects them.
- **Linux only:** Ledger udev rules installed, so the device is reachable over
  USB-HID without root, then replug it:
  ```bash
  curl -fsSL https://raw.githubusercontent.com/LedgerHQ/udev-rules/master/add_udev_rules.sh | sudo bash
  ```
- A **native** `hyperlane-sealevel-client` binary **with built-in Ledger
  support** (a published release build). The Ledger talks USB-HID on the host,
  so this can't run inside the deployer Docker image. Point
  `HYPERLANE_SEALEVEL_CLIENT_BIN` at it (see below).

### Get the Ledger's Solana address

With the device unlocked and the Solana app open:

```bash
solana-keygen pubkey "usb://ledger?key=0/0"
```

That value is `E2E_LEDGER_PUBKEY` below. (`key=0/0` is the first account — bump
it to match whichever derivation path you intend to sign with.)

### Enable and run

Export three variables, then run the test on its own or as part of the whole
suite — it slots in after the bridge tests:

```bash
export E2E_LEDGER=1
export HYPERLANE_SEALEVEL_CLIENT_BIN=/abs/path/to/hyperlane-sealevel-client
export E2E_LEDGER_PUBKEY=<address from solana-keygen above>

# Just this test (still brings up the full stack it depends on):
xvfb-run pytest -v -x test_14_ledger_signing.py

# As part of the entire suite:
xvfb-run pytest -v -x
```

Approve the prompt on the device when the transfer-back is signed. If any of
the three variables is unset the test skips with a message explaining what's
missing, so the default suite run is unaffected. (You can also select it
explicitly with `-m requires_ledger`.)

## Iterating on a specific stack

The most common workflow during development: tear down one stack, fix something, rerun from that point without recreating the cluster or redeploying earlier stacks.

### Skip flags

Each `--skip-*` flag tells the test session to treat a deployment fixture as already done. Use them to skip everything *before* the stack you want to work on:

| Flag | Skips |
|---|---|
| `--skip-cluster-setup` | Kind cluster + Caddy ingress creation |
| `--skip-chain-setup` | Gorchain + Solana node startup |
| `--skip-core-deploy` | `hyperlane-svm-deployer` Job |
| `--skip-warp-deploy` | `hyperlane-svm-warp-deployer` Job |
| `--skip-minio-deploy` | `hyperlane-minio` stack |
| `--skip-validator-deploy` | Both validator stacks |
| `--skip-relayer-deploy` | `hyperlane-relayer` stack |
| `--skip-gas-oracle-deploy` | `hyperlane-gas-oracle` stack |
| `--skip-warp-ui-deploy` | `hyperlane-warp-ui` stack |

### Teardown a single stack

```bash
# Stop the stack, delete its volumes and namespace, then wipe the deployment dir.
# Replace <stack> with the directory name under .deployments/:
#   hyperlane-minio | hyperlane-validator-gorchain | hyperlane-validator-solana |
#   hyperlane-relayer | hyperlane-gas-oracle | hyperlane-warp-ui

laconic-so deployment --dir ./.deployments/<stack> stop --delete-volumes --delete-namespace
sudo rm -rf .deployments/<stack>
rm .deployments/<stack>-spec.yml
```

### Rerun from a given stack

Stack the `--skip-*` flags for everything *before* what you want to rerun. Always add `--skip-cluster-setup --skip-chain-setup` unless you also need to recreate those.

```bash
# Rerun MinIO (cluster, chains, deployers already up):
xvfb-run pytest -vx --skip-cleanup \
  --skip-cluster-setup --skip-chain-setup \
  --skip-core-deploy --skip-warp-deploy

# Rerun validators (cluster, chains, deployers, MinIO already up):
xvfb-run pytest -vx --skip-cleanup \
  --skip-cluster-setup --skip-chain-setup \
  --skip-core-deploy --skip-warp-deploy --skip-minio-deploy

# Rerun relayer (everything before it already up):
xvfb-run pytest -vx --skip-cleanup \
  --skip-cluster-setup --skip-chain-setup \
  --skip-core-deploy --skip-warp-deploy --skip-minio-deploy \
  --skip-validator-deploy

# Rerun warp UI only (everything else already up):
xvfb-run pytest -vx --skip-cleanup \
  --skip-cluster-setup --skip-chain-setup \
  --skip-core-deploy --skip-warp-deploy --skip-minio-deploy \
  --skip-validator-deploy --skip-relayer-deploy --skip-gas-oracle-deploy
```

`--skip-cleanup` keeps all stacks running after the test session ends — useful for inspecting logs or the cluster state after a failure. Drop it if you want teardown to happen normally.

### Rerun everything from scratch (keep cluster + chains)

```bash
# Wipe all stack deployments but keep the Kind cluster and running chain nodes:
for stack in hyperlane-svm-deployer hyperlane-svm-warp-deployer hyperlane-minio \
             hyperlane-validator-gorchain hyperlane-validator-solana hyperlane-relayer \
             hyperlane-gas-oracle hyperlane-warp-ui; do
  laconic-so deployment --dir ./.deployments/$stack stop --delete-volumes --delete-namespace 2>/dev/null || true
  sudo rm -rf .deployments/$stack
  rm .deployments/$stack-spec.yml
done

xvfb-run pytest -vx --skip-cleanup --skip-cluster-setup --skip-chain-setup
```

## Structure

```
tests/e2e/
├── conftest.py                          # Session-scoped fixtures (setup/teardown)
├── pytest.ini                           # pytest configuration
├── requirements.txt                     # Python dependencies (use with venv)
├── test_00_cluster_helpers.py           # Unit tests for cluster utility functions
├── test_01_deployer.py                  # Core deployer verification tests
├── test_02_warp_deployer.py             # Warp deployer verification tests
├── test_03_ownership_whitelist.py       # On-chain ownership handoff + relayer whitelist assertions
├── test_04_minio.py                     # MinIO stack tests
├── test_05_validator.py                 # Validator stack tests (gorchain + solana)
├── test_06_relayer.py                   # Relayer stack tests
├── test_07_gas_oracle.py                # Gas oracle stack tests
├── test_08_monitoring.py                # Monitoring stack tests (Prometheus, Grafana, balance monitor)
├── test_09_bridge.py                    # Cross-chain bridge transfer tests
├── test_10_fee_claim.py                 # IGP fee claim tests
├── test_11_warp_ui.py                   # Warp UI HTTP smoke tests (Tier 1)
├── test_12_ingress_endpoints.py         # Ingress URL probes for all stacks (Caddy wiring sanity)
├── test_13_warp_ui_bridge.py            # Warp UI browser bridge tests (Tier 2, Playwright)
├── test_14_ledger_signing.py            # Ledger device-signing test (dormant fork feature; needs a device)
├── .logs/                               # k8s logs captured during test runs (gitignored)
├── lib/
│   ├── common.py                        # Logging, assertions, wait helpers, log capture
│   ├── cluster.py                       # Host prep: hosts entries, mkcert, kind network
│   ├── chain.py                         # Solana/Gorchain node lifecycle
│   ├── deploy.py                        # laconic-so deployment helpers
│   ├── keygen.py                        # Keypair generation, funding, k8s secrets
│   ├── privy_mock.py                    # Mock Privy server for validator signing
│   └── backpack.py                      # Backpack wallet extension helpers for Playwright
└── fixtures/
    ├── kind-config.yaml                 # Kind cluster with ingress ports
    ├── test-spec-deployer.yml           # laconic-so spec for core deployer
    ├── test-spec-warp-deployer.yml      # laconic-so spec for warp deployer (routes via WARP_ROUTES)
    ├── test-spec-minio.yml              # laconic-so spec for MinIO
    ├── test-spec-validator-gorchain.yml # laconic-so spec for gorchain validator
    ├── test-spec-validator-solana.yml   # laconic-so spec for solana validator
    ├── test-spec-relayer.yml            # laconic-so spec for relayer
    ├── test-spec-gas-oracle.yml         # laconic-so spec for gas oracle
    └── test-spec-warp-ui.yml            # laconic-so spec for warp UI
```

## TLS in tests

Tests serve TLS via Caddy (same as prod). At session start the `host_prep`
fixture:

1. Generates one multi-SAN cert with mkcert covering all `*.test` hostnames at
   `<BRIDGE_STATE_ROOT>/local-certs/hyperlane.test.{crt,key}`.
2. Writes a `caddy-secrets.yaml` to
   `<BRIDGE_STATE_ROOT>/caddy-cert-backup/caddy-secrets.yaml` — one k8s Secret
   per hostname at the fake-ACME path Caddy uses for its `secret_store`.

When the first `deploy_start --perform-cluster-management` fires, SO creates
the kind cluster and runs `install_ingress_for_kind`. Phase 2 of that install
(`_restore_caddy_certs`) reads our `caddy-secrets.yaml` and creates the
Secrets before Caddy starts. Caddy then serves them at request time without
ever attempting ACME.

No cert-manager. No nginx-ingress. No `ClusterIssuer`. The TLS path matches
prod (Caddy + ACME-shaped flow); only the cert source differs (mkcert in dev,
Let's Encrypt in prod).

## How it works

1. **Setup** creates a venv and installs Python dependencies (`requirements.txt`)
2. **Image setup** uses published container images by default; pass `--build-from-source` to build locally (`deployer_image` fixture)
3. **Keypair generation** creates test Ed25519 + secp256k1 keypairs (`keypairs` fixture)
4. **Host prep** adds /etc/hosts entries, ensures mkcert + Docker `kind` network exist, generates a multi-SAN cert, and writes Caddy's cert-backup file (`host_prep` fixture). The kind cluster + Caddy ingress controller are created later by SO at the first `deploy_start`.
5. **Chain nodes** start a Solana test validator on the host (`chain_nodes` fixture)
6. **MinIO deployment** deploys the checkpoint storage stack (`minio_deployment` fixture)
7. **Mock Privy server** starts a local HTTP server that implements the Privy wallet signing API, used by the KMS proxy sidecar in validator pods (`privy_mock` fixture)
8. **Deployer deployment** exposes host chain nodes to the cluster via k8s Service+Endpoints, funds wallets, creates secrets, and starts the deployer stack (`deployer_deployment` fixture). Deployer writes state files to `/state` (host-path mount).
9. **Validator deployment** deploys gorchain and solana validators with per-chain namespaces, populates `agent-config` ConfigMap from deployer state files via `BridgeStateLoader`, creates per-chain secrets (`validator_gorchain`, `validator_solana` fixtures)
10. **Relayer deployment** deploys the hyperlane relayer with chain signer keys and IGP configuration, populates `agent-config` ConfigMap from deployer state files via `BridgeStateLoader`, and patches `HYP_WHITELIST` from the warp-deployer's `relayer-whitelist.json` (`relayer_deployment` fixture)
11. **Gas oracle deployment** deploys the gas oracle with mock Privy signing, waits for first price update, captures computed exchange rates and gas prices (`gas_oracle_deployment` fixture)
12. **Bridge transfers** executes cross-chain warp route transfers via CLI (`bridge_setup` fixture)
13. **Warp UI deployment** builds and deploys the warp-ui stack with resolved mailbox/warp addresses (`warp_ui_deployment` fixture)
14. **Warp UI browser tests** launches a Playwright browser with a mock Solana wallet injected (`warp_ui_browser` fixture)
15. **Test functions** verify deployment outputs: state files (via `BridgeStateLoader` reads), pod health, container readiness, metrics endpoints, log sanity, checkpoint files in MinIO, bridge transfers, UI rendering
16. **Teardown** is handled automatically by fixture finalizers -- stopping stacks, chain nodes, and destroying the kind cluster (unless `--skip-cleanup` is passed)

## Logs

Kubernetes logs are automatically captured to `tests/e2e/.logs/` during test runs:

- **Job logs** (deployer, warp-deployer, minio-init) are saved immediately after the job completes
- **Pod logs** (validators, relayer, warp-ui) are saved during fixture teardown, before the stack is stopped

Each log file is named by stack and container, e.g. `job_hyperlane-svm-deployer.log`, `relayer_relayer.log`. The `.logs/` directory is gitignored. In CI, logs are uploaded as artifacts alongside `.deployments/` and the Solana validator log.
