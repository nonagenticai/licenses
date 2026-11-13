-- Downgrade 052: drop the per-bundle ensurer allowlist.
--
-- ⚠️ Roll back the CODE as well (models.BundleEnsurer, the ensurer check in
-- main.internal_ensure_tenant_bundle, DatabaseManager.create_bundle), or the
-- schema-parity gate reports the arms diverging and every bundle ensure 500s on the
-- missing table. LOSSY: any ensurer row added after 052 is dropped with the table.
--
-- Run 052's downgrade BEFORE 051's: 051's downgrade deletes the n8n-nodes bundle
-- (its ensurer rows go with it, ON DELETE CASCADE), and a later re-apply of 052
-- re-seeds them.

BEGIN;

DROP TABLE IF EXISTS bundle_ensurer;

-- Un-stamp 052 so scripts/apply_migrations.py re-applies it (up -> down -> up).
DO $$
BEGIN
    IF to_regclass('public.schema_migrations') IS NOT NULL THEN
        DELETE FROM schema_migrations WHERE filename = '052_bundle_ensurer.sql';
    END IF;
END $$;

COMMIT;
