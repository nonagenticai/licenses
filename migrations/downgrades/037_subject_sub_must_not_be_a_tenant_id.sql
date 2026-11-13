-- Downgrade 037. A NO-OP, and deliberately so — same shape as downgrade 035.
--
-- 037 creates no schema object and changes no row: no constraint, no index, no
-- column, no trigger, no DELETE, no UPDATE. It is a REPORT — a DO block that
-- walks the `subject` × `tenants` join from the subject side and RAISEs
-- NOTICE/WARNING. There is nothing to undo, and this file exists because the
-- repo convention is that every migration ships
-- `migrations/downgrades/<same name>` (see 024/026/028/031/032/034/035 and their
-- tests). A missing file would read as "not yet written", a different and
-- misleading claim.
--
-- ⚠️ WHAT ROLLING BACK WOULD ACTUALLY COST, IF IT COULD. Nothing — because the
-- enforcement 037 documents does not live in 037:
--   * DOOR 1 — `DatabaseManager.ensure_tenant` (+ `seed_platform_tenants`,
--              `scripts/backfill_tenants_byo_mode`) refuse a NEW tenant id that
--              is already a principal, via
--              `tenant_subject_invariant.subject_for_tenant_id`.
--   * DOOR 2 — `DatabaseManager.create_or_get_subject` refuses a NEW subject
--              whose `keycloak_sub` is already a registered `tenants.id`, via
--              `tenant_subject_invariant.tenant_for_subject_sub`. This is the
--              guard 037 exists to record.
--   * BOTH   — `report_at_boot` at boot, and on /metrics
--              `nonagentic_license_tenant_is_subject_rows{class,match,source}`
--              plus `nonagentic_license_subject_principal_refusals`.
-- Reverting this file removes none of those. To genuinely disable the guard you
-- would have to revert the commit, which is the honest way to do it.
--
-- ⛔ DO NOT "IMPROVE" THIS INTO A DROP OF ANYTHING, and do not read the absence
-- of a constraint as an omission. There is no `ck_subject_sub_not_a_tenant` and
-- there never was: a CHECK cannot express a cross-table predicate, and the
-- trigger alternative was rejected in 035 on a measured fact — plus `create_all()`
-- would not reproduce a trigger on a fresh database, so the guarantee would hold
-- on prod and silently not hold in CI and in a DR rebuild.
--
-- ⛔ AND DO NOT "IMPROVE" IT INTO A DATA REPAIR. 037 deliberately re-keys
-- nothing. Every offending id is a live serving identity whose billing history
-- lives in the BILLING service's database with NO foreign key back to `tenants`,
-- so a re-key would silently detach money this service cannot even count. That is
-- a commercial decision, tracked in issue #217, not a downgrade step.

DO $$
BEGIN
    RAISE NOTICE
        '037 downgrade: nothing to undo — 037 created no schema object and '
        'changed no row. The invariant is enforced by ensure_tenant (door 1), '
        'create_or_get_subject (door 2), report_at_boot and the '
        'nonagentic_license_tenant_is_subject_rows / '
        'nonagentic_license_subject_principal_refusals gauges, none of which '
        'this file can remove. Revert the commit instead.';
END $$;
