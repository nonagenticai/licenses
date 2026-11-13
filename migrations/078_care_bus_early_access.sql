-- 078: care-bus as an early-access product — its realm, its tool apps, their
-- dependency edges, the `care-bus` bundle and care-bus's bundle-token minter.
--
-- WHY (owner ask 2026-10-22, care-bus-agents plan
-- docs/superpowers/plans/2026-10-22-care-bus-17-early-access-product.md, L1). care-bus
-- gets its own Keycloak realm `care-bus`; every person with a login holds their own
-- `care-bus` licence, and the tenant's licence set carries the tools care-bus calls
-- for the tenant: nonagenticai_mcp_server (app `mcp`), strata_rag and strata_ingest.
-- Before this file licences had no `strata-rag` / `strata-ingest` app, no edge from
-- care-bus to anything, no `care-bus` bundle and no care-bus minter, so care-bus could
-- only call strata with ONE pinned static tenant (every other tenant 409).
--
-- WHAT THIS ADDS (data only, no schema):
--   realm_owner   ('care-bus', 'care-bus') — 072/074/076/077's shape: a verified token
--                 from realm `care-bus` may act only on tenants owner care-bus OWNS or
--                 holds an ACTIVE grant on; else 403 `realm_owner_mismatch`. Inert until
--                 gitops adds the realm to the trusted issuers.
--   app           strata-rag    "Strata RAG",    audience `platform`
--                 strata-ingest "Strata Ingest", audience `platform`
--                 Shared platform services like `mcp` (seed_apps.py edge-gate block) and
--                 `speech` (075): a service hop for a tenant, never offered to a person.
--   plan          <app>/standard, global, 600 rpm / 60 burst, perpetual, quota 1,000,000
--                 / token budget 50,000,000 (speech/standard's shape; never unlimited).
--   app_issuer    strata-rag -> {platform}, strata-ingest -> {platform} (075's shape,
--                 models.APP_ISSUER_OVERRIDES). NOT care-bus: care-bus never issues a
--                 tool licence directly. Its tenants get them as DEPENDENCY licences
--                 minted by licences itself on ensure (059), and the tenant's owner may
--                 already revoke those (main._bundle_owner_may_revoke, allow_dependency).
--                 Issuing them would only open POST /licenses/user + user tokens for a
--                 tool — exactly what a dependency licence must never have (059).
--                 `mcp` keeps its issuers ({platform, fr}); care-bus -> {care-bus} (067)
--                 is unchanged.
--   app_requires  care-bus -> mcp, strata-rag, strata-ingest, caller care-bus: the
--                 care-bus service calls each tool for the tenant with the tenant's own
--                 licence for it, minted through bundle-token as care-bus-bundle-minter.
--                 Evidence: care-bus-agents demo-backends per-caller tokens (#30, MCP is
--                 nonagenticai_mcp_server at mcp.mcp.svc:8033) and the strata calls the
--                 plan's C1 moves off STRATA_TOKENS_TENANT_ID. None of the three
--                 requires anything (no edges out), so the closure is exactly them.
--   bundle        `care-bus` = {care-bus}. A bundle lists what is SOLD; the tools come
--                 from the closure as dependency licences (kag-service's shape, 063).
--                 Needed because ensure and bundle-token both resolve through a bundle
--                 the tenant holds.
--   bundle_ensurer care-bus -> {care-bus} (052 rule 1: care-bus issues every member).
--   api_client    ('secure-apps', 'care-bus-bundle-minter') -> care-bus, NOT
--                 cross-owner. Holds `bundle-token-minter` ONLY once the gitops keycloak
--                 realm-patch creates it (BUNDLE_MINTER_CLIENTS); until then the row is
--                 inert. A separate identity from care-bus-licenses-admin (license-admin
--                 never admits bundle-token: auth/dependencies.py BUNDLE_TOKEN_MINTER_ROLE).
--
-- GRANTS: none written here. Ensure on a care-bus-OWNED tenant writes no
-- tenant_dependency_grant (the edge caller is the tenant's own owner) and no issuer
-- grant for `care-bus` (the owner issues it); for the tools it grants `platform`, which
-- already holds a grant on every non-platform tenant (050).
--
-- A missing app (e.g. `mcp` on a database seed_apps.py never ran on) is ANNOUNCED AND
-- SKIPPED (051's rule).
--
-- FRESH DATABASE: this file is stamped without running; the realm_owner + api_client
-- rows come from the boot seed (models.REALM_OWNERS / API_CLIENTS), the apps + plans +
-- issuers from scripts/seed_apps.py (models.APP_ISSUER_OVERRIDES), and the ensurer /
-- edge seeds are mirrored in models.BUNDLE_ENSURER_SEED / APP_REQUIRES_SEED — pinned
-- equal at head by tests/unit/test_migration_078_care_bus_early_access.py.
--
-- ONE TRANSACTION. Every INSERT is ON CONFLICT DO NOTHING / NOT EXISTS: an operator's
-- row is never overwritten, a re-run is a no-op. A cycle through app_requires (an
-- operator edge closing one) aborts the whole file.
--
-- DOWNGRADE: migrations/downgrades/078_care_bus_early_access.sql (REFUSES while any
-- tenant holds `care-bus`, any licence names it, or anything references the strata
-- apps). Roll back models.py and scripts/seed_apps.py too, or the boot / DR seed
-- re-creates the rows.

BEGIN;

INSERT INTO realm_owner (realm, owner_key) VALUES
    ('care-bus', 'care-bus')
ON CONFLICT (realm) DO NOTHING;

INSERT INTO api_client (realm, client_id, owner_key, cross_owner) VALUES
    ('secure-apps', 'care-bus-bundle-minter', 'care-bus', false)
ON CONFLICT (realm, client_id) DO NOTHING;

INSERT INTO app (key, name, description, audience) VALUES
    (
        'strata-rag',
        'Strata RAG',
        'Strata RAG — retrieval for a tenant, called by care-bus with a licence of '
        'that tenant (bundle-token; a dependency of care-bus). Shared platform service, '
        'migration 078.',
        'platform'
    ),
    (
        'strata-ingest',
        'Strata Ingest',
        'Strata Ingest — document ingestion for a tenant, called by care-bus with a '
        'licence of that tenant (bundle-token; a dependency of care-bus). Shared '
        'platform service, migration 078.',
        'platform'
    )
ON CONFLICT (key) DO NOTHING;

INSERT INTO plan (
    app_id, code, name, description, region_id, duration_days,
    rate_limit_rpm, rate_limit_burst, quota_monthly, token_budget_monthly
)
SELECT a.id, 'standard', 'Standard', 'Default per-tenant plan for ' || a.name || '.', NULL,
       NULL, 600, 60, 1000000, 50000000
FROM app a
WHERE a.key IN ('strata-rag', 'strata-ingest')
  AND NOT EXISTS (
        SELECT 1 FROM plan p
         WHERE p.app_id = a.id AND p.code = 'standard' AND p.region_id IS NULL
  );

INSERT INTO app_issuer (app_id, owner_key)
SELECT a.id, 'platform'
FROM app a
WHERE a.key IN ('strata-rag', 'strata-ingest')
ON CONFLICT (app_id, owner_key) DO NOTHING;

INSERT INTO bundle (key, name, description)
VALUES (
    'care-bus',
    'Care Bus',
    'care-bus alone; its tools (mcp, strata-rag, strata-ingest) come from app_requires '
    'as dependency licences (migration 078, 2026-10-22)'
)
ON CONFLICT (key) DO NOTHING;

DO $$
DECLARE
    v_bundle  UUID;
    v_app     UUID;
BEGIN
    SELECT id INTO v_bundle FROM bundle WHERE key = 'care-bus';
    SELECT id INTO v_app FROM app WHERE key = 'care-bus';
    IF v_app IS NULL THEN
        RAISE NOTICE '078: skipping care-bus/care-bus — no such app';
        RETURN;
    END IF;
    INSERT INTO bundle_app (bundle_id, app_id)
    VALUES (v_bundle, v_app)
    ON CONFLICT (bundle_id, app_id) DO NOTHING;
END $$;

-- 052 rule 1: care-bus ensures the care-bus bundle only if it issues every app in it.
DO $$
DECLARE
    v_bundle    UUID;
    unissued    TEXT;
BEGIN
    SELECT id INTO v_bundle FROM bundle WHERE key = 'care-bus';
    IF NOT EXISTS (SELECT 1 FROM tenant_owner WHERE key = 'care-bus') THEN
        RAISE NOTICE '078: no care-bus owner — care-bus bundle left WITHOUT an ensurer';
        RETURN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM bundle_app WHERE bundle_id = v_bundle) THEN
        RAISE NOTICE '078: care-bus bundle is empty — left WITHOUT an ensurer';
        RETURN;
    END IF;
    SELECT string_agg(a.key, ', ' ORDER BY a.key) INTO unissued
      FROM bundle_app ba JOIN app a ON a.id = ba.app_id
     WHERE ba.bundle_id = v_bundle
       AND NOT EXISTS (
            SELECT 1 FROM app_issuer ai
             WHERE ai.app_id = ba.app_id AND ai.owner_key = 'care-bus'
       );
    IF unissued IS NOT NULL THEN
        RAISE NOTICE '078: care-bus does not issue % — care-bus bundle left WITHOUT care-bus as ensurer', unissued;
        RETURN;
    END IF;
    INSERT INTO bundle_ensurer (bundle_id, owner_key)
    VALUES (v_bundle, 'care-bus')
    ON CONFLICT (bundle_id, owner_key) DO NOTHING;
END $$;

DO $$
DECLARE
    r         RECORD;
    v_app     UUID;
    v_req     UUID;
    linked    INT := 0;
    skipped   INT := 0;
BEGIN
    FOR r IN
        SELECT * FROM (VALUES
            ('care-bus', 'mcp', 'care-bus'),
            ('care-bus', 'strata-rag', 'care-bus'),
            ('care-bus', 'strata-ingest', 'care-bus')
        ) AS t(app_key, required_key, caller)
    LOOP
        SELECT id INTO v_app FROM app WHERE key = r.app_key;
        SELECT id INTO v_req FROM app WHERE key = r.required_key;
        IF v_app IS NULL OR v_req IS NULL
           OR NOT EXISTS (SELECT 1 FROM tenant_owner WHERE key = r.caller) THEN
            RAISE NOTICE '078: skipping app_requires % -> % (caller %) — app or owner missing',
                r.app_key, r.required_key, r.caller;
            skipped := skipped + 1;
            CONTINUE;
        END IF;
        INSERT INTO app_requires (app_id, required_app_id, caller_owner_key)
        VALUES (v_app, v_req, r.caller)
        ON CONFLICT (app_id, required_app_id) DO NOTHING;
        linked := linked + 1;
    END LOOP;
    RAISE NOTICE '078: % app_requires edges ensured, % skipped', linked, skipped;
END $$;

-- No cycle may exist (an operator row included): refuse the whole migration if one does.
DO $$
DECLARE
    v_cycle TEXT;
BEGIN
    WITH RECURSIVE walk(start_id, cur_id, path) AS (
        SELECT app_id, required_app_id, ARRAY[app_id, required_app_id] FROM app_requires
        UNION ALL
        SELECT w.start_id, ar.required_app_id, w.path || ar.required_app_id
          FROM walk w JOIN app_requires ar ON ar.app_id = w.cur_id
         WHERE NOT ar.required_app_id = ANY (w.path[2:])
           AND w.cur_id <> w.start_id
    )
    SELECT string_agg(a.key, ' -> ' ORDER BY ord) INTO v_cycle
      FROM (SELECT path FROM walk WHERE cur_id = start_id LIMIT 1) c,
           unnest(c.path) WITH ORDINALITY AS u(app_id, ord)
      JOIN app a ON a.id = u.app_id;
    IF v_cycle IS NOT NULL THEN
        RAISE EXCEPTION '078: app_requires holds a cycle (%) — nothing was changed', v_cycle;
    END IF;
END $$;

COMMIT;
