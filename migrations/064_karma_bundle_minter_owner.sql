-- 064: owner for karma's OWN outbound bundle-token minter
-- (secure-apps/karma-bundle-minter -> kag).
--
-- WHY (George 2026-10-15: licences are tenant x app only;
-- ~/.claude/plans/program/20-designs/tenant-app-licence-migration-PLAN.md). The karma
-- service (karma.karma) calls worker_management and inference-router (among its hops)
-- for an END tenant. Each hop must carry that tenant's licence for the TARGET app, minted through
-- POST /internal/tenants/{tenant}/bundle-token?app=<app> (a dependency licence mints
-- ONLY there — user-token routes answer 403 `dependency_licence`). That route accepts
-- ONLY the realm role `bundle-token-minter` and, since 054, is OWNER-SCOPED: the
-- caller's app (api_client) must own the tenant, hold an active grant on it, or (059)
-- hold an app-scoped tenant_dependency_grant for exactly that app; an unregistered
-- client is 403 `unknown_api_client`. The new Keycloak client (gitops keycloak
-- realm-patch BUNDLE_MINTER_CLIENTS, realm secure-apps, client karma-bundle-minter,
-- holds `bundle-token-minter` and NOTHING else) therefore needs a row.
--
-- OWNER kag, the same owner as 061's kag-bundle-minter: karma runs inside the kag
-- product, so its reach is exactly every kag-owned client's —
--   * any kag-owned tenant (owner scope), any app in its held bundles' closure;
--   * another owner's tenant only through a grant kag holds there (059's app-scoped
--     dependency grants), so it mints those apps ONLY (any other app is 403
--     `dependency_grant_scope`) — the reach 059 gave owner kag, not a widening here;
--   * nothing else (404 `tenant_not_found`). Not cross-owner.
-- SEPARATE from kag-bundle-minter (061) and kag-frontend-bundle-minter (060): one
-- Keycloak client and secret per service (per-service secrets, blast radius).
--
-- Data only, idempotent (ON CONFLICT DO NOTHING — an operator's row is never
-- overwritten), one transaction. A FRESH database gets the row from the boot seed
-- (models.API_CLIENTS, pinned equal at head by tests/unit/test_migration_064_*.py).
--
-- DOWNGRADE: migrations/downgrades/064_karma_bundle_minter_owner.sql (deletes the row).
-- Remove it from models.API_CLIENTS too, or the boot seed re-inserts it.

BEGIN;

INSERT INTO api_client (realm, client_id, owner_key, cross_owner) VALUES
    ('secure-apps', 'karma-bundle-minter', 'kag', false)
ON CONFLICT (realm, client_id) DO NOTHING;

COMMIT;
