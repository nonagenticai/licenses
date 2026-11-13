-- 076: realm_owner row for the Studio app's own Keycloak realm `w2v`.
--
-- WHY (realm-per-app, Studio runbook). Studio (public host studio.nonagentic.ai;
-- internal ids stay `w2v`: owner `w2v`, realm `w2v`) moves to its own Keycloak realm
-- `w2v`. Exactly as 072 (commonpractice) and 074 (agent): with this row a verified
-- token from the `w2v` realm may act only on tenants owner `w2v` OWNS or holds an
-- ACTIVE tenant_app_grant on; anything else is 403 `realm_owner_mismatch`
-- (cross-realm tenant forgery refused).
--
-- No user-visible change until gitops adds the `w2v` realm to the trusted issuers.
--
-- FRESH DATABASE: this file is stamped without running; the row comes from
-- models.REALM_OWNERS via the boot seed — pinned equal by
-- tests/unit/test_migration_076_realm_owner_w2v.py.
--
-- ONE TRANSACTION. ON CONFLICT DO NOTHING: a re-run is a no-op and an operator's row
-- is never overwritten.
--
-- DOWNGRADE: migrations/downgrades/076_realm_owner_w2v.sql (deletes the row).

BEGIN;

INSERT INTO realm_owner (realm, owner_key) VALUES
    ('w2v', 'w2v')
ON CONFLICT (realm) DO NOTHING;

COMMIT;
