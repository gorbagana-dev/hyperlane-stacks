#!/bin/sh
set -e

# Build the metadata DB DSN at runtime from the injected password (POSTGRES_PASSWORD
# comes from the generated k8s secret via envFrom — not available to compose-time
# substitution). 'db' resolves to localhost in-pod. Then hand off to the base
# cli-migrations entrypoint (applies migrations + metadata, then starts the engine).
export HASURA_GRAPHQL_DATABASE_URL="postgresql://${POSTGRES_USER:-postgres}:${POSTGRES_PASSWORD}@${DB_HOST:-localhost}:5432/${POSTGRES_DB:-postgres}"

exec docker-entrypoint.sh "$@"
