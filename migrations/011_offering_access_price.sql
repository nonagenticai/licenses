-- Migration: Offering ACCESS price (the access-billing axis)
-- Date: 2026-07-08
-- Description: Adds the ACCESS half to offering — a fixed per-product access fee
-- (access_price_usd + access_cadence), distinct from the usage markup. NULL price
-- = a free-access offering. Provisioning a priced offering onto a tenant now also
-- writes the tenant's billing tenant_access_charge (S2S). This makes "free vs
-- paid" real; the included-token allowance remains a later phase.
--
-- Forward-only / additive / idempotent. NO behavior change until a priced
-- offering is created + provisioned. The live pod also adds these columns at
-- startup via Base.metadata.create_all (models.Offering); this migration keeps
-- the migrations/ series coherent for a from-scratch DB build.

ALTER TABLE offering ADD COLUMN IF NOT EXISTS access_price_usd NUMERIC(20, 8);
ALTER TABLE offering ADD COLUMN IF NOT EXISTS access_cadence VARCHAR(16) NOT NULL DEFAULT 'monthly';

-- Cadence check (idempotent: only add if not already present).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_offering_access_cadence'
    ) THEN
        ALTER TABLE offering ADD CONSTRAINT ck_offering_access_cadence
            CHECK (access_cadence IN ('monthly', 'one_time'));
    END IF;
END$$;

-- Verify
SELECT id, code, access_price_usd, access_cadence FROM offering ORDER BY created_at;
