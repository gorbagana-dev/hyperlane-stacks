# hyperlane-svm-deployer

Docker image that deploys Hyperlane core contracts on two SVM chains (Gorchain and Solana).

## What it does

1. **Deploys** Hyperlane core programs on both chains:
   - Mailbox
   - Interchain Gas Paymaster (IGP)
   - Multisig ISM (Message ID)
   - Validator Announce
   - Merkle Tree Hook

2. **Configures** each deployment:
   - Multisig ISM: 1-of-1 validator set per chain
   - IGP: gas oracle with token exchange rate and gas price
   - IGP beneficiary

3. **Verifies** deployed program hashes match local build artifacts (via `solana-verify`)

4. **Transfers ownership**:
   - Program upgrade authority → `HARDWARE_WALLET_PUBKEY` (all programs)
   - IGP account ownership → `IGP_ORACLE_PUBKEY` (Privy oracle wallet, for automated gas updates)
   - Other account ownership → `HARDWARE_WALLET_PUBKEY`

5. **Outputs** deployment artifacts as Kubernetes ConfigMaps:
   - `hyperlane-agent-config` — agent-config.json with chain definitions and contract addresses
   - `hyperlane-program-ids` — per-chain program ID mappings
   - `hyperlane-gas-oracle-config` — gas oracle configuration
   - `hyperlane-multisig-config` — multisig ISM validator sets

6. **Discards** the hot deployer key after ownership transfer

## Build

```bash
./build.sh                   # builds hyperlane-svm-deployer:local
./build.sh --tag v1.0.0      # custom tag
./build.sh --no-cache         # clean build
```

The Dockerfile is a multi-stage build:
- **Stage 1 (builder):** Ubuntu 22.04, Rust toolchain, Solana CLI v1.18.18, compiles `hyperlane-sealevel-client` and all `.so` program artifacts from `hyperlane-monorepo` @ `agents-v2.0.0`
- **Stage 2 (runtime):** Ubuntu 22.04 minimal, copies binaries + programs + `solana-verify`

## Required Environment Variables

| Variable | Description |
|----------|-------------|
| `GORCHAIN_RPC_URL` | Gorchain RPC endpoint |
| `SOLANA_RPC_URL` | Solana RPC endpoint |
| `DEPLOYER_KEYPAIR` | JSON array of deployer secret key bytes |
| `HARDWARE_WALLET_PUBKEY` | Pubkey to receive program upgrade authority |
| `IGP_ORACLE_PUBKEY` | Pubkey for IGP account ownership (Privy oracle wallet) |
| `GORCHAIN_VALIDATOR_ADDRESS` | H160 validator address for Gorchain's Multisig ISM |
| `SOLANA_VALIDATOR_ADDRESS` | H160 validator address for Solana's Multisig ISM |

## Optional Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GORCHAIN_DOMAIN_ID` | `99999` | Gorchain Hyperlane domain ID |
| `SOLANA_DOMAIN_ID` | `99998` | Solana Hyperlane domain ID |
| `GORCHAIN_CHAIN_NAME` | `gorchain` | Chain name used in agent config |
| `SOLANA_CHAIN_NAME` | `solanasvm` | Chain name used in agent config |
| `IGP_GAS_ORACLE_TOKEN_EXCHANGE_RATE` | `1000000000` | Token exchange rate for gas oracle |
| `IGP_GAS_ORACLE_GAS_PRICE` | `1` | Gas price for gas oracle |
| `IGP_BENEFICIARY` | deployer pubkey | Address to receive IGP fee claims |
| `DRY_RUN` | `false` | Print commands without executing |
| `SKIP_VERIFICATION` | `false` | Skip post-deploy hash verification |

## Run (standalone)

```bash
docker run --rm \
  -e GORCHAIN_RPC_URL=https://gorchain.example.com \
  -e SOLANA_RPC_URL=https://solana.example.com \
  -e DEPLOYER_KEYPAIR='[1,2,3,...,64]' \
  -e HARDWARE_WALLET_PUBKEY=HWpub... \
  -e IGP_ORACLE_PUBKEY=Oracle... \
  -e GORCHAIN_VALIDATOR_ADDRESS=0xabc... \
  -e SOLANA_VALIDATOR_ADDRESS=0xdef... \
  hyperlane-svm-deployer:local
```

## Run (Kubernetes Job)

The deployer is designed to run as a one-time Kubernetes Job. It creates ConfigMaps directly in the cluster if it has kubectl access, otherwise writes YAML manifests to `/opt/hyperlane/artifacts/`.

## Chain Configuration

- **Gorchain:** domain 99999, SVM protocol
- **Solana:** domain 99998, SVM protocol

Both chains use Sealevel (SVM) with deterministic finality (reorgPeriod=0), 0.4s block time, and 9-decimal native tokens.

## Security Notes

- The deployer keypair is ephemeral — used only during deployment, then securely overwritten and deleted
- All program upgrade authority is transferred to the hardware wallet
- IGP account ownership goes to the Privy oracle wallet for automated gas updates
- Post-deploy hash verification ensures programs match the build output
- `cargo build --release --locked` prevents dependency drift
- `cargo-audit` runs during the Docker build to check for known vulnerabilities

## Image Reuse

This same image is used by:
- `hyperlane-svm-warp-deployer` stack (has `hyperlane-sealevel-client` + token programs)
- `hyperlane-svm-ops` stack (has `hyperlane-sealevel-client` for kill switch / teardown)
