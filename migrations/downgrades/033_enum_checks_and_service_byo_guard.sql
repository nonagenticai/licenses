-- Downgrade 033. Drops all three CHECK constraints. Schema-only, so unlike a
-- data migration's downgrade there is no row-level scoping to get wrong — this
-- simply removes the guard the upgrade added.
--
-- ⚠️ WHAT DOWNGRADING COSTS. Once dropped, the database will again accept an
-- unrecognised `license.status`, an unrecognised `token.token_type`, and a
-- `tenants` row with `kind='service' AND byo_mode='byo_required'` — the exact
-- incoherent state migration 031 repaired and 033's own header measured at 0
-- rows. `DatabaseManager.ensure_tenant`'s Python-level default/guard (added in
-- the same commit as 033) is NOT reverted by this file — it stays in place and
-- keeps `kind='service'` tenants coherent even with the CHECK gone. Only roll
-- back if 033 itself is the problem.
--
-- IF NOT EXISTS-guarded, so a re-run (or running against a database that never
-- had 033 applied) is a no-op.

ALTER TABLE license DROP CONSTRAINT IF EXISTS ck_license_status;
ALTER TABLE token DROP CONSTRAINT IF EXISTS ck_token_token_type;
ALTER TABLE tenants DROP CONSTRAINT IF EXISTS ck_tenants_service_not_byo_required;
