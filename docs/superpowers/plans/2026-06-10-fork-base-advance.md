# Fork Base Advance to Agents v2.2.0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the monorepo fork's history on upstream `4da9c4419a` (agents v2.2.0), fold the two docker patches into fork commits, tag the result, and point the stacks + CI at the fork instead of upstream-plus-patches.

**Architecture:** Spec §4.0 + D1/D2/D8 (`docs/superpowers/specs/2026-06-10-websocket-fast-bridging-design.md`). The fork (`gorbagana-dev/hyperlane-monorepo`, private) currently sits at old base `16c056a09a` + 2 commits (CI prune `82ab03c064`, Ledger signing `a2f6ef11b0`); the agent image meanwhile builds from **upstream** `4da9c4419a` + 2 build-time patches, and the deployer images from **upstream** `16c056a09a`. After this work everything builds from one fork tag: new local branch at `4da9c4419a` + recreated CI prune + cherry-picked Ledger commit + the two patches as commits, tagged `v2.2.0-gorbagana.1`. WS work (`hyp-d34.3`) lands on top of this.

**Tech Stack:** git surgery (hyperlane-monorepo checkout), Dockerfile/bash (hyperlane-stacks container-build), GitHub Actions YAML, SO stack.yml pins.

**Repos/branches:** monorepo work in `/home/dev/git_puller/repos/hyperlane-monorepo`, delivered as a single local branch `fold-docker-patches` = base `4da9c4419a` + recreated CI prune (`5e210f3d30`) + cherry-picked Ledger (`18c2ecfcf6`) + the two NEW patch commits. The user moves the default `gorbagana` branch ahead themselves (to `18c2ecfcf6`, the already-reviewed lineage) and PRs the two new patch commits from `fold-docker-patches` (fork rule: changes land via PRs, never directly on the default branch). Leave local `gorbagana` untouched. NO local tags — the user cuts `v2.2.0-gorbagana.1` via the GitHub release UI after the PR merges. Stacks work in `/home/dev/git_puller/repos/hyperlane-stacks` on the existing `fast-bridging-design` branch. NEVER push anywhere.

**Shared dev machine:** No cargo builds, no docker builds, no test suites here — image builds are verified by CI after the user pushes. Local verification is git/grep/yaml only.

**Pebble:** `hyp-d34.2` — mark `in_progress` at start; closure happens only after the user's CI builds succeed.

---

## Verified facts (do not re-derive)

