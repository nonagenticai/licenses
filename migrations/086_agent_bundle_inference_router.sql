-- 086: the `agent` bundle reaches inference-router — agent -> inference-router.
--
-- WHY (agent per-tenant LLM licences, 2026-10-27). The agent service calls
-- inference-router for a tenant with the tenant's OWN inference-router licence
-- (bundle-token as `agent-bundle-minter`, 082), exactly as it calls mcp (082:
-- agent -> mcp). Before this file the `agent` bundle's app_requires closure was
-- {mcp, coding, strata-rag, strata-ingest, laya}: no inference-router, so
-- bundle-token?app=inference-router&bundle=agent was 404 `app_not_in_bundle`.
--
-- WHAT THIS ADDS (data only, no schema): ONE app_requires edge
--   agent -> inference-router, caller `agent`
-- the same shape as 082's `agent -> mcp` (caller = the owner whose minter mints the
-- required app). Direct, not via mcp: the agent calls inference-router itself; mcp's
-- broker never does. inference-router has no outgoing edges, so the closure grows by
-- exactly that one app. Ensure then provisions a dependency licence for it on every
-- tenant holding the `agent` bundle, and on a tenant NOT owned by `agent` writes
-- tenant_dependency_grant (tenant, agent, inference-router). Unchanged: no issuer is
-- widened (inference-router stays {fr, commonpractice, kag}; a dependency licence is
-- written by ensure, not issued), no bundle_app row, no api_client row.
--
-- GRANTS / LICENCES: none written here. Existing agent-bundle holders get the
-- inference-router dependency licence only on their next ensure.
--
-- PLAN: none added. inference-router was created in PROD via POST /apps with plan
-- `standard` (040); no seed or migration here creates the app or its plan, and 082
-- adds no plan for any of its apps either. A missing app or owner is ANNOUNCED AND
-- SKIPPED (051's rule).
--
-- FRESH DATABASE: stamped without running; the edge is mirrored in
-- models.APP_REQUIRES_SEED (boot seed).
--
-- ONE TRANSACTION. ON CONFLICT DO NOTHING: an operator's row is never overwritten, a
-- re-run is a no-op. A cycle through app_requires aborts the whole file.
--
-- DOWNGRADE: migrations/downgrades/086_agent_bundle_inference_router.sql

BEGIN;

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
            ('agent', 'inference-router', 'agent')
        ) AS t(app_key, required_key, caller)
    LOOP
        SELECT id INTO v_app FROM app WHERE key = r.app_key;
        SELECT id INTO v_req FROM app WHERE key = r.required_key;
        IF v_app IS NULL OR v_req IS NULL
           OR NOT EXISTS (SELECT 1 FROM tenant_owner WHERE key = r.caller) THEN
            RAISE NOTICE '086: skipping app_requires % -> % (caller %) — app or owner missing',
                r.app_key, r.required_key, r.caller;
            skipped := skipped + 1;
            CONTINUE;
        END IF;
        INSERT INTO app_requires (app_id, required_app_id, caller_owner_key)
        VALUES (v_app, v_req, r.caller)
        ON CONFLICT (app_id, required_app_id) DO NOTHING;
        linked := linked + 1;
    END LOOP;
    RAISE NOTICE '086: % app_requires edges ensured, % skipped', linked, skipped;
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
        RAISE EXCEPTION '086: app_requires holds a cycle (%) — nothing was changed', v_cycle;
    END IF;
END $$;

COMMIT;
