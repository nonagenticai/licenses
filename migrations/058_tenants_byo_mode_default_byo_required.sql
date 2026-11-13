-- 058: new tenants default to byo_mode 'byo_required' (owner ruling 2026-10-15,
-- licenses#273).
--
-- WHY. Early access: platform Mac GPU services (mflux image, kokoro/kitten TTS,
-- whisper ASR) are only for the owner's EXISTING tenants; every NEW early-access
-- tenant brings its own API tokens. A tenant created without an explicit byo_mode
-- (POST /tenants, PUT /tenants/{id}, ensure_tenant/create_tenant with no byo_mode)
-- is therefore 'byo_required'. models.Tenant.byo_mode's server_default moves in
-- the same PR so the ORM arm (a fresh database) and this corpus agree.
--
-- This REVERSES migration 042 (George 2026-10-02, 'local_allowed') and restores
-- the 007 default.
--
-- SCOPE. The column DEFAULT only: it applies to rows INSERTed after this runs
-- without a byo_mode. ⛔ It does NOT update any existing row — the owner's tenants
-- (fr-test-user, claude, claude-test, ...) keep 'local_allowed', Netcare keeps
-- 'byo_required'. Explicit byo_mode values are still honoured, and a
-- kind='service' tenant created without one is still born 'local_allowed'
-- (ensure_tenant, migration 033 — ck_tenants_service_not_byo_required forbids
-- service + byo_required). tenant_app_byo_mode has no column default (NOT NULL,
-- always written explicitly), so it needs no change.
--
-- SAFETY. ALTER COLUMN ... SET DEFAULT is catalogue-only (no rewrite, no row
-- touched) and idempotent: a re-run sets the same default again.
--
-- DOWNGRADE: migrations/downgrades/058_tenants_byo_mode_default_byo_required.sql
-- (restores 'local_allowed'; lossless — no row data is involved).

ALTER TABLE tenants
    ALTER COLUMN byo_mode SET DEFAULT 'byo_required';