1. **Fork state:** `gorbagana` = `16c056a09a` + `82ab03c064` (ci prune) + `a2f6ef11b0` (Ledger signing). Remotes: `gorbagana-dev.github.com` (fork, private — unauthenticated https clone fails), `hyperlane-xyz.github.com` (upstream). `4da9c4419a` = upstream `agents-v2.2.0` tag, present locally.
2. **CI prune cannot be cherry-picked cleanly:** upstream Modified/Deleted/Added many workflow files in `16c056a09a..4da9c4419a` (M on most files the prune deleted; A: `ghcr-cleanup.yml`, `spellcheck.yml`, `test-cli-e2e.yml`, `test-coverage.yml`, `test-env.yml`, `test-rust-e2e.yml`, `test-sdk-e2e.yml`, …). Recreate the prune at the new base instead: after `82ab03c064` the fork's `.github/workflows/` contains exactly one file, `rust.yml` (fork-trimmed). The prune commit touched ONLY `.github/workflows/`.
3. **Ledger commit cherry-picks clean:** `a2f6ef11b0` touches `rust/sealevel/client/{Cargo.toml,src/context.rs,src/main.rs,src/signer.rs}`, `rust/sealevel/Cargo.lock`, and adds `.github/workflows/sealevel-client-release.yml` + `sealevel-client-release-dryrun.yml`. Upstream did not touch `rust/sealevel/client` in range. (Cargo.lock may still conflict — resolution below.)
4. **Patches:** `stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-agent/{kms-endpoint,s3-path-style}.patch` target `rust/main/hyperlane-base/src/settings/signers.rs` and `rust/main/hyperlane-base/src/types/s3_storage.rs`; both files saw only chore/tron churn in range (verified — patches expected to apply). Applied in the Dockerfile (`COPY …patch /tmp/` + `RUN cd /usr/src && git apply …`), with `build.sh` copying the patches into the build context first.
5. **Stack pins today:** `hyperlane-validator/stack.yml` → upstream`@4da9c4419a…`; `hyperlane-svm-deployer/stack.yml` and `hyperlane-svm-warp-deployer/stack.yml` → upstream`@16c056a09a…`. SO `setup-repositories` supports tags as the `@ref` (falls back to `git describe --tags --exact-match` — `setup_repositories.py:94-100`).
6. **Deployer image survives the base bump:** in range, `rust/sealevel` changed only `programs/build-programs.sh` (adds `ism/test-ism` to CORE_PROGRAM_PATHS → one extra harmless `.so` in the image), deletes `programs/install-solana-1.14.20.sh` (unused by our Dockerfile), `mailbox/src/processor.rs` (±4 lines, `handle_intruction`→`handle_instruction` typo rename), `Cargo.lock` metadata, and an env config json.
7. **CI fork-clone auth pattern** already exists in this repo: `publish-images.yml:276-279` (warp-ui job) — `git config --global url."https://x-access-token:${{ secrets.CICD_REPO_TOKEN_TEMP }}@github.com/gorbagana-dev/".insteadOf "https://github.com/gorbagana-dev/"`, with a TODO to revert to `CICD_REPO_TOKEN` once fork access is approved. `e2e.yml:30` uses the same rewrite with `CICD_REPO_TOKEN`.
8. **Image publish trigger:** appending a line to `.github/trigger-publish-agent.txt` / `trigger-publish-deployer.txt` (merged to main) fires the respective build job.

---

### Task 1: Rebuild the fork branch on the new base (monorepo)

**Repo:** `/home/dev/git_puller/repos/hyperlane-monorepo`

- [ ] **Step 1: Preconditions**

```bash
cd /home/dev/git_puller/repos/hyperlane-monorepo
git status --porcelain
git tag -l '*gorbagana*'
```
Expected: empty status (if dirty, STOP and report — do not stash someone else's work); no existing `*gorbagana*` tags (if one exists, report it — the new tag's `.N` may need bumping).

- [ ] **Step 2: New branch at the v2.2.0 base**

```bash
git switch -c gorbagana-v2.2.0 4da9c4419ad89783fd005e0b776f1fdcd7b59c12
```

- [ ] **Step 3: Recreate the CI prune at the new base**

```bash
git rm -r -q .github/workflows
git checkout 82ab03c064 -- .github/workflows
git status --porcelain | head
```
Expected status: deletions of all upstream workflow files plus `.github/workflows/rust.yml` staged (the fork-trimmed copy). Commit:

```bash
git commit -m "ci: remove upstream Hyperlane workflows not applicable to this fork

Recreates the prune originally done in 82ab03c064 on the previous base;
upstream added/modified workflows in between, so the removal is re-applied
wholesale: everything goes except the fork-trimmed rust.yml.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

- [ ] **Step 4: Verify the prune end-state matches the old fork exactly**

```bash
git diff 82ab03c064 HEAD -- .github/workflows
git ls-tree --name-only HEAD .github/workflows/
```
Expected: empty diff; listing shows only `rust.yml`.

- [ ] **Step 5: Cherry-pick the Ledger signing commit**

```bash
git cherry-pick -x a2f6ef11b0
```
If `rust/sealevel/Cargo.lock` conflicts: take the upstream (HEAD) version and re-add only the hunk the Ledger commit introduced — inspect with `git show a2f6ef11b0 -- rust/sealevel/Cargo.lock` (it adds 3 lines for the client's new deps); apply those additions to the conflicted file, `git add`, `git cherry-pick --continue`. No other file may conflict (upstream didn't touch `rust/sealevel/client` in range) — if one does, STOP and report.

- [ ] **Step 6: Verify the Ledger content carried over identically**

```bash
git diff gorbagana HEAD -- rust/sealevel/client .github/workflows/sealevel-client-release.yml .github/workflows/sealevel-client-release-dryrun.yml
```
Expected: empty diff (the fork's Ledger work is byte-identical on the new base).

---

### Task 2: Fold the docker patches into fork commits (monorepo)

**Repo:** `/home/dev/git_puller/repos/hyperlane-monorepo`, branch `gorbagana-v2.2.0` (continuing Task 1)

- [ ] **Step 1: Confirm each patch is still needed at v2.2.0**

```bash
grep -n "AWS_ENDPOINT_URL_KMS" rust/main/hyperlane-base/src/settings/signers.rs || echo "KMS patch still needed"
grep -n "AWS_ENDPOINT_URL_S3\|force_path_style" rust/main/hyperlane-base/src/types/s3_storage.rs || echo "S3 patch still needed"
```
Expected: both `still needed` lines (upstream gained no equivalent). If upstream DID add equivalent handling, skip that patch's commit and note it in the report.

- [ ] **Step 2: Apply and commit the KMS endpoint patch**

```bash
git apply --check /home/dev/git_puller/repos/hyperlane-stacks/stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-agent/kms-endpoint.patch && echo APPLIES
git apply /home/dev/git_puller/repos/hyperlane-stacks/stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-agent/kms-endpoint.patch
git add rust/main/hyperlane-base/src/settings/signers.rs
git commit -m "feat(base): honor AWS_ENDPOINT_URL_KMS for the AWS KMS signer

rusoto_core::Region::from_str only produces named variants and cannot
express a custom endpoint, so wrap the region as Region::Custom when
AWS_ENDPOINT_URL_KMS is set (e.g. a local KMS proxy sidecar).

Previously applied as a docker build-time patch; now a fork commit.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```
If `--check` fails, retry with `git apply -3 <patch>` (3-way against the blobs); if that also fails, STOP and report the rejects.

- [ ] **Step 3: Apply and commit the S3 path-style patch**

```bash
git apply --check /home/dev/git_puller/repos/hyperlane-stacks/stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-agent/s3-path-style.patch && echo APPLIES
git apply /home/dev/git_puller/repos/hyperlane-stacks/stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-agent/s3-path-style.patch
git add rust/main/hyperlane-base/src/types/s3_storage.rs
git commit -m "feat(base): custom endpoint + path-style addressing for S3 storage

aws-config 1.1.7 does not read AWS_ENDPOINT_URL / AWS_ENDPOINT_URL_S3 from
the environment, so set the endpoint programmatically; S3-compatible
stores like MinIO also require path-style addressing because
virtual-hosted-style needs wildcard DNS.

Previously applied as a docker build-time patch; now a fork commit.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

- [ ] **Step 4: Verify the working tree equals base + exactly our five deltas**

```bash
git status --porcelain          # must be empty
git log --oneline 4da9c4419a..HEAD
```
Expected log (newest first): s3 path-style, kms endpoint, Ledger signing (cherry-pick), ci prune — 4 commits.

```bash
git diff 4da9c4419a HEAD --stat
```
Expected: only `.github/workflows/*`, `rust/sealevel/client/*`, `rust/sealevel/Cargo.lock`, `rust/main/hyperlane-base/src/settings/signers.rs`, `rust/main/hyperlane-base/src/types/s3_storage.rs`.

---

### Task 3: Branch split for PR delivery (monorepo) — NO tagging

**Repo:** `/home/dev/git_puller/repos/hyperlane-monorepo`

- [ ] **Step 1: Single PR branch, no intermediate branch**

The four commits live on one local branch `fold-docker-patches` (base `4da9c4419a` + CI prune + Ledger + the 2 patch commits). The user moves the default `gorbagana` branch ahead to `18c2ecfcf6` themselves; the PR then carries only the 2 new patch commits.

- [ ] **Step 2: Do NOT create any tag**

