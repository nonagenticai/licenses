-- Downgrade 073: remove the w2v grant on svc-agent-api.
--
-- Deletes only the row 073 wrote (granted_by 'migration-073'); a grant an operator, the
-- API, or provision_service_token.py --grant-owner wrote for the same (tenant, owner) is
-- left alone. No licence references a grant row, so nothing cascades — but tts-backend's
-- /licenses/active lookup on this tenant is refused 403 `tenant_not_accessible` again.

BEGIN;

DELETE FROM tenant_app_grant
 WHERE tenant_id = '9a4366bf-5684-59dd-b36c-2cfffef9607e'
   AND grantee_owner_key = 'w2v'
   AND granted_by = 'migration-073';

-- Un-stamp 073 so scripts/apply_migrations.py re-applies it (up -> down -> up).
DO $$
BEGIN
    IF to_regclass('public.schema_migrations') IS NOT NULL THEN
        DELETE FROM schema_migrations
         WHERE filename = '073_svc_agent_api_w2v_grant.sql';
    END IF;
END $$;

COMMIT;
