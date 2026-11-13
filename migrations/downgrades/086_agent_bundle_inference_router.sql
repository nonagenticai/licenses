-- Downgrade 086: the agent bundle stops reaching inference-router.
--
-- ⚠️ Remove ("agent", "inference-router", "agent") from models.APP_REQUIRES_SEED as
-- well, or the boot seed re-inserts the edge. Licences / tenant_dependency_grant rows
-- ensure already wrote for it are NOT removed (an inference-router dependency licence
-- is billing-visible state); they stop being re-ensured and bundle-token for
-- app=inference-router under bundle=agent is 404 `app_not_in_bundle` again.
-- Only the exact edge 086 inserts is removed; an operator-changed caller is kept.

BEGIN;

DELETE FROM app_requires r
 USING app a, app q
 WHERE a.id = r.app_id AND q.id = r.required_app_id
   AND a.key = 'agent' AND q.key = 'inference-router'
   AND r.caller_owner_key = 'agent';

-- Un-stamp 086 so scripts/apply_migrations.py re-applies it (up -> down -> up).
DO $$
BEGIN
    IF to_regclass('public.schema_migrations') IS NOT NULL THEN
        DELETE FROM schema_migrations
        WHERE filename = '086_agent_bundle_inference_router.sql';
    END IF;
END $$;

COMMIT;
