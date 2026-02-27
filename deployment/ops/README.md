# Hyperlane SVM Ops -- Ansible Playbooks

Operational playbooks for Hyperlane bridge management. Each playbook creates a k8s Job on the target cluster, waits for completion, retrieves outputs (unsigned transactions), and cleans up automatically.

## Prerequisites

- Python 3 with Ansible installed (`pip install ansible`)
- `kubernetes.core` Ansible collection (`ansible-galaxy collection install kubernetes.core`)
- `kubectl` CLI configured with cluster access
- Hardware wallet (Ledger) connected with Solana app (for signing)
- `solana` CLI installed locally
- Core deployer has run and `hyperlane-program-ids` ConfigMap exists in the target namespace

## Configuration

Edit `group_vars/all.yml` to set your environment-specific values:

| Variable | Description | Used by |
|----------|-------------|---------|
| `kubeconfig_path` | Path to kubeconfig file | All playbooks |
| `namespace` | Kubernetes namespace | All playbooks |
| `gorchain_rpc_url` | Gorchain RPC endpoint | All playbooks |
| `solana_rpc_url` | Solana RPC endpoint | All playbooks |
| `gorchain_domain_id` | Gorchain Hyperlane domain ID (default: 99999) | kill-switch, restore |
| `solana_domain_id` | Solana Hyperlane domain ID (default: 99998) | kill-switch, restore |
| `hardware_wallet_pubkey` | Hardware wallet Solana pubkey | All playbooks |
| `treasury_address` | Address to receive recovered funds | teardown |
| `dry_run` | `true`/`false` (default: `true`) | teardown |
| `confirm_teardown` | Must be `true` when dry_run is false | teardown |
| `gorchain_validator_address` | Gorchain validator H160 address | restore |
| `solana_validator_address` | Solana validator H160 address | restore |
| `igp_oracle_pubkey` | Privy oracle wallet pubkey | verify-ownership |

## Playbooks

### teardown.yml -- Full Bridge Teardown

Closes all programs, recovers rent deposits, transfers funds to treasury. Runs in dry-run mode by default.

```bash
# Dry run (default)
ansible-playbook playbooks/teardown.yml

# Real execution
ansible-playbook playbooks/teardown.yml -e dry_run=false -e confirm_teardown=true

# Override specific vars
ansible-playbook playbooks/teardown.yml -e treasury_address=<addr> -e gorchain_rpc_url=<url>
```

### kill-switch.yml -- Emergency Kill Switch

Reconfigures Multisig ISM to null validator on both chains, making message delivery impossible. Scales agents to 0 as a pre-task.

```bash
ansible-playbook playbooks/kill-switch.yml
```

### restore.yml -- Restore After Kill Switch

Reconfigures ISM back to real validator addresses. Post-signing instructions include kubectl scale commands to bring agents back up.

```bash
ansible-playbook playbooks/restore.yml \
  -e gorchain_validator_address=<addr> \
  -e solana_validator_address=<addr>
```

### verify-ownership.yml -- Ownership Verification

Read-only check that all programs have correct upgrade authority and IGP account ownership. No signing required.

```bash
ansible-playbook playbooks/verify-ownership.yml
```

## Signing Workflow

All playbooks that modify on-chain state (teardown, kill-switch, restore) produce unsigned transactions that must be signed with a hardware wallet:

1. Run the playbook -- it creates a k8s Job, waits for completion, and copies unsigned transactions locally
2. Review the `.summary.txt` files for per-transaction details
3. Sign each transaction:
   ```bash
   solana sign-offloaded-transaction <output-dir>/<file>.json --signer usb://ledger
   ```
4. Submit each signed transaction:
   ```bash
   solana send-signed-transaction <output-dir>/<file>.json --url <RPC_URL>
   ```
5. Process all `.json` files in order

## Directory Structure

```
deployment/ops/
  ansible.cfg                          # Ansible configuration
  inventory/hosts.yml                  # Localhost inventory
  group_vars/all.yml                   # Shared variables
  playbooks/
    teardown.yml                       # Full bridge teardown
    kill-switch.yml                    # Emergency kill switch
    restore.yml                        # Restore after kill switch
    verify-ownership.yml               # Ownership verification
  scripts/
    teardown.sh                        # Teardown job script
    kill-switch.sh                     # Kill switch job script
    restore.sh                         # Restore job script
    verify-ownership.sh                # Verification job script
  templates/
    teardown-job.yml.j2                # Teardown k8s Job template
    kill-switch-job.yml.j2             # Kill switch k8s Job template
    restore-job.yml.j2                 # Restore k8s Job template
    verify-ownership-job.yml.j2        # Verification k8s Job template
```
