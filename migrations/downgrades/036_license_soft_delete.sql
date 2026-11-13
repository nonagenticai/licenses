-- Downgrade 036. Drops the four soft-delete columns.
--
-- ⛔ THIS IS LOSSLESS ONLY WHILE `LICENSE_SOFT_DELETE` HAS NEVER BEEN ARMED, and
-- the direction of the loss is the reason 036 exists in the first place: these
-- columns are the ONLY record that a licence was deleted. Dropping them does not
-- restore the licence — the row survives — it destroys the fact that anyone asked
-- for it to go, WHO asked, WHY, and WHEN. A soft-deleted row then becomes
-- indistinguishable from an ordinary revoked one, and every past-instant seat
-- count that depended on `deleted_at` silently returns the smaller number again.
--
-- ⚠️ IT ALSO SILENTLY CHANGES A LIVE ANSWER, WHICH A COLUMN DROP NORMALLY DOES
-- NOT. `models.effectively_active_clauses` reads `deleted_at` and
-- `deleted_from_status`, so on a tree that still has that code the queries behind
-- BOTH billing endpoints stop compiling (UndefinedColumn -> 500 on
-- `/internal/tenants/{id}/seat-count` and `/internal/licenses`). Downgrade the
-- CODE first, or downgrade both together; this file cannot detect the mismatch.
--
-- So before running this on a database where the flag has ever been on:
--     SELECT count(*) FROM license WHERE deleted_at IS NOT NULL;
-- and if it is not 0, dump the rows first — they are unrecoverable afterwards:
--     \copy (SELECT id, tenant_id, subject_id, app_id, status, starts_at,
--                   expires_at, deleted_at, deleted_by, delete_reason,
--                   deleted_from_status
--              FROM license WHERE deleted_at IS NOT NULL)
--           TO 'license_soft_deleted_backup.csv' CSV HEADER
--
-- On a database where 036 is the head and the flag was never armed, that count is
-- 0 by construction — 036 performs no backfill and the write path is gated — and
-- this downgrade is genuinely lossless.
--
-- The column comments go with the columns; there is nothing else to undo (036
-- adds no index and no constraint, for the reasons stated in its header).
DO $$
DECLARE
    n_soft_deleted bigint := 0;
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'license' AND column_name = 'deleted_at'
    ) THEN
        EXECUTE 'SELECT count(*) FROM license WHERE deleted_at IS NOT NULL'
           INTO n_soft_deleted;
        IF n_soft_deleted > 0 THEN
            -- WARNING, not EXCEPTION: a downgrade that refuses to run is a
            -- downgrade nobody can use in an incident. It is announced loudly
            -- instead, naming the count, so the loss is a decision rather than a
            -- surprise.
            RAISE WARNING
                'downgrade 036: DESTROYING the deletion record of % licence row(s). '
                'The rows themselves survive but become indistinguishable from '
                'ordinary revoked licences, and past-instant seat counts will '
                'under-report again. Dump them first (see this file''s header) if '
                'that has not already been done.',
                n_soft_deleted;
        END IF;
    END IF;
END $$;

ALTER TABLE license DROP COLUMN IF EXISTS deleted_from_status;
ALTER TABLE license DROP COLUMN IF EXISTS delete_reason;
ALTER TABLE license DROP COLUMN IF EXISTS deleted_by;
ALTER TABLE license DROP COLUMN IF EXISTS deleted_at;