The user cuts `v2.2.0-gorbagana.1` manually via the GitHub release UI after the `fold-docker-patches` PR merges (pre-merge local tags would point at dead history after a squash/rebase merge, and tags anchor releases).

- [ ] **Step 3: Report the handoff facts**

Record in the final report (the user pushes; we never do): branch `fold-docker-patches`, the boundary SHA `18c2ecfcf6` for the default-branch advance, tag `v2.2.0-gorbagana.1` to be cut via release UI post-merge.

---

### Task 4: Remove the patch machinery from the agent container build (hyperlane-stacks)

**Repo:** `/home/dev/git_puller/repos/hyperlane-stacks`, branch `fast-bridging-design`

**Files:**
- Modify: `stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-agent/Dockerfile`
- Modify: `stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-agent/build.sh`
- Delete: `stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-agent/kms-endpoint.patch`
- Delete: `stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-agent/s3-path-style.patch`

- [ ] **Step 1: Dockerfile — drop the patch steps and update the header**

Remove this block (after the workspace COPY lines):
```dockerfile
# Apply patches:
# 1. KMS endpoint — redirect rusoto KMS calls via AWS_ENDPOINT_URL_KMS
# 2. S3 path-style — use path-style addressing for S3-compatible stores (MinIO)
COPY stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-agent/kms-endpoint.patch /tmp/
COPY stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-agent/s3-path-style.patch /tmp/
RUN cd /usr/src && git apply /tmp/kms-endpoint.patch /tmp/s3-path-style.patch
```

Replace the file header comment:
```dockerfile
# gorbagana-dev-hyperlane-agent
# Patched build of hyperlane agent with two fixes for S3-compatible / custom endpoints:
# 1. KMS endpoint — rusoto_kms ignores AWS_ENDPOINT_URL_KMS; patch uses Region::Custom
# 2. S3 path-style — force path-style addressing when AWS_ENDPOINT_URL is set (MinIO)
#
# Build context: ~/cerc/hyperlane-monorepo (or equivalent)
# Invoked via: docker build -f <this-file> ~/cerc/hyperlane-monorepo
```
with:
```dockerfile
# gorbagana-dev-hyperlane-agent
# Build of the hyperlane agent from the gorbagana-dev fork; the KMS-endpoint
# and S3-path-style fixes formerly applied here as patches are fork commits.
#
# Build context: ~/cerc/hyperlane-monorepo (or equivalent)
# Invoked via: docker build -f <this-file> ~/cerc/hyperlane-monorepo
```

Also: the build clears sccache before building specifically because of the patches ("sccache may serve stale objects from prior unpatched builds") — KEEP that clearing logic; it remains correct across ref changes.

- [ ] **Step 2: build.sh — drop the patch-copy plumbing**

Replace the whole file body so it reads:
```bash
#!/usr/bin/env bash
source ${CERC_CONTAINER_BASE_DIR}/build-base.sh

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

MONOREPO_DIR="${CERC_REPO_BASE_DIR}/hyperlane-monorepo"

DOCKER_BUILDKIT=1 docker build -t gorbagana-dev/hyperlane-agent:local \
  -f ${SCRIPT_DIR}/Dockerfile \
  ${build_command_args} \
  "${MONOREPO_DIR}"
```
(Removes the patch `cp` block and the `rm -rf "${MONOREPO_DIR}/stack_orchestrator"` cleanup that existed only for the patch COPYs.)

- [ ] **Step 3: Delete the patch files**

```bash
git rm stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-agent/kms-endpoint.patch \
       stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-agent/s3-path-style.patch
```

- [ ] **Step 4: Verify no references remain**

```bash
grep -rn "kms-endpoint.patch\|s3-path-style.patch" --include="*" -l . 2>/dev/null | grep -v ".git/" | grep -v "docs/superpowers" | grep -v ".pebbles"
bash -n stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-agent/build.sh && echo SYNTAX_OK
```
Expected: no file hits outside docs/pebbles history; `SYNTAX_OK`.

- [ ] **Step 5: Commit**

