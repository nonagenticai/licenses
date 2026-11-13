-- Downgrade 074: remove the `agent` realm_owner row.
--
-- ⚠️ Roll back the code too (models.REALM_OWNERS), or the boot seed re-inserts it.
-- Effect: tokens from the `agent` realm get the pre-072 behaviour (no realm-owner check).

BEGIN;

DELETE FROM realm_owner WHERE realm = 'agent' AND owner_key = 'agent';

-- Un-stamp 074 so scripts/apply_migrations.py re-applies it (up -> down -> up).
DO $$
BEGIN
    IF to_regclass('public.schema_migrations') IS NOT NULL THEN
        DELETE FROM schema_migrations WHERE filename = '074_realm_owner_agent.sql';
    END IF;
END $$;

COMMIT;
