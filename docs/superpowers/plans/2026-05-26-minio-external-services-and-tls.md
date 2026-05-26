# MinIO External-Services + Caddy TLS Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut MinIO consumers (both validators, relayer) over from the cross-namespace FQDN to `external-services:` and route the data path through Caddy. Production uses HTTPS (LE certs verified by the SDK's built-in webpki-roots). Dev uses plain HTTP between consumers and Caddy because `aws-sdk-rust` does not honor `AWS_CA_BUNDLE` (awslabs/aws-sdk-rust#1362), so distributing the mkcert root would silently do nothing.

**Architecture:** Validators and relayer reach MinIO only through Caddy. In dev, each consumer's `external-services:` entry creates a Service named `hyperlane-minio` in its own namespace, routing to the Kind gateway IP (port 80). In prod, the URL targets the public hostname `s3.bridge.gorbagana.wtf` and cluster DNS forwards to upstream resolvers. Dev and prod differ only in URL scheme.

**Tech Stack:** docker-compose, laconic-so, kubernetes, Caddy ingress, AWS Signature v4 / S3 path-style addressing, pytest.

---

## Current branch state (commits already landed)

The first six tasks have been implemented on this branch. They assumed
HTTPS would work in dev via `AWS_CA_BUNDLE`. Subsequent investigation
found `aws-sdk-rust` does not implement that env var. **Task R below
rolls back the dev-HTTPS plumbing and switches dev to HTTP.**

Tasks 1–6 already committed (in commit order):

- `67e8d0b` Task 1 — stub `minio-ca-config` source dir + `hyperlane-minio` in TEST_HOSTNAMES.
- `17e61fc` + `c667000` Task 2 — `write_mkcert_root_to_configmap` helper + unit tests + code-review fixes.
- `8c04f65` Task 3 — minio http-proxy host entries (`hyperlane-minio` in dev, `s3.bridge.gorbagana.wtf` in prod, both → `minio:9000`).
- `17325a6` + `0203ff4` Task 4 — validator compose passthrough + 2 prod specs + 2 dev fixtures + conftest populator call.
- `69d2e2f` Task 5 — relayer compose passthrough + prod spec + dev fixture + conftest populator call.
- `567596f` Task 6 — minio stack's `commands.py` `start()` made a no-op.
- `c034caa` design spec update — HTTP in dev, HTTPS in prod.

Tasks 3 and 6 land unchanged. Tasks 1, 2, 4, 5 each have pieces to roll
back (see Task R).

---

## File map (final target state)

**Compose & stack (SO data dir):**
- `stack_orchestrator/data/compose/docker-compose-hyperlane-validator.yml` — passthrough `AWS_ENDPOINT_URL_S3` only (no `AWS_CA_BUNDLE`, no `minio-ca-config` mount).
- `stack_orchestrator/data/compose/docker-compose-hyperlane-relayer.yml` — same.
- `stack_orchestrator/data/stacks/hyperlane-minio/deploy/commands.py` — `start()` is a no-op.
- `stack_orchestrator/data/config/minio-ca-config/` — does not exist.

**Production specs:**
- `deployment/spec-minio.yml` — `http-proxy:` includes both `s3.bridge.gorbagana.wtf` → minio:9000 and `minio-console.bridge.gorbagana.wtf` → minio:9001.
- `deployment/spec-validator-gorchain.yml`, `spec-validator-solana.yml`, `spec-relayer.yml` — `config:` includes `AWS_ENDPOINT_URL_S3: https://s3.bridge.gorbagana.wtf:443`. No `configmaps:` entry for `minio-ca-config`. No `external-services:` block.

