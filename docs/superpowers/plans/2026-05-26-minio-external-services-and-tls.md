# MinIO External-Services + Caddy TLS Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut MinIO consumers (both validators, relayer) over from the cross-namespace FQDN to `external-services:`. Production reaches MinIO through Caddy over HTTPS via public DNS (LE cert trusted by the SDK's built-in webpki-roots). Dev reaches MinIO directly via cross-NS pod-IP discovery (selector-mode `external-services:`), bypassing Caddy entirely — Caddy v2 308-redirects HTTP to HTTPS, and aws-sdk-rust does not honor `AWS_CA_BUNDLE` (awslabs/aws-sdk-rust#1362), so neither a "plain HTTP through Caddy" nor an "HTTPS through Caddy with mkcert trusted" path works without a fork patch.

**Architecture:** In dev, each consumer's `external-services:` entry uses selector mode (`app.kubernetes.io/stack: hyperlane-minio`, `namespace: laconic-hyperlane-minio`, `port: 9000`) to create a headless Service in the consumer's namespace with Endpoints populated from cross-NS pod-IP discovery at deploy time. Validator dials `http://hyperlane-minio:9000` and lands directly on MinIO. In prod, no `external-services:` entry — the URL `https://s3.bridge.gorbagana.wtf` resolves via cluster DNS forwarding to upstream resolvers, traffic enters the prod cluster via Caddy, and Caddy reverse_proxies to MinIO.

**Tech Stack:** docker-compose, laconic-so, kubernetes, Caddy ingress (prod only on this leg), AWS Signature v4 / S3 path-style addressing, pytest.

---

## Current branch state (commits already landed)

The first six tasks (now plus Task R) have been implemented on this branch.

Iteration 1 — Tasks 1, 2, 4, 5 introduced `AWS_CA_BUNDLE` + `minio-ca-config`
plumbing to test HTTPS in dev. Verification found `aws-sdk-rust` does not
honor `AWS_CA_BUNDLE`. Task R rolled that back.

Iteration 2 — Task R's commit shape used `external-services: { ip:
REPLACE_HOST_IP, port: 80 }` plus dev URL `http://hyperlane-minio:80`.
A subsequent empirical test confirmed Caddy v2 308-redirects HTTP to
HTTPS — AWS SDK won't follow that on signed S3 requests. **Task R2 below
switches dev to selector-mode `external-services:` that bypasses Caddy
entirely, using port 9000 directly on the MinIO pod.**

Tasks 1–6 + R already committed (in commit order):

- `67e8d0b` Task 1 — stub `minio-ca-config` source dir + `hyperlane-minio` in TEST_HOSTNAMES.
- `17e61fc` + `c667000` Task 2 — `write_mkcert_root_to_configmap` helper + unit tests + code-review fixes.
- `8c04f65` Task 3 — minio http-proxy host entries (`hyperlane-minio` in dev, `s3.bridge.gorbagana.wtf` in prod, both → `minio:9000`).
- `17325a6` + `0203ff4` Task 4 — validator compose passthrough + 2 prod specs + 2 dev fixtures + conftest populator call.
- `69d2e2f` Task 5 — relayer compose passthrough + prod spec + dev fixture + conftest populator call.
- `567596f` Task 6 — minio stack's `commands.py` `start()` made a no-op.
- `c034caa` design spec update (iteration 1 → 2): HTTPS replaced with HTTP-in-dev.
- `eada206` plan update + Task R definition.
- `2857cb6` Task R — removed AWS_CA_BUNDLE / minio-ca-config / write_mkcert_root_to_configmap; switched dev to `http://hyperlane-minio:80`.
- `c553fc2` design spec update (iteration 2 → 3): HTTP-through-Caddy replaced with cross-NS-direct.

Task R2 (below) implements the iteration-3 design. Task 6 and the prod-side
of Tasks 3, 4, 5 land unchanged after R2.

---

## File map (final target state after Task R2)

**Compose & stack (SO data dir):**
- `stack_orchestrator/data/compose/docker-compose-hyperlane-validator.yml` — passthrough `AWS_ENDPOINT_URL_S3` only.
- `stack_orchestrator/data/compose/docker-compose-hyperlane-relayer.yml` — same.
- `stack_orchestrator/data/stacks/hyperlane-minio/deploy/commands.py` — `start()` is a no-op.

**Production specs:**
- `deployment/spec-minio.yml` — `http-proxy:` includes both `s3.bridge.gorbagana.wtf` → minio:9000 and `minio-console.bridge.gorbagana.wtf` → minio:9001.
- `deployment/spec-validator-gorchain.yml`, `spec-validator-solana.yml`, `spec-relayer.yml` — `config:` includes `AWS_ENDPOINT_URL_S3: https://s3.bridge.gorbagana.wtf`. No `external-services:` block.

