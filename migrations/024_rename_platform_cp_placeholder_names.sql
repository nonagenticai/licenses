-- 024: two registry rows stop claiming to be anonymous control-plane slots.
--
-- WHAT IS WRONG. `0195f48b-d000-7000-8000-000000000001` and `…-000000000003` are
-- not platform scratch ids. Each is a real tenant's canonical uuid7 in the
-- Keycloak PORTAL realm — the literal `tenant_id` claim carried inside live user
-- JWTs:
--
--     …-000000000001  =  n8n-dev   (portal realm)
--     …-000000000003  =  george-dev (portal realm)
--
-- Sources, all three agreeing: gitops-production
-- clusters/strato/services/keycloak/realm-portal-configmap.yaml,
-- .../realm-patch-script-configmap.yaml, and
-- worker_management/src/api/tenant_constants.py.
--
-- Migration 014 registered them on 2026-07-10 under invented `platform-cp-N`
-- labels, because at that point nobody had matched the ids to the realm. The
-- labels were a placeholder that then stuck, and the registry — which is the
-- fleet's answer to "who is this tenant?" — has been answering "nobody in
-- particular" for two real identities ever since. Every downstream repo that
-- documents these ids (artifact_service, procedure_management, worker_management,
-- billing_service, kag) transcribed the placeholder into its own comments.
--
-- ⛔ WHAT THIS DOES NOT DO. It renames two rows. Nothing is re-pointed, merged,
-- deleted or migrated. No licence, token, budget or FK moves — `license` and
-- `tenant_app_byo_mode` reference `tenants.id`, never the name, and no column
-- anywhere in this database stores a tenant name as text (verified against prod
-- 2026-08-28 via information_schema). The rows keep their 5 and 6 licences.
--
-- ⛔ THE DUPLICATION IS NOT SETTLED HERE. The registry SEPARATELY holds `n8n-dev`
-- at `019de842-c92a-7e37-8fdd-eb0d8c5396f9`, so after this migration two rows
-- describe one human tenant, under two names, honestly. Which of them should own
-- the billing relationship is issue #86 and needs a decision this migration is
-- not entitled to make. The `-portal-realm` suffix says WHICH id this is without
-- asserting that it is the primary one. It is also required: `n8n-dev` is taken,
-- and `tenants.name` is UNIQUE.
--
-- ⛔ …-000000000002, …-000000000004 and …-000000000005 ARE DELIBERATELY LEFT
-- ALONE. `…0002` is `platform-core` here and `gerard-dev` in the portal realm —
-- a documented collision with 20 active licences and 20 unrevoked tokens;
-- renaming it takes a side in #86. `platform-cp-4`/`-5` have no evidence of what
-- they are and are not row-free (14,455 and 29,275 rows fleet-wide). An honest
-- "we do not know" is better than a second invented label.
--
-- NAME CONSTRAINTS. `ck_tenants_name_format` is `^[a-z0-9][a-z0-9-]*$` and
-- `tenants_name_key` is UNIQUE. Both new names satisfy the pattern and neither
-- was taken (checked against prod's 100+ rows before writing this).
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
-- replicas: 2. So nothing here may fail on data:
--   * Each rename is guarded on the row still holding the EXACT placeholder, so a
--     re-run is a no-op and an operator's later rename is never clobbered.
--   * Each rename is guarded on the target name being free, so the UNIQUE
--     constraint cannot raise.
--   * A row that cannot be renamed is announced and skipped, not fatal.
-- No lock_timeout, matching the rest of this corpus: `tenants` is ~100 rows and
-- this touches two of them by primary key.
--
-- DOWNGRADE: migrations/downgrades/024_rename_platform_cp_placeholder_names.sql
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
            ('0195f48b-d000-7000-8000-000000000001'::uuid, 'platform-cp-1', 'n8n-dev-portal-realm'),
            ('0195f48b-d000-7000-8000-000000000003'::uuid, 'platform-cp-3', 'george-dev-portal-realm')
        ) AS v(tenant_id, old_name, new_name)
    LOOP
        -- Already done (or never applied here): leave it exactly as it is.
        IF NOT EXISTS (
            SELECT 1 FROM tenants WHERE id = r.tenant_id AND name = r.old_name
        ) THEN
            RAISE NOTICE '024: % is not named % — leaving it alone (current: %)',
                r.tenant_id, r.old_name,
                COALESCE((SELECT name FROM tenants WHERE id = r.tenant_id), '<no such row>');
            CONTINUE;
        END IF;

        -- UNIQUE(name) must not be able to raise and refuse the pod service.
        IF EXISTS (SELECT 1 FROM tenants WHERE name = r.new_name) THEN
            RAISE WARNING '024: cannot rename % to % — that name is already taken; skipping',
                r.old_name, r.new_name;
            CONTINUE;
        END IF;

        UPDATE tenants SET name = r.new_name WHERE id = r.tenant_id;
        renamed := renamed + 1;
        RAISE NOTICE '024: renamed % -> % (%)', r.old_name, r.new_name, r.tenant_id;
    END LOOP;

    RAISE NOTICE '024: % row(s) renamed; no licence, token or FK was touched', renamed;
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
    '0195f48b-d000-7000-8000-000000000001',
    '0195f48b-d000-7000-8000-000000000003'
)
GROUP BY t.id, t.name, t.byo_mode
ORDER BY t.id;