**Test fixtures:**
- `tests/e2e/fixtures/test-spec-minio.yml` — `http-proxy:` includes both `hyperlane-minio` → minio:9000 and `minio-console.test` → minio:9001.
- `tests/e2e/fixtures/test-spec-validator-gorchain.yml`, `test-spec-validator-solana.yml`, `test-spec-relayer.yml` — `config:` includes `AWS_ENDPOINT_URL_S3: http://hyperlane-minio:80` (no `AWS_CA_BUNDLE`). `external-services:` includes `hyperlane-minio: { ip: REPLACE_HOST_IP, port: 80 }` with comment explaining dev/prod asymmetry. No `configmaps:` entry for `minio-ca-config`.

**Test code:**
- `tests/e2e/lib/cluster.py` — `TEST_HOSTNAMES` still includes `hyperlane-minio` (mkcert SAN; harmless, kept for any future HTTPS-in-dev work).
- `tests/e2e/lib/state_loader.py` — no `_resolve_caroot` or `write_mkcert_root_to_configmap`.
- `tests/e2e/test_00_cluster_helpers.py` — no tests for the removed helper.
- `tests/e2e/conftest.py` — `write_mkcert_root_to_configmap` import removed; no calls to it.

---

## Task R: Roll back dev-HTTPS plumbing; switch dev to HTTP

The current branch contains an `AWS_CA_BUNDLE` + `minio-ca-config` infrastructure that doesn't work because `aws-sdk-rust` ignores `AWS_CA_BUNDLE` (open feature request #1362). Remove that infrastructure and switch dev URLs/ports to HTTP/80. Production specs already use HTTPS (works without CA config — LE in webpki-roots) and need no changes.

**Files:**
- Modify: `stack_orchestrator/data/compose/docker-compose-hyperlane-validator.yml`
- Modify: `stack_orchestrator/data/compose/docker-compose-hyperlane-relayer.yml`
- Modify: `deployment/spec-validator-gorchain.yml`
- Modify: `deployment/spec-validator-solana.yml`
- Modify: `deployment/spec-relayer.yml`
- Modify: `tests/e2e/fixtures/test-spec-validator-gorchain.yml`
- Modify: `tests/e2e/fixtures/test-spec-validator-solana.yml`
- Modify: `tests/e2e/fixtures/test-spec-relayer.yml`
- Modify: `tests/e2e/conftest.py`
- Modify: `tests/e2e/lib/state_loader.py`
- Modify: `tests/e2e/test_00_cluster_helpers.py`
- Delete: `stack_orchestrator/data/config/minio-ca-config/.gitkeep` (and the empty dir)

- [ ] **Step R.1: Remove `AWS_CA_BUNDLE` and `minio-ca-config` mount from validator compose**

Edit `stack_orchestrator/data/compose/docker-compose-hyperlane-validator.yml`.

In the `validator` service's `environment:` block, delete these three lines:

```yaml
      # mkcert root CA bundle path (dev only). Empty in prod → SDK uses
      # the container's built-in public CA bundle for LE verification.
      AWS_CA_BUNDLE: ${AWS_CA_BUNDLE:-}
```

In the `validator` service's `volumes:` block, delete this line:

```yaml
      - minio-ca-config:/etc/ssl/certs/minio-ca:ro
```

In the bottom `volumes:` block, delete the `minio-ca-config:` entry and its comment, leaving:

```yaml
volumes:
  # agent-config: ConfigMap volume sourced from BridgeStateLoader at deploy-create
  agent-config:
  validator-data:
```

Update the comment above `AWS_ENDPOINT_URL_S3` (which currently mentions "Caddy via external-services Service" and HTTPS) to reflect the new HTTP-in-dev / HTTPS-in-prod design. Replace:

```yaml
      # MinIO endpoint — set in each spec's config: block.
      # Dev: https://hyperlane-minio:443 (Caddy via external-services Service).
      # Prod: https://s3.bridge.gorbagana.wtf:443 (Caddy via public DNS).
      AWS_ENDPOINT_URL_S3: ${AWS_ENDPOINT_URL_S3}
```

With:

```yaml
      # MinIO endpoint — set in each spec's config: block.
      # Dev: http://hyperlane-minio:80 (Caddy via external-services Service).
      # Prod: https://s3.bridge.gorbagana.wtf:443 (Caddy via public DNS).
      AWS_ENDPOINT_URL_S3: ${AWS_ENDPOINT_URL_S3}
```

- [ ] **Step R.2: Same removal in relayer compose**

Edit `stack_orchestrator/data/compose/docker-compose-hyperlane-relayer.yml`.

Same three deletions as Step R.1 (the AWS_CA_BUNDLE env block, the volume mount line, and the volumes-block entry — the relayer's bottom volumes block also has `relayer-data:` and `igp-fee-claim-scripts-config:` which stay). Same comment update on the AWS_ENDPOINT_URL_S3 block.

After the edit the bottom `volumes:` should read:

```yaml
volumes:
  # agent-config: ConfigMap volume sourced from BridgeStateLoader at deploy-create
  agent-config:
  relayer-data:
  igp-fee-claim-scripts-config:
```

- [ ] **Step R.3: Remove `minio-ca-config` from the three prod specs**

In each of:
- `deployment/spec-validator-gorchain.yml`
- `deployment/spec-validator-solana.yml`
- `deployment/spec-relayer.yml`

Delete the two lines:

```yaml
  # Empty in prod — only populated in dev with the mkcert root CA.
  minio-ca-config: ./configmaps/minio-ca-config
```

from each spec's `configmaps:` block. Leave the `agent-config:` entry (and `igp-fee-claim-scripts-config:` in the relayer spec) in place. Do not change anything else in the prod specs — the `AWS_ENDPOINT_URL_S3: "https://s3.bridge.gorbagana.wtf:443"` line stays exactly as is.

- [ ] **Step R.4: Switch dev validator fixtures to HTTP**

In each of:
- `tests/e2e/fixtures/test-spec-validator-gorchain.yml`
- `tests/e2e/fixtures/test-spec-validator-solana.yml`

In the `config:` block:
- Change `AWS_ENDPOINT_URL_S3: "https://hyperlane-minio:443"` to `AWS_ENDPOINT_URL_S3: "http://hyperlane-minio:80"`.
- Delete the line `AWS_CA_BUNDLE: "/etc/ssl/certs/minio-ca/rootCA.pem"`.
- Update the comment above `AWS_ENDPOINT_URL_S3` if it mentions HTTPS — replace any "via Caddy on 443" or similar with "via Caddy on 80".

In the `configmaps:` block, delete the two lines:

```yaml
  # Seeded with mkcert root CA by conftest (write_mkcert_root_to_configmap).
  minio-ca-config: ./configmaps/minio-ca-config
```

In the `external-services:` block, change the `hyperlane-minio:` entry's port from `443` to `80`. Update the comment to match — the gorchain fixture has a 4-line comment; replace `re-enters via Caddy on 443` with `re-enters via Caddy on 80`. (The solana fixture's comment, post-Task-4-fixup, is identical and gets the same edit.)

- [ ] **Step R.5: Switch dev relayer fixture to HTTP**

Edit `tests/e2e/fixtures/test-spec-relayer.yml`. Same shape as Step R.4:

In `config:`:
- `AWS_ENDPOINT_URL_S3: "https://hyperlane-minio:443"` → `AWS_ENDPOINT_URL_S3: "http://hyperlane-minio:80"`.
- Delete `AWS_CA_BUNDLE: "/etc/ssl/certs/minio-ca/rootCA.pem"`.
- Update the `AWS_ENDPOINT_URL_S3` comment if it references HTTPS.

In `configmaps:`, delete the `minio-ca-config` entry and its preceding comment.

In `external-services:`, the `hyperlane-minio:` port changes from `443` to `80`. Update the comment if it says `Caddy on 443`.

- [ ] **Step R.6: Switch minio test fixture's http-proxy route to port 80**

