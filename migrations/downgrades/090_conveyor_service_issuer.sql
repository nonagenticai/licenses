-- Downgrade 090: the conveyor backend stops being an issuer through provisioning.
--
-- ⚠️ Remove ('secure-apps', 'conveyor-backend', 'fr', false) from models.API_CLIENTS as
-- well, or the boot seed re-inserts the client on the next boot.
-- Only the exact rows 090 inserts are removed; an operator-changed owner or an
-- operator-added issuer row is kept. Tenants, licences and billing quotas already minted
-- through this client are NOT removed — they are billing-visible state.

BEGIN;

DELETE FROM api_client
 WHERE realm = 'secure-apps'
   AND client_id = 'conveyor-backend'
   AND owner_key = 'fr'
   AND cross_owner = false;

DELETE FROM app_issuer ai
 USING app a
 WHERE ai.app_id = a.id
   AND a.key = 'conveyor'
   AND ai.owner_key = 'fr';

-- Un-stamp 090 so scripts/apply_migrations.py re-applies it (up -> down -> up).
DO $$
BEGIN
    IF to_regclass('public.schema_migrations') IS NOT NULL THEN
        DELETE FROM schema_migrations
        WHERE filename = '090_conveyor_service_issuer.sql';
    END IF;
END $$;

COMMIT;
