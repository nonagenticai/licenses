-- Migration: plan.quota_monthly non-negative CHECK
-- Date: 2026-07-29
-- Description: quota_monthly semantics are NULL = unlimited, 0 = BLOCK-ALL
-- (zero-allowance plan), >0 = monthly cap. Negatives are invalid. The API now
-- rejects them at PlanCreate/PlanUpdate (Field ge=0); this CHECK is defence in
-- depth so no path (direct SQL, a future endpoint, a bad backfill) can ever
-- land a negative quota that would corrupt the block-all/unlimited contract.
--
-- Forward-only / additive / idempotent. On a fresh DB the constraint is created
-- by Base.metadata.create_all (models.Plan.__table_args__); this migration keeps
-- the migrations/ series coherent for a from-scratch prod apply. Guarded so a
-- re-run (or a DB that already has it from create_all) is a no-op.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_plan_quota_monthly_nonneg'
    ) THEN
        ALTER TABLE plan
            ADD CONSTRAINT ck_plan_quota_monthly_nonneg
            CHECK (quota_monthly IS NULL OR quota_monthly >= 0);
    END IF;
END
$$;

-- Verify
SELECT conname FROM pg_constraint WHERE conname = 'ck_plan_quota_monthly_nonneg';
