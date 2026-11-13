-- =========================================================
-- License Service Data Model (PostgreSQL)
-- No PII, identity handled by Keycloak, secrets in Secrets app
-- Shared postgres18 through pgbouncer
-- =========================================================

-- Enable UUIDs if not already enabled (Postgres-specific)
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- =========================================================
-- Region
-- Optional lookup for region-scoped plans/licenses
-- =========================================================
CREATE TABLE IF NOT EXISTS region (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code        TEXT NOT NULL UNIQUE,        -- 'eu', 'us', 'apac'
    name        TEXT NOT NULL,               -- 'Europe', 'United States'
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- =========================================================
-- Subject (opaque user/account reference)
-- No PII, only references back to Keycloak
-- =========================================================
CREATE TABLE IF NOT EXISTS subject (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    keycloak_sub    TEXT NOT NULL UNIQUE,   -- from Keycloak "sub" claim
    keycloak_realm  TEXT NOT NULL,          -- realm name or ID
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_subject_realm ON subject(keycloak_realm);

-- =========================================================
-- App (licensed application or module, e.g. n8n node, platform app)
-- =========================================================
CREATE TABLE IF NOT EXISTS app (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    key         TEXT NOT NULL UNIQUE,       -- machine key: 'code_graph_helper', 'n8n_node_x'
    name        TEXT NOT NULL,              -- human label
    description TEXT,
    revoked     BOOLEAN NOT NULL DEFAULT FALSE, -- if app is retired
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- =========================================================
-- Plan (per-app plan definitions: test / trial / full, possibly region-scoped)
-- =========================================================
CREATE TABLE IF NOT EXISTS plan (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    app_id          UUID NOT NULL REFERENCES app(id) ON DELETE CASCADE,
    code            TEXT NOT NULL,          -- 'test', 'trial', 'full'
    name            TEXT NOT NULL,          -- 'Test Plan', 'Trial Plan', etc.
    description     TEXT,
    region_id       UUID REFERENCES region(id), -- NULL = global plan
    duration_days   INTEGER,                -- NULL = no automatic expiry
    -- Rate limiting fields for Envoy rate limit service
    rate_limit_rpm  INTEGER NOT NULL DEFAULT 60,   -- requests per minute
    rate_limit_burst INTEGER NOT NULL DEFAULT 10,  -- burst allowance
    quota_monthly   INTEGER,                       -- optional hard cap per month (NULL = unlimited)
    revoked         BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ensure uniqueness of plan code per app+region
CREATE UNIQUE INDEX IF NOT EXISTS ux_plan_app_code_region
    ON plan(app_id, code, COALESCE(region_id, '00000000-0000-0000-0000-000000000000'::uuid));

-- =========================================================
-- License (subject's entitlement to use an app under a plan)
-- =========================================================
CREATE TABLE IF NOT EXISTS license (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    subject_id      UUID NOT NULL REFERENCES subject(id) ON DELETE CASCADE,
    app_id          UUID NOT NULL REFERENCES app(id) ON DELETE CASCADE,
    plan_id         UUID NOT NULL REFERENCES plan(id) ON DELETE RESTRICT,
    region_id       UUID REFERENCES region(id), -- region where license applies; may match plan.region_id
    status          TEXT NOT NULL,             -- 'active', 'revoked', 'expired', 'pending'
    starts_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ,              -- NULL = perpetual
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at      TIMESTAMPTZ,
    metadata        JSONB                      -- optional, non-PII notes (e.g. {"source":"invite"})
);

-- optional: one non-expired active license per subject+app
-- you can enforce this via a partial unique index if desired:
-- CREATE UNIQUE INDEX ux_license_subject_app_active
--   ON license(subject_id, app_id)
--   WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_license_subject ON license(subject_id);
CREATE INDEX IF NOT EXISTS idx_license_app ON license(app_id);
CREATE INDEX IF NOT EXISTS idx_license_status ON license(status);
CREATE INDEX IF NOT EXISTS idx_license_expires_at ON license(expires_at);

-- =========================================================
-- Token (API tokens used by clients/nodes to call the license API)
-- Only token_hash is stored, not the raw token
-- =========================================================
CREATE TABLE IF NOT EXISTS token (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    license_id      UUID NOT NULL REFERENCES license(id) ON DELETE CASCADE,
    token_hash      TEXT NOT NULL,           -- hash(raw_token) using a strong KDF
    token_type      TEXT NOT NULL,           -- 'api', 'node', 'internal'
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ,             -- NULL = until revoked
    revoked         BOOLEAN NOT NULL DEFAULT FALSE,
    revoked_at      TIMESTAMPTZ,
    last_used_at    TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_token_license_hash ON token(license_id, token_hash);
CREATE INDEX IF NOT EXISTS idx_token_license ON token(license_id);
CREATE INDEX IF NOT EXISTS idx_token_revoked ON token(revoked);
CREATE INDEX IF NOT EXISTS idx_token_expires_at ON token(expires_at);
-- Introspection filters on token_hash ALONE; the composite ux_token_license_hash
-- cannot serve a hash-only lookup (wrong leading column). Standalone index keeps
-- the auth hot path an index scan instead of an O(n) seq scan. See migration 008.
CREATE INDEX IF NOT EXISTS idx_token_hash ON token(token_hash);

-- =========================================================
-- Notes on usage
-- ---------------------------------------------------------
-- - Keycloak handles authentication; this service receives a subject identifier
--   (keycloak_sub/keycloak_realm) and no PII is stored here.
-- - Secrets are never stored in this schema; only entitlements and tokens.
-- - Introspection flow:
--     1) Client sends raw token to /license/introspect
--     2) Service hashes token, looks up in token.token_hash
--     3) Verifies token.revoked=false, token.expires_at>=now()
--     4) Checks license.status='active' and license.expires_at>=now()
--     5) Returns app/plan/status/expiry info to caller.
-- - Revocation:
--     - Mark license.status='revoked', set revoked_at
--     - Set token.revoked=true for all tokens of that license.
-- =========================================================
