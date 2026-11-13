-- Migration: index token.token_hash for the introspection hot path
-- Date: 2026-07-02
-- Description:
--   introspect_token (models.py) looks up a token by token_hash ALONE:
--       SELECT ... FROM token WHERE token_hash = $1
--   The only hash-bearing index is the composite ux_token_license_hash
--   (license_id, token_hash) whose LEADING column is license_id, so Postgres
--   cannot use it for a hash-only filter and falls back to a SEQUENTIAL SCAN of
--   token on every introspect. That scan grows O(n) with token count and, under
--   the concurrent e2e burst, pushes introspect latency past the callers'
--   client timeouts (w2v_common 10s, license-authz 2s) -> the caller conflates
--   the timeout with an invalid token and returns a spurious 401. Adding a
--   standalone index on token_hash turns the lookup into an index scan.
--
-- Forward-only / additive. Safe to run online.
--
-- Apply MANUALLY (no automated migration runner):
--   psql "$LICENSES_DATABASE_URL" -f migrations/008_add_token_hash_index.sql
-- CONCURRENTLY must run OUTSIDE a transaction (psql autocommit default is fine;
-- do NOT wrap in BEGIN/COMMIT). If a prior attempt was interrupted, drop the
-- INVALID leftover first: DROP INDEX IF EXISTS idx_token_hash;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_token_hash ON token (token_hash);

-- Verify:
--   EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM token WHERE token_hash = '<hash>';
--   -> expect "Index Scan using idx_token_hash", not "Seq Scan on token".
