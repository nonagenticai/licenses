-- Downgrade 069: remove the strata-configurator / care-bus grants on fr-test-user.
--
-- Deletes only the rows 069 wrote (granted_by 'migration-069'); a grant an operator or
-- the API wrote for the same (tenant, owner) is left alone. No licence references a
-- grant row, so nothing cascades — but a licence issued under the grant stays, and its
-- issuer can no longer revoke it without break-glass.

BEGIN;

DELETE FROM tenant_app_grant
 WHERE tenant_id = '0195f48b-d000-7000-8000-000000000099'
   AND grantee_owner_key IN ('strata-configurator', 'care-bus')
   AND granted_by = 'migration-069';

-- Un-stamp 069 so scripts/apply_migrations.py re-applies it (up -> down -> up).
DO $$
BEGIN
    IF to_regclass('public.schema_migrations') IS NOT NULL THEN
        DELETE FROM schema_migrations
         WHERE filename = '069_fr_test_user_strata_grants.sql';
    END IF;
END $$;

COMMIT;
