-- Migration: Seed the platform render/E2E service-account tenant row
-- Date: 2026-07-10
-- Description: Restores the `tenants` row for the platform's own render / nightly-E2E
-- service account (`portal-render-e2e`, id e148e8af-3751-49bf-a0fa-56ab46d31ddc) as
-- `local_allowed`, and makes its existence code-tracked / self-asserting.
--
-- Why it was missing: migration 004 runs `DELETE FROM tenants WHERE name = id::text`
-- to drop the legacy "name == its own UUID" hack rows. This account's row had been
-- backfilled with name == its UUID (it only gained the `portal-render-e2e` friendly
-- name + `local_allowed` classification on the 2026-07-07 posture flip), so when 004
-- applied on this DB (2026-07-09) it deleted the row along with the real hack rows.
-- With no `tenants` row, the token/introspection BYO lookup (`_lookup_byo_mode`, which
-- fails CLOSED on an unresolved tenant) returned `byo_required`, so the account's own
-- worker runs (e.g. w2v_orchestrator `growth_bundle`) hit WM `402 byo_llm_not_configured`.
--
-- This seeds the row with its FRIENDLY name, so migration 004's `name = id::text`
-- predicate can never match/delete it again. Idempotent + self-asserting: ON CONFLICT
-- re-affirms the correct name + posture if a stale row is ever present.
--
-- NOTE (scope): this seeds only the render/E2E service account — the single tenant that
-- fails on the token path. Other platform/internal tenants missing a `tenants` row still
-- resolve `local_allowed` via the tokenless-S2S fail-open path; the broader mass-deletion
-- (004 vs. the name==uuid rows the backfill creates) is tracked separately.

INSERT INTO tenants (id, name, byo_mode, created_at) VALUES
    ('e148e8af-3751-49bf-a0fa-56ab46d31ddc', 'portal-render-e2e', 'local_allowed', NOW())
ON CONFLICT (id) DO UPDATE
    SET name = EXCLUDED.name,
        byo_mode = EXCLUDED.byo_mode;

-- Verify
SELECT id, name, byo_mode FROM tenants
WHERE id = 'e148e8af-3751-49bf-a0fa-56ab46d31ddc';