**Test fixtures:**
- `tests/e2e/fixtures/test-spec-minio.yml` — `http-proxy:` includes only `minio-console.test` → minio:9001. No `hyperlane-minio` host (consumers reach MinIO directly in dev, not via Caddy).
- `tests/e2e/fixtures/test-spec-validator-gorchain.yml`, `test-spec-validator-solana.yml`, `test-spec-relayer.yml` — `config:` includes `AWS_ENDPOINT_URL_S3: http://hyperlane-minio:9000`. `external-services:` includes:
  ```yaml
  hyperlane-minio:
    selector:
      app.kubernetes.io/stack: hyperlane-minio
    namespace: laconic-hyperlane-minio
    port: 9000
  ```
  with comment explaining dev/prod asymmetry.

**Test code:**
- `tests/e2e/lib/cluster.py` — `TEST_HOSTNAMES` does NOT include `hyperlane-minio` (no longer reached via Caddy in dev, so mkcert SAN coverage is moot).
- `tests/e2e/lib/state_loader.py` — no `_resolve_caroot` or `write_mkcert_root_to_configmap`.
- `tests/e2e/test_00_cluster_helpers.py` — no tests for the removed helper.
- `tests/e2e/conftest.py` — no references to `write_mkcert_root_to_configmap`.

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

## Task R2: Switch dev MinIO to selector-mode external-services (bypass Caddy)