Caddy listens on 80 and 443 by default. The minio stack's dev `http-proxy:` entry for `hyperlane-minio` currently routes to `minio:9000`. That part stays — what changes is consumer-side: validators now hit Caddy via the kind gateway on port 80, and Caddy routes by Host. No Caddy config change is needed (it already serves both 80 and 443 for every configured host-name).

**No edit to `test-spec-minio.yml` is required for this step.** Verify by reading the file: `tests/e2e/fixtures/test-spec-minio.yml` should still have:

```yaml
http-proxy:
  - host-name: hyperlane-minio
    routes:
      - path: /
        proxy-to: minio:9000
  - host-name: minio-console.test
    routes:
      - path: /
        proxy-to: minio:9001
```

Confirm and move on.

- [ ] **Step R.7: Remove `write_mkcert_root_to_configmap` from conftest**

Edit `tests/e2e/conftest.py`.

Change the import line:

```python
from lib.state_loader import BridgeStateLoader, write_mkcert_root_to_configmap
```

back to:

```python
from lib.state_loader import BridgeStateLoader
```

In `_deploy_validator` (around line 921–922), delete the line:

```python
    write_mkcert_root_to_configmap(deploy_info.deploy_dir)
```

In `relayer_deployment` (around line 1085–1086), delete the equivalent line.

- [ ] **Step R.8: Remove `_resolve_caroot` and `write_mkcert_root_to_configmap` from state_loader.py**

Edit `tests/e2e/lib/state_loader.py`.

Delete both functions from the bottom of the file (they were appended as free functions in commit c667000). After the edit, the file should end with `BridgeStateLoader`'s `read_program_ids` method as it did before the commit. The module-level imports of `os` and `subprocess` that Task 2's code review moved to the top should also be removed, since they're only used by the deleted functions. Verify nothing else in the file references `os` or `subprocess` first.

`shutil` and `json` and `Path` from `pathlib` stay — they're used by `BridgeStateLoader`.

- [ ] **Step R.9: Remove the three `write_mkcert_root_to_configmap_*` unit tests**

Edit `tests/e2e/test_00_cluster_helpers.py`.

Delete the three test functions added in commit 17e61fc / refined in c667000:
- `test_write_mkcert_root_to_configmap_copies_cert`
- `test_write_mkcert_root_to_configmap_creates_parent_dir`
- `test_write_mkcert_root_to_configmap_raises_when_caroot_missing`

Also remove the `from lib.state_loader import write_mkcert_root_to_configmap` import added to the top of the file. The `import pytest` line added in commit c667000 may stay even if no remaining test uses it; verify the existing `write_caddy_cert_backup` tests still pass without modification.

- [ ] **Step R.10: Delete the empty `minio-ca-config` source dir**

```bash
git rm stack_orchestrator/data/config/minio-ca-config/.gitkeep
rmdir stack_orchestrator/data/config/minio-ca-config 2>/dev/null || true
```

- [ ] **Step R.11: Run the cluster-helper unit tests**

```bash
cd tests/e2e
pytest test_00_cluster_helpers.py -v
```

Expected: 2 passed (the original `write_caddy_cert_backup` tests). No collection errors, no remaining references to the deleted helper.

- [ ] **Step R.12: Verify the old FQDN literal is still absent and there are no broken references**

```bash
grep -rn "hyperlane-minio.laconic-hyperlane-minio" \
  stack_orchestrator/ deployment/ tests/e2e/ 2>&1
echo "---"
grep -rn "AWS_CA_BUNDLE\|minio-ca-config\|write_mkcert_root_to_configmap\|_resolve_caroot" \
  stack_orchestrator/ deployment/ tests/e2e/ docs/ 2>&1
```

First grep expected: no matches.
Second grep expected: matches only in docs (the spec and this plan reference the removed names while explaining what changed); no matches in stack_orchestrator/, deployment/, or tests/e2e/.

