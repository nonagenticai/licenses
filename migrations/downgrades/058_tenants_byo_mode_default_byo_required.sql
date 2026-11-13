-- Downgrade 058. Restores the 042 column DEFAULT 'local_allowed'.
--
-- Lossless: catalogue-only, no row is read or written. Tenants created while 058
-- was live keep the 'byo_required' they were born with; flip any that must be
-- local_allowed explicitly (PUT /tenants/{id}/byo-mode).
--
-- ⚠️ Roll back the CODE as well (models.Tenant.byo_mode server_default), or the
-- schema-parity gate will report the ORM and corpus arms diverging.
ALTER TABLE tenants
    ALTER COLUMN byo_mode SET DEFAULT 'local_allowed';
