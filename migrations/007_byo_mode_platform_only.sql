-- Migration: platform inference becomes an explicit grant
-- Date: 2026-06-21
-- Description:
--   1) Extend tenants.byo_mode to a third value 'platform_only' (demo/test/trial
--      tenants: locked to platform inference, BYO disabled — any stored external
--      LLM row is ignored by worker_management).
--   2) Flip the column DEFAULT from 'local_allowed' to 'byo_required'. Platform
--      inference runs on OWNER GPU, so new tenants get NO platform access unless
--      explicitly granted via PATCH /tenants/{name_or_id}/byo-mode (license-admin).
--
-- Forward-only / additive. The DEFAULT change affects ONLY new inserts; existing
-- rows keep their stored value (all current tenants are internal and stay on
-- 'local_allowed'). Grant demo tenants platform_only after this lands, e.g.:
--   PATCH /tenants/<demo>/byo-mode {"byo_mode": "platform_only"}

ALTER TABLE tenants
    DROP CONSTRAINT IF EXISTS ck_tenants_byo_mode;
ALTER TABLE tenants
    ADD CONSTRAINT ck_tenants_byo_mode
    CHECK (byo_mode IN ('local_allowed', 'byo_required', 'platform_only'));

ALTER TABLE tenants
    ALTER COLUMN byo_mode SET DEFAULT 'byo_required';

-- Verify
SELECT id, name, byo_mode FROM tenants ORDER BY created_at;
