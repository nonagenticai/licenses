-- 067: the strata apps — `strata-configurator` and `care-bus` — as owners and apps,
-- and strata_configurator's licences client.
--
-- WHY (strata_configurator plan §W6, 2026-10-17). strata_configurator
-- (configurator.nonagentic.ai, Rust) issues a licence per tenant on onboarding and
-- introspects it on every tenant request (gate 2) — commonpractice's pattern
-- (crates/configurator-api/src/licenses_admin.rs: PUT /tenants/{id},
-- POST /licenses/user/{sub}, POST /licenses/{id}/tokens, POST /license/revoke, as the
-- secure-apps client-credentials SA `strata-configurator-licenses-admin`). Before this
-- file licences had no `strata-configurator` or `care-bus` app, owner or client
-- (`git grep -i strata origin/main` -> nothing), so every one of those calls failed.
--
-- WHAT THIS ADDS (data only, no schema):
--   tenant_owner  strata-configurator, care-bus — cell apps own their tenants.
--   api_client    ('secure-apps', 'strata-configurator-licenses-admin')
--                 -> strata-configurator, NOT cross-owner. Holds `license-admin` once
--                 the gitops keycloak realm-patch creates it; until then the row is
--                 inert. Mirrors ('secure-apps', 'commonpractice-licenses-admin').
--                 care-bus gets NO client here: none exists yet (break-glass only).
--   app           strata-configurator "Strata Configurator", audience `human`
--                 care-bus            "Care Bus",            audience `human`
--   plan          <app>/standard, global, 600 rpm / 60 burst, perpetual,
--                 quota 1,000,000 / token budget 50,000,000 — commonpractice/standard's
--                 shape and seed_apps.py's DEFAULT_QUOTA_MONTHLY /
--                 DEFAULT_TOKEN_BUDGET_MONTHLY (no plan is ever unlimited).
--   app_issuer    strata-configurator -> {strata-configurator}
--                 care-bus            -> {care-bus}
--                 Each issued by its OWN owner only (commonpractice -> {commonpractice}):
--                 main._authorize_issuance then admits the configurator's SA to issue /
--                 revoke `strata-configurator` licences on tenants it owns, and nobody
--                 else (not fr, not platform) except audited break-glass.
--
-- NOT ADDED. No bundle, no ensurer, no app_requires edge, no grant: the configurator
-- issues its licence directly (POST /licenses/user/{sub}), not through a bundle.
-- Offerings (BYO-only) come from scripts/seed_offerings.py, never a migration.
--
-- FRESH DATABASE: this file is stamped without running; the owners + client come from
-- the boot seed (models.TENANT_OWNERS / API_CLIENTS), the apps + plans + issuers from
-- scripts/seed_apps.py (models.APP_ISSUER_OVERRIDES) — pinned equal at head by
-- tests/unit/test_migration_067_strata_apps.py.
--
-- ONE TRANSACTION. Every INSERT is ON CONFLICT DO NOTHING / NOT EXISTS: an operator's
-- row is never overwritten, a re-run is a no-op.
--
-- DOWNGRADE: migrations/downgrades/067_strata_configurator_and_care_bus_apps.sql
-- (REFUSES while any tenant is owned by either owner or anything references the apps).
-- Roll back models.py and scripts/seed_apps.py too, or the boot / DR seed re-creates
-- the rows.

BEGIN;

INSERT INTO tenant_owner (key, display_name) VALUES
    ('strata-configurator', 'Strata Configurator'),
    ('care-bus', 'Care Bus')
ON CONFLICT (key) DO NOTHING;

INSERT INTO api_client (realm, client_id, owner_key, cross_owner) VALUES
    ('secure-apps', 'strata-configurator-licenses-admin', 'strata-configurator', false)
ON CONFLICT (realm, client_id) DO NOTHING;

INSERT INTO app (key, name, description, audience) VALUES
    (
        'strata-configurator',
        'Strata Configurator',
        'Strata Configurator — per-tenant licence issued on onboarding and gated at the '
        'app layer (gate 2), migration 067.',
        'human'
    ),
    (
        'care-bus',
        'Care Bus',
        'Care Bus — per-tenant licence for the interaction layer, issued by its own '
        'owner, migration 067.',
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
WHERE a.key IN ('strata-configurator', 'care-bus')
  AND NOT EXISTS (
        SELECT 1 FROM plan p
         WHERE p.app_id = a.id AND p.code = 'standard' AND p.region_id IS NULL
  );

INSERT INTO app_issuer (app_id, owner_key)
SELECT a.id, a.key
FROM app a
WHERE a.key IN ('strata-configurator', 'care-bus')
ON CONFLICT (app_id, owner_key) DO NOTHING;

COMMIT;
