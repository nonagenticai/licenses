-- Migration: Add rate limiting fields to plan table
-- Date: 2025-12-25
-- Description: Adds rate_limit_rpm, rate_limit_burst, quota_monthly columns for Envoy rate limiting

-- Add rate_limit_rpm column with default (requests per minute)
ALTER TABLE plan ADD COLUMN IF NOT EXISTS rate_limit_rpm INTEGER NOT NULL DEFAULT 60;

-- Add rate_limit_burst column with default (burst allowance)
ALTER TABLE plan ADD COLUMN IF NOT EXISTS rate_limit_burst INTEGER NOT NULL DEFAULT 10;

-- Add quota_monthly column (optional hard cap per month, NULL = unlimited)
ALTER TABLE plan ADD COLUMN IF NOT EXISTS quota_monthly INTEGER;

-- Update existing plans with tier-appropriate rate limits
UPDATE plan SET rate_limit_rpm = 30, rate_limit_burst = 5 WHERE code = 'trial' AND rate_limit_rpm = 60;
UPDATE plan SET rate_limit_rpm = 300, rate_limit_burst = 30 WHERE code = 'full' AND rate_limit_rpm = 60;
UPDATE plan SET rate_limit_rpm = 60, rate_limit_burst = 10 WHERE code = 'test' AND rate_limit_rpm = 60;

-- Verify
SELECT code, rate_limit_rpm, rate_limit_burst, quota_monthly FROM plan;
