-- 053: a tenant NAME (and external_ref) is unique per owner among LIVE tenants only.
--
-- WHY. 050 made `(owner_key, name)` and `(owner_key, external_ref)` unique with NO
-- archived filter. Licences tenants are never deleted — archiving is the retirement
-- (PATCH /tenants/{id}/archive) — so an archived/offboarded tenant held its name and
-- its external_ref for its owner FOREVER: self-registration churn, or an fr customer
-- offboarded and later re-provisioned under the same tenant_slug, could never get
-- that name back. 051 had to RENAME the legacy netcare tenant (netcare-legacy) for
-- exactly this reason.
--
-- WHAT. Both unique indexes become PARTIAL: `WHERE archived_at IS NULL`. A name /
-- external_ref may now be held by ONE live tenant of an owner plus any number of
-- archived ones. The application follows (models._tenant_by_name,
-- get_tenant_by_external_ref, get_tenant_scoped): a NAME resolves to the LIVE row
-- only; an archived tenant is addressable by ID only (several archived rows may
-- share a name, so a name cannot pick one). POST /tenants' external_ref idempotency
-- matches live rows only, so re-provisioning an archived fr tenant creates a NEW live
-- tenant instead of handing back the archived one (whose licences are refused).
-- Un-archiving a tenant whose name / external_ref a live tenant of the same owner
-- now holds is refused 409 `tenant_name_taken` / `tenant_external_ref_taken`.
--
-- SAFE TO APPLY. The old indexes were stricter (all rows), so the live subset is
-- already unique: CREATE cannot fail on existing data. Prod read-only check
-- (the primary, 2026-10-14): 0 duplicate live (owner_key, name), 0 duplicate live
-- (owner_key, external_ref).
--
-- IDEMPOTENT: DROP IF EXISTS + CREATE, so a re-run (or a database whose create_all
-- already built the partial indexes from models.Tenant) ends in the same state.
-- ONE TRANSACTION (the runner autocommits each statement; BEGIN/COMMIT run as
-- statements, same pattern as 050). `tenants` is small, so the brief ACCESS
-- EXCLUSIVE lock of DROP/CREATE INDEX is acceptable.
--
-- DOWNGRADE: migrations/downgrades/053_tenant_names_unique_among_live.sql. It
-- REFUSES while a live and an archived tenant of one owner share a name or an
-- external_ref (the full indexes could not be rebuilt); rename / re-ref first.

BEGIN;

DROP INDEX IF EXISTS uq_tenants_owner_name;
CREATE UNIQUE INDEX uq_tenants_owner_name
    ON tenants (owner_key, name) WHERE archived_at IS NULL;

DROP INDEX IF EXISTS uq_tenants_owner_external_ref;
CREATE UNIQUE INDEX uq_tenants_owner_external_ref
    ON tenants (owner_key, external_ref)
    WHERE external_ref IS NOT NULL AND archived_at IS NULL;

COMMIT;
