-- 089: the `agent` bundle reaches the conveyor — agent -> conveyor.
--
-- WHY (the Rust conveyor's own MCP surface, SPEC D19, 2026-11-09). The agent service
-- calls the conveyor's `/mcp` for a tenant with the tenant's OWN conveyor licence
-- (bundle-token as `agent-bundle-minter`, 082), exactly as it calls mcp (082:
-- agent -> mcp) and inference-router (086: agent -> inference-router). Before this
-- file the `agent` bundle's app_requires closure had no conveyor, so
-- bundle-token?app=conveyor&bundle=agent was 404 `app_not_in_bundle`, and every
-- conveyor call from the agent was `licence_unavailable`.
--
-- WHAT THIS ADDS (data only, no schema): ONE app_requires edge
--   agent -> conveyor, caller `agent`
-- the same shape as 086's `agent -> inference-router` (caller = the owner whose
-- minter mints the required app). Direct, not via mcp: the agent calls the conveyor
-- itself. The `conveyor` app already exists (migrations 050/059: the product app fr
-- issues); this file creates no app, plan, bundle_app or api_client row and widens no
-- issuer. `conveyor` has no outgoing app_requires edge, so the closure grows by
-- exactly that one app. Ensure then provisions a dependency licence for it on every
-- tenant holding the `agent` bundle, and on a tenant NOT owned by `agent` writes
-- tenant_dependency_grant (tenant, agent, conveyor).
--
-- GRANTS / LICENCES: none written here. Existing agent-bundle holders get the conveyor
-- dependency licence only on their next ensure.
--
-- FRESH DATABASE: stamped without running; the edge is mirrored in
-- models.APP_REQUIRES_SEED (boot seed). A missing app or owner is ANNOUNCED AND
-- SKIPPED (051's rule).
--
-- ONE TRANSACTION. ON CONFLICT DO NOTHING: an operator's row is never overwritten, a
-- re-run is a no-op. A cycle through app_requires aborts the whole file.
--
-- DOWNGRADE: migrations/downgrades/089_agent_bundle_conveyor.sql

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
            ('agent', 'conveyor', 'agent')
        ) AS t(app_key, required_key, caller)
    LOOP
        SELECT id INTO v_app FROM app WHERE key = r.app_key;
        SELECT id INTO v_req FROM app WHERE key = r.required_key;
        IF v_app IS NULL OR v_req IS NULL
           OR NOT EXISTS (SELECT 1 FROM tenant_owner WHERE key = r.caller) THEN
            RAISE NOTICE '089: skipping app_requires % -> % (caller %) — app or owner missing',
                r.app_key, r.required_key, r.caller;
            skipped := skipped + 1;
            CONTINUE;
        END IF;
        INSERT INTO app_requires (app_id, required_app_id, caller_owner_key)
        VALUES (v_app, v_req, r.caller)
        ON CONFLICT (app_id, required_app_id) DO NOTHING;
        linked := linked + 1;
    END LOOP;
    RAISE NOTICE '089: % app_requires edges ensured, % skipped', linked, skipped;
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
        RAISE EXCEPTION '089: app_requires holds a cycle (%) — nothing was changed', v_cycle;
    END IF;
END $$;

COMMIT;
