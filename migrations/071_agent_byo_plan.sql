-- 071: the `agent` app's `byo` plan — agent issues licences on `byo`, like CommonPractice.
--
-- WHY (owner decision 2026-10-20). CommonPractice issues every signup-approval licence on
-- its `byo` plan (LICENSE_PLAN_CODE=byo) and retired `standard`. agent-api issued on
-- `agent/standard` (070). The owner ruled agent follows CP: issue on `byo`. Before this
-- file there was no `agent/byo` plan, so LICENSE_PLAN_CODE=byo on agent-api would fail
-- every issuance with "plan not found".
--
-- WHAT THIS ADDS (data only, no schema) — CP's `byo` row shape for the agent app:
--   plan  agent/byo "Bring your own key", global (region NULL), perpetual, limits copied
--         from agent/standard: 600 rpm / 60 burst, quota 1,000,000 / token budget
--         50,000,000 (CP's plan-migration creates `byo` with its `standard` limits;
--         seed_apps.py's DEFAULT_QUOTA_MONTHLY / DEFAULT_TOKEN_BUDGET_MONTHLY — no plan
--         is ever unlimited). The BYO posture itself is the offering / tenant byo_mode
--         (`byo_required`, scripts/seed_offerings.py), not a plan column.
--
-- NOT CHANGED. agent/standard stays in place, unrevoked: licences already issued on it
-- (e.g. tenant d431fc63-6811-40aa-967b-eca94f6215bf) keep working. Retiring it is a
-- separate step once those licences are moved — exactly CP's order (move, then retire).
--
-- FRESH DATABASE: this file is stamped without running; the plan comes from
-- scripts/seed_apps.py (the agent spec's `extra_plans`) — pinned equal at head by
-- tests/unit/test_migration_071_agent_byo_plan.py.
--
-- ONE TRANSACTION. NOT EXISTS: an operator's row is never overwritten, a re-run is a no-op.
--
-- DOWNGRADE: migrations/downgrades/071_agent_byo_plan.sql (REFUSES while any licence is
-- on agent/byo). Roll back scripts/seed_apps.py too, or the DR seed re-creates the plan.

BEGIN;

INSERT INTO plan (
    app_id, code, name, description, region_id, duration_days,
    rate_limit_rpm, rate_limit_burst, quota_monthly, token_budget_monthly
)
SELECT a.id, 'byo', 'Bring your own key',
       'The workspace brings its own provider key and pays the provider; the platform '
       'invoices the markup.',
       NULL, NULL, 600, 60, 1000000, 50000000
FROM app a
WHERE a.key = 'agent'
  AND NOT EXISTS (
        SELECT 1 FROM plan p
         WHERE p.app_id = a.id AND p.code = 'byo' AND p.region_id IS NULL
  );

COMMIT;
