#!/bin/sh
set -e

# Runtime wiring for the upstream hasura/graphql-engine:*.cli-migrations-v3 image
# so no custom hasura image has to be built/published. Two things the old baked
# image carried are done here instead:
#
# 1. Build HASURA_GRAPHQL_DATABASE_URL from the injected POSTGRES_PASSWORD
#    (generated secret via envFrom — not available to compose-time substitution).
#    'db' resolves to localhost in-pod.
# 2. Rebuild the nested /hasura-metadata tree the cli-migrations entrypoint applies
#    on boot. laconic-so flattens configmap subdirectories, so the metadata files
#    arrive side-by-side in /hasura-config; place them back into the tree here.
export HASURA_GRAPHQL_DATABASE_URL="postgresql://${POSTGRES_USER:-postgres}:${POSTGRES_PASSWORD}@${DB_HOST:-localhost}:5432/${POSTGRES_DB:-postgres}"

META=/hasura-metadata
mkdir -p "$META/databases/default/tables"
cp /hasura-config/version.yaml             "$META/version.yaml"
cp /hasura-config/databases.yaml           "$META/databases/databases.yaml"
cp /hasura-config/tables.yaml              "$META/databases/default/tables/tables.yaml"
cp /hasura-config/public_domain.yaml       "$META/databases/default/tables/public_domain.yaml"
cp /hasura-config/public_message_view.yaml "$META/databases/default/tables/public_message_view.yaml"

exec docker-entrypoint.sh "$@"
