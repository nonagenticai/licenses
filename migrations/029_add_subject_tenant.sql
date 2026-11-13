-- 029: give `subject` a tenant PARENT, so "user OF a tenant" becomes expressible.
--
-- WHAT IS MISSING. `subject` has four columns — id, keycloak_sub, keycloak_realm,
-- created_at — and NONE of them says which organisation the subject belongs to.
-- `tenants` has no parent column either. So the registry can express
-- "(tenant, app, subject) holds a licence" but cannot express the far more basic
-- "this subject IS a member of this tenant". Every consumer that needs it has had
-- to infer it from the licence rows, which inverts the relationship: membership
-- becomes a consequence of entitlement instead of its precondition.
--
-- WHY IT MATTERS NOW, stated plainly so the next reader does not have to
-- reconstruct it. The target model is:
--     tenant   = the org, the billable party, NEVER a machine
--     subject  = a real Keycloak human OR a service actor (`svc-*`)
--     licence  = (tenant, app, subject)
-- Measured on prod 2026-09-05, the registry does not hold that shape: 43 of the
-- 76 `tenants` rows are `svc-*` service actors (`kind = 'service'`), i.e. machines
-- sitting in the org table. 44 licences hang off them. Because a service actor is
-- modelled as an ORG rather than as a subject OF an org, app-to-app spend is
-- billed to the middleman service instead of the originating tenant.
--
-- ⛔ THIS FILE DOES NOT FIX THAT, AND MUST NOT BE READ AS DOING SO. It adds the
-- column that makes the corrected shape SAYABLE. Moving the 43 `svc-*` rows is a
-- separate, destructive, ordering-sensitive operation whose plan is written up in
-- `docs/svc-tenant-migration-plan.md` and which is DELIBERATELY NOT EXECUTED here.
-- The ordering constraint that forces the split: `billing_service/src/tenancy.py`
-- carries an INGRESS resolver, `LEGACY_TENANT_NAMES`, keyed on those very names.
-- Removing or breaking a key 400s a live caller, so the resolver must learn the
-- new shape BEFORE any row moves. Read that document before touching this data.
--
-- WHY NULLABLE, WITH NO BACKFILL. This migration must be a NO-OP for every
-- running caller, and a column nobody writes and nobody reads is exactly that.
--   * NOT NULL would require a value for all 254 existing subjects, and the
--     correct value for most of them is a business fact this database does not
--     hold (see the plan document — it is the same question the `svc-*` move
--     turns on). Inventing one here would put a guess somewhere durable and it
--     would later read as a measurement.
--   * A backfill "from the licence rows" is precisely the inverted inference this
--     column exists to replace, and it is not even well defined: a subject may
--     hold licences under more than one tenant, so the join returns a SET, not a
--     parent. Deriving a parent from it would silently pick one.
-- NULL therefore means "nobody has stated this subject's tenant yet" — an honest
-- absence — and never "no tenant".
--
-- FK SHAPE, mirroring `fk_license_tenant` (migration 016) and
-- `TenantAppByoMode.tenant_id`:
--   * ON DELETE RESTRICT — a tenant with members cannot be deleted out from
--     under them. (Tenants are soft-deleted via `archived_at` in any case; there
--     is no hard-delete path. RESTRICT keeps that true structurally.)
--   * The constraint is NAMED. Unnamed, `create_all` emits PostgreSQL's default
--     `subject_tenant_id_fkey` while this file emits `fk_subject_tenant`, and the
--     two schemas would differ by constraint name — the exact drift 016's comment
--     in models.py documents having suffered, and the thing this file's guard
--     (`WHERE conname = 'fk_subject_tenant'`) would then read FALSE for.
--   * The index is declared HERE and as an explicit `Index(...)` in models.py.
--     NOT via `index=True` on the column: that emits a SECOND identical btree
--     named `ix_subject_tenant_id`, which is how `license.tenant_id` ended up
--     with two copies of one index on a fresh database and one on prod.
--
-- BASELINE INTERACTION (scripts/apply_migrations.py). On a FRESH database this
-- file NEVER EXECUTES — the runner stamps every .sql file as applied without
-- running it, because `create_all` has already built the schema from models.py.
-- That is why `Subject.tenant_id`, `Index("idx_subject_tenant")` and the named FK
-- are added to models.py in the SAME commit. Without the ORM half, a from-scratch
-- database would never get this column at all and this file would be the only
-- place it exists — which is the divergence class
-- `tests/unit/test_schema_parity.py` exists to catch, and which it WILL catch if
-- the two halves disagree.
--
-- SAFETY. A failed migration raises out of `_run_db_bootstrap` and the pod REFUSES
-- TO SERVE — and this service backs ext_authz for the whole platform at
-- `replicas: 2`. So nothing here may fail on data:
--   * ONE DO block: the column, the FK and the index land together or not at all.
--   * Guarded on the column, so a re-run is a no-op.
--   * The FK is added to a column in which EVERY row is NULL by construction (it
--     was created in the same block), so validation cannot find a violating row.
--     No NOT VALID / VALIDATE dance is needed — unlike 016, which retyped a
--     column that already held 258 values.
--   * `subject` is 254 rows and `tenants` is 76, so the ACCESS EXCLUSIVE /
--     SHARE ROW EXCLUSIVE window is sub-millisecond; the runner's
--     MIGRATION_LOCK_TIMEOUT_MS bounds the wait to get it.
--
-- DOWNGRADE: migrations/downgrades/029_add_subject_tenant.sql

DO $$
DECLARE
    n_subjects bigint;
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'subject' AND column_name = 'tenant_id'
    ) THEN
        RAISE NOTICE 'subject.tenant_id already present — nothing to do';
        RETURN;
    END IF;

    -- UUID, matching tenants.id and models.py's PGUUID(as_uuid=True). Nullable
    -- with NO default: a default would make every future INSERT assert a parent
    -- nobody chose.
    ALTER TABLE subject ADD COLUMN tenant_id UUID;

    ALTER TABLE subject
        ADD CONSTRAINT fk_subject_tenant
        FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE RESTRICT;

    -- Mirrors Index("idx_subject_tenant") in models.py. Without this line an
    -- EXISTING database gets the column and the constraint but no index, while a
    -- fresh create_all'd one has all three — the ORM/corpus divergence the
    -- schema-parity gate exists to report.
    CREATE INDEX idx_subject_tenant ON subject (tenant_id);

    SELECT count(*) INTO n_subjects FROM subject;
    RAISE NOTICE 'subject.tenant_id added — % subject rows, all NULL (no backfill by design)',
        n_subjects;
END $$;

-- Verify. Every row NULL is the CORRECT post-condition of this file, not a
-- warning: parentage is stated deliberately, never inferred.
SELECT count(*)                                    AS subjects,
       count(tenant_id)                            AS with_tenant,
       count(*) - count(tenant_id)                 AS without_tenant
FROM subject;
