-- Downgrade 035. A NO-OP, and deliberately so.
--
-- 035 creates no schema object: no constraint, no index, no column, no trigger,
-- no row. It is a REPORT — a DO block that walks the `tenants` × `subject` join
-- and RAISEs NOTICE/WARNING per offending identity. There is nothing to undo,
-- and this file exists because the repo convention is that every migration ships
-- `migrations/downgrades/<same name>` (see 024/026/028/031/032/034 and their
-- tests). A missing file would read as "not yet written", which is a different
-- and misleading claim.
--
-- ⚠️ WHAT ROLLING BACK WOULD ACTUALLY COST, IF IT COULD. Nothing — because the
-- enforcement 035 documents does not live in 035:
--   * WRITE TIME  — `DatabaseManager.ensure_tenant` (models.py) refuses a NEW
--                   registration of an id that is already a `subject.keycloak_sub`
--                   or a `subject.id`, with `ClientSafeError`.
--   * BOOT TIME   — `services.tenant_subject_invariant.report_at_boot`, called
--                   from `_run_db_bootstrap` in main.py.
--   * CONTINUOUS  — `nonagentic_license_tenant_is_subject_rows{class,match}` on
--                   `/metrics`.
-- Reverting this file removes none of those. To genuinely disable the invariant
-- you would have to revert the commit, which is the honest way to do it.
--
-- ⛔ DO NOT "IMPROVE" THIS INTO A DROP OF ANYTHING. There is no
-- `ck_tenant_not_a_subject` and there never was: a CHECK cannot express a
-- cross-table predicate, and the trigger alternative was rejected on a measured
-- fact (`seed_platform_tenants` re-INSERTs two of the three violating ids on
-- every boot, and BEFORE INSERT triggers fire before ON CONFLICT arbitration).
-- 035's header carries the full reasoning.

DO $$
BEGIN
    RAISE NOTICE
        '035 downgrade: nothing to undo — 035 created no schema object. The '
        'invariant is enforced by ensure_tenant (write time), report_at_boot '
        '(boot) and the nonagentic_license_tenant_is_subject_rows gauge, none of '
        'which this file can remove. Revert the commit instead.';
END $$;
