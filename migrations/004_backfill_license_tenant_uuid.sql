-- Migration: Backfill license.tenant_id to canonical UUID
-- Date: 2026-06-18
-- Description: Completes the cluster-wide tenant-identity standard by rewriting every
-- legacy TEXT tenant_id in the `license` table (e.g. 'n8n-dev', 'gerard-dev', 'portal')
-- to its canonical UUIDv7 from the `tenants` registry. After this, `license.tenant_id`
-- is always a UUID, so /license/introspect returns a UUID and license-authz can inject
-- it directly as X-Tenant-Id without the /license/tenants/{name} name-resolution lookup.
--
-- Forward-only (no shim). The downstream services already store the RESOLVED UUID that
-- license-authz injects today, so the backfilled values match existing tenant-scoped data.
--
-- `license.tenant_id` is the ONLY tenant-bearing column in this database (verified live).

-- 1. Register the two legacy text tenants that have live licenses but no tenants row.
--    (`marketing`, `sales` — both have an active license; confirmed with product owner.)
--    uuidv7() is a PostgreSQL 18 built-in. ON CONFLICT keeps this idempotent.
INSERT INTO tenants (id, name, created_at) VALUES
    (uuidv7(), 'marketing', NOW()),
    (uuidv7(), 'sales', NOW())
ON CONFLICT (name) DO NOTHING;

-- 2. Rewrite every text-name tenant_id to its canonical UUID in one statement.
--    Rows whose tenant_id is already a UUID (no matching tenants.name) are untouched.
UPDATE license l
SET tenant_id = t.id::text
FROM tenants t
WHERE l.tenant_id = t.name;

-- 3. Drop the legacy hack tenants row whose name == its own id (a UUID). It was created
--    only to make the old name-resolution lookup succeed for a non-v7 UUID tenant; with
--    license-authz now accepting any UUID directly it is dead weight. Its licenses keep
--    their UUID tenant_id and continue to work. Safe + reversible (re-INSERTable).
DELETE FROM tenants WHERE name = id::text;

-- 4. Post-condition: NO text tenant_ids may remain in the license table. Fail the
--    migration loudly if any survive (un-mappable name → needs a tenants row first).
DO $$
DECLARE
    leftover INT;
BEGIN
    SELECT count(*) INTO leftover
    FROM license
    WHERE tenant_id IS NOT NULL
      AND tenant_id !~ '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$';
    IF leftover > 0 THEN
        RAISE EXCEPTION
            'Backfill incomplete: % license rows still have a non-UUID tenant_id. '
            'Add the missing tenants row(s) and re-run.', leftover;
    END IF;
END $$;

-- Verify
SELECT
    count(*)                                                                       AS total,
    count(*) FILTER (
        WHERE tenant_id ~ '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
    )                                                                              AS uuid_shaped
FROM license;
