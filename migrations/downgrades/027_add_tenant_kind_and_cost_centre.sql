-- Downgrade 027. Drops the constraint then both columns; the classification is
-- not recoverable afterwards, which is fine — it is re-derivable from 027 itself.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_tenants_kind') THEN
        ALTER TABLE tenants DROP CONSTRAINT ck_tenants_kind;
    END IF;
END $$;
DROP INDEX IF EXISTS ix_tenants_kind;
ALTER TABLE tenants DROP COLUMN IF EXISTS cost_centre;
ALTER TABLE tenants DROP COLUMN IF EXISTS kind;
