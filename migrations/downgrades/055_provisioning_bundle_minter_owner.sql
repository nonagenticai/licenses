-- Downgrade 055: provisioning's bundle-token minter loses its owner.
--
-- ⚠️ Remove ('secure-apps', 'provisioning-bundle-minter', 'fr', False) from
-- models.API_CLIENTS as well, or the boot seed re-inserts the row on the next start.
-- After this, every mint by secure-apps/provisioning-bundle-minter is 403
-- `unknown_api_client` (provisioning's tenant-inference smoke step fails).
-- Only the exact row 055 inserts is removed; an operator-changed owner is kept.

BEGIN;

DELETE FROM api_client
WHERE realm = 'secure-apps'
  AND client_id = 'provisioning-bundle-minter'
  AND owner_key = 'fr'
  AND cross_owner = false;

-- Un-stamp 055 so scripts/apply_migrations.py re-applies it (up -> down -> up).
DO $$
BEGIN
    IF to_regclass('public.schema_migrations') IS NOT NULL THEN
        DELETE FROM schema_migrations
        WHERE filename = '055_provisioning_bundle_minter_owner.sql';
    END IF;
END $$;

COMMIT;
