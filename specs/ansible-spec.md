# Ops Ansible Playbooks — Implementation Spec

## Overview

Convert the raw k8s Job manifests in `deployment/ops/` into Ansible playbooks. Each playbook creates a k8s Job on the target cluster, waits for completion, retrieves outputs (unsigned transactions), and cleans up. The inline shell scripts currently embedded in the Job YAML `args:` field are extracted into standalone `.sh` files and loaded into the Job as a ConfigMap volume.

This spec covers **teardown** (the most complex job). The other three jobs (kill-switch, restore, verify-ownership) follow the same patterns.

## Directory Structure

```
deployment/ops/
  ansible.cfg
  inventory/
    hosts.yml                  # cluster connection (kubeconfig path, namespace)
  group_vars/
    all.yml                    # shared vars: RPC URLs, domain IDs, wallet pubkeys
  playbooks/
    teardown.yml
    kill-switch.yml
    restore.yml
    verify-ownership.yml
  scripts/
    teardown.sh
    kill-switch.sh
    restore.sh
    verify-ownership.sh
  templates/
    teardown-job.yml.j2
    kill-switch-job.yml.j2
    restore-job.yml.j2
    verify-ownership-job.yml.j2
```

## Teardown Playbook: `playbooks/teardown.yml`

### Flow

```
1. Pre-flight checks
2. Prompt for confirmation
3. Create ConfigMap from scripts/teardown.sh
4. Create Job from templates/teardown-job.yml.j2
5. Wait for Job completion
6. Copy unsigned txs from pod to local dir
7. Print signing instructions
8. Clean up Job + ConfigMap
```

### Variables

Defined in `group_vars/all.yml` (shared across all playbooks):

```yaml
# Cluster
kubeconfig_path: "~/.kube/config"
namespace: default

# Chain config
gorchain_rpc_url: ""
solana_rpc_url: ""
gorchain_domain_id: 99999
solana_domain_id: 99998

# Wallets
hardware_wallet_pubkey: ""
```

Teardown-specific vars (can be set in `group_vars/all.yml` or passed via `--extra-vars`):

```yaml
treasury_address: ""
dry_run: true
confirm_teardown: false
```

### Playbook Structure

```yaml
# playbooks/teardown.yml
- name: Hyperlane bridge teardown
  hosts: localhost
  connection: local
  gather_facts: false

  vars:
    job_name: "hyperlane-teardown-{{ lookup('pipe', 'date +%s') }}"
    script_configmap_name: "{{ job_name }}-script"
    output_dir: "./teardown-output"
    image: "laconic/hyperlane-svm-deployer:local"

  pre_tasks:
    - name: Validate required variables
      ansible.builtin.assert:
        that:
          - gorchain_rpc_url | length > 0
          - solana_rpc_url | length > 0
          - hardware_wallet_pubkey | length > 0
          - treasury_address | length > 0
        fail_msg: "Required variables not set. Check group_vars/all.yml"

    - name: Check cluster connectivity
      kubernetes.core.k8s_info:
        kind: Namespace
        name: "{{ namespace }}"
      register: ns_check

    - name: Check program-ids ConfigMap exists
      kubernetes.core.k8s_info:
        kind: ConfigMap
        name: hyperlane-program-ids
        namespace: "{{ namespace }}"
      register: program_ids_cm
      failed_when: program_ids_cm.resources | length == 0

    - name: Confirm teardown (non-dry-run)
      ansible.builtin.pause:
        prompt: >-
          DESTRUCTIVE OPERATION: This will close all programs and recover rent.
          Type 'yes' to proceed
      register: confirm
      when: not dry_run
      failed_when: confirm.user_input != 'yes'

  tasks:
    - name: Create script ConfigMap
      kubernetes.core.k8s:
        state: present
        definition:
          apiVersion: v1
          kind: ConfigMap
          metadata:
            name: "{{ script_configmap_name }}"
            namespace: "{{ namespace }}"
          data:
            teardown.sh: "{{ lookup('file', '../scripts/teardown.sh') }}"

    - name: Create teardown Job
      kubernetes.core.k8s:
        state: present
        definition: "{{ lookup('template', '../templates/teardown-job.yml.j2') }}"

    - name: Wait for Job completion
      kubernetes.core.k8s_info:
        kind: Job
        name: "{{ job_name }}"
        namespace: "{{ namespace }}"
      register: job_status
      until: >-
        (job_status.resources[0].status.succeeded | default(0)) > 0 or
        (job_status.resources[0].status.failed | default(0)) > 0
      retries: 60
      delay: 5

    - name: Fail if Job failed
      ansible.builtin.fail:
        msg: "Teardown job failed. Check logs: kubectl logs job/{{ job_name }}"
      when: (job_status.resources[0].status.failed | default(0)) > 0

    - name: Get Job pod name
      kubernetes.core.k8s_info:
        kind: Pod
        namespace: "{{ namespace }}"
        label_selectors:
          - "job-name={{ job_name }}"
      register: job_pod

    - name: Print Job logs
      ansible.builtin.command:
        cmd: >-
          kubectl --kubeconfig {{ kubeconfig_path }}
          logs {{ job_pod.resources[0].metadata.name }}
          -n {{ namespace }}
      register: job_logs

    - name: Display logs
      ansible.builtin.debug:
        var: job_logs.stdout_lines

    - name: Create local output directory
      ansible.builtin.file:
        path: "{{ output_dir }}"
        state: directory
      when: not dry_run

    - name: Copy unsigned transactions from pod
      ansible.builtin.command:
        cmd: >-
          kubectl --kubeconfig {{ kubeconfig_path }}
          cp {{ namespace }}/{{ job_pod.resources[0].metadata.name }}:/output/
          {{ output_dir }}/
      when: not dry_run

    - name: List output files
      ansible.builtin.find:
        paths: "{{ output_dir }}"
        patterns: "*.json"
      register: tx_files
      when: not dry_run

    - name: Print signing instructions
      ansible.builtin.debug:
        msg: |
          Unsigned transactions saved to {{ output_dir }}/

          For each .json file, sign and submit:
            solana sign-offloaded-transaction {{ output_dir }}/<file>.json --signer usb://ledger
            solana send-signed-transaction {{ output_dir }}/<file>.json --url <RPC_URL>

          Review .summary.txt files for per-transaction details.
      when: not dry_run

  post_tasks:
    - name: Clean up Job
      kubernetes.core.k8s:
        state: absent
        kind: Job
        name: "{{ job_name }}"
        namespace: "{{ namespace }}"
        delete_options:
          propagationPolicy: Background

    - name: Clean up script ConfigMap
      kubernetes.core.k8s:
        state: absent
        kind: ConfigMap
        name: "{{ script_configmap_name }}"
        namespace: "{{ namespace }}"
```

