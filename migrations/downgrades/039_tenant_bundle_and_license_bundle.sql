-- Downgrade 039. Drops `license.bundle_id` (+ its FK and index) and `tenant_bundle`.
--
-- ⚠️ Roll back the CODE first: a tree that still reads License.bundle_id / TenantBundle
-- would 500 on UndefinedColumn / UndefinedTable once this runs.
--
-- Lossy for bundle licences: they keep authenticating as their own anchor app, but
-- lose `apps[]` on introspect, so a service that accepted them via `app ∈ apps[]`
-- now refuses them — the downgrade fails CLOSED. Which tenant holds which bundle is
-- lost; re-derive it from scripts/provision_tenant_bundle.py runs.
--
-- The five `non-agentic-ai` memberships 039 added are LEFT IN PLACE: bundle_app is
-- 028's table and those rows are catalogue data that harm nothing without 039.
DROP INDEX IF EXISTS idx_license_bundle;
ALTER TABLE license DROP CONSTRAINT IF EXISTS fk_license_bundle;
ALTER TABLE license DROP COLUMN IF EXISTS bundle_id;
DROP TABLE IF EXISTS tenant_bundle;
