-- 062: the `evidence-review` app, and kag -> evidence-review as an app dependency.
--
-- WHY (George 2026-10-15: licences are tenant x app only, and every hop authenticates
-- with the tenant's licence for the TARGET app; ~/.claude/plans/kag-frontend-separation.md
-- "ER design"). kag.nonagentic.ai (kag-frontend, api_client owner kag, 060) calls
-- Evidence Review (knowledge/evidence_review_service) DIRECTLY for a tenant, with that
-- tenant's own licence for app `evidence-review` (ER_ACCEPTED_LICENCE_APPS) plus the
-- user's X-Actor-Token. kag-frontend mints it through
-- POST /internal/tenants/{t}/bundle-token?app=evidence-review as
-- secure-apps/kag-frontend-bundle-minter. Before this file no `evidence-review` app
-- existed (the primary licenses DB, BEGIN READ ONLY, 2026-10-15: zero rows), so nothing
-- could be minted for it.
--
-- WHAT THIS ADDS (data only, no schema):
--   app          evidence-review  "Evidence Review", audience `platform` (a
--                service-to-service hop: humans use kag-frontend, which holds the
--                `kag` licence; ER is never offered to a person directly — the same
--                class as mlx-backend / workers).
--   plan         evidence-review/standard, global, 600 rpm / 60 burst, perpetual,
--                quota 1,000,000 / token budget 50,000,000 — byte-for-byte the live
--                mlx-backend/standard row (measured 2026-10-15) and seed_apps.py's
--                DEFAULT_QUOTA_MONTHLY / DEFAULT_TOKEN_BUDGET_MONTHLY (no plan is
--                ever unlimited). kag ensures kag-research with plans
--                ["tenant-pooled", "standard"]; choose_plan takes the first that
--                exists, so `standard` is what the dependency licence lands on.
--   app_issuer   evidence-review -> {kag, platform}.
--                * kag: the product owner of the only caller (kag-frontend); lets
--                  kag's own clients mint / revoke ER licences directly on kag tenants
--                  (main._authorize_issuance) and keeps 052 rule 1 satisfiable if ER
--                  is ever SOLD inside kag-research (kag must issue every member).
--                * platform: platform admin keeps revoke / app settings on every
--                  tenant (the platform long-tail rule), not only via break-glass.
--                * NOT fr: fr is being narrowed to super-admin provisioning (George
--                  10-12), and 057 already plans to drop fr from kag's issuers; adding
--                  fr here would be a widening with no caller behind it.
--                Neither issuer is needed for the dependency path itself: ensure mints
--                dependency licences internally (059 "Dependency apps are not issued
--                by the ensurer") and bundle-token checks owner scope, not issuers.
--   app_requires kag -> evidence-review, caller kag. Every bundle whose closure holds
--                `kag` now provisions an evidence-review DEPENDENCY licence on ensure
--                (is_dependency, bundle_id NULL, bundle-token only) and grants the
--                caller kag an APP-SCOPED tenant_dependency_grant for it — except on
--                kag's own tenants, where owner scope already admits kag.
--
-- SCOPE NOTE (accepted, same shape as 059's fr note). `kag` is in the closure of
-- `kag-research` (member), `conveyor` (member, ensurer fr) and `w2v` (w2v -> kag;
-- ensurers w2v, merqi, commonpractice). So a RE-ENSURE of conveyor / w2v on an fr /
-- w2v / merqi / commonpractice tenant also inserts an evidence-review DEPENDENCY
-- licence there, plus an app-scoped tenant_dependency_grant (kag, evidence-review).
-- Two consequences, both from bundle-token's existing rules, not from this file:
--   1. kag-owned minters (kag-frontend-bundle-minter, kag-bundle-minter) may mint
--      evidence-review — and ONLY evidence-review beyond 059's workers / mlx-* — on
--      those tenants (403 `dependency_grant_scope` for any other app).
--   2. ANY minter whose owner OWNS the tenant or holds an ACTIVE tenant_app_grant on
--      it may ALSO mint evidence-review there: owner scope (main.py bundle-token,
--      `_resolve_tenant_for_caller`, ~line 10546) admits the tenant WITHOUT restricting
--      the app, and the dependency licence sits in the closure of a bundle the tenant
--      holds. E.g. fr's minter on an fr tenant with conveyor ensured, w2v's / merqi's /
--      CP's minter on their own (or granted) w2v tenants. 059 already gave every such
--      minter workers / mlx-* the same way; 062 adds evidence-review to that set.
-- That is the app-dependency model working as designed (a tenant holding `kag` gets
-- what kag calls, and whoever serves the tenant can mint what the tenant holds), not a
-- widening of any owner's tenant set: no tenant_app_grant, api_client, bundle
-- membership or ensurer changes.
--
-- NOTHING ELSE. No bundle membership (ER is a dependency, not sold), no api_client
-- (060 already registered kag-frontend-bundle-minter), no ensurer change.
--
-- A missing `kag` app is ANNOUNCED AND SKIPPED (051's rule). FRESH database: this file
-- is stamped without running; the app + plan come from scripts/seed_apps.py, the
-- issuers from models.APP_ISSUER_OVERRIDES, the edge from models.APP_REQUIRES_SEED
-- (pinned equal at head by tests/unit/test_migration_062_evidence_review_app.py).
--
-- ONE TRANSACTION. Every INSERT is ON CONFLICT DO NOTHING / NOT EXISTS: an operator's
-- row is never overwritten, a re-run is a no-op. The 059 cycle check is re-run.
--
-- AFTER DEPLOY nothing is provisioned by this file: a tenant gets its licence on the
-- next ensure of a bundle whose closure holds kag (e.g. kag-research by
-- secure-apps/kag-licenses-admin on kag tenants).
--
-- DOWNGRADE: migrations/downgrades/062_evidence_review_app.sql (REFUSES while any
-- evidence-review licence, dependency grant, or operator row in offering /
-- tenant_app_byo_mode / bundle_app / bundle_caller references the app). Roll back
-- models.py and scripts/seed_apps.py too, or the boot seed / DR seed re-creates the rows.

BEGIN;

INSERT INTO app (key, name, description, audience)
VALUES (
    'evidence-review',
    'Evidence Review',
    'Evidence Review service — called by kag-frontend for a tenant with that tenant''s '
    'own evidence-review licence (dependency of kag, migration 062, 2026-10-15).',
    'platform'
)
ON CONFLICT (key) DO NOTHING;

INSERT INTO plan (
    app_id, code, name, description, region_id, duration_days,
    rate_limit_rpm, rate_limit_burst, quota_monthly, token_budget_monthly
)
SELECT a.id, 'standard', 'Standard', 'Default per-tenant plan for Evidence Review.', NULL,
       NULL, 600, 60, 1000000, 50000000
FROM app a
WHERE a.key = 'evidence-review'
  AND NOT EXISTS (
        SELECT 1 FROM plan p
         WHERE p.app_id = a.id AND p.code = 'standard' AND p.region_id IS NULL
  );

INSERT INTO app_issuer (app_id, owner_key)
SELECT a.id, o.owner_key
FROM app a, (VALUES ('kag'), ('platform')) AS o(owner_key)
WHERE a.key = 'evidence-review'
  AND EXISTS (SELECT 1 FROM tenant_owner t WHERE t.key = o.owner_key)
ON CONFLICT (app_id, owner_key) DO NOTHING;

DO $$
DECLARE
    v_app  UUID;
    v_req  UUID;
BEGIN
    SELECT id INTO v_app FROM app WHERE key = 'kag';
    SELECT id INTO v_req FROM app WHERE key = 'evidence-review';
    IF v_app IS NULL OR v_req IS NULL
       OR NOT EXISTS (SELECT 1 FROM tenant_owner WHERE key = 'kag') THEN
        RAISE NOTICE '062: skipping app_requires kag -> evidence-review (caller kag) — app or owner missing';
        RETURN;
    END IF;
    INSERT INTO app_requires (app_id, required_app_id, caller_owner_key)
    VALUES (v_app, v_req, 'kag')
    ON CONFLICT (app_id, required_app_id) DO NOTHING;
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
        RAISE EXCEPTION '062: app_requires holds a cycle (%) — nothing was changed', v_cycle;
    END IF;
END $$;

COMMIT;
