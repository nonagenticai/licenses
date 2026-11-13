-- Downgrade 072: drop realm_owner.
--
-- ⚠️ Roll back the code too (models.RealmOwner / REALM_OWNERS and the auth middleware
-- realm-owner check), or the boot seed fails on the missing table.
-- Effect: tokens from every realm get the pre-072 behaviour again (no realm-owner check).

BEGIN;

DROP TABLE IF EXISTS realm_owner;

-- Un-stamp 072 so scripts/apply_migrations.py re-applies it (up -> down -> up).
DO $$
BEGIN
    IF to_regclass('public.schema_migrations') IS NOT NULL THEN
        DELETE FROM schema_migrations WHERE filename = '072_realm_owner.sql';
    END IF;
END $$;

COMMIT;
