-- Migration: license.metering_provisioned_at — make the metering gap queryable
-- Date: 2026-08-09
-- Description: records WHEN the licences→billing provisioning hand-off last
-- completed for a licence. NULL means it never completed, i.e. the licence may
-- be UNMETERED in billing.
--
-- WHY (measured in prod 2026-08-08): create_user_license provisioned metering
-- with an unreferenced `asyncio.create_task(_provision_budget(...))` whose
-- failures were swallowed by design. A transient billing error — or the pod
-- being terminated mid-rollout — lost metering for that licence permanently,
-- and NOTHING noticed. The one licence it had already cost
-- (019fd7a0-888f-749e-86c2-3b935a196ebf, svc-ingestion/kag, quota 1,000,000)
-- was found only by hand-diffing the licences DB against the billing DB:
-- 100 active licences on quota-bearing plans, 99 with a
-- billing.request_quota_config row and one without — and it was the NEWEST
-- quota-bearing licence in the estate, so an ONGOING leak, not a historical one.
--
-- ⚠️ RETRACTED 2026-08-10 — this file used to claim that with this column the
-- question is "one query against THIS database". It is not, and the baseline
-- below is exactly why. See migration 019 and models.License.metering_verified_at.
-- Measured in prod: 205 of 208 rows carry this migration's single transaction
-- timestamp, so a non-NULL value here does NOT distinguish "we observed a
-- successful push" from "the migration stamped this row sight unseen". Roughly
-- half the baselined, budget-owing licences had no billing.token_budgets row
-- while this column called them provisioned, and the reconcile sweep that keyed
-- off `metering_provisioned_at IS NULL` therefore scanned 0 and repaired 0.
-- The honest coverage query keys on metering_verified_at (019) and is answered
-- by asking billing, not by reading a stamp:
--   SELECT l.id FROM license l JOIN plan p ON p.id = l.plan_id
--    WHERE l.status = 'active'
--      AND (p.quota_monthly IS NOT NULL OR p.token_budget_monthly IS NOT NULL)
--      AND l.metering_verified_at IS NULL;
--
-- BASELINE OF EXISTING ROWS — deliberate, and the reason the backfill sits
-- INSIDE the same guard as the ADD COLUMN:
--   Every pre-existing row is stamped once, at the moment the column is
--   created. The estate was hand-converged to 100/100 covered on 2026-08-08, so
--   "already provisioned" is the true state for them; leaving them NULL would
--   make the first reconcile run re-push ~100 quotas instead of the no-op it
--   should be. These stamps are BASELINED, not observed — an operator can tell
--   them apart because they all share this migration's transaction timestamp.
--   ⚠️ RETRACTED 2026-08-10: the convergence this leans on covered QUOTA only,
--   and only the licences alive on 08-08, so "already provisioned" was NOT the
--   true state for budgets — ~half the baselined budget-owing licences had no
--   billing.token_budgets row. And "an operator can tell them apart" is a
--   histogram someone has to eyeball, not something a query keys on, which is
--   what made this baseline load-bearing instead of harmless. Migration 019 adds
--   metering_verified_at with NO backfill for exactly that reason.
--   Because the UPDATE only runs on the branch that creates the column, a
--   RE-APPLY can never re-stamp a row that a later, genuine failure set back to
--   NULL. That is exactly the leak this column exists to expose, so masking it
--   on a re-run would defeat the migration.
--
-- Forward-only / additive / idempotent. On a fresh DB the column is created by
-- Base.metadata.create_all (models.License) and this file is baselined by
-- scripts/apply_migrations.py without running. On prod, 17/17 files are already
-- stamped, so the applied set is NOT empty and the baseline short-circuit does
-- NOT fire — this file executes normally.
--
-- No index: `license` holds a few hundred rows and the reconcile sweep is an
-- operator-triggered scan, so a partial index would be pure schema surface
-- (models.py + this file having to agree) for no measurable gain.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'license'
          AND column_name = 'metering_provisioned_at'
    ) THEN
        ALTER TABLE license ADD COLUMN metering_provisioned_at TIMESTAMPTZ;
        -- One-shot baseline of everything that predates the column. See above.
        UPDATE license SET metering_provisioned_at = now();
    END IF;
END
$$;

-- Verify: the column exists, and how many licences are recorded as unprovisioned.
SELECT
    (SELECT count(*) FROM information_schema.columns
      WHERE table_schema = current_schema()
        AND table_name = 'license'
        AND column_name = 'metering_provisioned_at')            AS column_present,
    (SELECT count(*) FROM license l
       JOIN plan p ON p.id = l.plan_id
      WHERE l.status = 'active'
        AND p.quota_monthly IS NOT NULL
        AND l.metering_provisioned_at IS NULL)                  AS unprovisioned_quota_bearing;
