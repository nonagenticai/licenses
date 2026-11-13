-- 041: a licence's own token budget / request quota, durable (licenses #239).
--
-- WHY (audit finding on 221b895, 2026-10-02). Per-licence `--token-budget` /
-- `--quota` overrides at provisioning were passed ONLY to the one-shot billing push
-- and stored nowhere. If billing rejected that push (provision_tenant_bundle.py
-- exit 6), the documented repair — the nightly `licenses-metering-reconcile`
-- CronJob (`main.run_metering_reconcile`) — re-pushed the PLAN's values (standard:
-- 50M tokens / 1M requests), silently RAISING the tenant's pooled spend cap that
-- the override existed to hold. Owner ruling (George, 2026-10-02): budget and quota
-- are configurable PER LICENCE, durably.
--
-- SEMANTICS. NULL = use the plan's value (every pre-041 row, and every licence not
-- created with an override). Non-NULL = THIS licence's value, used by every push
-- (`_provision_budget` and the reconcile sweep, via `models.effective_metering`) and
-- by the sweep's drift comparison, so an overridden licence is never reported as
-- quota drift. 0 is a BLOCK-ALL value and is pushed, exactly like a plan's 0. NULL
-- here can never mean "unlimited": an unlimited licence is an unlimited PLAN.
--
-- TYPE: INTEGER, the type of plan.token_budget_monthly / plan.quota_monthly that it
-- replaces. Same `IS NULL OR >= 0` CHECK shape as ck_plan_quota_monthly_nonneg.
--
-- SAFETY. Two NULLable columns with no default (catalogue-only, no rewrite), named
-- CHECKs pg_constraint-probed so a re-run is a no-op. No backfill.
--
-- DOWNGRADE: migrations/downgrades/041_license_metering_overrides.sql (lossy).

ALTER TABLE license
    ADD COLUMN IF NOT EXISTS token_budget_override INTEGER NULL,
    ADD COLUMN IF NOT EXISTS quota_override INTEGER NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_license_token_budget_override_nonneg'
          AND conrelid = 'license'::regclass
    ) THEN
        ALTER TABLE license
            ADD CONSTRAINT ck_license_token_budget_override_nonneg
            CHECK (token_budget_override IS NULL OR token_budget_override >= 0);
        RAISE NOTICE '041: ck_license_token_budget_override_nonneg added';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_license_quota_override_nonneg'
          AND conrelid = 'license'::regclass
    ) THEN
        ALTER TABLE license
            ADD CONSTRAINT ck_license_quota_override_nonneg
            CHECK (quota_override IS NULL OR quota_override >= 0);
        RAISE NOTICE '041: ck_license_quota_override_nonneg added';
    END IF;
END $$;

COMMENT ON COLUMN license.token_budget_override IS
    'Per-licence monthly token budget pushed to billing instead of plan.token_budget_monthly. '
    'NULL = plan value. Read by provisioning AND the metering reconcile sweep (#239).';
COMMENT ON COLUMN license.quota_override IS
    'Per-licence monthly request quota pushed to billing instead of plan.quota_monthly. '
    'NULL = plan value. Read by provisioning AND the metering reconcile sweep (#239).';
