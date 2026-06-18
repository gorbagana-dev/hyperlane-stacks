-- Seed the gorchain + solana domain rows the scraper needs (idempotent).
-- domain cols at this scraper version: id, time_created, time_updated, name,
-- native_token, chain_id, is_test_net, is_deprecated.
-- Values are passed by entrypoint.sh via psql -v (numbers/bools: :var, strings: :'var').
INSERT INTO domain (id, time_created, time_updated, name, native_token, chain_id, is_test_net, is_deprecated)
VALUES
  (:gorchain_domain_id, now(), now(), :'gorchain_name', :'gorchain_token', :gorchain_chain_id, :gorchain_testnet, false),
  (:solana_domain_id,  now(), now(), :'solana_name',  :'solana_token',  :solana_chain_id,  :solana_testnet,  false)
ON CONFLICT (id) DO NOTHING;
