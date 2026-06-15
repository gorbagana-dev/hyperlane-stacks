# hyperlane-explorer

Self-hosted Hyperlane message explorer for the gorbagana bridge. Indexes the
gorchain ↔ solana mailboxes into Postgres and serves a Next.js search/detail UI
over a read-only Hasura GraphQL API. Four pods in one stack:

| Pod | Role |
|-----|------|
| `postgres` | Indexed-message store (Postgres 15, persistent volume) |
| `scraper` | Hyperlane Rust scraper. Its entrypoint runs `init-db` (creates base tables + `message_view`/`total_gas_payment` views) and seeds the gorchain/solana `domain` rows, then indexes the mailboxes. Built from the deployer monorepo pin. |
| `hasura` | Read-only GraphQL over `message_view` + `domain` (anonymous role, aggregations on). Metadata baked into the image (cli-migrations). Internal only. |
| `explorer` | Next.js frontend (fork). Serves the UI and a same-origin `/api/graphql` proxy to in-cluster Hasura; injects gorbagana chain metadata at runtime. |

Built from the fork [gorbagana-dev/hyperlane-explorer](https://github.com/gorbagana-dev/hyperlane-explorer) plus the scraper/hasura images defined under `stack_orchestrator/data/container-build/`.

## Prerequisites

- A running `k8s-kind` cluster
- `laconic-so` (stack-orchestrator) installed
- `hyperlane-svm-deployer` stack deployed (provides `agent-config.json`: mailbox addresses, domain ids)

## 1. Build containers

```bash
laconic-so --stack hyperlane-explorer setup-repositories
laconic-so --stack hyperlane-explorer build-containers
```

Builds `gorbagana-dev/hyperlane-explorer:local`, `gorbagana-dev/hyperlane-scraper:local`, and `gorbagana-dev/hyperlane-hasura:local`.

## 2. Create deployment

```bash
laconic-so --stack hyperlane-explorer deploy init --output explorer-spec.yml
```

Edit the spec (see `deployment/spec-explorer.yml` for reference): set the chain
RPCs / domain ids, the `agent-config` configmap source, and the
`POSTGRES_PASSWORD` / `HASURA_GRAPHQL_ADMIN_SECRET` / `SOLANA_RPC_URL` secrets.

## 3. Deploy

```bash
laconic-so --stack hyperlane-explorer deploy create --spec-file explorer-spec.yml --deployment-dir explorer-deployment
# place agent-config.json into explorer-deployment/configmaps/agent-config/
laconic-so deployment --dir explorer-deployment start
```

The frontend is served at the configured ingress host; Hasura and Postgres are
cluster-internal only.
