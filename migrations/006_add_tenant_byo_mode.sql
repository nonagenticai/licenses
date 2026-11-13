-- Migration: Add per-tenant BYO LLM entitlement (byo_mode)
-- Date: 2026-06-19
-- Description: Adds tenants.byo_mode ('local_allowed' | 'byo_required'). Surfaced
-- via /license/introspect so worker_management can hard-fail agent runs for
-- commercial (byo_required) tenants that have not configured their own LLM
-- provider — guaranteeing they never use owner GPU hardware.
--
-- Forward-only / additive. Default 'local_allowed' preserves every existing
-- tenant's current behavior; commercial tenants are flipped to 'byo_required'
-- via PATCH /tenants/{name_or_id}/byo-mode (license-admin).

ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS byo_mode VARCHAR(32) NOT NULL DEFAULT 'local_allowed';

ALTER TABLE tenants
    DROP CONSTRAINT IF EXISTS ck_tenants_byo_mode;
ALTER TABLE tenants
    ADD CONSTRAINT ck_tenants_byo_mode CHECK (byo_mode IN ('local_allowed', 'byo_required'));

-- Verify
SELECT id, name, byo_mode FROM tenants ORDER BY created_at;
