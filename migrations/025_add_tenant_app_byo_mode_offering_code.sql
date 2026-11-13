-- 025: record WHICH offering produced a tenant's per-product posture.
--
-- WHY. Provisioning an offering onto a tenant
-- (`POST /tenants/{n}/offerings/{app_key}/{code}`) writes its EFFECTS to two
-- places: the per-(tenant, app) posture row here, and the tenant's markup rule +
-- access charge in billing_service (S2S). The offering's own identity — the
-- `(app_key, code)` pair that names it, e.g. `(w2v-image, 'paid-managed')` — was
-- persisted NOWHERE, in either service. No table referenced `offering.id`.
--
-- So "which offerings does this tenant have" could not be answered at all. You
-- could see the posture the offering left behind and the charge billing raised,
-- but never the offering that caused them, which is the thing an operator
-- actually names when they talk about a tenant's commercial setup.
--
-- ⚠️ THIS RECORDS GOING FORWARD ONLY. History is unrecoverable — the code was
-- never written down, so it cannot be backfilled from anything. Every existing
-- row gets NULL, and NULL means "posture set before this column existed, or set
-- directly rather than by provisioning an offering". It does NOT mean "no
-- offering". Any reader MUST render that distinction rather than showing a blank
-- that reads as "nothing provisioned" — see `list_tenant_offerings`.
--
-- Deliberately a plain code string, not a FK to `offering.id`: the posture is a
-- historical fact about what was applied, and it must survive the offering being
-- renamed, re-priced or deleted from the catalogue. An ON DELETE CASCADE here
-- would erase the record of what a tenant was provisioned the moment someone
-- tidied the catalogue. `app_id` is already on this row, so `(app_id, code)` —
-- the offering's natural key — is reconstructible when the offering still exists.
--
-- BASELINE INTERACTION (scripts/apply_migrations.py). On a database with the app
-- tables but no stamps, apply_all() stamps every current file WITHOUT running
-- it, so on a FRESH database this file never executes: the column arrives from
-- create_all reading models.TenantAppByoMode.offering_code. This file is for the
-- databases that already exist.
--
-- SAFETY. A failed migration raises out of _run_db_bootstrap and the pod REFUSES
-- TO SERVE — and this service backs ext_authz for the whole platform at
-- replicas: 2. So: guarded on the column so a re-run is a no-op, and nothing
-- here can fail on data. ADD COLUMN with no DEFAULT and no NOT NULL is
-- metadata-only on PG11+ — no table rewrite, no backfill.
--
-- No lock_timeout, matching every other ADD COLUMN in this corpus (001, 002,
-- 006, 011, 019, 022, 023). `tenant_app_byo_mode` is a small table.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'tenant_app_byo_mode'
          AND column_name = 'offering_code'
    ) THEN
        RAISE NOTICE 'tenant_app_byo_mode.offering_code already present — nothing to do';
        RETURN;
    END IF;

    ALTER TABLE tenant_app_byo_mode ADD COLUMN offering_code VARCHAR(64) NULL;

    RAISE NOTICE 'tenant_app_byo_mode.offering_code added (% posture rows, all NULL = provenance unknown)',
        (SELECT count(*) FROM tenant_app_byo_mode);
END $$;

-- Verify
SELECT
    count(*) AS posture_rows,
    count(*) FILTER (WHERE offering_code IS NULL) AS provenance_unknown,
    count(*) FILTER (WHERE offering_code IS NOT NULL) AS from_an_offering
FROM tenant_app_byo_mode;
