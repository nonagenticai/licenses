-- Downgrade 089: the agent bundle stops reaching the conveyor.
--
-- ⚠️ Remove ("agent", "conveyor", "agent") from models.APP_REQUIRES_SEED as well, or
-- the boot seed re-inserts the edge. Licences / tenant_dependency_grant rows ensure
-- already wrote for it are NOT removed (a conveyor dependency licence is
-- billing-visible state); they stop being re-ensured and bundle-token for
-- app=conveyor under bundle=agent is 404 `app_not_in_bundle` again.
-- Only the exact edge 089 inserts is removed; an operator-changed caller is kept.

BEGIN;

DELETE FROM app_requires r
 USING app a, app q
 WHERE a.id = r.app_id AND q.id = r.required_app_id
   AND a.key = 'agent' AND q.key = 'conveyor'
   AND r.caller_owner_key = 'agent';

-- Un-stamp 089 so scripts/apply_migrations.py re-applies it (up -> down -> up).
DO $$
BEGIN
    IF to_regclass('public.schema_migrations') IS NOT NULL THEN
        DELETE FROM schema_migrations
        WHERE filename = '089_agent_bundle_conveyor.sql';
    END IF;
END $$;

COMMIT;
