-- Migration: give every UNLIMITED plan a real monthly allowance
-- Date: 2026-08-11
-- Description: 42 of 88 plan rows carried quota_monthly IS NULL AND
-- token_budget_monthly IS NULL. Per 017 that is not "unset" — NULL is the
-- documented contract for UNLIMITED (0 = block-all, >0 = cap). So those plans
-- were explicitly uncapped, and the whole metering chain honoured it: the
-- licences reconcile selects only licences "on a plan that OWES billing a quota
-- row OR a token budget", so billing held no token_budgets row for any of them
-- and skipped every debit with
--   {"event":"budget_debit_skipped","reason":"no_active_budgets"}
-- at INFO — no ceiling, no alert. Measured 2026-08-11: 43 of 141 LIVE licences
-- sat on such a plan (36 rows `standard`, 3 `portal-n8n-dev`, 1 each
-- `production` / `system-prompt` / `system-workflow`), and 5 of the last 7
-- licences minted landed on one.
--
-- George, 2026-08-11: "every plan needs to be metered! that's the whole point
-- of having metering! so set all plans up with proper budgets!"
--
-- VALUES — measured, not guessed (billing_cost_events, 3 months):
--   busiest single licence, current month .... 11,379,133 tokens
--     (019f7f7c…, app w2v; 9,428,668 of it in ONE day, ~40x its ~180k baseline)
--   next busiest, 3-month peak ...............  7,282,765 tokens
--   busiest single licence, requests/month ...      5,330
-- 50,000,000 tokens is ~4.4x the observed peak and ~1.8x that licence's 31-day
-- projected run-rate; 1,000,000 requests is ~188x its observed peak. Chosen
-- deliberately generous: this change makes usage MEASURED and enforceable, it is
-- not meant to deny anyone on day one. Tighten later, with data.
--
-- SAFETY
--   * Touches ONLY rows where BOTH columns are NULL. A row with an explicit 0
--     (block-all) or any existing cap is left exactly as it is — verified
--     2026-08-11: 0 rows carry a 0, and 0 rows are half-NULL.
--   * Forward-only, idempotent: a re-run matches nothing.
--   * Tolerates the EMPTY database. On a fresh DB `plan` has no rows and this is
--     a clean no-op — the guards below assert a POST-condition ("nothing is left
--     unlimited"), never a pre-condition on row counts, so it cannot fail CI on
--     a database that legitimately has nothing to repair.
--
-- ⚠️ AFTER THIS LANDS the licences metering reconcile will select ~43 more
-- licences and create their billing budgets. That is the point — but it means
-- enforcement becomes REAL for them, so the reconcile run that follows must be
-- watched (license-authz denials, billing budget_exceeded).

-- Guard: a half-NULL row is a state this migration was not designed for
-- (one column unlimited, the other capped). Fail loud rather than guess.
DO $$
DECLARE
    mixed integer;
BEGIN
    SELECT count(*) INTO mixed
      FROM plan
     WHERE (quota_monthly IS NULL) <> (token_budget_monthly IS NULL);
    IF mixed > 0 THEN
        RAISE EXCEPTION
          'plan has % half-unlimited row(s) (exactly one of quota_monthly / token_budget_monthly is NULL). '
          'Prod diverged from the 2026-08-11 measurement (0 such rows); decide each case by hand.', mixed;
    END IF;
END
$$;

UPDATE plan
   SET quota_monthly        = 1000000,
       token_budget_monthly = 50000000
 WHERE quota_monthly IS NULL
   AND token_budget_monthly IS NULL;

-- Post-condition: no plan is unlimited any more. Holds on an empty DB too.
DO $$
DECLARE
    still_unlimited integer;
BEGIN
    SELECT count(*) INTO still_unlimited
      FROM plan
     WHERE quota_monthly IS NULL AND token_budget_monthly IS NULL;
    IF still_unlimited > 0 THEN
        RAISE EXCEPTION 'after backfill, % plan row(s) are still unlimited', still_unlimited;
    END IF;
END
$$;

-- Verify (row counts by allowance, for the apply log)
SELECT quota_monthly, token_budget_monthly, count(*) AS plans
  FROM plan
 GROUP BY 1, 2
 ORDER BY 3 DESC;
