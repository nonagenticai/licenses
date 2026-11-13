-- Downgrade 064: karma's outbound bundle-token minter loses its owner.
--
-- ⚠️ Remove ('secure-apps', 'karma-bundle-minter', 'kag', False) from
-- models.API_CLIENTS as well, or the boot seed re-inserts the row on the next start.
-- After this, every mint by secure-apps/karma-bundle-minter is 403
-- `unknown_api_client` (karma's per-tenant workers / inference-router tokens fail).
-- Only the exact row 064 inserts is removed; an operator-changed owner is kept.

BEGIN;

DELETE FROM api_client
WHERE realm = 'secure-apps'
  AND client_id = 'karma-bundle-minter'
  AND owner_key = 'kag'
  AND cross_owner = false;

-- Un-stamp 064 so scripts/apply_migrations.py re-applies it (up -> down -> up).
DO $$
BEGIN
    IF to_regclass('public.schema_migrations') IS NOT NULL THEN
        DELETE FROM schema_migrations
        WHERE filename = '064_karma_bundle_minter_owner.sql';
    END IF;
END $$;

COMMIT;
