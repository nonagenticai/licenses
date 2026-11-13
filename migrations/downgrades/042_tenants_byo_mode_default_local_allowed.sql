-- Downgrade 042. Restores the pre-042 column DEFAULT 'byo_required' (migration 007).
--
-- Lossless: catalogue-only, no row is read or written. Tenants created while 042
-- was live keep the 'local_allowed' they were born with; flip any that must be
-- byo_required explicitly (PUT /tenants/{id}/byo-mode).
--
-- ⚠️ Roll back the CODE as well (models.Tenant.byo_mode server_default), or the
-- schema-parity gate will report the ORM and corpus arms diverging.
ALTER TABLE tenants
    ALTER COLUMN byo_mode SET DEFAULT 'byo_required';
