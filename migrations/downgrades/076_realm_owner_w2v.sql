-- Downgrade 076: remove the `w2v` realm_owner row.
--
-- ⚠️ Roll back the code too (models.REALM_OWNERS), or the boot seed re-inserts it.
-- Effect: tokens from the `w2v` realm get the pre-072 behaviour (no realm-owner check).

BEGIN;

DELETE FROM realm_owner WHERE realm = 'w2v' AND owner_key = 'w2v';

-- Un-stamp 076 so scripts/apply_migrations.py re-applies it (up -> down -> up).
DO $$
BEGIN
    IF to_regclass('public.schema_migrations') IS NOT NULL THEN
        DELETE FROM schema_migrations WHERE filename = '076_realm_owner_w2v.sql';
    END IF;
END $$;

COMMIT;
