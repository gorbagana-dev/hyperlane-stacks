#!/bin/bash
set -euo pipefail

# Build the DB DSN at runtime from the injected password (POSTGRES_PASSWORD comes
# from the generated k8s secret via envFrom — it isn't available to compose-time
# substitution). init-db reads DATABASE_URL; the scraper reads HYP_DB — same DSN.
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD required}"
: "${GORCHAIN_DOMAIN_ID:?}" "${GORCHAIN_CHAIN_ID:?}"
: "${SOLANA_DOMAIN_ID:?}"  "${SOLANA_CHAIN_ID:?}"

DATABASE_URL="postgresql://${POSTGRES_USER:-postgres}:${POSTGRES_PASSWORD}@${DB_HOST:-localhost}:5432/${POSTGRES_DB:-postgres}"
export DATABASE_URL
export HYP_DB="${DATABASE_URL}"

GORCHAIN_NAME="${GORCHAIN_CHAIN_NAME:-gorchain}"
SOLANA_NAME="${SOLANA_CHAIN_NAME:-solana}"
GORCHAIN_TOKEN="${GORCHAIN_NATIVE_TOKEN_SYMBOL:-GOR}"
SOLANA_TOKEN="${SOLANA_NATIVE_TOKEN_SYMBOL:-SOL}"
GORCHAIN_TESTNET="${GORCHAIN_IS_TESTNET:-true}"
SOLANA_TESTNET="${SOLANA_IS_TESTNET:-true}"

echo "[scraper-init] Running init-db migrations (creates base tables + views)..."
init-db   # reads DATABASE_URL; idempotent (Migrator::up skips applied migrations)

echo "[scraper-init] Seeding gorchain + solana domain rows (idempotent)..."
# Values pass to seed-domains.sql as psql variables (-v) so the SQL file carries
# no shell interpolation; it quotes them via :var (numbers/bools) / :'var' (strings).
psql "${DATABASE_URL}" -v ON_ERROR_STOP=1 \
  -v gorchain_domain_id="${GORCHAIN_DOMAIN_ID}" \
  -v gorchain_name="${GORCHAIN_NAME}" \
  -v gorchain_token="${GORCHAIN_TOKEN}" \
  -v gorchain_chain_id="${GORCHAIN_CHAIN_ID}" \
  -v gorchain_testnet="${GORCHAIN_TESTNET}" \
  -v solana_domain_id="${SOLANA_DOMAIN_ID}" \
  -v solana_name="${SOLANA_NAME}" \
  -v solana_token="${SOLANA_TOKEN}" \
  -v solana_chain_id="${SOLANA_CHAIN_ID}" \
  -v solana_testnet="${SOLANA_TESTNET}" \
  -f /scraper-config/seed-domains.sql

echo "[scraper-init] Starting scraper..."
exec scraper
