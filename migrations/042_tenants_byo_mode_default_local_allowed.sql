-- 042: new tenants default to byo_mode 'local_allowed' (George ruling 2026-10-02).
--
-- WHY. George, 2026-10-02: "for now the only tenants we have will use our
-- local-llm". A tenant created without an explicit byo_mode should therefore be
-- allowed onto the platform local-llm (while still honouring a configured BYO
-- provider), not refused with 402 byo_llm_not_configured. BYO stays switchable per
-- tenant later (PUT /tenants/{id}/byo-mode -> set_tenant_byo_mode).
--
-- This REVERSES the default set by migration 007 / #51 (the 2026-07-07 posture
-- flip, scripts/backfill_tenants_byo_mode.py), which made 'byo_required' the
-- column DEFAULT. models.Tenant.byo_mode's server_default moves in the same PR so
-- the ORM arm (what a fresh database gets) and this corpus agree.
--
-- SCOPE. The column DEFAULT only: it applies to rows INSERTed after this runs
-- without a byo_mode. ⛔ It does NOT update any existing row — moving specific
-- existing tenants is a separate, explicit operator action. A MISSING tenant row
-- still fails CLOSED to 'byo_required' at lookup time (_lookup_tenant_posture);
-- that rule is unchanged. The backfill script's classify() fall-through is
-- unchanged too (it classifies EXISTING rows; see its comment).
--
-- SAFETY. ALTER COLUMN ... SET DEFAULT is catalogue-only (no rewrite, no row
-- touched) and idempotent: a re-run sets the same default again.
--
-- DOWNGRADE: migrations/downgrades/042_tenants_byo_mode_default_local_allowed.sql
-- (restores 'byo_required'; lossless — no row data is involved).

ALTER TABLE tenants
    ALTER COLUMN byo_mode SET DEFAULT 'local_allowed';
