-- 073: grant `w2v` access to the svc-agent-api service tenant.
--
-- WHY (owner decision 2026-10-21/22). tts-backend (w2v, Keycloak client
-- portal/w2v-backend) resolves its entitlement with
-- GET /licenses/active?tenant_id=9a4366bf-5684-59dd-b36c-2cfffef9607e&app_key=w2v and is
-- refused 403 `tenant_not_accessible` (auth/middleware.caller_may_retarget): tenant
-- 9a4366bf-5684-59dd-b36c-2cfffef9607e (`svc-agent-api`, kind 'service') is owned by
-- `platform` — scripts/provision_service_token.py registers it under --tenant-owner and,
-- until this change, had no grant step — and `w2v` neither owns it nor holds a grant.
--
-- WHAT THIS ADDS (data only, no schema):
--   tenant_app_grant  (svc-agent-api, w2v), granted_by 'migration-073' — exactly 069's
--                     shape.
-- Ownership is UNCHANGED (stays `platform`). The grant is tenant-level ("act on this
-- tenant"); what w2v may ISSUE there is still app_issuer. No other tenant is touched.
--
-- FRESH DATABASE: the tenant is registered at runtime by provision_service_token.py, so a
-- fresh/CI database does not hold it and this file is a no-op there (and the runner
-- baselines it without running it anyway). Provision it with
-- `provision_service_token.py ... --grant-owner w2v`, or
-- PUT /tenants/{id}/grants/w2v as the owner / break-glass client.
--
-- GUARDED + IDEMPOTENT. Inserts only where the tenant exists and is NOT owned by the
-- grantee; ON CONFLICT on the active-grant unique index, so an existing active grant
-- (from an operator or the API) is left alone and a re-run is a no-op. NOTE: a
-- REVOKED row does not occupy the partial unique index, so a deliberate manual re-run
-- after a revocation writes a new active grant. The runner applies each file once
-- (schema_migrations), so that never happens on its own.
--
-- ONE TRANSACTION (BEGIN/COMMIT as statements, same pattern as 050 / 067 / 068 / 069).
--
-- DOWNGRADE: migrations/downgrades/073_svc_agent_api_w2v_grant.sql.

BEGIN;

INSERT INTO tenant_app_grant (tenant_id, grantee_owner_key, granted_by)
SELECT t.id, g.owner_key, 'migration-073'
FROM tenants t
CROSS JOIN (VALUES ('w2v')) AS g(owner_key)
JOIN tenant_owner o ON o.key = g.owner_key
WHERE t.id = '9a4366bf-5684-59dd-b36c-2cfffef9607e'  -- svc-agent-api
  AND t.owner_key <> g.owner_key
ON CONFLICT (tenant_id, grantee_owner_key) WHERE revoked_at IS NULL DO NOTHING;

COMMIT;
