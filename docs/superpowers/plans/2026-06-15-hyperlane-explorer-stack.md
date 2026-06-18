# Hyperlane Explorer Stack Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a self-hosted Hyperlane message explorer (frontend + scraper + Postgres + Hasura) to the gorbagana deployment as one bundled laconic-so stack, indexing the gorchain ↔ solana bridge across local/staging/prod.

**Architecture:** One laconic-so stack, four Deployments on the bridge-ops host. The **scraper** (built from the deployer's monorepo pin `16c056a`) indexes mailbox accounts into **Postgres 15**; its container entrypoint runs the scraper's own `init-db` migrator (which creates the base tables **and** `message_view`/`total_gas_payment` views) plus an idempotent gorchain/solana domain-row seed, then execs the scraper. **Hasura** (cli-migrations, baked metadata) tracks `message_view` + `domain` and grants the anonymous role read-only select with aggregations. The forked **frontend** (Next.js) serves the UI and a same-origin `/api/graphql` proxy to in-cluster Hasura, injects gorbagana chain metadata at runtime, and lists testnet (gorbagana) messages in its default feed.

**Tech Stack:** Next.js 16 (pnpm 11 / Node 24), Hyperlane Rust scraper (rust 1.88.0, monorepo `16c056a`), PostgreSQL 15, Hasura `graphql-engine:v2.36.0.cli-migrations-v3`, laconic-so / stack-orchestrator, Ansible ops, GitHub Actions.

**Two repos change:**
- `hyperlane-explorer` (fork `gorbagana-dev/hyperlane-explorer`) — frontend code changes. **Branch off `gorbagana`** (the fork's effective main). Commit, do not push.
- `hyperlane-stacks` (current repo, branch `add-explorer`) — container builds, stack/compose, specs, ops, CI, tests. Commit, do not push.

**Ground truth:** This plan is grounded in two committed docs — read them first:
- `docs/superpowers/specs/2026-06-15-hyperlane-explorer-stack-design.md` (design)
- `docs/superpowers/specs/2026-06-15-hyperlane-explorer-spike-findings.md` (spike corrections — authoritative where they differ)

**Testing note (per user):** All code is implemented first; testing happens **at the end**, in order — (1) `laconic-so` bring-up, (2) e2e, (3) local deployment — and runs on the **separate test host**, not this dev host (this host has no deployment; don't curl/kubectl/kind/docker against it). Frontend tasks (Phase A) ARE verifiable here via `pnpm typecheck/build/lint/test`. Infra tasks (Phases B–G) are authored here and verified on the test host.

**Conventions to follow:** match existing file style exactly (warp-ui is the closest analog for the frontend image + specs + CI; relayer for the agent-config/state-distribution path; svm-deployer for the Rust build). Minimal changes only. No pebble IDs in code/commits.

---

## File Structure (what gets created/modified)

**hyperlane-explorer fork (branch `add-explorer-stack` off `gorbagana`):**
- Modify `src/consts/config.ts` — env-aware GraphQL endpoint (browser proxy vs server in-cluster URL).
- Create `src/pages/api/graphql.ts` — same-origin GraphQL proxy → in-cluster Hasura.
- Create `src/features/chains/injectedChainMetadata.ts` — load `/gorbagana-chains.json` from `/public`.
- Modify `src/features/chains/loadChainMetadata.ts` — merge injected metadata (registry < injected < user overrides).
- Modify `src/features/messages/queries/useMessageQuery.ts` — default feed lists all scraped chains (not just non-testnet).
- Create `src/features/messages/queries/build.test.ts` (or extend existing) — feed-filter unit test.

**hyperlane-stacks (branch `add-explorer`):**
- Create `stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-explorer/{Dockerfile,build.sh,entrypoint.sh}` — frontend image.
- Create `stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-scraper/{Dockerfile,build.sh,entrypoint.sh}` — scraper+init-db image from monorepo `16c056a`.
- Create `stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-hasura/{Dockerfile}` + `.../hasura/metadata/**` — Hasura image with baked metadata.
- Create `stack_orchestrator/data/stacks/hyperlane-explorer/{stack.yml,README.md}`.
- Create `stack_orchestrator/data/compose/docker-compose-hyperlane-explorer.yml`.
- Create `deployment/spec-explorer.yml`, `deployment/staging/spec-explorer.yml`, `deployment/local/spec-explorer.yml`.
- Modify `ops/inventories/{local,staging,prod}/group_vars/all.yml` — `stacks` + `stack_env_vars`.
- Modify `ops/inventories/{local,staging,prod}/hosts.yml` (or inventory file) — `explorer_hosts` group.
- Modify `ops/playbooks/deploy-all.yml` — Explorer play.
- Modify `ops/roles/credentials/tasks/generate.yml` — generate Postgres password + Hasura admin secret.
- Modify `.github/workflows/publish-images.yml` — `build-explorer`, `build-scraper`, `build-hasura` jobs + path filters + changes outputs.
- Create `.github/trigger-publish-explorer.txt`, `.github/trigger-publish-scraper.txt`, `.github/trigger-publish-hasura.txt`.
- Create `tests/e2e/fixtures/test-spec-explorer.yml` + `tests/e2e/test_15_explorer.py`; modify `tests/e2e/conftest.py` + `tests/e2e/lib/state_loader.py`.
- Modify the local-single-host runbook under `ops/runbooks/`.

---

## Key facts locked in during research (do not re-derive)

- `message_view` + `total_gas_payment` are created by the scraper migrator (`init-db`), NOT authored by us. `domain` table already has `native_token` + `is_deprecated`.
- `init-db` reads **`DATABASE_URL`** env (fallback hardcoded localhost), retries 10× — `rust/main/agents/scraper/migration/bin/common.rs`.
- The migration crate is **not** a `rust/main` workspace member — build it from its own dir: `cargo build --release --bin init-db` inside `rust/main/agents/scraper/migration`.
- Scraper is a workspace member — `cargo build --release --bin scraper` from `rust/main`.
- `rust/main/rust-toolchain.toml` at `16c056a` pins **1.88.0** → builder image `rust:1.88.0`.
- Scraper runtime env: `CONFIG_FILES=/config/agent-config.json`, `HYP_DB`, `HYP_CHAINSTOSCRAPE=gorchain,solana`, `HYP_CHAINS_<CHAIN>_CUSTOMRPCURLS`, `HYP_METRICSPORT` (must be set; binds or panics `AddrInUse`). Needs a `./config` dir in CWD.
- `index.from` is a message **nonce**; `0` is correct for fresh chains (comes from agent-config.json).
- Domain rows must be seeded (FK `block_domain_fkey`); scraper does not upsert them.
- laconic-so maps each compose service to a **separate k8s Deployment**; cross-service `depends_on` ordering is NOT honored. Ordering is handled in-app: scraper entrypoint creates schema (idempotent) before scraping; Hasura crash-restarts until the views exist.
- Frontend `config.apiUrl` consumers: `src/pages/_app.tsx:25` (urql client) and `src/features/messages/queries/serverFetch.ts:27,97`.
- `@cached` works on Hasura CE — leave queries unchanged.
- Default feed filter origin: `useMessageQuery.ts:55-57` computes `mainnetDomainIds` from `!isTestnet` scraped chains → excludes gorbagana (testnet).

---

# Phase A — Frontend fork (hyperlane-explorer)

**Branch setup (do once):**

- [ ] **A0: Create the implementation branch off `gorbagana`**

```bash
cd /home/dev/workspace/pranav/hyperlane-explorer
git checkout gorbagana
git pull --ff-only || true            # gorbagana already has trim-fork-ci (#1) merged
git checkout -b add-explorer-stack
pnpm install --frozen-lockfile
```

Expected: on `add-explorer-stack`, deps installed.

## Task A1: Env-aware GraphQL endpoint + same-origin proxy

**Files:**
- Modify: `src/consts/config.ts`
- Create: `src/pages/api/graphql.ts`

- [ ] **Step 1: Add the env-aware endpoint to the config**

In `src/consts/config.ts`, add `serverApiUrl` to the `Config` interface and compute both URLs. Replace the `apiUrl` line and add `serverApiUrl`:

```ts
const isDevMode = process.env.NODE_ENV === 'development';
const version = process.env.NEXT_PUBLIC_VERSION ?? null;
const registryUrl = process.env.NEXT_PUBLIC_REGISTRY_URL || undefined;
const registryBranch = process.env.NEXT_PUBLIC_REGISTRY_BRANCH || 'main';
const explorerApiKeys = JSON.parse(process.env.EXPLORER_API_KEYS || '{}');

// GraphQL endpoint resolution:
// - Browser: a same-origin proxy route (/api/graphql) so Hasura and its admin
//   secret stay off the public internet (no second host, no CORS).
// - Server (SSR / API route / getServerSideProps): the in-cluster Hasura URL
//   from HASURA_GRAPHQL_URL. Not NEXT_PUBLIC, so it is never shipped to the browser.
const serverApiUrl = process.env.HASURA_GRAPHQL_URL || 'http://localhost:8080/v1/graphql';
const browserApiUrl = '/api/graphql';

interface Config {
  debug: boolean;
  version: string | null;
  apiUrl: string;
  serverApiUrl: string;
  explorerApiKeys: Record<string, string>;
  githubProxy?: string;
  registryUrl: string | undefined;
  registryBranch?: string | undefined;
}

export const config: Config = Object.freeze({
  debug: isDevMode,
  version,
  // `typeof window` is statically known per bundle: server bundle → in-cluster
  // URL, browser bundle → relative proxy path.
  apiUrl: typeof window === 'undefined' ? serverApiUrl : browserApiUrl,
  serverApiUrl,
  explorerApiKeys,
  githubProxy: 'https://proxy.hyperlane.xyz',
  registryBranch,
  registryUrl,
});
```

Leave `unscrapedChainsInDb` and `debugIgnoredChains` unchanged.

- [ ] **Step 2: Create the proxy API route**

Create `src/pages/api/graphql.ts`:

```ts
import type { NextApiRequest, NextApiResponse } from 'next';

import { config } from '../../consts/config';
import { logger } from '../../utils/logger';

// Same-origin GraphQL proxy. The browser posts here; we forward to the
// in-cluster Hasura (config.serverApiUrl). Keeps Hasura and its admin secret
// off the public internet — only this read-only proxy is reachable.
export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'POST');
    return res.status(405).json({ errors: [{ message: 'Method not allowed' }] });
  }

  try {
    const upstream = await fetch(config.serverApiUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(req.body),
    });
    const body = await upstream.text();
    res.status(upstream.status);
    res.setHeader('Content-Type', upstream.headers.get('content-type') || 'application/json');
    return res.send(body);
  } catch (error) {
    logger.error('GraphQL proxy request failed', error);
    return res.status(502).json({ errors: [{ message: 'Upstream GraphQL request failed' }] });
  }
}
```

No change needed in `_app.tsx` or `serverFetch.ts` — they already read `config.apiUrl`, which now resolves correctly per environment.

- [ ] **Step 3: Verify typecheck + build**

```bash
pnpm typecheck
pnpm build
```

Expected: both succeed. (`build` runs `prebuild` font fetch — needs network.)

- [ ] **Step 4: Commit**

```bash
git add src/consts/config.ts src/pages/api/graphql.ts
git commit -m "feat(graphql): same-origin proxy + env-aware Hasura endpoint

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

## Task A2: Inject gorbagana chain metadata at runtime

**Files:**
- Create: `src/features/chains/injectedChainMetadata.ts`
- Modify: `src/features/chains/loadChainMetadata.ts`

- [ ] **Step 1: Create the injected-metadata loader**

Create `src/features/chains/injectedChainMetadata.ts`:

```ts
import type { ChainMetadata } from '@hyperlane-xyz/sdk/metadata/chainMetadataTypes';
import type { ChainMap } from '@hyperlane-xyz/sdk/types';

import { logger } from '../../utils/logger';

// Path (served from /public) of the runtime-rendered gorbagana chain metadata.
// The container entrypoint writes this from deploy env, so one image serves any
// environment. Absent in upstream/dev — then we return {} and rely on the registry.
const INJECTED_METADATA_PATH = '/gorbagana-chains.json';

let cache: ChainMap<Partial<ChainMetadata>> | null = null;

export async function loadInjectedChainMetadata(): Promise<ChainMap<Partial<ChainMetadata>>> {
  if (cache) return cache;
  // Only fetched in the browser; SSR is data-disabled and has no origin to resolve.
  if (typeof window === 'undefined') return {};
  try {
    const res = await fetch(INJECTED_METADATA_PATH);
    if (!res.ok) return {};
    cache = (await res.json()) as ChainMap<Partial<ChainMetadata>>;
    return cache;
  } catch (error) {
    logger.debug('No injected chain metadata found', error);
    return {};
  }
}
```

- [ ] **Step 2: Merge injected metadata in loadChainMetadata**

In `src/features/chains/loadChainMetadata.ts`, import the loader and insert the merge so precedence is **registry < injected < user overrides**. Replace the single `mergeChainMetadataMap` call:

```ts
import { loadInjectedChainMetadata } from './injectedChainMetadata';
// ...existing imports unchanged...

  // ...existing metadataWithLogos block unchanged...

  // Self-hosted gorbagana chains (gorchain + solana) shipped in /public so the
  // UI resolves them without the public Hyperlane registry. User overrides
  // (added via the UI) still win over these.
  const injectedMetadata = await loadInjectedChainMetadata();
  const withInjected = mergeChainMetadataMap(metadataWithLogos, injectedMetadata);
  const mergedMetadata = mergeChainMetadataMap(withInjected, overrideChainMetadata);

  return objFilter(
    // ...unchanged...
```

- [ ] **Step 3: Verify**

```bash
pnpm typecheck && pnpm build
```

Expected: success.

- [ ] **Step 4: Commit**

```bash
git add src/features/chains/injectedChainMetadata.ts src/features/chains/loadChainMetadata.ts
git commit -m "feat(chains): inject self-hosted gorbagana chain metadata at runtime

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

## Task A3: Default feed lists all scraped chains (not just non-testnet)

**Files:**
- Modify: `src/features/messages/queries/useMessageQuery.ts`
- Create/extend: `src/features/messages/queries/build.test.ts`

- [ ] **Step 1: Write the failing unit test for the feed allowlist**

The default (no-filter) feed restricts to a provided domain-id allowlist via `buildMessageSearchQuery(..., mainnetDomainIds)`. Add a test that the allowlist is applied verbatim. Create or extend `src/features/messages/queries/build.test.ts`:

```ts
import { buildMessageSearchQuery } from './build';

describe('buildMessageSearchQuery default feed', () => {
  it('restricts the no-filter feed to the provided domain ids', () => {
    const { query } = buildMessageSearchQuery(
      '', // no search input
      null, // no origin filter
      null, // no destination filter
      null,
      null,
      100,
      true,
      [1198486095, 1399811151], // feed domain ids (gorbagana testnet chains)
    );
    expect(query).toContain('origin_domain_id: {_in: [1198486095,1399811151]}');
    expect(query).toContain('destination_domain_id: {_in: [1198486095,1399811151]}');
  });
});
```

- [ ] **Step 2: Run it — confirm it passes against current build.ts**

```bash
pnpm test -- build.test.ts
```

Expected: PASS (this asserts existing `buildDomainIdWhereClause` behavior — it's the guard for Step 3, which changes the *caller* that computes the ids).

- [ ] **Step 3: Change the caller to include every scraped chain**

In `src/features/messages/queries/useMessageQuery.ts`, replace the `mainnetDomainIds` computation (lines ~55–57) and its use at the `buildMessageSearchQuery` call (line ~122):

```ts
  const { chains } = useScrapedChains(chainMetadataResolver);
  // Default feed (no search/filters) lists messages whose origin AND destination
  // are scraped chains. Upstream restricts this to non-testnet chains; our
  // gorbagana chains are testnet, so include every scraped chain instead.
  const feedDomainIds = Object.values(chains).map((chain) => chain.domainId);
```

And update the call argument from `mainnetDomainIds` to `feedDomainIds`:

```ts
  const { query, variables } = buildMessageSearchQuery(
    sanitizedInput,
    isValidOrigin ? originDomainId : null,
    isValidDestination ? destDomainId : null,
    startTimeFilter,
    endTimeFilter,
    queryLimit,
    true,
    feedDomainIds,
    dbStatusFilter,
    warpAddresses,
    isPendingFilter,
  );
```

Leave `build.ts` (param name `mainnetDomainIds`) unchanged — it is just the allowlist.

- [ ] **Step 4: Verify**

```bash
pnpm test -- build.test.ts && pnpm typecheck && pnpm build && pnpm lint
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/features/messages/queries/useMessageQuery.ts src/features/messages/queries/build.test.ts
git commit -m "feat(messages): list scraped testnet chains in the default feed

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

> **After Phase A:** the fork branch `add-explorer-stack` is ready. The colleague tags a release (e.g. `vX.Y.Z-gorbagana.N`) — the stack pins that tag. Until tagged, the stack pins the branch for build-from-source testing.

---

# Phase B — Container builds (hyperlane-stacks)

All work below is on the existing `hyperlane-stacks` repo branch `add-explorer`.

## Task B1: Frontend image (mirror warp-ui)

**Files (create):**
- `stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-explorer/Dockerfile`
- `stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-explorer/build.sh`
- `stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-explorer/entrypoint.sh`

- [ ] **Step 1: Dockerfile** (mirrors warp-ui; pnpm 11 / Node 24; standalone output)

`.../gorbagana-dev-hyperlane-explorer/Dockerfile`:

```dockerfile
# Stage 1: Build Next.js app
#
# Build context: ~/cerc/hyperlane-explorer (cloned by laconic-so setup-repositories)
# Invoked via: docker build -f <this-file> ~/cerc/hyperlane-explorer
# Note: build.sh copies entrypoint.sh into the build context before building.

FROM node:24-alpine AS builder

RUN corepack enable && corepack prepare pnpm@11.1.3 --activate

WORKDIR /app

# Copy only dependency files first — pnpm install is cached until these change
COPY package.json pnpm-lock.yaml ./
COPY patches/ patches/

RUN pnpm install --frozen-lockfile || pnpm install

# Now copy the rest of the source
COPY . .

# Enable standalone output for a minimal runtime image.
RUN sed -i 's/reactStrictMode: true,/reactStrictMode: true,\n  output: "standalone",/' next.config.js

RUN pnpm build

# Stage 2: Runtime (standalone)
FROM node:24-alpine

RUN addgroup -g 1001 -S explorer && \
    adduser -u 1001 -S explorer -G explorer

WORKDIR /app

COPY --from=builder --chown=explorer:explorer /app/.next/standalone ./
COPY --from=builder --chown=explorer:explorer /app/.next/static .next/static
COPY --from=builder --chown=explorer:explorer /app/public public

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER explorer

EXPOSE 3000

ENTRYPOINT ["/entrypoint.sh"]
```

> Implementation note: confirm `next.config.js` in the fork still contains `reactStrictMode: true,` for the `sed` anchor (it does as of `gorbagana`). The fork's `next.config.js` has no `outputFileTracingExcludes`, so unlike warp-ui no second `sed` is needed. The `prebuild` font fetch runs inside `pnpm build` and needs network during the image build (CI has it).

- [ ] **Step 2: build.sh** (mirror warp-ui)

`.../gorbagana-dev-hyperlane-explorer/build.sh`:

```bash
#!/usr/bin/env bash
source ${CERC_CONTAINER_BASE_DIR}/build-base.sh

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

REPO_DIR=${CERC_REPO_BASE_DIR}/hyperlane-explorer

cleanup() {
  rm -f "${REPO_DIR}/entrypoint.sh"
}
trap cleanup EXIT

cp ${SCRIPT_DIR}/entrypoint.sh ${REPO_DIR}/entrypoint.sh

docker build -t gorbagana-dev/hyperlane-explorer:local \
  -f ${SCRIPT_DIR}/Dockerfile \
  ${build_command_args} \
  ${REPO_DIR}
```

- [ ] **Step 3: entrypoint.sh** (render `/gorbagana-chains.json` from env into `/public`)

`.../gorbagana-dev-hyperlane-explorer/entrypoint.sh`:

```bash
#!/bin/sh
set -eu

# Required chain config for the injected metadata file. HASURA_GRAPHQL_URL is
# read by the Next server at runtime (server-side only); the browser uses the
# relative /api/graphql proxy and needs no GraphQL env.
missing=""
for var in GORCHAIN_DOMAIN_ID SOLANA_DOMAIN_ID GORCHAIN_CHAIN_ID SOLANA_CHAIN_ID \
           GORCHAIN_RPC_URL SOLANA_RPC_URL HASURA_GRAPHQL_URL; do
  eval "val=\${$var:-}"
  [ -z "$val" ] && missing="$missing $var"
done
if [ -n "$missing" ]; then
  echo "ERROR: Required environment variables not set:$missing" >&2
  exit 1
fi

PUBLIC_DIR="/app/public"

# Self-hosted chain metadata (gorchain + solana), merged over the public registry
# by loadChainMetadata at runtime. Keyed by chain name (must match agent-config /
# domain.name, i.e. gorchain / solana).
cat > "$PUBLIC_DIR/gorbagana-chains.json" <<EOF
{
  "${GORCHAIN_CHAIN_NAME:-gorchain}": {
    "protocol": "sealevel",
    "chainId": ${GORCHAIN_CHAIN_ID},
    "domainId": ${GORCHAIN_DOMAIN_ID},
    "name": "${GORCHAIN_CHAIN_NAME:-gorchain}",
    "displayName": "${GORCHAIN_DISPLAY_NAME:-Gorbagana}",
    "rpcUrls": [{ "http": "${GORCHAIN_RPC_URL}" }],
    "nativeToken": { "name": "${GORCHAIN_NATIVE_TOKEN_NAME:-GOR}", "symbol": "${GORCHAIN_NATIVE_TOKEN_SYMBOL:-GOR}", "decimals": ${GORCHAIN_NATIVE_TOKEN_DECIMALS:-9} },
    "blocks": { "confirmations": 1, "estimateBlockTime": 1, "reorgPeriod": 0 }
  },
  "${SOLANA_CHAIN_NAME:-solana}": {
    "protocol": "sealevel",
    "chainId": ${SOLANA_CHAIN_ID},
    "domainId": ${SOLANA_DOMAIN_ID},
    "name": "${SOLANA_CHAIN_NAME:-solana}",
    "displayName": "${SOLANA_DISPLAY_NAME:-Solana}",
    "rpcUrls": [{ "http": "${SOLANA_RPC_URL}" }],
    "nativeToken": { "name": "${SOLANA_NATIVE_TOKEN_NAME:-SOL}", "symbol": "${SOLANA_NATIVE_TOKEN_SYMBOL:-SOL}", "decimals": ${SOLANA_NATIVE_TOKEN_DECIMALS:-9} },
    "blocks": { "confirmations": 1, "estimateBlockTime": 1, "reorgPeriod": 0 }
  }
}
EOF
echo "Rendered gorbagana-chains.json"

echo "Starting Next.js standalone server..."
exec node server.js
```

> Note: `GORCHAIN_RPC_URL`/`SOLANA_RPC_URL` here are only embedded into the injected metadata file (browser-visible). For the explorer's read-only path the browser never calls them, but `ChainMetadataSchema` requires `rpcUrls`. Use public/non-secret RPC URLs in the spec `config:` (NOT the Helius secret) — the explorer does not need the keyed solana RPC.

- [ ] **Step 4: Commit**

```bash
cd /home/dev/workspace/pranav/hyperlane-stacks
chmod +x stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-explorer/build.sh
git add stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-explorer
git commit -m "build(explorer): frontend container build (mirror warp-ui)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

## Task B2: Scraper image (from monorepo `16c056a`)

**Files (create):**
- `stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-scraper/Dockerfile`
- `stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-scraper/build.sh`
- `stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-scraper/entrypoint.sh`

- [ ] **Step 1: Dockerfile** (mirror the agent builder; no patches; add `scraper` + `init-db` + psql)

`.../gorbagana-dev-hyperlane-scraper/Dockerfile`:

```dockerfile
# syntax=docker/dockerfile:1.4
# gorbagana-dev-hyperlane-scraper
# Hyperlane scraper + sea-orm init-db migrator, built from the monorepo at the
# DEPLOYER pin (16c056a) so it parses the same on-chain program accounts the
# deployer published. No KMS/S3 patches (the scraper signs nothing, writes no S3).
#
# Build context: ~/cerc/hyperlane-monorepo (cloned by laconic-so setup-repositories)
# Invoked via: docker build -f <this-file> ~/cerc/hyperlane-monorepo

# ============================================================
# Stage 1: Builder — compile scraper + init-db
# ============================================================
FROM rust:1.88.0 AS builder

RUN apt-get update && \
    apt-get install -y --no-install-recommends clang protobuf-compiler && \
    apt-get clean && rm -rf /var/lib/apt/lists/* && \
    cargo install --locked sccache

ENV RUSTC_WRAPPER=sccache
ENV SCCACHE_DIR=/sccache

WORKDIR /usr/src/rust/main

# Copy git metadata for vergen build-time info
COPY .git ../../.git

# Copy workspace crates (rust/main) + sealevel + the standalone migration crate
COPY rust/main/agents ./agents
COPY rust/main/applications ./applications
COPY rust/main/chains ./chains
COPY rust/main/ethers-prometheus ./ethers-prometheus
COPY rust/main/hyperlane-base ./hyperlane-base
COPY rust/main/hyperlane-core ./hyperlane-core
COPY rust/main/hyperlane-metric ./hyperlane-metric
COPY rust/main/hyperlane-test ./hyperlane-test
COPY rust/main/lander ./lander
COPY rust/main/utils ./utils
COPY rust/main/Cargo.toml ./
COPY rust/main/Cargo.lock ./
COPY rust/sealevel ../sealevel

# Build the scraper (workspace member) and the init-db migrator (standalone
# crate under agents/scraper/migration — NOT a workspace member, so build it
# from its own dir).
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/usr/local/cargo/git \
    --mount=type=cache,target=$SCCACHE_DIR,sharing=locked \
    RUSTFLAGS="--cfg tokio_unstable" cargo build --release --bin scraper && \
    (cd agents/scraper/migration && \
     RUSTFLAGS="--cfg tokio_unstable" cargo build --release --bin init-db) && \
    mkdir -p /release && \
    cp target/release/scraper /release && \
    cp agents/scraper/migration/target/release/init-db /release

# ============================================================
# Stage 2: Runtime
# ============================================================
FROM ubuntu:22.04
WORKDIR /app

# postgresql-client (psql) + jq for the idempotent domain-row seed; openssl/ca
# for TLS RPC; tini for signal handling.
RUN apt-get update && \
    apt-get install -y --no-install-recommends openssl ca-certificates tini \
        libcurl4 postgresql-client jq && \
    apt-get clean && rm -rf /var/lib/apt/lists/* && \
    chmod 777 /app && \
    mkdir -p /home/hyperlane && chown -R 1000:1000 /home/hyperlane

# The scraper merges ./config/*.json with CONFIG_FILES; it requires a ./config
# dir to exist in its CWD. Ship the monorepo's config dir like the agent image.
COPY rust/main/config /app/config

COPY --from=builder /release/* /usr/local/bin/
COPY stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-scraper/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV HOME=/home/hyperlane
USER 1000
ENTRYPOINT ["tini", "--", "/entrypoint.sh"]
```

> Note: the entrypoint is COPY'd from the hyperlane-stacks path inside the monorepo context — build.sh copies it in first (next step), mirroring how the agent build copies its patches into the monorepo context.

- [ ] **Step 2: build.sh** (copy entrypoint into the monorepo context, then build)

`.../gorbagana-dev-hyperlane-scraper/build.sh`:

```bash
#!/usr/bin/env bash
source ${CERC_CONTAINER_BASE_DIR}/build-base.sh

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

# Build context is hyperlane-monorepo; copy our entrypoint into it so COPY works
# (mirrors the agent build copying its patch files in).
MONOREPO_DIR="${CERC_REPO_BASE_DIR}/hyperlane-monorepo"
DEST="${MONOREPO_DIR}/stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-scraper"
mkdir -p "${DEST}"
cp "${SCRIPT_DIR}/entrypoint.sh" "${DEST}/"

cleanup() {
  rm -rf "${MONOREPO_DIR}/stack_orchestrator"
}
trap cleanup EXIT

DOCKER_BUILDKIT=1 docker build -t gorbagana-dev/hyperlane-scraper:local \
  -f ${SCRIPT_DIR}/Dockerfile \
  ${build_command_args} \
  "${MONOREPO_DIR}"
```

- [ ] **Step 3: entrypoint.sh** (idempotent schema init + domain seed, then scrape)

`.../gorbagana-dev-hyperlane-scraper/entrypoint.sh`:

```bash
#!/bin/bash
set -euo pipefail

# Required env (DB DSN + per-chain seed values). HYP_DB and DATABASE_URL are the
# same DSN under the two names the scraper (HYP_DB) and init-db (DATABASE_URL) read.
: "${DATABASE_URL:?DATABASE_URL required}"
: "${HYP_DB:?HYP_DB required}"
: "${GORCHAIN_DOMAIN_ID:?}" "${GORCHAIN_CHAIN_ID:?}"
: "${SOLANA_DOMAIN_ID:?}"  "${SOLANA_CHAIN_ID:?}"

GORCHAIN_NAME="${GORCHAIN_CHAIN_NAME:-gorchain}"
SOLANA_NAME="${SOLANA_CHAIN_NAME:-solana}"
GORCHAIN_TOKEN="${GORCHAIN_NATIVE_TOKEN_SYMBOL:-GOR}"
SOLANA_TOKEN="${SOLANA_NATIVE_TOKEN_SYMBOL:-SOL}"
GORCHAIN_TESTNET="${GORCHAIN_IS_TESTNET:-true}"
SOLANA_TESTNET="${SOLANA_IS_TESTNET:-true}"

echo "[scraper-init] Running init-db migrations (creates base tables + views)..."
init-db   # reads DATABASE_URL; idempotent (Migrator::up skips applied migrations)

echo "[scraper-init] Seeding gorchain + solana domain rows (idempotent)..."
# domain cols at this scraper version: id, time_created, time_updated, name,
# native_token, chain_id, is_test_net, is_deprecated.
psql "${DATABASE_URL}" -v ON_ERROR_STOP=1 <<SQL
INSERT INTO domain (id, time_created, time_updated, name, native_token, chain_id, is_test_net, is_deprecated)
VALUES
  (${GORCHAIN_DOMAIN_ID}, now(), now(), '${GORCHAIN_NAME}', '${GORCHAIN_TOKEN}', ${GORCHAIN_CHAIN_ID}, ${GORCHAIN_TESTNET}, false),
  (${SOLANA_DOMAIN_ID},  now(), now(), '${SOLANA_NAME}',  '${SOLANA_TOKEN}',  ${SOLANA_CHAIN_ID},  ${SOLANA_TESTNET},  false)
ON CONFLICT (id) DO NOTHING;
SQL

echo "[scraper-init] Starting scraper..."
exec scraper
```

> Verify on the test host: confirm the `domain` column list against the running scraper's schema (the spike captured `id, time_created, time_updated, name, native_token, chain_id, is_test_net, is_deprecated`). If `time_created`/`time_updated` have DB defaults, they can be dropped from the INSERT — keep them explicit unless a default exists.

- [ ] **Step 4: Commit**

```bash
chmod +x stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-scraper/{build.sh,entrypoint.sh}
git add stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-scraper
git commit -m "build(scraper): scraper + init-db image from monorepo 16c056a

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

## Task B3: Hasura image (baked metadata)

**Files (create):**
- `stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-hasura/Dockerfile`
- `stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-hasura/hasura/metadata/version.yaml`
- `.../hasura/metadata/databases/databases.yaml`
- `.../hasura/metadata/databases/default/tables/tables.yaml`
- `.../hasura/metadata/databases/default/tables/public_message_view.yaml`
- `.../hasura/metadata/databases/default/tables/public_domain.yaml`

> We bake metadata into the image (most deterministic; matches the repo's "build a custom image" convention). cli-migrations applies it on boot. We track only views/tables that `init-db` already created — no SQL migrations needed. Hasura crash-restarts until the scraper has created the schema; then metadata applies and it stays up.

- [ ] **Step 1: Dockerfile**

```dockerfile
# gorbagana-dev-hyperlane-hasura
# Hasura with baked metadata: tracks message_view + domain and grants the
# anonymous role read-only select (with aggregations). cli-migrations applies
# metadata on boot. No SQL migrations — init-db (scraper) owns the schema.
FROM hasura/graphql-engine:v2.36.0.cli-migrations-v3

COPY hasura/metadata /hasura-metadata
```

- [ ] **Step 2: metadata/version.yaml**

```yaml
version: 3
```

- [ ] **Step 3: metadata/databases/databases.yaml**

```yaml
- name: default
  kind: postgres
  configuration:
    connection_info:
      database_url:
        from_env: HASURA_GRAPHQL_DATABASE_URL
      isolation_level: read-committed
      use_prepared_statements: true
  tables: "!include default/tables/tables.yaml"
```

- [ ] **Step 4: metadata/databases/default/tables/tables.yaml**

```yaml
- "!include public_message_view.yaml"
- "!include public_domain.yaml"
```

- [ ] **Step 5: metadata/databases/default/tables/public_message_view.yaml**

```yaml
table:
  name: message_view
  schema: public
select_permissions:
  - role: anonymous
    permission:
      columns: '*'
      filter: {}
      allow_aggregations: true
```

- [ ] **Step 6: metadata/databases/default/tables/public_domain.yaml**

```yaml
table:
  name: domain
  schema: public
select_permissions:
  - role: anonymous
    permission:
      columns: '*'
      filter: {}
      allow_aggregations: true
```

> Verify on the test host: after Hasura boots against a scraper-initialized DB, `hasura metadata ic status` (or the console) shows zero inconsistencies, and an anonymous `message_view`/`domain` select + `message_view_aggregate` resolve.

- [ ] **Step 7: Commit**

```bash
git add stack_orchestrator/data/container-build/gorbagana-dev-hyperlane-hasura
git commit -m "build(hasura): image with baked metadata tracking message_view + domain

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

# Phase C — Stack assembly (hyperlane-stacks)

## Task C1: stack.yml + README

**Files (create):**
- `stack_orchestrator/data/stacks/hyperlane-explorer/stack.yml`
- `stack_orchestrator/data/stacks/hyperlane-explorer/README.md`

- [ ] **Step 1: stack.yml** (two repos: the fork for the frontend image + the monorepo at the deployer pin for the scraper image)

```yaml
version: "1.1"
name: hyperlane-explorer
description: "Hyperlane Explorer — self-hosted message indexer + search UI (frontend + scraper + Postgres + Hasura)"
repos:
  - github.com/gorbagana-dev/hyperlane-explorer@add-explorer-stack
  # Scraper is built from the DEPLOYER pin (matches the on-chain programs).
  - github.com/hyperlane-xyz/hyperlane-monorepo@16c056a09af862b3ce9e14bd3b5b8034750af9d0
# containers is commented out so deploy-start won't kind-load images;
# k8s pulls them from the registry instead. Uncomment for local builds.
# containers:
#   - gorbagana-dev/hyperlane-explorer
#   - gorbagana-dev/hyperlane-scraper
#   - gorbagana-dev/hyperlane-hasura
pods:
  - hyperlane-explorer
```

> When the fork is tagged, change `@add-explorer-stack` → `@vX.Y.Z-gorbagana.N`.

- [ ] **Step 2: README.md** — short description mirroring other stacks' README (purpose, pods, how it's deployed, image list). Keep concise.

- [ ] **Step 3: Commit**

```bash
git add stack_orchestrator/data/stacks/hyperlane-explorer
git commit -m "feat(explorer): stack definition

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

## Task C2: compose file (4 services)

**Files (create):** `stack_orchestrator/data/compose/docker-compose-hyperlane-explorer.yml`

- [ ] **Step 1: compose**

```yaml
services:
  postgres:
    image: postgres:15
    restart: unless-stopped
    hostname: postgres
    environment:
      POSTGRES_USER: ${POSTGRES_USER:-postgres}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: ${POSTGRES_DB:-postgres}
    volumes:
      - explorer-postgres-data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER:-postgres}"]
      interval: 10s
      timeout: 5s
      retries: 10
      start_period: 20s

  scraper:
    image: ghcr.io/gorbagana-dev/hyperlane-scraper:latest
    # image: gorbagana-dev/hyperlane-scraper:local
    restart: unless-stopped
    hostname: scraper
    environment:
      CONFIG_FILES: /config/agent-config.json
      # init-db reads DATABASE_URL; scraper reads HYP_DB — same DSN.
      DATABASE_URL: postgresql://${POSTGRES_USER:-postgres}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB:-postgres}
      HYP_DB: postgresql://${POSTGRES_USER:-postgres}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB:-postgres}
      HYP_CHAINSTOSCRAPE: "gorchain,solana"
      # agent-config.json carries placeholder rpcUrls; real URLs arrive here.
      HYP_CHAINS_GORCHAIN_CUSTOMRPCURLS: ${GORCHAIN_RPC_URL:-http://rpc-placeholder.invalid}
      # Solana (secret) is injected via secrets: as HYP_CHAINS_SOLANA_CUSTOMRPCURLS.
      HYP_METRICSPORT: "9090"
      # Domain-seed values consumed by the entrypoint.
      GORCHAIN_DOMAIN_ID: ${GORCHAIN_DOMAIN_ID}
      SOLANA_DOMAIN_ID: ${SOLANA_DOMAIN_ID}
      GORCHAIN_CHAIN_ID: ${GORCHAIN_CHAIN_ID}
      SOLANA_CHAIN_ID: ${SOLANA_CHAIN_ID}
      GORCHAIN_CHAIN_NAME: ${GORCHAIN_CHAIN_NAME:-gorchain}
      SOLANA_CHAIN_NAME: ${SOLANA_CHAIN_NAME:-solana}
      GORCHAIN_NATIVE_TOKEN_SYMBOL: ${GORCHAIN_NATIVE_TOKEN_SYMBOL:-GOR}
      SOLANA_NATIVE_TOKEN_SYMBOL: ${SOLANA_NATIVE_TOKEN_SYMBOL:-SOL}
      GORCHAIN_IS_TESTNET: ${GORCHAIN_IS_TESTNET:-true}
      SOLANA_IS_TESTNET: ${SOLANA_IS_TESTNET:-true}
    volumes:
      - agent-config:/config:ro
    healthcheck:
      test: ["CMD-SHELL", "nc -z localhost 9090 || exit 1"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 60s

  hasura:
    image: ghcr.io/gorbagana-dev/hyperlane-hasura:latest
    # image: gorbagana-dev/hyperlane-hasura:local
    restart: unless-stopped
    hostname: hasura
    environment:
      HASURA_GRAPHQL_DATABASE_URL: postgresql://${POSTGRES_USER:-postgres}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB:-postgres}
      HASURA_GRAPHQL_ADMIN_SECRET: ${HASURA_GRAPHQL_ADMIN_SECRET}
      HASURA_GRAPHQL_UNAUTHORIZED_ROLE: anonymous
      HASURA_GRAPHQL_ENABLE_CONSOLE: "false"
      HASURA_GRAPHQL_ENABLE_TELEMETRY: "false"
    ports:
      - "8080"
    healthcheck:
      test: ["CMD-SHELL", "curl -fsS http://localhost:8080/healthz || exit 1"]
      interval: 30s
      timeout: 5s
      retries: 5
      start_period: 30s

  explorer:
    image: ghcr.io/gorbagana-dev/hyperlane-explorer:latest
    # image: gorbagana-dev/hyperlane-explorer:local
    restart: unless-stopped
    hostname: explorer
    environment:
      # Server-side only (browser uses the relative /api/graphql proxy).
      HASURA_GRAPHQL_URL: http://hasura:8080/v1/graphql
      GORCHAIN_RPC_URL: ${GORCHAIN_RPC_URL}
      SOLANA_RPC_URL: ${SOLANA_RPC_URL}
      GORCHAIN_DOMAIN_ID: ${GORCHAIN_DOMAIN_ID}
      SOLANA_DOMAIN_ID: ${SOLANA_DOMAIN_ID}
      GORCHAIN_CHAIN_ID: ${GORCHAIN_CHAIN_ID}
      SOLANA_CHAIN_ID: ${SOLANA_CHAIN_ID}
      GORCHAIN_CHAIN_NAME: ${GORCHAIN_CHAIN_NAME:-gorchain}
      SOLANA_CHAIN_NAME: ${SOLANA_CHAIN_NAME:-solana}
      # Standalone server must bind all interfaces for k8s Service routing.
      HOSTNAME: "0.0.0.0"
    ports:
      - "3000"
    healthcheck:
      test: ["CMD", "wget", "--spider", "-q", "http://localhost:3000"]
      interval: 30s
      timeout: 10s
      retries: 5
      start_period: 20s

volumes:
  # agent-config: ConfigMap volume sourced from BridgeStateLoader / state_distribute at deploy-create.
  agent-config:
  explorer-postgres-data:
```

> `SOLANA_RPC_URL` on the `explorer` service is the **public, non-secret** solana RPC (used only to populate injected metadata `rpcUrls`, never called for our read path). The scraper's keyed solana RPC arrives via the `secrets:` block as `HYP_CHAINS_SOLANA_CUSTOMRPCURLS` (Task D). Keep the secret out of the browser-facing service.

- [ ] **Step 2: Commit**

```bash
git add stack_orchestrator/data/compose/docker-compose-hyperlane-explorer.yml
git commit -m "feat(explorer): docker-compose (postgres + scraper + hasura + frontend)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

# Phase D — Deployment specs (local / staging / prod)

## Task D1: three spec files

**Files (create):** `deployment/spec-explorer.yml`, `deployment/staging/spec-explorer.yml`, `deployment/local/spec-explorer.yml`.

Domain/chain IDs per env (from warp-ui specs): **prod** gorchain `1198486093` / solana `1399811149`; **staging** gorchain `1198486095` / solana `1399811151`; **local** same as staging. Hostnames mirror warp-ui: prod `explorer.bridge.gorbagana.wtf`, staging `explorer.staging.gorbagana.wtf`, local `explorer.__BASE_DOMAIN__`.

- [ ] **Step 1: staging spec** (`deployment/staging/spec-explorer.yml`) — the verified target

```yaml
# Hyperlane Explorer - staging deployment spec
# Self-hosted message indexer + search UI (frontend + scraper + Postgres + Hasura).
stack: stack_orchestrator/data/stacks/hyperlane-explorer
deploy-to: k8s-kind
kind-cluster-name: hyperlane
kind-mount-root: /srv/kind/hyperlane
network:
  acme-email: admin@gorbagana.wtf
  http-proxy:
    - host-name: explorer.staging.gorbagana.wtf
      routes:
        - path: /
          proxy-to: explorer:3000
volumes:
  explorer-postgres-data: /srv/kind/hyperlane/explorer/postgres
config:
  # Public, non-secret RPCs for injected chain metadata (explorer never calls
  # them for its read path). The scraper's keyed solana RPC is a secret below.
  GORCHAIN_RPC_URL: "https://rpc.staging.gorbagana.wtf"
  SOLANA_RPC_URL: "https://api.devnet.solana.com"
  GORCHAIN_DOMAIN_ID: "1198486095"
  SOLANA_DOMAIN_ID: "1399811151"
  GORCHAIN_CHAIN_ID: "1198486095"
  SOLANA_CHAIN_ID: "1399811151"
  GORCHAIN_CHAIN_NAME: "gorchain"
  SOLANA_CHAIN_NAME: "solana"
  GORCHAIN_IS_TESTNET: "true"
  SOLANA_IS_TESTNET: "true"
configmaps:
  agent-config: ./configmaps/agent-config
# Before deploying, export these env vars in the shell that runs laconic-so:
#   SOLANA_RPC_URL  (Helius RPC — embeds an API key; scraper-only)
#   POSTGRES_PASSWORD, HASURA_GRAPHQL_ADMIN_SECRET (generated by credentials role)
secrets:
  hyperlane-explorer-secrets:
    keys:
      # Keyed solana RPC for the SCRAPER (overrides agent-config placeholder).
      HYP_CHAINS_SOLANA_CUSTOMRPCURLS: { env: SOLANA_RPC_URL }
      POSTGRES_PASSWORD: { env: POSTGRES_PASSWORD }
      HASURA_GRAPHQL_ADMIN_SECRET: { env: HASURA_GRAPHQL_ADMIN_SECRET }
image-pull-secret:
  server: ghcr.io
  username: gorbagana-dev
  token-env: GHCR_PAT
image-overrides:
  explorer: ghcr.io/gorbagana-dev/hyperlane-explorer:latest
  scraper: ghcr.io/gorbagana-dev/hyperlane-scraper:latest
  hasura: ghcr.io/gorbagana-dev/hyperlane-hasura:latest
resources:
  containers:
    postgres:
      reservations: { cpus: "0.5", memory: 512M }
      limits: { cpus: "1.0", memory: 1024M }
    scraper:
      reservations: { cpus: "0.25", memory: 512M }
      limits: { cpus: "0.5", memory: 1024M }
    hasura:
      reservations: { cpus: "0.25", memory: 256M }
      limits: { cpus: "0.5", memory: 512M }
    explorer:
      reservations: { cpus: "0.25", memory: 256M }
      limits: { cpus: "0.5", memory: 512M }
  volumes:
    explorer-postgres-data:
      reservations: { storage: 10Gi }
```

> Pin `image-overrides` to released tags (`:vX.Y.Z-gorbagana.N` / `:<timestamp>-<sha>`) once published; `:latest` is for first bring-up only. POSTGRES_PASSWORD appears both in `secrets:` (for hasura/scraper/explorer envs via the secret) and is referenced by the postgres service env — laconic injects the secret env into all services in the stack; the postgres container reads `POSTGRES_PASSWORD` from it.

- [ ] **Step 2: prod spec** (`deployment/spec-explorer.yml`) — same as staging with: host `explorer.bridge.gorbagana.wtf`; `GORCHAIN_RPC_URL: https://rpc.gorbagana.wtf`; solana public RPC `https://api.mainnet-beta.solana.com`; domain/chain IDs `1198486093` / `1399811149`; `*_IS_TESTNET` per prod reality (set `false` if prod gorbagana is mainnet — confirm; the feed-filter fix makes the explorer feed independent of this, but it sets `domain.is_test_net`); larger resource limits.

- [ ] **Step 3: local spec** (`deployment/local/spec-explorer.yml`) — host `explorer.__BASE_DOMAIN__`; RPCs as `__BROWSER_GORCHAIN_RPC_URL__` / `__BROWSER_SOLANA_RPC_URL__` rendered on host (mirror warp-ui local); no `image-overrides` (uses `:latest`/`:local`); staging IDs.

- [ ] **Step 4: Commit**

```bash
git add deployment/spec-explorer.yml deployment/staging/spec-explorer.yml deployment/local/spec-explorer.yml
git commit -m "feat(explorer): deployment specs (local/staging/prod)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

# Phase E — Ops wiring (hyperlane-stacks)

## Task E1: group_vars (3 envs)

**Files (modify):** `ops/inventories/{local,staging,prod}/group_vars/all.yml`

- [ ] **Step 1: add to `stacks` and `stack_env_vars`** in each of the three files

```yaml
# under stacks:
  hyperlane-explorer:
    spec: spec-explorer.yml
    path: stack_orchestrator/data/stacks/hyperlane-explorer
    configmaps:
      - agent-config

# under stack_env_vars:
  hyperlane-explorer:
    - SOLANA_RPC_URL
    - POSTGRES_PASSWORD
    - HASURA_GRAPHQL_ADMIN_SECRET
    - GHCR_PAT
```

Also add the generated-secret bindings near the other secret-env values in each file:

```yaml
POSTGRES_PASSWORD: "{{ explorer_db_password }}"
HASURA_GRAPHQL_ADMIN_SECRET: "{{ explorer_hasura_admin_secret }}"
```

> For **local**, follow the warp-ui-local precedent: `SOLANA_RPC_URL` may be a host-rendered token rather than a Helius secret. Match warp-ui-local's handling exactly.

- [ ] **Step 2: Commit** after E2/E3 (group them).

## Task E2: deploy-all.yml Explorer play + inventory host group

**Files (modify):** `ops/playbooks/deploy-all.yml`, `ops/inventories/{local,staging,prod}/hosts.yml` (the inventory file that defines `warp_ui_hosts`/`relayer_hosts`).

- [ ] **Step 1: add `explorer_hosts`** to each inventory, pointing at the same bridge-ops host as `warp_ui_hosts` (copy that group's host/vars).

- [ ] **Step 2: add the Explorer play** to `ops/playbooks/deploy-all.yml`, after the Warp UI play (it depends on deployer output via `agent-config`):

```yaml
- name: Explorer
  hosts: explorer_hosts
  gather_facts: true
  pre_tasks:
    - name: Load the deployment config
      ansible.builtin.include_tasks: ../roles/common/tasks/load_deployment_config.yml
  vars:
    stack_name: hyperlane-explorer
    spec_render_generated: true
    configmap_names: "{{ stacks['hyperlane-explorer'].configmaps }}"
    deploy_dir: "{{ ansible_env.HOME }}/deployments/hyperlane-explorer"
    stack_pre_start_tasks: "{{ playbook_dir }}/../roles/state_distribute/tasks/main.yml"
  roles:
    - fetch_stack
    - stack_deploy
```

> Same shape as the Relayer play. `state_distribute` delivers `agent-config.json` into the `agent-config` configmap dir (no `generated_subdir` — the scraper reads the full `agent-config.json`, matching the relayer).

## Task E3: credentials role (Postgres password + Hasura admin secret)

**Files (modify):** `ops/roles/credentials/tasks/generate.yml`

- [ ] **Step 1: add idempotent generation** (mirror the minio password tasks)

```yaml
- name: Generate explorer Postgres password if absent
  ansible.builtin.set_fact:
    _secrets: >-
      {{ _secrets | combine({'explorer_db_password':
      lookup('ansible.builtin.password', '/dev/null length=32 chars=ascii_letters,digits')}) }}
  when: "'explorer_db_password' not in _secrets"
  no_log: true

- name: Generate explorer Hasura admin secret if absent
  ansible.builtin.set_fact:
    _secrets: >-
      {{ _secrets | combine({'explorer_hasura_admin_secret':
      lookup('ansible.builtin.password', '/dev/null length=32 chars=ascii_letters,digits')}) }}
  when: "'explorer_hasura_admin_secret' not in _secrets"
  no_log: true
```

Ensure these `_secrets` keys are persisted back to `deployment-config.yml` by the role's existing write-back step, and surfaced as `explorer_db_password` / `explorer_hasura_admin_secret` vars (mirror how `minio_root_password` becomes available).

- [ ] **Step 2: Commit Phase E**

```bash
git add ops/inventories ops/playbooks/deploy-all.yml ops/roles/credentials/tasks/generate.yml
git commit -m "ops(explorer): group_vars, deploy play, generated DB/Hasura secrets

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 3: Lint** (matches the repo's `ops-lint` workflow)

```bash
cd ops && ansible-lint playbooks/deploy-all.yml || true   # run via the project's lint venv if present
```

> Run with the repo's pinned ansible-lint venv (see `feedback`/`reference_ansible_test_venvs`). Fix real findings; pre-existing style is out of scope.

---

# Phase F — CI image publishing

## Task F1: publish-images.yml jobs + triggers

**Files:** modify `.github/workflows/publish-images.yml`; create `.github/trigger-publish-{explorer,scraper,hasura}.txt`.

- [ ] **Step 1: add path filters** to the `on.push.paths` list:

```yaml
      - '.github/trigger-publish-explorer.txt'
      - '.github/trigger-publish-scraper.txt'
      - '.github/trigger-publish-hasura.txt'
```

- [ ] **Step 2: add `changes` outputs + detection** for `explorer`, `scraper`, `hasura` (mirror the existing lines exactly).

- [ ] **Step 3: add `build-explorer` job** (mirror `build-warp-ui`, including the `CICD_REPO_TOKEN_TEMP` private-fork auth, `setup-repositories`, `build-containers`, timestamp+SHA + ref-tag from `stack.yml`). `LOCAL_IMAGE: gorbagana-dev/hyperlane-explorer:local`, `REMOTE_IMAGE: ghcr.io/${{ github.repository_owner }}/hyperlane-explorer`. The `Enable containers` sed targets `stacks/hyperlane-explorer/stack.yml` and must uncomment all three container lines:

```bash
STACK_YML="$(pwd)/stack_orchestrator/data/stacks/hyperlane-explorer/stack.yml"
sed -i 's/^# containers:/containers:/' "$STACK_YML"
sed -i 's/^#   - gorbagana-dev/  - gorbagana-dev/' "$STACK_YML"
```

For `build-explorer`, build only the frontend image:

```bash
laconic-so --stack $(pwd)/stack_orchestrator/data/stacks/hyperlane-explorer build-containers --include gorbagana-dev/hyperlane-explorer
```

- [ ] **Step 4: add `build-scraper` job** — same skeleton; `timeout-minutes: 120` (Rust build); the monorepo is public (no fork-token needed, but keep the auth step harmless or drop it). Build only the scraper image:

```bash
laconic-so --stack $(pwd)/stack_orchestrator/data/stacks/hyperlane-explorer build-containers --include gorbagana-dev/hyperlane-scraper
```

`REMOTE_IMAGE: ghcr.io/${{ github.repository_owner }}/hyperlane-scraper`.

- [ ] **Step 5: add `build-hasura` job** — quick build (FROM hasura + COPY). Build only the hasura image:

```bash
laconic-so --stack $(pwd)/stack_orchestrator/data/stacks/hyperlane-explorer build-containers --include gorbagana-dev/hyperlane-hasura
```

`REMOTE_IMAGE: ghcr.io/${{ github.repository_owner }}/hyperlane-hasura`.

- [ ] **Step 6: trigger files** — create the three `.github/trigger-publish-*.txt` with a header comment + a first `publish` line (mirror `trigger-publish-warp-ui.txt`).

- [ ] **Step 7: Commit**

```bash
git add .github/workflows/publish-images.yml .github/trigger-publish-explorer.txt .github/trigger-publish-scraper.txt .github/trigger-publish-hasura.txt
git commit -m "ci(explorer): publish frontend, scraper, and hasura images

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

# Phase G — Tests + runbook

## Task G1: e2e fixture, conftest, test

**Files:** create `tests/e2e/fixtures/test-spec-explorer.yml`, `tests/e2e/test_15_explorer.py`; modify `tests/e2e/conftest.py`, `tests/e2e/lib/state_loader.py`.

- [ ] **Step 1: state_loader mapping** — add explorer's consumer state files:

```python
    "hyperlane-explorer": [
        ("agent-config.json", "agent-config"),
    ],
```

- [ ] **Step 2: test-spec fixture** (`tests/e2e/fixtures/test-spec-explorer.yml`) — mirror `test-spec-warp-ui.yml`: e2e network, `explorer.test` http-proxy → `explorer:3000`, e2e domain/chain IDs (`99999`/`99998`), public test RPCs, `configmaps: agent-config`, `image-pull-secret`, and `image-overrides` with `REPLACE_EXPLORER_IMAGE` / `REPLACE_SCRAPER_IMAGE` / `REPLACE_HASURA_IMAGE`. Add `POSTGRES_PASSWORD`/`HASURA_GRAPHQL_ADMIN_SECRET` as fixed test values in `config:` (test-only).

- [ ] **Step 3: conftest fixtures** — add `_resolve_image_refs` entries for the three explorer images; add an `explorer_deployment` session fixture (mirror `warp_ui_deployment`): patch the spec, `deploy_prepare`, `bridge_state_loader.populate("hyperlane-explorer", ...)`, `deploy_start`, wait for the `explorer` pod Running + Caddy 200 at `EXPLORER_URL`. Add `--skip-explorer-deploy`.

- [ ] **Step 4: test_15_explorer.py** — HTTP + data assertions:

```python
import subprocess
import pytest

@pytest.mark.slow
class TestExplorer:
    def test_explorer_pod_healthy(self, explorer_deployment: dict) -> None:
        ns = explorer_deployment["deployment"].namespace
        deployment_id = explorer_deployment["deployment"].deployment_id
        result = subprocess.run(
            ["kubectl", "-n", ns, "get", "pods", "-l", f"app={deployment_id}",
             "-o", "jsonpath={.items[0].status.phase}"],
            capture_output=True, text=True, check=True,
        )
        assert result.stdout.strip() == "Running"

    def test_explorer_serves_html(self, explorer_deployment: dict) -> None:
        url = explorer_deployment["url"]
        result = subprocess.run(
            ["curl", "-fsS", url + "/"], capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0
        assert "<html" in result.stdout.lower() or "<!doctype" in result.stdout.lower()

    def test_graphql_proxy_resolves_domain(self, explorer_deployment: dict) -> None:
        url = explorer_deployment["url"]
        result = subprocess.run(
            ["curl", "-fsS", url + "/api/graphql", "-H", "content-type: application/json",
             "-d", '{"query":"{ domain { id name } }"}'],
            capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0
        assert '"domain"' in result.stdout
```

> Add a bridge-message → `message_view` assertion later (mirror `test_13_warp_ui_bridge`) once the HTTP smoke tests pass; it needs a sent message + relay wait.

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/fixtures/test-spec-explorer.yml tests/e2e/test_15_explorer.py tests/e2e/conftest.py tests/e2e/lib/state_loader.py
git commit -m "test(explorer): e2e fixture, conftest deploy fixture, smoke tests

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

## Task G2: local runbook

**Files:** modify the local-single-host runbook under `ops/runbooks/`.

- [ ] **Step 1:** add an Explorer bring-up section + a "find your test transfer in the explorer" step (mirror the warp-ui section). Commit.

---

# Phase H — Wrap-up

- [ ] **Step 1:** Update `stack.yml` repo pin to the fork's release tag once the colleague tags it (replace `@add-explorer-stack`).
- [ ] **Step 2:** Update `image-overrides` in all three specs to the published pinned tags (drop `:latest`).
- [ ] **Step 3:** Final review pass against both ground-truth docs; ensure the decisions log items are all reflected.

---

## Self-review (against the spec + spike findings)

- **Stack shape (4 pods, one stack):** Phase C2 ✓
- **All 3 environments:** Phase D ✓
- **Proxy GraphQL via Next `/api/graphql`:** Phase A1 ✓
- **`message_view`/`total_gas_payment` created by migrator (not authored):** scraper entrypoint runs `init-db`; Hasura only tracks ✓ (Phase B2/B3)
- **`domain` tracked, not viewed:** Hasura metadata `public_domain.yaml` ✓
- **Seed gorchain+solana domain rows (idempotent):** scraper entrypoint `ON CONFLICT DO NOTHING` ✓
- **Scraper built from `16c056a`, no KMS/S3 patches:** Phase B2 ✓
- **`index.from` nonce / `0`:** comes from agent-config.json (unchanged) ✓
- **Hasura cli-migrations; anon select on view+domain; aggregations on:** Phase B3 ✓
- **`@cached` unchanged:** no task touches queries' directives ✓
- **Frontend: endpoint via proxy + inject chain metadata + relax feed filter:** Phases A1/A2/A3 ✓
- **Metrics port set:** `HYP_METRICSPORT=9090` in compose ✓
- **State distribution of `agent-config.json`:** Phase E2 `state_distribute` ✓
- **CI publishes the new images:** Phase F ✓
- **e2e + local tests:** Phase G ✓

**Open items to confirm on the test host during testing (not blockers to implementation):**
1. `domain` exact column list/defaults vs the seed INSERT (Phase B2 Step 3 note).
2. Whether the migration crate builds cleanly in-Docker exactly as `cd agents/scraper/migration && cargo build --release --bin init-db` (it's standalone; confirm its `Cargo.lock`/deps resolve in the builder).
3. Hasura crash-restart-until-schema-exists behavior is acceptable (it is self-healing; if noisy, add a tiny wait-for-table entrypoint wrapper to the hasura image later).
4. Prod `*_IS_TESTNET` values (affects only `domain.is_test_net`; feed is filter-independent after A3).
