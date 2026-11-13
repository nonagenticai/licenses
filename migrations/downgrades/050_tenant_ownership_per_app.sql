-- Downgrade 050: back to ONE global tenant namespace.
--
-- ⚠️ LOSSY: every owner, grant, api_client mapping, app issuer and external_ref is
-- dropped. Roll back the CODE as well (models.TenantOwner / ApiClient / AppIssuer /
-- TenantAppGrant, Tenant.owner_key / external_ref, the owner checks in main.py and
-- auth/middleware.py), or the schema-parity gate reports the arms diverging and
-- every tenant/licence write 500s on the missing tables.
--
-- ⛔ REFUSES if two tenants share a name (possible only after 050): re-creating the
-- global UNIQUE would fail half-way. Rename one of them first.

BEGIN;

DO $$
DECLARE
    dupes INT;
BEGIN
    SELECT count(*) INTO dupes FROM (
        SELECT name FROM tenants GROUP BY name HAVING count(*) > 1
    ) d;
    IF dupes > 0 THEN
        RAISE EXCEPTION
            'downgrade 050 REFUSED: % tenant name(s) are used by more than one owner; '
            'rename them before restoring the global UNIQUE(name)', dupes;
    END IF;
END $$;

DROP TABLE IF EXISTS tenant_app_grant;
DROP TABLE IF EXISTS app_issuer;
DROP TABLE IF EXISTS api_client;

DROP INDEX IF EXISTS uq_tenants_owner_external_ref;
DROP INDEX IF EXISTS uq_tenants_owner_name;
DROP INDEX IF EXISTS ix_tenants_name;
ALTER TABLE tenants DROP CONSTRAINT IF EXISTS fk_tenants_owner;
ALTER TABLE tenants DROP COLUMN IF EXISTS external_ref;
ALTER TABLE tenants DROP COLUMN IF EXISTS owner_key;

DROP TABLE IF EXISTS tenant_owner;

ALTER TABLE tenants ADD CONSTRAINT tenants_name_key UNIQUE (name);
CREATE UNIQUE INDEX IF NOT EXISTS ix_tenants_name ON tenants (name);

-- Un-stamp 050 so scripts/apply_migrations.py re-applies it (up -> down -> up).
-- Guarded: a database the runner never tracked has no schema_migrations table.
DO $$
BEGIN
    IF to_regclass('public.schema_migrations') IS NOT NULL THEN
        DELETE FROM schema_migrations WHERE filename = '050_tenant_ownership_per_app.sql';
    END IF;
END $$;

COMMIT;
