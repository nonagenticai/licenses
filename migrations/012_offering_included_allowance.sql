-- Migration: Offering included-usage ALLOWANCE (the allowance half of the ACCESS axis)
-- Date: 2026-07-08
-- Description: Adds included_allowance_usd to offering — a monthly USD usage
-- credit bundled into the access grant. Provisioning an offering that carries it
-- writes the tenant's billing tenant_access_charge.included_cost_usd (S2S), which
-- offsets the tenant's incurred usage BEFORE markup. NULL = no credit. A free
-- (unpriced) offering can still bundle an allowance (amount 0, included N).
--
-- Forward-only / additive / idempotent. NO behavior change until an offering
-- with a non-null allowance is created + provisioned. The live pod also adds this
-- column at startup via Base.metadata.create_all (models.Offering); this
-- migration keeps the migrations/ series coherent for a from-scratch DB build.

ALTER TABLE offering ADD COLUMN IF NOT EXISTS included_allowance_usd NUMERIC(20, 8);

-- Verify
SELECT id, code, access_price_usd, included_allowance_usd FROM offering ORDER BY created_at;
