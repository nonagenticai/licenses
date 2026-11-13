-- 082: mcp toolkit entitlements — owner `mcp` and its minter, mcp's backend edges, the
-- `agent` bundle with its ensurer and minter, and agent -> mcp.
--
-- WHY (MCP toolkit product plan §3, T0, 2026-10-24; nonagenticai_mcp_server
-- docs/MCP_TOOLKIT_PRODUCT_PLAN.md + docs/research/LICENSING_ENTITLEMENTS.md "Change
-- list -> licenses"). The products (agent, care-bus) call the mcp server for a tenant
-- with the tenant's own `mcp` licence (bundle-token?app=mcp), and the mcp broker calls
-- the toolkit backends (coding, strata-rag, strata-ingest, laya) for that tenant with
-- the tenant's own licence for each, minted through bundle-token as
-- `mcp-bundle-minter` (LicenceMinterCredentialResolver). Before this file licences had
-- no `mcp` owner, no mcp minter, no edge out of mcp, and agent had no bundle, ensurer,
-- minter or edge — so neither product could get an mcp licence plus backend licences
-- for a self-serve tenant.
--
-- WHAT THIS ADDS (data only, no schema):
--   tenant_owner  mcp "MCP" — owns NO tenant. It exists so `mcp` can be an edge
--                 caller (app_requires.caller_owner_key) and an api_client owner; ensure
--                 then writes tenant_dependency_grant (tenant, mcp, <backend>) on every
--                 tenant whose bundle closure walks an mcp edge.
--   api_client    ('secure-apps', 'mcp-bundle-minter') -> mcp, NOT cross-owner. mcp owns
--                 no tenant, so bundle-token admits it ONLY through those dependency
--                 grants, for exactly the granted apps (main.internal_mint_tenant_
--                 bundle_token: 403 `dependency_grant_scope` for any other app, 404 for a
--                 tenant with no grant). The broker passes `bundle=<product bundle>`
--                 (agent / care-bus) explicitly; the route resolves the app through that
--                 held bundle's transitive closure (404 `bundle_not_held` otherwise).
--                 Holds `bundle-token-minter` ONLY once the gitops keycloak realm-patch
--                 creates it (BUNDLE_MINTER_CLIENTS); until then the row is inert.
--   app_requires  mcp -> coding, strata-rag, strata-ingest, laya, caller mcp. None of
--                 the four requires anything, so mcp's closure is exactly them, and any
--                 bundle reaching mcp (care-bus 078, agent below) reaches them too —
--                 services.app_requires.closure is a transitive DFS.
--   bundle        `agent` = {agent}. A bundle lists what is SOLD; mcp and its backends
--                 come from the closure as dependency licences (care-bus's shape, 078).
--   bundle_ensurer agent -> {agent} (052 rule 1: agent issues agent, 070).
--   api_client    ('secure-apps', 'agent-bundle-minter') -> agent, NOT cross-owner: the
--                 agent service mints the tenant's `mcp` licence for its tool calls.
--                 Holds `bundle-token-minter` only (gitops realm-patch,
--                 BUNDLE_MINTER_CLIENTS); separate from agent-licenses-admin
--                 (license-admin never admits bundle-token).
--   app_requires  agent -> mcp, caller agent.
--
-- GRANTS: none written here. Ensure writes them: on an agent-OWNED tenant the agent ->
-- mcp edge grants nothing (caller = the tenant's own owner), the mcp -> backend edges
-- grant `mcp` per backend. Existing holders of `care-bus` (078/079/080) get the mcp
-- grants and a `coding` dependency licence only on their next ensure — no re-ensure
-- step here (care-bus moves onto bindings in plan T3).
--
-- A missing app (mcp / coding / laya are created by scripts/seed_apps.py, never by a
-- migration) or owner is ANNOUNCED AND SKIPPED (051's rule).
--
-- FRESH DATABASE: this file is stamped without running; tenant_owner + api_client rows
-- come from the boot seed (models.TENANT_OWNERS / API_CLIENTS), and the ensurer / edge
-- seeds are mirrored in models.BUNDLE_ENSURER_SEED / APP_REQUIRES_SEED — pinned equal
-- at head by tests/unit/test_migration_082_mcp_toolkit_entitlements.py.
--
-- ONE TRANSACTION. Every INSERT is ON CONFLICT DO NOTHING / NOT EXISTS: an operator's
-- row is never overwritten, a re-run is a no-op. A cycle through app_requires aborts
-- the whole file.
--
-- DOWNGRADE: migrations/downgrades/082_mcp_toolkit_entitlements.sql (REFUSES while any
-- tenant holds `agent` (the bundle), any licence names it, or any tenant_dependency_grant
-- / tenant is keyed on owner mcp). Roll back models.py too, or the boot seed re-creates
-- the owner + client rows.

BEGIN;

INSERT INTO tenant_owner (key, display_name) VALUES
    ('mcp', 'MCP')
ON CONFLICT (key) DO NOTHING;

INSERT INTO api_client (realm, client_id, owner_key, cross_owner) VALUES
    ('secure-apps', 'mcp-bundle-minter', 'mcp', false),
    ('secure-apps', 'agent-bundle-minter', 'agent', false)
ON CONFLICT (realm, client_id) DO NOTHING;

INSERT INTO bundle (key, name, description)
VALUES (
    'agent',
    'Agent',
    'agent alone; mcp and its toolkit backends (coding, strata-rag, strata-ingest, laya) '
    'come from app_requires as dependency licences (migration 082, 2026-10-24)'
)
ON CONFLICT (key) DO NOTHING;

DO $$
DECLARE
    v_bundle  UUID;
    v_app     UUID;
BEGIN
    SELECT id INTO v_bundle FROM bundle WHERE key = 'agent';
    SELECT id INTO v_app FROM app WHERE key = 'agent';
    IF v_app IS NULL THEN
        RAISE NOTICE '082: skipping agent/agent — no such app';
        RETURN;
    END IF;
    INSERT INTO bundle_app (bundle_id, app_id)
    VALUES (v_bundle, v_app)
    ON CONFLICT (bundle_id, app_id) DO NOTHING;
END $$;

-- 052 rule 1: agent ensures the agent bundle only if it issues every app in it.
DO $$
DECLARE
    v_bundle    UUID;
    unissued    TEXT;
BEGIN
    SELECT id INTO v_bundle FROM bundle WHERE key = 'agent';
    IF NOT EXISTS (SELECT 1 FROM tenant_owner WHERE key = 'agent') THEN
        RAISE NOTICE '082: no agent owner — agent bundle left WITHOUT an ensurer';
        RETURN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM bundle_app WHERE bundle_id = v_bundle) THEN
        RAISE NOTICE '082: agent bundle is empty — left WITHOUT an ensurer';
        RETURN;
    END IF;
    SELECT string_agg(a.key, ', ' ORDER BY a.key) INTO unissued
      FROM bundle_app ba JOIN app a ON a.id = ba.app_id
     WHERE ba.bundle_id = v_bundle
       AND NOT EXISTS (
            SELECT 1 FROM app_issuer ai
             WHERE ai.app_id = ba.app_id AND ai.owner_key = 'agent'
       );
    IF unissued IS NOT NULL THEN
        RAISE NOTICE '082: agent does not issue % — agent bundle left WITHOUT agent as ensurer', unissued;
        RETURN;
    END IF;
    INSERT INTO bundle_ensurer (bundle_id, owner_key)
    VALUES (v_bundle, 'agent')
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
            ('mcp', 'coding', 'mcp'),
            ('mcp', 'strata-rag', 'mcp'),
            ('mcp', 'strata-ingest', 'mcp'),
            ('mcp', 'laya', 'mcp'),
            ('agent', 'mcp', 'agent')
        ) AS t(app_key, required_key, caller)
    LOOP
        SELECT id INTO v_app FROM app WHERE key = r.app_key;
        SELECT id INTO v_req FROM app WHERE key = r.required_key;
        IF v_app IS NULL OR v_req IS NULL
           OR NOT EXISTS (SELECT 1 FROM tenant_owner WHERE key = r.caller) THEN
            RAISE NOTICE '082: skipping app_requires % -> % (caller %) — app or owner missing',
                r.app_key, r.required_key, r.caller;
            skipped := skipped + 1;
            CONTINUE;
        END IF;
        INSERT INTO app_requires (app_id, required_app_id, caller_owner_key)
        VALUES (v_app, v_req, r.caller)
        ON CONFLICT (app_id, required_app_id) DO NOTHING;
        linked := linked + 1;
    END LOOP;
    RAISE NOTICE '082: % app_requires edges ensured, % skipped', linked, skipped;
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
        RAISE EXCEPTION '082: app_requires holds a cycle (%) — nothing was changed', v_cycle;
    END IF;
END $$;

COMMIT;
