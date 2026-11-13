-- 074: realm_owner row for the agent app's own Keycloak realm `agent`.
--
-- WHY (realm-per-app, agent runbook §4). The agent app (agent.nonagentic.ai, owner
-- `agent`, migration 070) moves to its own Keycloak realm `agent`. Exactly as 072 did
-- for commonpractice: with this row a verified token from the `agent` realm may act
-- only on tenants owner `agent` OWNS or holds an ACTIVE tenant_app_grant on; anything
-- else is 403 `realm_owner_mismatch` (cross-realm tenant forgery refused).
--
-- No user-visible change until gitops adds the `agent` realm to the trusted issuers.
--
-- FRESH DATABASE: this file is stamped without running; the row comes from
-- models.REALM_OWNERS via the boot seed — pinned equal by
-- tests/unit/test_migration_074_realm_owner_agent.py.
--
-- ONE TRANSACTION. ON CONFLICT DO NOTHING: a re-run is a no-op and an operator's row
-- is never overwritten.
--
-- DOWNGRADE: migrations/downgrades/074_realm_owner_agent.sql (deletes the row).

BEGIN;

INSERT INTO realm_owner (realm, owner_key) VALUES
    ('agent', 'agent')
ON CONFLICT (realm) DO NOTHING;

COMMIT;
