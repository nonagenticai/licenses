-- Downgrade 071: remove the `agent/byo` plan.
--
-- ⚠️ Roll back scripts/seed_apps.py (the agent spec's `extra_plans`) too, or the DR seed
-- re-creates the plan.
--
-- ⛔ REFUSES (raises, changes nothing) while any licence is on agent/byo — deleting the
-- plan would orphan or cascade-delete live licences. Move them to agent/standard first.

BEGIN;

DO $$
DECLARE
    n_lic INT;
BEGIN
    SELECT count(*) INTO n_lic
      FROM license l
      JOIN plan p ON p.id = l.plan_id
      JOIN app a ON a.id = p.app_id
     WHERE a.key = 'agent' AND p.code = 'byo';
    IF n_lic > 0 THEN
        RAISE EXCEPTION '071 downgrade REFUSED: % licence(s) are on agent/byo. Move them first; nothing was changed.',
            n_lic;
    END IF;

    DELETE FROM plan p
     USING app a
     WHERE a.id = p.app_id AND a.key = 'agent' AND p.code = 'byo' AND p.region_id IS NULL;
END $$;

-- Un-stamp 071 so scripts/apply_migrations.py re-applies it (up -> down -> up).
DO $$
BEGIN
    IF to_regclass('public.schema_migrations') IS NOT NULL THEN
        DELETE FROM schema_migrations WHERE filename = '071_agent_byo_plan.sql';
    END IF;
END $$;

COMMIT;
