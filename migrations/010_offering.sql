-- Migration: Per-product commercial offerings (usage-policy templates)
-- Date: 2026-07-08
-- Description: Adds offering(app_id, code, ...) — a reusable per-product
-- usage-policy template bundling the BYO posture (byo_mode) + a markup_pct and
-- absolute per-1M-token / per-request surcharges (mirroring billing_service
-- markup_rules). Provisioning an offering onto a tenant materializes it into the
-- per-(tenant, product) posture row (tenant_app_byo_mode) + the tenant's billing
-- markup_rule. This is the usage-policy half of the two-axis commercial model;
-- the ACCESS half (free/paid price + included-token allowance) is a later phase,
-- so an offering carries NO money field yet.
--
-- Forward-only / additive / idempotent. NO behavior change until offerings are
-- created (POST /offerings, license-admin) and provisioned onto a tenant
-- (POST /tenants/{name_or_id}/offerings/{app_key}/{code}). The live pod also
-- auto-creates this table at startup via Base.metadata.create_all
-- (models.Offering); this migration keeps the migrations/ series coherent for a
-- from-scratch DB build.

CREATE TABLE IF NOT EXISTS offering (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    app_id      UUID NOT NULL REFERENCES app(id) ON DELETE CASCADE,
    code        VARCHAR(64) NOT NULL,
    name        VARCHAR(255) NOT NULL,
    byo_mode    VARCHAR(32) NOT NULL,
    markup_pct                          NUMERIC(6, 2),
    surcharge_input_per_1m_usd          NUMERIC(20, 8),
    surcharge_output_per_1m_usd         NUMERIC(20, 8),
    surcharge_cache_creation_per_1m_usd NUMERIC(20, 8),
    surcharge_cache_read_per_1m_usd     NUMERIC(20, 8),
    surcharge_per_request_usd           NUMERIC(20, 8),
    notes       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ux_offering_app_code UNIQUE (app_id, code),
    CONSTRAINT ck_offering_byo_mode
        CHECK (byo_mode IN ('local_allowed', 'byo_required', 'platform_only'))
);

CREATE INDEX IF NOT EXISTS idx_offering_app ON offering(app_id);

-- Verify
SELECT id, app_id, code, byo_mode, markup_pct FROM offering ORDER BY created_at;