```bash
git add stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-agent/
git commit -m "build(agent): drop build-time patches — fixes live in the fork now

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Repoint stack pins to the fork tag + CI fork-clone auth (hyperlane-stacks)

**Files:**
- Modify: `stack_orchestrator/data/stacks/hyperlane-validator/stack.yml:5-6`
- Modify: `stack_orchestrator/data/stacks/hyperlane-svm-deployer/stack.yml:5-6`
- Modify: `stack_orchestrator/data/stacks/hyperlane-svm-warp-deployer/stack.yml:5-6`
- Modify: `.github/workflows/publish-images.yml` (build-deployer + build-agent jobs)

- [ ] **Step 1: Validator stack pin**

Replace:
```yaml
  # agents-v2.2.0 — patched at build time with AWS_ENDPOINT_URL_KMS support
  - github.com/hyperlane-xyz/hyperlane-monorepo@4da9c4419ad89783fd005e0b776f1fdcd7b59c12
```
with:
```yaml
  # fork of agents-v2.2.0 (4da9c4419a) + KMS-endpoint/S3-path-style/Ledger commits
  - github.com/gorbagana-dev/hyperlane-monorepo@v2.2.0-gorbagana.1
```

- [ ] **Step 2: Deployer + warp-deployer stack pins**

In BOTH `hyperlane-svm-deployer/stack.yml` and `hyperlane-svm-warp-deployer/stack.yml`, replace:
```yaml
  # @hyperlane-xyz/core@10.2.0 — includes Solana SDK v3.x migration
  - github.com/hyperlane-xyz/hyperlane-monorepo@16c056a09af862b3ce9e14bd3b5b8034750af9d0
```
with:
```yaml
  # fork of agents-v2.2.0 (4da9c4419a); contracts semantically unchanged from
  # @hyperlane-xyz/core@10.2.0 in range, client gains built-in Ledger signing
  - github.com/gorbagana-dev/hyperlane-monorepo@v2.2.0-gorbagana.1
```

- [ ] **Step 3: CI auth for the private fork clone**

In `.github/workflows/publish-images.yml`, in BOTH the `build-deployer` and `build-agent` jobs, insert this step directly BEFORE their `- name: Setup repositories` step (mirroring the warp-ui job at line ~276, same secret + same TODO):

```yaml
      # TODO: revert to CICD_REPO_TOKEN once its access to the fork is approved;
      # using CICD_REPO_TOKEN_TEMP meanwhile (CICD_REPO_TOKEN lacks fork access).
      - name: Authenticate fork clones
        run: git config --global url."https://x-access-token:${{ secrets.CICD_REPO_TOKEN_TEMP }}@github.com/gorbagana-dev/".insteadOf "https://github.com/gorbagana-dev/"
```

- [ ] **Step 4: Validate YAML**

```bash
python3 - <<'EOF'
import yaml
for f in ("stack_orchestrator/data/stacks/hyperlane-validator/stack.yml",
          "stack_orchestrator/data/stacks/hyperlane-svm-deployer/stack.yml",
          "stack_orchestrator/data/stacks/hyperlane-svm-warp-deployer/stack.yml",
          ".github/workflows/publish-images.yml"):
    yaml.safe_load(open(f))
    print("OK", f)
EOF
```
Expected: four OK lines.

- [ ] **Step 5: Commit**

```bash
git add stack_orchestrator/data/stacks/hyperlane-validator/stack.yml \
        stack_orchestrator/data/stacks/hyperlane-svm-deployer/stack.yml \
        stack_orchestrator/data/stacks/hyperlane-svm-warp-deployer/stack.yml \
        .github/workflows/publish-images.yml
git commit -m "build: pin all monorepo builds to the fork provenance tag

Agent drops upstream+patches; deployer/warp-deployer move off the old
core@10.2.0 pin (contracts semantically unchanged in range). CI clones
the private fork with the same token rewrite the warp-ui job uses.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Docs sync, publish triggers, pebble (hyperlane-stacks)

