-- 083: secure-apps twins of the two remaining portal S2S licence clients
-- (realm-per-app P7 chain C, "realm portal = staff only", George GO 2026-10-25).
--
-- WHY. Every client that calls ACROSS apps moves to realm `secure-apps` (realm-per-app
-- design §2). Two licence-writing service accounts still mint from `portal` only:
--   portal/w2v-backend           -> w2v   (050)   the w2v services' licences SA
--   portal/provisioning-backend  -> fr    (050)   provisioning's licences SA
-- gitops keycloak PATCH_ONLY=p7-s2s-secure-apps creates a client of the SAME id in
-- secure-apps for each. A client NOT in api_client is refused 403 `unknown_api_client`
-- on every tenant / licence / grant / bundle write, and the owner is keyed on
-- (token iss realm, azp) — so each twin needs its own row before its service flips
-- its token realm, or that service's licence writes 403.
--
-- SAME OWNERS as the portal rows (w2v / fr): the secure-apps client is the same
-- service under a different realm, not a new principal. Not cross-owner.
-- The portal rows are NOT removed here: they stay until the portal clients are deleted
-- (gitops keycloak delete Job), and a row for a deleted client authorises nothing.
--
-- Data only, idempotent (ON CONFLICT DO NOTHING — an operator's row is never
-- overwritten), one transaction. A FRESH database gets the rows from the boot seed
-- (models.API_CLIENTS, pinned equal at head by tests/unit/test_migration_083_*.py).
--
-- DOWNGRADE: migrations/downgrades/083_secure_apps_s2s_api_clients.sql (deletes the two
-- rows). Remove them from models.API_CLIENTS too, or the boot seed re-inserts them.

BEGIN;

INSERT INTO api_client (realm, client_id, owner_key, cross_owner) VALUES
    ('secure-apps', 'w2v-backend', 'w2v', false),
    ('secure-apps', 'provisioning-backend', 'fr', false)
ON CONFLICT (realm, client_id) DO NOTHING;

COMMIT;
