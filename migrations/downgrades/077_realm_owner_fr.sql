-- Downgrade 077: remove the `fr` realm_owner row.
--
-- ⚠️ Roll back the code too (models.REALM_OWNERS), or the boot seed re-inserts it.
-- Effect: tokens from the `fr` realm get the pre-072 behaviour (no realm-owner check).

BEGIN;

DELETE FROM realm_owner WHERE realm = 'fr' AND owner_key = 'fr';

-- Un-stamp 077 so scripts/apply_migrations.py re-applies it (up -> down -> up).
DO $$
BEGIN
    IF to_regclass('public.schema_migrations') IS NOT NULL THEN
        DELETE FROM schema_migrations WHERE filename = '077_realm_owner_fr.sql';
    END IF;
END $$;

COMMIT;
