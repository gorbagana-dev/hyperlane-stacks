# gorbagana-dev-hyperlane-hasura
# Hasura with baked metadata: tracks message_view + domain and grants the
# anonymous role read-only select (with aggregations). The cli-migrations variant
# applies the baked metadata on boot. No SQL migrations — the scraper's init-db
# owns the schema (base tables + message_view + total_gas_payment views), so this
# image only tracks the objects and sets permissions.
#
# No build context files needed beyond this dir; build.sh builds from here.
FROM hasura/graphql-engine:v2.36.0.cli-migrations-v3

COPY hasura/metadata /hasura-metadata

# Wrapper builds HASURA_GRAPHQL_DATABASE_URL from the injected POSTGRES_PASSWORD at
# runtime, then hands off to the base cli-migrations entrypoint. Setting ENTRYPOINT
# resets the base CMD, so re-declare it.
COPY hasura-entrypoint.sh /usr/local/bin/hasura-entrypoint.sh
RUN chmod +x /usr/local/bin/hasura-entrypoint.sh
ENTRYPOINT ["/usr/local/bin/hasura-entrypoint.sh"]
CMD ["/bin/sh", "-c", "$HGE_BINARY serve"]
