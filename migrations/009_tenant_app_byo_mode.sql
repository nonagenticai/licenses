-- Migration: Per-(tenant, product) BYO posture override
-- Date: 2026-07-08
-- Description: Adds tenant_app_byo_mode(tenant_id, app_id, byo_mode) so BYO
-- posture can be resolved PER-(tenant, product) instead of only tenant-wide
-- (tenants.byo_mode). When a row exists it OVERRIDES the tenant-wide value for
-- that product; absent -> the tenant-wide byo_mode still applies. Foundation
-- for per-product commercial offerings (free/paid access x BYO/markup per
-- product) — the same tenant can be 'byo_required' for product A but
-- 'platform_only' for product B.
--
-- Forward-only / additive / idempotent. NO behavior change until (a) an override
-- row is inserted via PUT /tenants/{name_or_id}/apps/{app_key}/byo-mode
-- (license-admin) AND (b) a consumer passes ?app= to GET /tenants/{id}/byo-mode.
-- The live pod also auto-creates this table at startup via
-- Base.metadata.create_all (models.TenantAppByoMode); this migration keeps the
-- migrations/ series coherent for a from-scratch DB build.

CREATE TABLE IF NOT EXISTS tenant_app_byo_mode (
    tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    app_id      UUID NOT NULL REFERENCES app(id) ON DELETE CASCADE,
    byo_mode    VARCHAR(32) NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT tenant_app_byo_mode_pkey PRIMARY KEY (tenant_id, app_id),
    CONSTRAINT ck_tenant_app_byo_mode
        CHECK (byo_mode IN ('local_allowed', 'byo_required', 'platform_only'))
);

CREATE INDEX IF NOT EXISTS idx_tenant_app_byo_mode_tenant
    ON tenant_app_byo_mode(tenant_id);

-- Verify
SELECT tenant_id, app_id, byo_mode FROM tenant_app_byo_mode ORDER BY created_at;
