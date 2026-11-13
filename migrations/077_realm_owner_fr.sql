-- 077: realm_owner row for the fr app's own Keycloak realm `fr`.
--
-- WHY (realm-per-app, fr runbook). fr (fr.nonagentic.ai, owner `fr`) moves to its own
-- Keycloak realm `fr` — the last app. Exactly as 072 (commonpractice), 074 (agent) and
-- 076 (w2v): with this row a verified token from the `fr` realm may act only on tenants
-- owner `fr` OWNS or holds an ACTIVE tenant_app_grant on; anything else is 403
-- `realm_owner_mismatch` (cross-realm tenant forgery refused). The fr fleet's tenants
-- (platform-owned system tenants) all carry an active `fr` grant (verified read-only on
-- prod 2026-10-22), so moving those identities to realm `fr` keeps them working.
--
-- No user-visible change until gitops adds the `fr` realm to the trusted issuers.
--
-- FRESH DATABASE: this file is stamped without running; the row comes from
-- models.REALM_OWNERS via the boot seed — pinned equal by
-- tests/unit/test_migration_077_realm_owner_fr.py.
--
-- ONE TRANSACTION. ON CONFLICT DO NOTHING: a re-run is a no-op and an operator's row
-- is never overwritten.
--
-- DOWNGRADE: migrations/downgrades/077_realm_owner_fr.sql (deletes the row).

BEGIN;

INSERT INTO realm_owner (realm, owner_key) VALUES
    ('fr', 'fr')
ON CONFLICT (realm) DO NOTHING;

COMMIT;
