-- Downgrade 029. Drops the FK, the index and the column.
--
-- ⚠️ ANY PARENTAGE RECORDED IN `subject.tenant_id` IS DESTROYED AND IS NOT
-- RE-DERIVABLE. Unlike 027's `kind` (recomputable from 027's own name lists) and
-- 028's bundle membership (re-derivable from 028's own VALUES block), this column
-- holds facts stated by an operator or by a later backfill — there is no list in
-- this repo to regenerate them from, and "infer the parent from the licence rows"
-- is exactly the inverted inference the column exists to replace (a subject may
-- hold licences under more than one tenant, so that join returns a SET).
--
-- So before running this on a database where anything has been written:
--     SELECT count(*) FROM subject WHERE tenant_id IS NOT NULL;
-- and if it is not 0, dump the pairs first:
--     \copy (SELECT id, keycloak_sub, tenant_id FROM subject WHERE tenant_id IS
--            NOT NULL) TO 'subject_tenant_backup.csv' CSV HEADER
-- On a database where 029 is the head, that count is 0 by construction — 029
-- performs no backfill — and this downgrade is genuinely lossless.
--
-- Order matters: the index and the constraint both depend on the column, and
-- DROP COLUMN would take them with it. They are dropped explicitly anyway so the
-- intent is legible and so a partially-applied 029 (column present, constraint
-- absent) downgrades cleanly.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_subject_tenant') THEN
        ALTER TABLE subject DROP CONSTRAINT fk_subject_tenant;
    END IF;
END $$;
DROP INDEX IF EXISTS idx_subject_tenant;
ALTER TABLE subject DROP COLUMN IF EXISTS tenant_id;
