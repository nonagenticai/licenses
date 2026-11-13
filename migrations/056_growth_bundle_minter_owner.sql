-- 056: owner for growth's bundle-token minter (secure-apps/growth-bundle-minter -> merqi).
--
-- WHY. growth (merqi's Twenty app, nonagenticai/growth#459) mints per-tenant licence
-- tokens at runtime for the tenants it serves:
-- POST /internal/tenants/{tenant}/bundle-token?app=raise-my-tickets.
-- That route accepts ONLY the realm role `bundle-token-minter` (license-admin is
-- refused) and, since 054, is OWNER-SCOPED: the caller's app (api_client) must own the
-- tenant or hold an active grant on it, and an unregistered client is 403
-- `unknown_api_client`. The new Keycloak client (gitops-production#4636 realm patch,
-- clusters/strato/services/keycloak; realm secure-apps, client growth-bundle-minter,
-- holds `bundle-token-minter` and NOTHING else) therefore needs a row.
--
-- OWNER merqi. growth is merqi's product, and the tenants it mints for are
-- merqi-owned — the same reach as merqi's other two minters, marketing-bundle-minter
-- and sales-bundle-minter, both owned by 'merqi' (054). Minting on any other owner's
-- tenant answers 404 (owner scope) unless merqi holds an active grant on it, which is
-- the intended reach. Not cross-owner.
--
-- Data only, idempotent (ON CONFLICT DO NOTHING — an operator's row is never
-- overwritten), one transaction. A FRESH database gets the row from the boot seed
-- (models.API_CLIENTS, pinned equal at head by tests/unit/test_migration_056_*.py).
--
-- DOWNGRADE: migrations/downgrades/056_growth_bundle_minter_owner.sql (deletes the
-- row). Remove it from models.API_CLIENTS too, or the boot seed re-inserts it.

BEGIN;

INSERT INTO api_client (realm, client_id, owner_key, cross_owner) VALUES
    ('secure-apps', 'growth-bundle-minter', 'merqi', false)
ON CONFLICT (realm, client_id) DO NOTHING;

COMMIT;
