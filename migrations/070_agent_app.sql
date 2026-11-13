-- 070: the `agent` app (agent.nonagentic.ai) as owner and app, and its licences client.
--
-- WHY (agent deploy + early-access plan, 2026-10-19). The agent app's backend
-- (agent-api, Rust) registers its tenants, issues a licence per tenant on signup
-- approval and introspects it — strata_configurator's pattern (067), as the
-- secure-apps client-credentials SA `agent-licenses-admin`. Before this file licences
-- had no `agent` app, owner or client, so every one of those calls failed.
--
-- WHAT THIS ADDS (data only, no schema) — 067's shape for one app:
--   tenant_owner  agent — a cell app owns its tenants.
--   api_client    ('secure-apps', 'agent-licenses-admin') -> agent, NOT cross-owner.
--                 Holds `license-admin` once the gitops keycloak realm-patch creates
--                 it; until then the row is inert. Mirrors
--                 ('secure-apps', 'strata-configurator-licenses-admin').
--   app           agent "Agent", audience `human`
--   plan          agent/standard, global, 600 rpm / 60 burst, perpetual,
--                 quota 1,000,000 / token budget 50,000,000 — the strata apps' shape
--                 and seed_apps.py's DEFAULT_QUOTA_MONTHLY / DEFAULT_TOKEN_BUDGET_MONTHLY
--                 (no plan is ever unlimited).
--   app_issuer    agent -> {agent}: issued by its OWN owner only. main._authorize_issuance
--                 then admits agent-licenses-admin to issue / revoke `agent` licences on
--                 tenants it owns, and nobody else (not fr, not platform) except audited
--                 break-glass.
--
-- NOT ADDED. No bundle, no ensurer, no app_requires edge, no grant. Offerings
-- (BYO-only) come from scripts/seed_offerings.py, never a migration.
--
-- FRESH DATABASE: this file is stamped without running; the owner + client come from
-- the boot seed (models.TENANT_OWNERS / API_CLIENTS), the app + plan + issuer from
-- scripts/seed_apps.py (models.APP_ISSUER_OVERRIDES) — pinned equal at head by
-- tests/unit/test_migration_070_agent_app.py.
--
-- ONE TRANSACTION. Every INSERT is ON CONFLICT DO NOTHING / NOT EXISTS: an operator's
-- row is never overwritten, a re-run is a no-op.
--
-- DOWNGRADE: migrations/downgrades/070_agent_app.sql (REFUSES while any tenant is owned
-- by `agent` or anything references the app). Roll back models.py and
-- scripts/seed_apps.py too, or the boot / DR seed re-creates the rows.

BEGIN;

INSERT INTO tenant_owner (key, display_name) VALUES
    ('agent', 'Agent')
ON CONFLICT (key) DO NOTHING;

INSERT INTO api_client (realm, client_id, owner_key, cross_owner) VALUES
    ('secure-apps', 'agent-licenses-admin', 'agent', false)
ON CONFLICT (realm, client_id) DO NOTHING;

INSERT INTO app (key, name, description, audience) VALUES
    (
        'agent',
        'Agent',
        'Agent (agent.nonagentic.ai) — per-tenant licence issued by its own owner on '
        'signup approval, migration 070.',
        'human'
    )
ON CONFLICT (key) DO NOTHING;

INSERT INTO plan (
    app_id, code, name, description, region_id, duration_days,
    rate_limit_rpm, rate_limit_burst, quota_monthly, token_budget_monthly
)
SELECT a.id, 'standard', 'Standard', 'Default per-tenant plan for ' || a.name || '.', NULL,
       NULL, 600, 60, 1000000, 50000000
FROM app a
WHERE a.key = 'agent'
  AND NOT EXISTS (
        SELECT 1 FROM plan p
         WHERE p.app_id = a.id AND p.code = 'standard' AND p.region_id IS NULL
  );

INSERT INTO app_issuer (app_id, owner_key)
SELECT a.id, a.key
FROM app a
WHERE a.key = 'agent'
ON CONFLICT (app_id, owner_key) DO NOTHING;

COMMIT;
