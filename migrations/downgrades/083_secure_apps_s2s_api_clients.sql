-- Downgrade 083: the secure-apps w2v-backend / provisioning-backend clients lose their owner.
--
-- ⚠️ Remove both rows from models.API_CLIENTS as well, or the boot seed re-inserts them
-- on the next start. After this, every licence write by secure-apps/w2v-backend or
-- secure-apps/provisioning-backend is 403 `unknown_api_client` — only safe while both
-- services still mint from portal (KEYCLOAK_SERVICE_REALM unset).
-- Only the exact rows 083 inserts are removed; an operator-changed owner is kept.

BEGIN;

DELETE FROM api_client
WHERE realm = 'secure-apps'
  AND cross_owner = false
  AND ((client_id = 'w2v-backend' AND owner_key = 'w2v')
    OR (client_id = 'provisioning-backend' AND owner_key = 'fr'));

-- Un-stamp 083 so scripts/apply_migrations.py re-applies it (up -> down -> up).
DO $$
BEGIN
    IF to_regclass('public.schema_migrations') IS NOT NULL THEN
        DELETE FROM schema_migrations
        WHERE filename = '083_secure_apps_s2s_api_clients.sql';
    END IF;
END $$;

COMMIT;