- [ ] **Step R.13: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
tests + specs: switch dev MinIO to HTTP (aws-sdk-rust ignores AWS_CA_BUNDLE)

aws-sdk-rust does not honor AWS_CA_BUNDLE (awslabs/aws-sdk-rust#1362),
so the mkcert-root distribution via minio-ca-config ConfigMap silently
did nothing. Switch dev consumers to http://hyperlane-minio:80 through
Caddy; prod keeps https://s3.bridge.gorbagana.wtf:443 (LE trusted by
the SDK's built-in webpki-roots).
EOF
)"
```

---

## Task 7: Full e2e verification

Same as the original plan — controller runs against real infrastructure after Task R lands. Read the previous plan revision for the verification commands. The only differences vs. the originally-written §7 are:

- Step 4 (env-var verification) should expect `AWS_ENDPOINT_URL_S3=http://hyperlane-minio:80` in validator/relayer pods, not the previous HTTPS URL. There is no longer an `AWS_CA_BUNDLE` env var to check.
- Step 5 (mkcert root CA presence in pods) is dropped — no `minio-ca-config` mount exists anymore.

Verification commands:

- [ ] **Step 7.1: Run the e2e suite that exercises the MinIO data path**

```bash
cd tests/e2e
pytest test_03_minio.py test_04_validator.py test_05_relayer.py test_08_bridge.py -v -s
```

Expected: all tests PASS.

- [ ] **Step 7.2: Confirm the old FQDN literal is gone from the source tree**

```bash
grep -rn "hyperlane-minio.laconic-hyperlane-minio.svc.cluster.local" \
  stack_orchestrator/data/compose/ deployment/ tests/e2e/fixtures/
```

Expected: no matches.

- [ ] **Step 7.3: Confirm the cross-stack Service is gone in dev**

With the cluster still alive (`--skip-cleanup`):

```bash
kubectl -n laconic-hyperlane-minio get svc -o name
```

Expected: only the per-pod `service/minio` (created by SO from the compose service definition). No `service/hyperlane-minio`.

- [ ] **Step 7.4: Confirm validator + relayer pods carry the new endpoint env**

```bash
for ns in laconic-hyperlane-validator-gorchain \
          laconic-hyperlane-validator-solana \
          laconic-hyperlane-relayer; do
  echo "=== $ns ==="
  kubectl -n "$ns" get pods -o jsonpath='{.items[0].metadata.name}' | \
    xargs -I{} sh -c 'kubectl -n '"$ns"' exec "$1" -c validator -- env 2>/dev/null \
      || kubectl -n '"$ns"' exec "$1" -c relayer -- env' _ {} | \
    grep -E '^AWS_ENDPOINT_URL_S3='
done
```

Expected for each NS:
```
AWS_ENDPOINT_URL_S3=http://hyperlane-minio:80
```

- [ ] **Step 7.5: No commit needed — verification only**

If any step fails, return to Task R or the relevant earlier task.

---

## Self-review notes

Coverage against the updated spec:

- §3 architecture diagram — Task R brings the implementation in line with the diagram (HTTP scheme + port 80 in dev).
- §4.1 (Caddy fronts both envs) — unchanged from already-landed Task 3.
- §4.2 (external-services dev-only, port 80) — Task R.4 + R.5 set the dev port and comment correctly.
- §4.3 (HTTP in dev, HTTPS in prod) — Task R is the implementation of this decision.
- §4.4 (cross-stack Service removed) — already done in Task 6 / commit 567596f.
- §4.5 (spec-driven endpoint URL) — already done in Tasks 4 + 5; Task R only changes the *value* the spec supplies.
- §5 file list — Task R's file list matches §5's "final target state" file list.
- §6 verification — Task 7 implements all four bullets.
- §7 (out of scope, deferred to PR2) — honored; nothing in Task R touches per-validator users.
- §8 alternatives — the spec now records why fork-patching the SDK or installing the mkcert root into the system trust store were considered and rejected.
