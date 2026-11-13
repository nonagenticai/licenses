-- Downgrade 044. Drops the long-lived token register (`token.long_lived_owner`,
-- `token.long_lived_reason`) and ck_token_long_lived_registered.
--
-- ⚠️ Rolls back the CODE first: a tree that still writes these columns would 500 on
-- UndefinedColumn once this runs.
--
-- Lossy for the REGISTER only: registered long-lived tokens keep authenticating (they
-- stay perpetual), but who owns them and why is lost. Export it first:
--   SELECT id, license_id, token_type, long_lived_owner, long_lived_reason
--   FROM token WHERE long_lived_owner IS NOT NULL;
ALTER TABLE token DROP CONSTRAINT IF EXISTS ck_token_long_lived_registered;
ALTER TABLE token DROP COLUMN IF EXISTS long_lived_reason;
ALTER TABLE token DROP COLUMN IF EXISTS long_lived_owner;
