-- Downgrade for 088: remove fr's app-scoped dependency grants on the execution tenant.
--
-- `tenant_dependency_grant` has NO revoked_at column (059) — existence IS the grant —
-- so the only way to undo one is to DELETE the row. Idempotent: a re-run deletes
-- nothing.

BEGIN;

DELETE FROM tenant_dependency_grant d
 USING app a
 WHERE d.app_id = a.id
   AND d.tenant_id = '0195f48b-d000-7000-8000-000000000003'::uuid
   AND d.owner_key = 'fr'
   AND a.key IN ('workers', 'secrets', 'skill-builder')
   AND d.granted_by = 'migration-088';

COMMIT;
