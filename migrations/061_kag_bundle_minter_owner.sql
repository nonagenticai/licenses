-- 061: owner for kag's OWN outbound bundle-token minter
-- (secure-apps/kag-bundle-minter -> kag).
--
-- WHY (George 2026-10-15: licences are tenant x app only;
-- ~/.claude/plans/program/20-designs/tenant-app-licence-migration-PLAN.md). The kag
-- service (kag.kag) calls worker_management, mlx-backend, mlx-ingestion-backend and
-- inference-router for an END tenant. Each hop must carry that tenant's licence for the
-- TARGET app, minted through POST /internal/tenants/{tenant}/bundle-token?app=<app>
-- (a dependency licence mints ONLY there — user-token routes answer 403
-- `dependency_licence`). That route accepts ONLY the realm role `bundle-token-minter`
-- and, since 054, is OWNER-SCOPED: the caller's app (api_client) must own the tenant,
-- hold an active grant on it, or (059) hold an app-scoped tenant_dependency_grant for
-- exactly that app; an unregistered client is 403 `unknown_api_client`. The new
-- Keycloak client (gitops keycloak realm-patch BUNDLE_MINTER_CLIENTS, realm
-- secure-apps, client kag-bundle-minter, holds `bundle-token-minter` and NOTHING else)
-- therefore needs a row.
--
-- OWNER kag. The kag -> {workers, mlx-backend, mlx-ingestion-backend}
-- tenant_dependency_grant rows (059) are keyed on owner kag, so only a kag-owned client
-- can mint those dependency licences on a tenant kag does not own. Reach, exactly as for
-- every kag-owned client (identical to 060's kag-frontend-bundle-minter):
--   * any kag-owned tenant (owner scope), any app in its held bundles' closure;
--   * another owner's tenant only through a grant kag holds there — on a `conveyor` /
--     `w2v` tenant the app-scoped (kag, workers / mlx-backend / mlx-ingestion-backend)
--     dependency grants 059 writes, so this minter mints those apps ONLY (any other
--     app is 403 `dependency_grant_scope`) — the reach 059 gave owner kag, not a
--     widening by this file;
--   * nothing else (404 `tenant_not_found`). Not cross-owner.
-- SEPARATE from kag-frontend-bundle-minter (060): one Keycloak client and secret per
-- service (per-service secrets, blast radius).
--
-- Data only, idempotent (ON CONFLICT DO NOTHING — an operator's row is never
-- overwritten), one transaction. A FRESH database gets the row from the boot seed
-- (models.API_CLIENTS, pinned equal at head by tests/unit/test_migration_061_*.py).
--
-- DOWNGRADE: migrations/downgrades/061_kag_bundle_minter_owner.sql (deletes the row).
-- Remove it from models.API_CLIENTS too, or the boot seed re-inserts it.

BEGIN;

INSERT INTO api_client (realm, client_id, owner_key, cross_owner) VALUES
    ('secure-apps', 'kag-bundle-minter', 'kag', false)
ON CONFLICT (realm, client_id) DO NOTHING;

COMMIT;
