-- 088: let fr mint the `workers`, `secrets` and `skill-builder` licences of the
-- execution tenant (0195f48b-d000-7000-8000-000000000003, fr's `nonagentic-exec`).
--
-- WHY (2026-11-06). 087 revoked the 33 `tenant_app_grant` rows that migration 050's
-- backfill had handed fr on tenants it never operated. That was correct for fr's
-- PROVISIONING reach — but fr also needs the tenant's own licences at RUNTIME: it
-- presents `Authorization: Bearer <tenant workers licence>` to worker_management so
-- WM can PROVE the tenant before minting an inference-router bundle token
-- (WM `src/auth/tenant_proof.py`; a bare asserted tenant is refused
-- `END_TENANT_NOT_PROVEN`). With 087 in place fr could no longer mint for anything
-- except the conveyor tenant and the two tenants that already held dependency
-- grants, so the fr chat page in `nonagentic-exec` listed no models:
--     licenses  POST /internal/tenants/…0003/bundle-token?app=workers -> 404
--               bundle_token_refused … error=no_bundle reason=tenant_not_found
-- The tenant itself is fine: it holds `non-agentic-ai` (which carries workers,
-- secrets, skill-builder) and `inference-router`, with ACTIVE licences for all three.
--
-- WHAT THIS ADDS (data only, no schema, no licences):
--   tenant_dependency_grant (…0003, 'fr', app) for app in (workers, secrets, skill-builder).
-- This is EXACTLY the set fr already holds on fr-test-user (…0099) and the conveyor
-- tenant (5f868d42…), created by 065/066's bundle_caller ensures on 2026-10-15/16. No
-- new grant class is invented here.
--
-- ⛔ NOT a tenant_app_grant. A dependency grant is app-scoped (migration 059): it
-- admits ONLY the bundle-token route for THAT app, cannot read the tenant, and cannot
-- issue anything (`models.py`, `_resolve_tenant_for_caller`). fr's minter still gets
-- 403 `dependency_grant_scope` for any other app on this tenant, and 087's revocation
-- of fr's provisioning reach is left exactly as it was.
--
-- ⛔ WHY NOT …0001 (`system`). It is fr's INTERNAL_SYSTEM_TENANT_ID — an internal
-- cross-tenant tenant, not a workspace for chat. The operator workspace is the
-- execution tenant, which fr's own source calls "George's canonical working / demo
-- tenant" (`backend/src/middleware/internal-tenant-only.ts`).
--
-- GUARDED + IDEMPOTENT: an app is granted only where it exists, is not revoked, and is
-- actually a member of a non-revoked bundle the tenant holds — the same app-in-bundle
-- condition `main.py` enforces when the mint runs, so this file cannot write a grant
-- that could never be used. PK is (tenant_id, owner_key, app_id), so a re-run is a no-op.
--
-- ONE TRANSACTION (BEGIN/COMMIT as statements, same pattern as 050 / 066 / 079).
--
-- DOWNGRADE: migrations/downgrades/088_fr_dependency_grants_execution_tenant.sql
--   (dependency grants carry NO revoked_at — existence is the signal — so the
--    downgrade DELETEs the rows.)

BEGIN;

INSERT INTO tenant_dependency_grant (tenant_id, owner_key, app_id, granted_by)
SELECT t.id, 'fr', a.id, 'migration-088'
FROM tenants t
CROSS JOIN (VALUES ('workers'), ('secrets'), ('skill-builder')) AS g(app_key)
JOIN app a ON a.key = g.app_key AND NOT a.revoked
WHERE t.id = '0195f48b-d000-7000-8000-000000000003'  -- execution tenant
  AND t.archived_at IS NULL
  AND t.owner_key <> 'fr'                            -- fr owns nothing here; stays `platform`
  AND EXISTS (
        SELECT 1
          FROM tenant_bundle tb
          JOIN bundle b     ON b.id = tb.bundle_id AND NOT b.revoked
          JOIN bundle_app ba ON ba.bundle_id = b.id
         WHERE tb.tenant_id = t.id
           AND ba.app_id = a.id
      )
ON CONFLICT DO NOTHING;

COMMIT;
