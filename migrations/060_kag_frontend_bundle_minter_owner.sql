-- 060: owner for kag-frontend's bundle-token minter
-- (secure-apps/kag-frontend-bundle-minter -> kag).
--
-- WHY (kag-frontend separation, ~/.claude/plans/kag-frontend-separation.md,
-- "Tenant-token unit design", 2026-10-15). worker_management will ENFORCE per-tenant
-- licences (wm_licence_app_total{wrong_app|delegated|no_app} -> 0). The `workers`
-- licence on a kag tenant is a DEPENDENCY licence (059: kag -> workers, caller kag;
-- reached from kag-research's `kag` member), and a dependency licence mints ONLY
-- through POST /internal/tenants/{tenant}/bundle-token?app=workers — user-token routes
-- answer 403 `dependency_licence`. kag-frontend therefore mints per tenant:
-- app=workers (dependency, via the kag-research closure) and app=kag (member), with
-- its OWN service account. That route accepts ONLY the realm role
-- `bundle-token-minter` and, since 054, is OWNER-SCOPED: the caller's app (api_client)
-- must own the tenant, hold an active grant on it, or (059) hold an app-scoped
-- tenant_dependency_grant for exactly that app; an unregistered client is 403
-- `unknown_api_client`. The new Keycloak client (gitops keycloak realm-patch
-- BUNDLE_MINTER_CLIENTS, realm secure-apps, client kag-frontend-bundle-minter, holds
-- `bundle-token-minter` and NOTHING else) therefore needs a row.
--
-- OWNER kag. kag-frontend is kag's product UI and the tenants it serves are kag-owned
-- (057). Reach, exactly as for every kag-owned client:
--   * any kag-owned tenant (owner scope), any app in its held bundles' closure;
--   * another owner's tenant only through a grant kag holds there. NOTE: ensuring
--     `conveyor` or `w2v` writes the app-scoped dependency grants (kag, workers /
--     mlx-backend / mlx-ingestion-backend) (059 edges kag -> *), so on such a tenant
--     this minter mints those apps ONLY (any other app is 403
--     `dependency_grant_scope`) — the reach 059 gave owner kag, not a widening by
--     this file;
--   * nothing else (404 `tenant_not_found`). Not cross-owner.
-- SEPARATE from kag's own `kag-bundle-minter` (kag service -> workers / mlx-*): one
-- Keycloak client and secret per service (per-service secrets, blast radius).
--
-- Data only, idempotent (ON CONFLICT DO NOTHING — an operator's row is never
-- overwritten), one transaction. A FRESH database gets the row from the boot seed
-- (models.API_CLIENTS, pinned equal at head by tests/unit/test_migration_060_*.py).
--
-- DOWNGRADE: migrations/downgrades/060_kag_frontend_bundle_minter_owner.sql (deletes
-- the row). Remove it from models.API_CLIENTS too, or the boot seed re-inserts it.

BEGIN;

INSERT INTO api_client (realm, client_id, owner_key, cross_owner) VALUES
    ('secure-apps', 'kag-frontend-bundle-minter', 'kag', false)
ON CONFLICT (realm, client_id) DO NOTHING;

COMMIT;
