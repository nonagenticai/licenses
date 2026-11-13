-- Downgrade 053: tenant names / external_refs unique per owner across ALL rows again
-- (archived included), i.e. back to 050's indexes.
--
-- ⛔ REFUSES while two tenants of one owner share a name, or a non-NULL external_ref
-- (possible only after 053: one live + archived ones). Rebuilding the full unique
-- indexes would fail half-way. Rename the archived tenant (PATCH /tenants/{id}) or
-- clear the duplicate first. Roll back the CODE as well (models.Tenant's two
-- postgresql_where clauses), or the schema-parity gate reports the arms diverging.

BEGIN;

DO $$
DECLARE
    name_dupes INT;
    ref_dupes INT;
BEGIN
    SELECT count(*) INTO name_dupes FROM (
        SELECT 1 FROM tenants GROUP BY owner_key, name HAVING count(*) > 1
    ) d;
    SELECT count(*) INTO ref_dupes FROM (
        SELECT 1 FROM tenants WHERE external_ref IS NOT NULL
        GROUP BY owner_key, external_ref HAVING count(*) > 1
    ) d;
    IF name_dupes > 0 OR ref_dupes > 0 THEN
        RAISE EXCEPTION
            'downgrade 053 REFUSED: % (owner_key, name) and % (owner_key, external_ref) '
            'pair(s) are shared by a live and an archived tenant (or several archived); '
            'rename / clear the archived duplicates before restoring the full unique indexes',
            name_dupes, ref_dupes;
    END IF;
END $$;

DROP INDEX IF EXISTS uq_tenants_owner_name;
CREATE UNIQUE INDEX uq_tenants_owner_name ON tenants (owner_key, name);

DROP INDEX IF EXISTS uq_tenants_owner_external_ref;
CREATE UNIQUE INDEX uq_tenants_owner_external_ref
    ON tenants (owner_key, external_ref) WHERE external_ref IS NOT NULL;

-- Un-stamp 053 so scripts/apply_migrations.py re-applies it (up -> down -> up).
DO $$
BEGIN
    IF to_regclass('public.schema_migrations') IS NOT NULL THEN
        DELETE FROM schema_migrations WHERE filename = '053_tenant_names_unique_among_live.sql';
    END IF;
END $$;

COMMIT;
