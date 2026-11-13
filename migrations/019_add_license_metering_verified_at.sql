-- Migration: license.metering_verified_at — coverage that was actually MEASURED
-- Date: 2026-08-10
-- Description: records WHEN licences last asked billing what it holds for a
-- licence and found every config the plan owes already present. NULL means we
-- have never verified it against billing.
--
-- WHY (measured 2026-08-10, both prod databases):
--
-- Migration 018 added `metering_provisioned_at` and, inside the same guard that
-- creates the column, ran `UPDATE license SET metering_provisioned_at = now()`
-- over the whole table. Its own comment argued that was safe because "the estate
-- was hand-converged to 100/100 covered on 2026-08-08, so 'already provisioned'
-- is the true state for them". That premise was true only for QUOTA, and only
-- for the licences alive on 08-08. Measured stamp distribution in prod:
--
--     stamp (minute)        licences
--     2026-08-09 20:30+00        205   <- this migration's transaction
--     2026-08-09 20:33+00          1
--     2026-08-09 20:40+00          1
--     2026-08-10 03:49+00          1
--
-- 205 of 208 stamps are BASELINED, not observed. Cross-diffing against the
-- billing database, roughly half of the live licences on plans with
-- `token_budget_monthly IS NOT NULL` had NO `billing.token_budgets` row — and
-- every one of them was reported as provisioned by the column. 018 also claimed
-- "an operator can tell them apart because they all share this migration's
-- transaction timestamp"; that is a forensic guess an operator has to make by
-- eyeballing a histogram, not something a query can key on. The reconcile sweep
-- keyed on `metering_provisioned_at IS NULL`, so it scanned 0 and repaired 0,
-- and would have done so forever.
--
-- THE FIX IS THE ABSENCE OF A BACKFILL. This column is added and left NULL for
-- every existing row, deliberately:
--
--   * NULL here means exactly one thing — "billing has not been asked about this
--     licence" — with no second meaning to disambiguate. That is the property
--     018 lost by baselining.
--   * The first reconcile sweep therefore genuinely examines the whole estate
--     instead of trusting a stamp. That is not the "re-push ~100 quotas" storm
--     018 was trying to avoid: the sweep now READS billing's budget list per
--     tenant and only pushes what is genuinely missing, so a covered licence
--     costs one cached GET and no write.
--   * Nothing is destroyed. `metering_provisioned_at` keeps its rows and its
--     (narrower) meaning: "a push was recorded, or 018 baselined it".
--
-- Forward-only / additive / idempotent. On a fresh DB the column is created by
-- Base.metadata.create_all (models.License) and this file is baselined by
-- scripts/apply_migrations.py without running.
--
-- No index: `license` holds a few hundred rows and the sweep is an
-- operator-triggered scan.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'license'
          AND column_name = 'metering_verified_at'
    ) THEN
        ALTER TABLE license ADD COLUMN metering_verified_at TIMESTAMPTZ;
        -- NO BACKFILL. See above: the whole point is that this column starts
        -- with no opinion, so it cannot inherit 018's false one.
    END IF;
END
$$;

-- Verify: the column exists, and how many live licences owe billing a config
-- that has never been verified against billing.
SELECT
    (SELECT count(*) FROM information_schema.columns
      WHERE table_schema = current_schema()
        AND table_name = 'license'
        AND column_name = 'metering_verified_at')              AS column_present,
    (SELECT count(*) FROM license l
       JOIN plan p ON p.id = l.plan_id
      WHERE l.status = 'active'
        AND (l.expires_at IS NULL OR l.expires_at > now())
        AND (p.quota_monthly IS NOT NULL OR p.token_budget_monthly IS NOT NULL)
        AND l.metering_verified_at IS NULL)                    AS unverified_metered_licenses;
