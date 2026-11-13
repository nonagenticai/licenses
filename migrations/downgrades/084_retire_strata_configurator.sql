-- Downgrade 084: restore what the strata-configurator retirement can give back.
--
-- ⚠️ Roll back models.py (API_CLIENTS) and scripts/seed_apps.py / seed_offerings.py
-- too, or a rebuilt database still lacks the app / client.
--
-- RESTORES: the tenant_owner row (if 084 deleted it), the configurator's licences
-- client, the app un-revoked, and 069's grant on fr-test-user (the newest revoked
-- migration-069 row is re-activated, unless an active grant already exists).
--
-- NOT RESTORED (irreversible): the C2 ownership move — once moved, which `agent`
-- tenants were the configurator's is not recorded — and the (tenant, agent) grants
-- 084 deleted as redundant. Revoked licences are the admin API's, never this file's.

BEGIN;

INSERT INTO tenant_owner (key, display_name) VALUES
    ('strata-configurator', 'Strata Configurator')
ON CONFLICT (key) DO NOTHING;

INSERT INTO api_client (realm, client_id, owner_key, cross_owner) VALUES
    ('secure-apps', 'strata-configurator-licenses-admin', 'strata-configurator', false)
ON CONFLICT (realm, client_id) DO NOTHING;

UPDATE app SET revoked = false WHERE key = 'strata-configurator' AND revoked;

UPDATE tenant_app_grant SET revoked_at = NULL
 WHERE id = (
        SELECT id FROM tenant_app_grant
         WHERE tenant_id = '0195f48b-d000-7000-8000-000000000099'  -- fr-test-user
           AND grantee_owner_key = 'strata-configurator'
           AND granted_by = 'migration-069'
           AND revoked_at IS NOT NULL
         ORDER BY revoked_at DESC
         LIMIT 1
 )
   AND NOT EXISTS (
        SELECT 1 FROM tenant_app_grant
         WHERE tenant_id = '0195f48b-d000-7000-8000-000000000099'
           AND grantee_owner_key = 'strata-configurator'
           AND revoked_at IS NULL
 );

-- Un-stamp 084 so scripts/apply_migrations.py re-applies it (up -> down -> up).
DO $$
BEGIN
    IF to_regclass('public.schema_migrations') IS NOT NULL THEN
        DELETE FROM schema_migrations WHERE filename = '084_retire_strata_configurator.sql';
    END IF;
END $$;

COMMIT;