## Job Template: `templates/teardown-job.yml.j2`

The Jinja2 template renders the k8s Job manifest. All operator-specific values come from Ansible vars — no `REPLACE_WITH_*` placeholders.

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: "{{ job_name }}"
  namespace: "{{ namespace }}"
  labels:
    app.kubernetes.io/name: hyperlane-svm-ops
    app.kubernetes.io/component: teardown
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: teardown
          image: "{{ image }}"
          command: ["/bin/bash", "/opt/scripts/teardown.sh"]
          env:
            - name: GORCHAIN_RPC_URL
              value: "{{ gorchain_rpc_url }}"
            - name: SOLANA_RPC_URL
              value: "{{ solana_rpc_url }}"
            - name: HARDWARE_WALLET_PUBKEY
              value: "{{ hardware_wallet_pubkey }}"
            - name: TREASURY_ADDRESS
              value: "{{ treasury_address }}"
            - name: DRY_RUN
              value: "{{ dry_run | string | lower }}"
            - name: CONFIRM_TEARDOWN
              value: "{{ 'yes' if (not dry_run and confirm_teardown) else 'no' }}"
          volumeMounts:
            - name: program-ids
              mountPath: /config/program-ids
              readOnly: true
            - name: scripts
              mountPath: /opt/scripts
              readOnly: true
            - name: output
              mountPath: /output
      volumes:
        - name: program-ids
          configMap:
            name: hyperlane-program-ids
        - name: scripts
          configMap:
            name: "{{ script_configmap_name }}"
        - name: output
          emptyDir: {}
```

## Script: `scripts/teardown.sh`

The existing inline script from `teardown-job.yaml` `args:` field, extracted verbatim into a standalone file. No logic changes — just moved out of the YAML.

## Operator Usage

```bash
# Dry run (default)
ansible-playbook playbooks/teardown.yml

# Real execution
ansible-playbook playbooks/teardown.yml -e dry_run=false -e confirm_teardown=true

# Override specific vars
ansible-playbook playbooks/teardown.yml -e treasury_address=<addr> -e gorchain_rpc_url=<url>
```

## Patterns for Other Jobs

All four playbooks follow the same structure:

1. **Pre-flight**: validate vars, check cluster, check ConfigMap
2. **Confirm**: prompt before destructive ops (skip for verify-ownership)
3. **ConfigMap**: create from `scripts/<name>.sh`
4. **Job**: create from `templates/<name>-job.yml.j2`
5. **Wait**: poll until succeeded/failed
6. **Retrieve**: `kubectl cp` outputs (skip for verify-ownership which only logs)
7. **Clean up**: delete Job + script ConfigMap

Job-specific differences:
- **kill-switch**: scales agents to 0 (can be a pre_task using `kubernetes.core.k8s`), no dry-run gate
- **restore**: needs extra vars (`gorchain_validator_address`, `solana_validator_address`), logs scale-up instructions post-signing
- **verify-ownership**: read-only, no output retrieval, exit code check only
- **teardown**: dry-run/confirm gates, treasury address var
