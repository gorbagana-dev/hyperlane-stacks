# Staging droplets — create the three VMs (doctl)

The [staging runbook](staging.md) assumes three DigitalOcean droplets with a
`dev` user you can SSH into. This page creates them. It is DO/doctl-specific —
on another provider, reproduce the same end state: three Ubuntu 24.04 hosts, a
passwordless-sudo `dev` user with your SSH key, ports 80/443 reachable.

| Droplet | Runs | DO size slug |
|---|---|---|
| `staging-bridge-ops` | MinIO, deployer Jobs, relayer, gas-oracle, monitoring, warp-ui, explorer | `s-8vcpu-16gb` |
| `staging-gorchain` | gorchain chain + Caddy RPC front | `s-8vcpu-32gb-640gb-intel` |
| `staging-hyperlane-validators` | both hyperlane validators | `s-4vcpu-8gb` |

Prerequisite: `doctl` installed and authenticated (`doctl auth init`).

## 1. Register your SSH key with DO

The inventory expects a `dev` user with passwordless sudo on every host
(`privileged_user`/`deploy_user` in host_vars) — cloud-init creates it at
droplet boot with your SSH key, so you never need a root session.

```bash
# Register your public key with DO (once); note the ID it prints.
doctl compute ssh-key import staging-ops --public-key-file ~/.ssh/id_ed25519.pub
# Already registered? Look it up instead:
doctl compute ssh-key list
```

## 2. Write the cloud-init user-data

```bash
# In $HOME, not /tmp: a snap-installed doctl has a private /tmp and would
# fail with "no such file or directory" on a path that plainly exists.
cat > ~/staging-user-data.yml <<EOF
#cloud-config
users:
  - name: dev
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    ssh_authorized_keys:
      - $(cat ~/.ssh/id_ed25519.pub)
EOF
```

## 3. Create the droplets

```bash
REGION=<region>    # pick one: doctl compute region list
KEY_ID=<key-id>    # printed by the ssh-key import/list above

for vm in staging-bridge-ops:s-8vcpu-16gb \
          staging-gorchain:s-8vcpu-32gb-640gb-intel \
          staging-hyperlane-validators:s-4vcpu-8gb; do
  doctl compute droplet create "${vm%%:*}" \
    --size "${vm##*:}" \
    --image ubuntu-24-04-x64 \
    --region "$REGION" \
    --ssh-keys "$KEY_ID" \
    --user-data-file ~/staging-user-data.yml \
    --wait
done
```

`--ssh-keys` additionally puts the key on root (console rescue); day-to-day
access is `dev`. No DO cloud firewall is attached — 80/443 must stay reachable
on every host for Let's Encrypt.

## 4. Harvest IPs and check SSH

```bash
doctl compute droplet list "staging-*" --format Name,PublicIPv4
ssh dev@<each-ip> 'sudo -n true && echo ok'   # accept the host key; prints ok
```

cloud-init runs asynchronously after boot — if `dev` is refused right after
create, wait a minute and retry.

Use the IPs to fill `public_ip` in each
`ops/inventories/staging/host_vars/<host>.yml`, then continue from
[staging.md → Configure inventory + secrets](staging.md#configure-inventory--secrets).

## Teardown

Destroying the droplets is the scorched-earth reset (chain state and all):

```bash
doctl compute droplet delete staging-bridge-ops staging-gorchain staging-hyperlane-validators
```
