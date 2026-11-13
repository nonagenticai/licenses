-- Migration: Add a real FK license.tenant_id -> tenants.id (typed UUID + validated)
-- Date: 2026-07-20
-- Description: Converts license.tenant_id from a free-form VARCHAR(255) with NO
-- foreign key into a proper UUID column referencing tenants(id), so an
-- unprovisioned or wrong (e.g. fr-user-id-shaped) tenant_id can NEVER again be
-- persisted onto a license. This closes the structural hole behind the "$2,446"
-- money-bug class (#73/#74/#75): a per-USER id landing in the per-TENANT column.
--
-- ⛔ PRECONDITION — RUN ONLY AFTER ORPHAN RESOLUTION. As of 2026-07-20 prod has
--    64 orphan license rows across 41 distinct tenant_ids (no matching
--    tenants.id) — see scripts/audit_orphan_tenants.py. Every orphan must first
--    be resolved (provision the missing tenants row, OR repoint/revoke the
--    license) or this migration FAILS LOUD by design: step 0 raises, and even
--    without it the UUID cast (step 1) chokes on any non-UUID string and
--    VALIDATE (step 4) rejects any remaining orphan. That fail-loud is the
--    point — a half-migrated licenses DB must never silently ship.
--
-- 🚧 INTENTIONALLY NOT MERGED until the orphan audit is at zero. This file is
--    committed as prep only; do not add it to a deploy that reaches prod before
--    the orphans are cleared. Forward-only, no back-compat shim.
--
-- ⚠️ REQUIRES A COUPLED models.py CHANGE — this SQL alone is INSUFFICIENT and
--    BREAKS the service on its own (proven by CI on this branch: after the
--    retype, every ORM insert fails with
--      psycopg.errors.DatatypeMismatch: column "tenant_id" is of type uuid
--      but expression is of type character varying
--    because models.py still declares `License.tenant_id = Column(String(255))`
--    while the column is now UUID). When this migration lands, models.py must
--    change in the SAME PR: make License.tenant_id a
--      Column(PGUUID(as_uuid=True), ForeignKey("tenants.id", ondelete="RESTRICT"), ...)
--    mirroring TenantAppByoMode.tenant_id (models.py). That in turn requires a
--    codebase audit of every string-typed tenant_id use (e.g. `str(tenant.id)`
--    returns, `t.id::text = l.tenant_id` comparisons, the
--    uq_license_active_tenant_app_subject partial index) — i.e. the full,
--    George-gated FK campaign, NOT this prep-only SQL. CI RED on this draft is
--    therefore EXPECTED and demonstrates exactly this coupling.
--
-- Idempotent-safe: the runner (scripts/apply_migrations.py) stamps each file once
-- in schema_migrations, but every step here is also individually re-runnable
-- (guarded constraint add; UUID->UUID retype and re-VALIDATE are no-ops).

-- 0. Fail-loud guard: refuse to proceed while ANY orphan tenant_id exists.
--    A non-UUID legacy string is inherently an orphan (it can never equal a
--    tenants.id::text), so this single NOT EXISTS count covers both the
--    "un-castable string" and the "valid UUID but unprovisioned/deleted tenant"
--    cases before we attempt the destructive retype.
--
-- Wrapped in a single transaction (the runner splits on ';' and executes
-- BEGIN/COMMIT as statements on its autocommit connection — same pattern as
-- migration 005) so the retype is ATOMIC: if the guard raises or VALIDATE fails,
-- the whole thing rolls back rather than leaving a half-migrated hot table. The
-- table is small (few hundred rows) so the single-transaction ACCESS EXCLUSIVE
-- window is negligible; atomicity is worth more than the NOT VALID/VALIDATE
-- lock-minimization here.
BEGIN;

DO $$
DECLARE
    orphans INT;
BEGIN
    SELECT count(*) INTO orphans
    FROM license l
    WHERE l.tenant_id IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM tenants t WHERE t.id::text = l.tenant_id);
    IF orphans > 0 THEN
        RAISE EXCEPTION
            'Refusing to add FK: % license row(s) have an orphan tenant_id with no '
            'matching tenants.id. Resolve them first '
            '(scripts/audit_orphan_tenants.py), then re-run.', orphans;
    END IF;
END $$;

-- 1. Retype the column to UUID. USING casts the existing (now guaranteed
--    UUID-shaped) text values in place. Fails loudly on any non-UUID leftover.
ALTER TABLE license ALTER COLUMN tenant_id TYPE uuid USING tenant_id::uuid;

-- 2. Add the FK as NOT VALID first (does not scan/lock existing rows, only
--    enforces new/changed rows) — then VALIDATE separately to avoid a long
--    ACCESS EXCLUSIVE lock on the hot license table. ON DELETE RESTRICT: a
--    tenant with live licenses cannot be deleted out from under them. Guarded so
--    a runner re-stamp does not error on an already-present constraint.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_license_tenant'
    ) THEN
        ALTER TABLE license
            ADD CONSTRAINT fk_license_tenant
            FOREIGN KEY (tenant_id) REFERENCES tenants(id)
            ON DELETE RESTRICT
            NOT VALID;
    END IF;
END $$;

-- 3. Validate the constraint against all existing rows (fails loud if the guard
--    above was somehow bypassed and an orphan survives). Re-running on an
--    already-validated constraint is a cheap no-op.
ALTER TABLE license VALIDATE CONSTRAINT fk_license_tenant;

COMMIT;

-- Verify: expect 0 orphans and a validated FK. (Outside the transaction — a
-- read-only check; the runner stamps 016 only after every statement here
-- succeeds.)
SELECT
    (SELECT count(*) FROM license l
       WHERE NOT EXISTS (SELECT 1 FROM tenants t WHERE t.id = l.tenant_id)) AS orphans,
    c.convalidated                                                          AS fk_validated
FROM pg_constraint c
WHERE c.conname = 'fk_license_tenant';