Caddy v2 308-redirects HTTP to HTTPS for any hostname-based site, so dialing `http://hyperlane-minio:80` from a validator pod fails (AWS SDK doesn't follow redirects on signed S3 requests). Switch dev to direct cross-NS routing using `external-services:` selector mode targeting MinIO pods on port 9000. Caddy is no longer in the dev validator→MinIO data path; the minio test fixture's `hyperlane-minio` http-proxy entry is removed; the `hyperlane-minio` mkcert SAN entry is removed.

Also drop the explicit `:443` port from prod URLs (`https://` already implies 443; the port is redundant).

**Files:**
- Modify: `tests/e2e/fixtures/test-spec-minio.yml`
- Modify: `tests/e2e/fixtures/test-spec-validator-gorchain.yml`
- Modify: `tests/e2e/fixtures/test-spec-validator-solana.yml`
- Modify: `tests/e2e/fixtures/test-spec-relayer.yml`
- Modify: `tests/e2e/lib/cluster.py`
- Modify: `stack_orchestrator/data/compose/docker-compose-hyperlane-validator.yml`
- Modify: `stack_orchestrator/data/compose/docker-compose-hyperlane-relayer.yml`
- Modify: `deployment/spec-validator-gorchain.yml`
- Modify: `deployment/spec-validator-solana.yml`
- Modify: `deployment/spec-relayer.yml`

- [ ] **Step R2.1: Remove `hyperlane-minio` host from the minio test fixture**

Edit `tests/e2e/fixtures/test-spec-minio.yml`. The current `http-proxy:` block contains two entries — `hyperlane-minio` → `minio:9000` and `minio-console.test` → `minio:9001`. Delete the first entry. The block should become:

```yaml
network:
  acme-email: e2e@example.test
  http-proxy:
    - host-name: minio-console.test
      routes:
        - path: /
          proxy-to: minio:9001
```

Leave the prod minio spec (`deployment/spec-minio.yml`) UNCHANGED — `s3.bridge.gorbagana.wtf` stays in its `http-proxy:` (prod still uses Caddy).

- [ ] **Step R2.2: Switch dev validator fixtures to selector-mode + port 9000**

Edit each of:
- `tests/e2e/fixtures/test-spec-validator-gorchain.yml`
- `tests/e2e/fixtures/test-spec-validator-solana.yml`

In the `config:` block, replace `AWS_ENDPOINT_URL_S3: "http://hyperlane-minio:80"` with `AWS_ENDPOINT_URL_S3: "http://hyperlane-minio:9000"`. Update any nearby comment that references "Caddy on 80" or "external-services Service" to say "direct to MinIO":

```yaml
  # Dev only — direct cross-NS to the MinIO pod via external-services
  # selector mode below. No Caddy in this path.
  AWS_ENDPOINT_URL_S3: "http://hyperlane-minio:9000"
```

In the `external-services:` block, replace the current `hyperlane-minio:` entry (which has `ip: REPLACE_HOST_IP, port: 80` and a multi-line "Caddy on 80" comment) with:

```yaml
  # Dev only — prod uses public DNS to resolve s3.bridge.gorbagana.wtf.
  # Selector mode: SO creates a headless Service named `hyperlane-minio`
  # in this NS with Endpoints discovered from MinIO pods in the
  # laconic-hyperlane-minio namespace at deploy time. Pod restart
  # invalidates the Endpoints; tests do full re-deploys, so this is fine
  # in dev. Prod uses the Caddy-fronted public hostname instead.
  hyperlane-minio:
    selector:
      app.kubernetes.io/stack: hyperlane-minio
    namespace: laconic-hyperlane-minio
    port: 9000
```

- [ ] **Step R2.3: Switch dev relayer fixture to selector-mode + port 9000**

Edit `tests/e2e/fixtures/test-spec-relayer.yml`. Same two changes as Step R2.2 (config: URL block and external-services: hyperlane-minio entry).

- [ ] **Step R2.4: Remove `hyperlane-minio` from TEST_HOSTNAMES**

Edit `tests/e2e/lib/cluster.py`. Delete the `"hyperlane-minio",` entry from the `TEST_HOSTNAMES` tuple. After the edit the tuple is back to its pre-Task-1 form:

```python
TEST_HOSTNAMES: tuple[str, ...] = (
    "bridge.test",
    "grafana.test",
    "prometheus.test",
    "validator-gorchain.test",
    "validator-solana.test",
    "relayer.test",
    "minio-console.test",
)
```

- [ ] **Step R2.5: Update compose comments to reflect the new dev URL**

Edit each of:
- `stack_orchestrator/data/compose/docker-compose-hyperlane-validator.yml`
- `stack_orchestrator/data/compose/docker-compose-hyperlane-relayer.yml`

The current comment above `AWS_ENDPOINT_URL_S3: ${AWS_ENDPOINT_URL_S3}` mentions "Dev: http://hyperlane-minio:80 (Caddy via external-services Service)." Update to:

```yaml
      # MinIO endpoint — set in each spec's config: block.
      # Dev: http://hyperlane-minio:9000 (direct cross-NS via external-services selector).
      # Prod: https://s3.bridge.gorbagana.wtf (Caddy via public DNS, LE cert trusted by webpki-roots).
      AWS_ENDPOINT_URL_S3: ${AWS_ENDPOINT_URL_S3}
```

No other changes to compose. `AWS_CA_BUNDLE` was already removed in Task R; `minio-ca-config` volume mount was already removed in Task R.

- [ ] **Step R2.6: Drop `:443` from prod URLs**

Edit each of:
- `deployment/spec-validator-gorchain.yml`
- `deployment/spec-validator-solana.yml`
- `deployment/spec-relayer.yml`

In each `config:` block, change:

```yaml
  AWS_ENDPOINT_URL_S3: "https://s3.bridge.gorbagana.wtf:443"
```

to:

```yaml
  AWS_ENDPOINT_URL_S3: "https://s3.bridge.gorbagana.wtf"
```

This is purely cosmetic — `https://` already implies port 443 — but removes confusing redundant port specification.

- [ ] **Step R2.7: Run the cluster-helper unit tests as a sanity check**

```bash
cd tests/e2e && pytest test_00_cluster_helpers.py -v --noconftest 2>&1 | tail -10
```

(The `--noconftest` flag is needed only if the host has the pre-existing pydantic env issue mentioned in Task R's report. If conftest loads cleanly, drop the flag.)

Expected: 2 passed. This task doesn't add or remove tests; just confirming the unit-test surface still works.

- [ ] **Step R2.8: Sanity grep**

```bash
cd /home/dev/git_puller/repos/hyperlane-stacks

echo "=== old FQDN literal (expect: no matches) ==="
grep -rn "hyperlane-minio.laconic-hyperlane-minio" \
  stack_orchestrator/ deployment/ tests/e2e/

echo "=== dev URL is :9000 in fixtures (expect: 3 matches) ==="
grep -rn "AWS_ENDPOINT_URL_S3" tests/e2e/fixtures/

echo "=== prod URL has no port (expect: 3 matches, no :443) ==="
grep -rn "AWS_ENDPOINT_URL_S3" deployment/

echo "=== port 80 references in fixtures (expect: no matches) ==="
grep -rn ":80\b\|port: 80\b" tests/e2e/fixtures/

echo "=== TEST_HOSTNAMES no longer has hyperlane-minio (expect: no match) ==="
grep -n "hyperlane-minio" tests/e2e/lib/cluster.py
```

Expectations: see the echo labels.

- [ ] **Step R2.9: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
tests + specs: dev MinIO uses selector-mode external-services (bypass Caddy)

Caddy v2 308-redirects HTTP to HTTPS for any hostname-based site, so
the previous dev plan (validator → http://hyperlane-minio:80 → Caddy
→ minio:9000) fails — AWS SDK doesn't follow redirects on signed S3
requests. Switch the dev external-services entry to selector mode
pointing at the MinIO pod in laconic-hyperlane-minio NS on port 9000;
validators dial http://hyperlane-minio:9000 directly. Caddy stays in
the prod path (HTTPS via public DNS, LE trusted by webpki-roots).
Also drop the redundant :443 from prod URLs.
EOF
)"
```

---

## Task 7: Full e2e verification

Same as the original plan — controller runs against real infrastructure after Task R lands. Read the previous plan revision for the verification commands. The only differences vs. the originally-written §7 are:

- Step 4 (env-var verification) should expect `AWS_ENDPOINT_URL_S3=http://hyperlane-minio:9000` in validator/relayer pods, not the previous HTTPS URL. There is no longer an `AWS_CA_BUNDLE` env var to check.
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
AWS_ENDPOINT_URL_S3=http://hyperlane-minio:9000
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
