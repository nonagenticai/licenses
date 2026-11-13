-- DOWNGRADE for migrations/026_rename_improvement_tenant_placeholders.sql
--
-- Reverses the two renames: `prompt-improvement` -> `platform-cp-4` and
-- `procedure-improvement` -> `platform-cp-5`.
--
-- ⚠️ THIS FILE IS NOT AUTO-APPLIED, AND MUST NOT BE. `scripts/apply_migrations.py`
-- discovers work with `migrations_dir.glob("*.sql")` — non-recursive — so a file
-- in this subdirectory is invisible to the runner. If it were a sibling of 026 it
-- would sort immediately after it and undo the migration on the same boot. Do not
-- move it up a level.
--
-- HOW TO RUN IT (against the primary -- a pooled connection cannot run this):
--   psql "$DIRECT_DATABASE_URL" -f - \
--     < migrations/downgrades/026_rename_improvement_tenant_placeholders.sql
--
-- Then also delete 026's stamp from the tracking table, or the runner will
-- consider it applied and never re-run it:
--   DELETE FROM schema_migrations
--    WHERE filename = '026_rename_improvement_tenant_placeholders.sql';
-- (`schema_migrations(filename, applied_at)` — scripts/apply_migrations.py:168.)
--
-- ⚠️ AND REVERT THE CODE. The names also live in `models.PLATFORM_TENANTS`,
-- `migrations/014_seed_platform_internal_tenants.sql` and
-- `scripts/backfill_tenants_byo_mode.py`. `seed_platform_tenants` runs on EVERY
-- boot but uses `ON CONFLICT DO NOTHING` and never UPDATEs, so it will not fight
-- this downgrade — the rows already exist by id and are left untouched. The code
-- would merely be describing a state the database no longer holds, and
-- `tests/unit/test_bootstrap_seed.py` pins the constant against 014, not against
-- the database. So this SQL is safe to run alone; revert the code for honesty,
-- not for correctness.
--
-- ⚠️ NOTE WHAT A DOWNGRADE HERE ACTUALLY BUYS YOU. It does not restore a truth —
-- it restores a placeholder. The evidence that …0004 is fr's `prompt-improvement`
-- tenant and …0005 its `procedure-improvement` tenant is in fr migrations 062/066,
-- the live fr `workspaces` table and the Keycloak realm-patch script, and none of
-- those change because this file ran. Use it only to unblock something that broke
-- on the NAME, and then fix that caller — the registry will still be wrong.
--
-- Guarded identically to 026: renames only a row still holding the exact
-- post-026 name, skips if the old name has been re-taken, and cannot raise.

DO $$
DECLARE
    r RECORD;
    reverted INT := 0;
BEGIN
    FOR r IN
        SELECT * FROM (VALUES
            ('0195f48b-d000-7000-8000-000000000004'::uuid, 'prompt-improvement', 'platform-cp-4'),
            ('0195f48b-d000-7000-8000-000000000005'::uuid, 'procedure-improvement', 'platform-cp-5')
        ) AS v(tenant_id, current_name, restored_name)
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM tenants WHERE id = r.tenant_id AND name = r.current_name
        ) THEN
            RAISE NOTICE '026-down: % is not named % — leaving it alone (current: %)',
                r.tenant_id, r.current_name,
                COALESCE((SELECT name FROM tenants WHERE id = r.tenant_id), '<no such row>');
            CONTINUE;
        END IF;

        IF EXISTS (SELECT 1 FROM tenants WHERE name = r.restored_name) THEN
            RAISE WARNING '026-down: cannot restore % — that name is already taken; skipping',
                r.restored_name;
            CONTINUE;
        END IF;

        UPDATE tenants SET name = r.restored_name WHERE id = r.tenant_id;
        reverted := reverted + 1;
        RAISE NOTICE '026-down: restored % -> % (%)', r.current_name, r.restored_name, r.tenant_id;
    END LOOP;

    RAISE NOTICE '026-down: % row(s) reverted; no licence, token or FK was touched', reverted;
END $$;

-- Verify.
SELECT id, name, byo_mode FROM tenants
WHERE id IN (
    '0195f48b-d000-7000-8000-000000000004',
    '0195f48b-d000-7000-8000-000000000005'
)
ORDER BY id;
