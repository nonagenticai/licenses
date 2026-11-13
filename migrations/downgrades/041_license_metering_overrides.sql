-- Downgrade 041. Drops `license.token_budget_override` / `license.quota_override`
-- and their CHECKs.
--
-- ⚠️ Roll back the CODE first: a tree that still reads License.token_budget_override
-- would 500 on UndefinedColumn once this runs.
--
-- LOSSY, and in the expensive direction: every per-licence override is forgotten, so
-- the next reconcile sweep that finds such a licence's billing row ABSENT re-pushes
-- the PLAN value (raising a pooled tenant spend cap the override held down). Export
-- them first:
--   SELECT id, tenant_id, token_budget_override, quota_override FROM license
--    WHERE token_budget_override IS NOT NULL OR quota_override IS NOT NULL;
ALTER TABLE license DROP CONSTRAINT IF EXISTS ck_license_quota_override_nonneg;
ALTER TABLE license DROP CONSTRAINT IF EXISTS ck_license_token_budget_override_nonneg;
ALTER TABLE license DROP COLUMN IF EXISTS quota_override;
ALTER TABLE license DROP COLUMN IF EXISTS token_budget_override;
