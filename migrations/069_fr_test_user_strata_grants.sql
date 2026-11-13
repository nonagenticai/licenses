-- 069: grant `strata-configurator` and `care-bus` access to the fr-test-user tenant.
--
-- WHY (owner decision 2026-10-18). fr-test-user 0195f48b-d000-7000-8000-000000000099 is
-- the shared browser-test tenant (models.PLATFORM_TENANTS). It is owned by `platform`
-- (050's backfill) and already granted to commonpractice (050 rule 3) and to the
-- owners that issue its live licences (fr, merqi, w2v). The strata apps (067) need to
-- test against the same tenant: strata_configurator's SA
-- ('secure-apps', 'strata-configurator-licenses-admin') re-registers
-- (PUT /tenants/{id}) and issues its licence on it, and is refused today with
-- 409 `tenant_owned_by_other_app` (main._reregister_existing) / 403
-- `tenant_not_accessible` (main._authorize_issuance) because `strata-configurator`
-- neither owns nor holds a grant on the tenant. care-bus (068 client) is in the same
-- position.
--
-- WHAT THIS ADDS (data only, no schema):
--   tenant_app_grant  (fr-test-user, strata-configurator), (fr-test-user, care-bus),
--                     granted_by 'migration-069' — exactly 050 rule 3's shape.
-- Ownership is UNCHANGED (stays `platform`). The grant is tenant-level ("act on this
-- tenant"); what each grantee may ISSUE there is still app_issuer: strata-configurator
-- -> {strata-configurator}, care-bus -> {care-bus} (067). No other tenant is touched.
--
-- FRESH DATABASE: the runner baselines this file without running it, exactly like
-- 050's grants — a rebuilt database carries no grants on fr-test-user at all, for any
-- owner. Re-run it by hand (or PUT /tenants/{id}/grants/{owner} as the break-glass
-- client) if one is needed there.
--
-- GUARDED + IDEMPOTENT. Inserts only where the tenant exists and is NOT owned by the
-- grantee; ON CONFLICT on the active-grant unique index, so an existing active grant
-- (from an operator or the API) is left alone and a re-run is a no-op. NOTE: a
-- REVOKED row does not occupy the partial unique index, so a deliberate manual re-run
-- after a revocation writes a new active grant. The runner applies each file once
-- (schema_migrations), so that never happens on its own.
--
-- ONE TRANSACTION (BEGIN/COMMIT as statements, same pattern as 050 / 067 / 068).
--
-- DOWNGRADE: migrations/downgrades/069_fr_test_user_strata_grants.sql.

BEGIN;

INSERT INTO tenant_app_grant (tenant_id, grantee_owner_key, granted_by)
SELECT t.id, g.owner_key, 'migration-069'
FROM tenants t
CROSS JOIN (VALUES ('strata-configurator'), ('care-bus')) AS g(owner_key)
JOIN tenant_owner o ON o.key = g.owner_key
WHERE t.id = '0195f48b-d000-7000-8000-000000000099'  -- fr-test-user
  AND t.owner_key <> g.owner_key
ON CONFLICT (tenant_id, grantee_owner_key) WHERE revoked_at IS NULL DO NOTHING;

COMMIT;
