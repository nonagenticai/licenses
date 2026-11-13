-- 026: the last two `platform-cp-N` placeholders are named. They are the
-- prompt- and procedure-improvement tenants of fr's internal improvement loop.
--
-- WHAT IS WRONG. Migration 024 renamed …0001 and …0003 and DELIBERATELY left
-- …0004 and …0005 alone, on the honest grounds that nobody had evidence of what
-- they were. That evidence now exists, so the placeholder is no longer honest —
-- it is just stale.
--
--     …-000000000004  =  prompt-improvement
--     …-000000000005  =  procedure-improvement
--
-- SOURCES, four independent, all agreeing (measured 2026-09-01):
--   1. fr `backend/migrations/062_super_admin_workspaces_namespace_realign.sql`
--      INSERTs …0004 as the workspace literally named `prompt-improvement`, and
--      pins `streams.kind='prompt'` to that uuid with a CHECK constraint.
--   2. fr `backend/migrations/066_procedure_improvement_tenant.sql` does the same
--      for …0005 as `procedure-improvement`, and says to keep it in lock-step
--      with `INTERNAL_PROCEDURE_TENANT_ID` in
--      backend/src/middleware/internal-tenant-only.ts.
--   3. gitops-production
--      clusters/strato/services/keycloak/realm-patch-script-configmap.yaml binds
--      them TWICE over, in two unrelated blocks: OPERATORS maps
--      `system-prompt-operator` -> workspace `__system_prompt__` -> …0004, and
--      CARD_WRITERS maps `aa-card-writer-prompt` -> `__prompt_improvement__` ->
--      …0004 and `aa-card-writer-procedure` -> `__procedure_improvement__` ->
--      …0005. Those are the `tenant_id` claims minted into live portal JWTs.
--      Checked in the DEPLOYED ConfigMap on the cluster too, not only in
--      origin/main — `kubectl get cm keycloak-realm-patch-script -n keycloak`
--      carries the same two maps.
--   And the live fr database agrees: `SELECT id, name FROM workspaces` on
--   the primary returns exactly `prompt-improvement` for …0004 and
--   `procedure-improvement` for …0005.
--
-- The names chosen here are therefore TRANSCRIBED from the system that owns
-- these identities, not invented. That is the whole difference between this
-- migration and the 2026-07-10 seeding that created the placeholders.
--
-- ⛔ WHAT THIS DOES NOT DO. It renames two rows. Nothing is re-pointed, merged,
-- deleted or migrated. `license` and `tenant_app_byo_mode` reference
-- `tenants.id`, never the name, and no column in this database stores a tenant
-- name as text — so the 3 and 2 licences on these rows do not move. The rows
-- referencing these uuids in OTHER services (fr, kag, procedure_management) key
-- on the uuid too and are untouched by a name change here.
--
-- ⛔ …0002 IS STILL DELIBERATELY LEFT ALONE. fr calls it `workflow-improvement`;
-- this registry calls it `platform-core`; the portal realm retired `gerard-dev`
-- at it. That is a genuine three-way dispute over 20 active licences and it is
-- billing issue #86 — not something a rename migration is entitled to settle.
-- The evidence that resolved …0004/…0005 does NOT resolve …0002: it makes the
-- collision sharper, because fr's claim on …0002 is exactly as well-sourced as
-- its claim on the two renamed here, and this registry's `platform-core` claim
-- contradicts it. Two well-sourced contradicting claims is a decision, not a
-- transcription.
--
-- WHY IT IS SAFE TO RENAME (measured 2026-09-01, not assumed):
--   * 0 by-name tenant lookups across both prod replicas over the full 10h life
--     of the current pods. Two independent channels agree: the
--     `byo_mode_open_route_name_lookup` marker (auth/middleware.py) never fired,
--     AND every one of the 15 distinct `/tenants/{seg}` path segments in 7012 log
--     lines is a uuid. The path channel is its own positive control — the
--     segments ARE being logged, so the zero is an absence of by-name callers and
--     not a broken probe. Both …0004 and …0005 ARE actively looked up (2 and 3
--     times), by uuid.
--
-- NAME CONSTRAINTS. `ck_tenants_name_format` is `^[a-z0-9][a-z0-9-]*$` and
-- `tenants_name_key` is UNIQUE. Both new names satisfy the pattern, and neither
-- was taken (checked against prod's 75 rows before writing this).
--
-- BASELINE INTERACTION (scripts/apply_migrations.py). On a FRESH database this
-- file NEVER EXECUTES — the runner stamps every .sql file as applied without
-- running it, because create_all has already built the schema. That is why the
-- honest names are ALSO written into `models.PLATFORM_TENANTS` (seeded every boot
-- by `seed_platform_tenants`) and into migration 014, which
-- `tests/unit/test_bootstrap_seed.py` pins against each other. This file is for
-- the databases that already exist. All three must agree or that gate goes red.
--
-- SAFETY. A failed migration raises out of `_run_db_bootstrap` and the pod
-- REFUSES TO SERVE — and this service backs ext_authz for the whole platform at
-- replicas: 2. So nothing here may fail on data, exactly as in 024:
--   * Each rename is guarded on the row still holding the EXACT placeholder, so a
--     re-run is a no-op and an operator's later rename is never clobbered.
--   * Each rename is guarded on the target name being free, so the UNIQUE
--     constraint cannot raise.
--   * A row that cannot be renamed is announced and skipped, not fatal.
-- No lock_timeout, matching the rest of this corpus: `tenants` is ~75 rows and
-- this touches two of them by primary key.
--
-- DOWNGRADE: migrations/downgrades/026_rename_improvement_tenant_placeholders.sql
-- (a real, runnable, equally-guarded reversal — it lives in a subdirectory
-- because the runner globs `migrations/*.sql` non-recursively and would otherwise
-- apply it immediately after this file).

DO $$
DECLARE
    r RECORD;
    renamed INT := 0;
BEGIN
    FOR r IN
        SELECT * FROM (VALUES
            ('0195f48b-d000-7000-8000-000000000004'::uuid, 'platform-cp-4', 'prompt-improvement'),
            ('0195f48b-d000-7000-8000-000000000005'::uuid, 'platform-cp-5', 'procedure-improvement')
        ) AS v(tenant_id, old_name, new_name)
    LOOP
        -- Already done (or never applied here): leave it exactly as it is.
        IF NOT EXISTS (
            SELECT 1 FROM tenants WHERE id = r.tenant_id AND name = r.old_name
        ) THEN
            RAISE NOTICE '026: % is not named % — leaving it alone (current: %)',
                r.tenant_id, r.old_name,
                COALESCE((SELECT name FROM tenants WHERE id = r.tenant_id), '<no such row>');
            CONTINUE;
        END IF;

        -- UNIQUE(name) must not be able to raise and refuse the pod service.
        IF EXISTS (SELECT 1 FROM tenants WHERE name = r.new_name) THEN
            RAISE WARNING '026: cannot rename % to % — that name is already taken; skipping',
                r.old_name, r.new_name;
            CONTINUE;
        END IF;

        UPDATE tenants SET name = r.new_name WHERE id = r.tenant_id;
        renamed := renamed + 1;
        RAISE NOTICE '026: renamed % -> % (%)', r.old_name, r.new_name, r.tenant_id;
    END LOOP;

    RAISE NOTICE '026: % row(s) renamed; no licence, token or FK was touched', renamed;
END $$;

-- Verify: the two ids, their names, and the licences that did NOT move.
SELECT
    t.id,
    t.name,
    t.byo_mode,
    count(l.id) AS licences
FROM tenants t
LEFT JOIN license l ON l.tenant_id = t.id
WHERE t.id IN (
    '0195f48b-d000-7000-8000-000000000004',
    '0195f48b-d000-7000-8000-000000000005'
)
GROUP BY t.id, t.name, t.byo_mode
ORDER BY t.id;
