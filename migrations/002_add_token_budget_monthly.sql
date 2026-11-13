-- Add token_budget_monthly column to plan table
-- Applied manually to production on 2026-03-24
ALTER TABLE plan ADD COLUMN IF NOT EXISTS token_budget_monthly INTEGER;
