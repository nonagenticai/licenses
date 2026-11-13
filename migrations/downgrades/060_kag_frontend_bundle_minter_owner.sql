-- Downgrade 060: kag-frontend's bundle-token minter loses its owner.
--
-- ⚠️ Remove ('secure-apps', 'kag-frontend-bundle-minter', 'kag', False) from
-- models.API_CLIENTS as well, or the boot seed re-inserts the row on the next start.
-- After this, every mint by secure-apps/kag-frontend-bundle-minter is 403
-- `unknown_api_client` (kag-frontend's per-tenant workers / kag tokens fail).
-- Only the exact row 060 inserts is removed; an operator-changed owner is kept.

BEGIN;

DELETE FROM api_client
WHERE realm = 'secure-apps'
  AND client_id = 'kag-frontend-bundle-minter'
  AND owner_key = 'kag'
  AND cross_owner = false;

-- Un-stamp 060 so scripts/apply_migrations.py re-applies it (up -> down -> up).
DO $$
BEGIN
    IF to_regclass('public.schema_migrations') IS NOT NULL THEN
        DELETE FROM schema_migrations
        WHERE filename = '060_kag_frontend_bundle_minter_owner.sql';
    END IF;
END $$;

COMMIT;
