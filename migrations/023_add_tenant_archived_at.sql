-- 023: give `tenants` an `archived_at`, so a tenant can be RETIRED rather than deleted.
--
-- WHY. There is no DELETE for a tenant and there should not be one. A tenant
-- OWNS licences, tokens, budgets, workspaces and fr `user_roles` rows; a
-- cascading delete would destroy billable history and the audit trail you would
-- need to undo the mistake. But "no removal at all" is not tenable either: a
-- mistyped provisioning run creates a real tenant permanently (the `claude`
-- tenant, 2026-08-26, is exactly that), and the operator list is a pick-list —
-- a retired tenant should not be pickable.
--
-- Soft delete resolves both. NULL = live, a timestamp = retired.
--
-- ⚠️ ARCHIVING REVOKES NOTHING. Licences, tokens, budgets and role grants all
-- survive and existing tokens keep authenticating. That is deliberate: retiring
-- a tenant must not become an accidental way to cut off live access. It removes
-- the tenant from operator lists and signals that new provisioning should not
-- target it. If you want access gone, revoke the licences — that is a different,
-- explicit act with its own audit trail.
--
-- BASELINE INTERACTION (scripts/apply_migrations.py). On a database with the app
-- tables but no stamps, apply_all() stamps every current file WITHOUT running
-- it, so on a FRESH database this file never executes: the column arrives from
-- create_all reading models.Tenant.archived_at. This file is for the databases
-- that already exist.
--
-- SAFETY. A failed migration raises out of _run_db_bootstrap and the pod REFUSES
-- TO SERVE — and this service backs ext_authz for the whole platform at
-- replicas: 2. So: guarded on the column so a re-run is a no-op, and nothing
-- here can fail on data. ADD COLUMN with no DEFAULT and no NOT NULL is
-- metadata-only on PG11+ — no table rewrite, no backfill, every existing row is
-- NULL and therefore live, which is the correct reading of "was never archived".
--
-- No lock_timeout, matching every other ADD COLUMN in this corpus (001, 002,
-- 006, 011, 019, 022). `tenants` is ~75 rows.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'tenants'
          AND column_name = 'archived_at'
    ) THEN
        RAISE NOTICE 'tenants.archived_at already present — nothing to do';
        RETURN;
    END IF;

    ALTER TABLE tenants ADD COLUMN archived_at TIMESTAMPTZ NULL;

    RAISE NOTICE 'tenants.archived_at added (% tenants, all live)',
        (SELECT count(*) FROM tenants);
END $$;

-- Verify
SELECT
    count(*) AS tenants_total,
    count(*) FILTER (WHERE archived_at IS NULL) AS live,
    count(*) FILTER (WHERE archived_at IS NOT NULL) AS archived
FROM tenants;
