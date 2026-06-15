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
