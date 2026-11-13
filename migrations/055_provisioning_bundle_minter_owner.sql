-- 055: owner for provisioning's bundle-token minter (secure-apps/provisioning-bundle-minter -> fr).
--
-- WHY. provisioning's tenant-inference apply (provisioning src/tenant_inference.py,
-- smoke step) mints an inference-router bundle token on the tenant it just set up:
-- POST /internal/tenants/{id}/bundle-token?app=inference-router&bundle=inference-router.
-- That route accepts ONLY the realm role `bundle-token-minter` (license-admin is
-- refused) and, since 054, is OWNER-SCOPED: the caller's app (api_client) must own the
-- tenant or hold an active grant on it, and an unregistered client is 403
-- `unknown_api_client`. The new Keycloak client (gitops-production
-- clusters/strato/services/keycloak realm-patch PATCH_ONLY=provisioning-bundle-minter;
-- realm secure-apps, holds `bundle-token-minter` and NOTHING else) therefore needs a row.
--
-- OWNER fr. provisioning is fr's super-admin provisioning backend: its other licences
-- identity, portal/provisioning-backend, is already owned by 'fr' (050), and the tenants
-- it applies inference to are the fr-owned tenants it provisioned. Minting on any other
-- owner's tenant answers 404 (owner scope), which is the intended reach. Not cross-owner.
--
-- Data only, idempotent (ON CONFLICT DO NOTHING — an operator's row is never
-- overwritten), one transaction. A FRESH database gets the row from the boot seed
-- (models.API_CLIENTS, pinned equal at head by tests/unit/test_migration_055_*.py).
--
-- DOWNGRADE: migrations/downgrades/055_provisioning_bundle_minter_owner.sql (deletes
-- the row). Remove it from models.API_CLIENTS too, or the boot seed re-inserts it.

BEGIN;

INSERT INTO api_client (realm, client_id, owner_key, cross_owner) VALUES
    ('secure-apps', 'provisioning-bundle-minter', 'fr', false)
ON CONFLICT (realm, client_id) DO NOTHING;

COMMIT;
