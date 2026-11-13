-- 021: put ck_tenants_byo_mode on databases that already exist.
--
-- WHY THIS IS NEW WORK AND NOT A RE-RUN OF 006/007.
-- 006 added a NARROW ck_tenants_byo_mode (two values) and 007 widened it to
-- three. Neither ever landed: measured 2026-08-12 against prod, `tenants`
-- carried ONLY ck_tenants_name_format, and a create_all'd database carried
-- nothing either. Both files sit in the runner's baseline set — on the first run
-- against a schema that already has the app tables, apply_all() stamps every
-- file as applied WITHOUT executing it — so the constraint existed on no
-- database anywhere. This file is unstamped, so the runner actually runs it.
--
-- The ORM now declares the same constraint (models.py Tenant.__table_args__),
-- which covers a FRESH database. This covers the existing ones.
--
-- NOT a live hole, and the commit message should not claim otherwise: every
-- write path already validates byo_mode in application code (main.py's tenant
-- PUT/PATCH handlers; DatabaseManager.set_tenant_byo_mode /
-- set_tenant_app_byo_mode). This is defence in depth against a direct SQL write
-- or a future code path that forgets.
--
-- SAFETY. A failed migration raises MigrationError out of _run_db_bootstrap and
-- the pod REFUSES TO SERVE — and this service backs ext_authz for the whole
-- platform at replicas: 2. So this file must not be able to fail:
--   * the DO block is guarded on pg_constraint, so a re-run is a no-op;
--   * ADD CONSTRAINT would fail if any row violated it, so the data was checked
--     first — prod is byo_required 27 / local_allowed 23 / platform_only 7, with
--     ZERO invalid and ZERO NULL (2026-08-12). The guarded EXISTS below re-checks
--     that at apply time and raises a NAMED error rather than a bare constraint
--     violation, so an operator reading the CrashLoop knows what to fix.
-- Values are kept identical to 007 and to models.py; a divergence between the
-- three is the failure this file exists to prevent.

DO $$
DECLARE
    bad_rows bigint;
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_tenants_byo_mode'
          AND conrelid = 'tenants'::regclass
    ) THEN
        RAISE NOTICE 'ck_tenants_byo_mode already present — nothing to do';
        RETURN;
    END IF;

    SELECT count(*) INTO bad_rows
    FROM tenants
    WHERE byo_mode IS NULL
       OR byo_mode NOT IN ('local_allowed', 'byo_required', 'platform_only');

    IF bad_rows > 0 THEN
        RAISE EXCEPTION
            'refusing to add ck_tenants_byo_mode: % tenants row(s) hold a byo_mode '
            'outside (local_allowed, byo_required, platform_only). Fix the data first '
            '— SELECT id, name, byo_mode FROM tenants WHERE byo_mode IS NULL OR '
            'byo_mode NOT IN (''local_allowed'', ''byo_required'', ''platform_only'');',
            bad_rows;
    END IF;

    ALTER TABLE tenants
        ADD CONSTRAINT ck_tenants_byo_mode
        CHECK (byo_mode IN ('local_allowed', 'byo_required', 'platform_only'));
    RAISE NOTICE 'ck_tenants_byo_mode added';
END $$;

-- Verify
SELECT conname, pg_get_constraintdef(oid)
FROM pg_constraint
WHERE conrelid = 'tenants'::regclass AND contype = 'c'
ORDER BY conname;
