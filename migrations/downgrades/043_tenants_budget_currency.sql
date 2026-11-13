-- Downgrade 043: drop tenants.budget_currency (and its CHECK with it).
--
-- ⚠️ LOSSY for any tenant marked 'EUR'. Roll back the CODE as well
-- (models.Tenant.budget_currency), or the schema-parity gate reports the ORM and
-- corpus arms diverging and _provision_budget reads a column that is gone.
ALTER TABLE tenants
    DROP COLUMN IF EXISTS budget_currency;