**Files:**
- Modify: `docs/architecture-decisions.md:66-80` (agent + deployer source sections)
- Modify: `.github/trigger-publish-agent.txt`, `.github/trigger-publish-deployer.txt`
- Check (grep, update if hit): `specs/stack-specifications.md`, `CLAUDE.md` for `16c056a`/`patch` references

- [ ] **Step 1: architecture-decisions.md**

At line ~68, replace the agent-source description:
```markdown
Custom build from `hyperlane-monorepo` at `agents-v2.2.0` (commit `4da9c44`) with two patches applied at build time:
```
with:
```markdown
Custom build from the `gorbagana-dev/hyperlane-monorepo` fork at tag
`v2.2.0-gorbagana.1` (base: upstream `agents-v2.2.0`, `4da9c44`).
The former build-time patches are fork commits:
```
(keep the two bullet descriptions of the KMS/S3 fixes that follow — they still describe the changes).

At line ~76-79, update the deployer source:
```markdown
**No existing image.** Must build from hyperlane-monorepo at `@hyperlane-xyz/core@10.2.0` (commit `16c056a09af862b3ce9e14bd3b5b8034750af9d0`).
```
→
```markdown
**No existing image.** Built from the `gorbagana-dev/hyperlane-monorepo` fork at tag
`v2.2.0-gorbagana.1` (contracts semantically unchanged from
`@hyperlane-xyz/core@10.2.0` in range; client gains built-in Ledger signing).
```
and the `- Source: hyperlane-monorepo at commit 16c056a…` line beneath it accordingly. Read the surrounding section and keep its voice; update any other `16c056a` mention in that file.

- [ ] **Step 2: Sweep remaining stale references**

```bash
grep -rn "16c056a\|patched at build time\|kms-endpoint\|s3-path-style" specs/ CLAUDE.md docs/architecture-decisions.md | grep -v superpowers
```
Update any hits to the fork-tag story (spec files under `docs/superpowers/` are historical records — leave them).

- [ ] **Step 3: Bump the publish triggers**

Append one line `publish` to each of `.github/trigger-publish-agent.txt` and `.github/trigger-publish-deployer.txt`. (CI builds fire when these land on main — sequencing is the user's: fork push first, then merge.)

- [ ] **Step 4: Commit**

```bash
git add docs/architecture-decisions.md specs/ CLAUDE.md .github/trigger-publish-agent.txt .github/trigger-publish-deployer.txt
git commit -m "docs+ci: record the fork provenance pin; bump agent/deployer publish triggers

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```
(Drop unmodified paths from `git add` if Step 2 found no hits.)

- [ ] **Step 5: Pebble + handoff**

```bash
cd /home/dev/git_puller/repos/hyperlane-stacks && pb update hyp-d34.2 --status in_progress
```
(Closure waits for green CI image builds after the user pushes.)

Hand off to the user, in order:
1. Move the fork's default `gorbagana` branch ahead to `18c2ecfcf6` (the already-reviewed lineage on the new base — their mechanics). Then push `fold-docker-patches` and open the PR for the two new commits; after it merges, cut the `v2.2.0-gorbagana.1` release (tag via the GitHub release UI).
2. Ensure `CICD_REPO_TOKEN_TEMP` has read access to `gorbagana-dev/hyperlane-monorepo` (it currently covers the warp-ui fork).
3. Push/merge the `fast-bridging-design` commits — the trigger bumps then fire agent + deployer image builds from the fork tag; green builds close `hyp-d34.2`.
4. Expected benign image delta: the deployer image gains an extra `test-ism.so` (upstream added `ism/test-ism` to `build-programs.sh` in range); our deploy scripts iterate explicit program names and ignore it.
5. Watch the e2e workflow's first post-repoint run: `e2e.yml:30` rewrites `gorbagana-dev/` clones with `CICD_REPO_TOKEN` — if that token lacks access to the monorepo fork (the publish-images TODO says it lacked warp-ui-fork access), mirror the `CICD_REPO_TOKEN_TEMP` rewrite there too.
