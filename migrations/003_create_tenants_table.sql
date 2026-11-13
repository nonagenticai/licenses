-- Migration: Create tenants table for UUIDv7 identity standard
-- Date: 2026-04-27
-- Description: Adds canonical Tenant(id: UUIDv7, name: VARCHAR(255), keycloak_realm: STRING)
-- table that maps human-readable tenant names (e.g. 'n8n-dev', 'gerard-dev') to time-ordered
-- UUIDv7 ids. Foundation for cluster-wide tenant identity migration: license-authz will call
-- GET /license/tenants/{name} to translate incoming string tenant names to canonical UUIDs
-- for downstream propagation as X-Tenant-Id (UUID) plus X-Tenant-Name (string).
--
-- UUIDv7 generation: PostgreSQL 18 ships uuidv7() built-in (not a contrib extension).
-- We use it for the seed INSERTs and as the column DEFAULT so any raw INSERT also gets v7.
-- Application-side INSERTs go through SQLAlchemy with default=uuid7 (Python 3.14 stdlib).

CREATE TABLE IF NOT EXISTS tenants (
    id              UUID PRIMARY KEY DEFAULT uuidv7(),
    name            VARCHAR(255) NOT NULL UNIQUE,
    keycloak_realm  VARCHAR(255),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_tenants_name_format CHECK (name ~ '^[a-z0-9][a-z0-9-]*$')
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_tenants_name ON tenants(name);

-- Backfill known tenants. ON CONFLICT keeps this migration idempotent.
INSERT INTO tenants (id, name, created_at) VALUES
    (uuidv7(), 'n8n-dev', NOW()),
    (uuidv7(), 'gerard-dev', NOW())
ON CONFLICT (name) DO NOTHING;

-- Verify
SELECT id, name, keycloak_realm, created_at FROM tenants ORDER BY created_at;
