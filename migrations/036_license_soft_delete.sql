-- 036: record a licence DELETION on the row instead of destroying the row.
--
-- ⛔ THE DEFECT: A CLOSED INVOICE STOPS BEING REPRODUCIBLE.
-- `DELETE /licenses/user/{subject_id}` -> `delete_licenses_for_subject` issues a
-- bare `.delete()` with no confirmation and no record of any kind, and `token`
-- cascades away with it (`token.license_id` ON DELETE CASCADE). Meanwhile
-- `GET /internal/tenants/{tenant_id}/seat-count?as_of=` — the DENOMINATOR of
-- billing's per-seat access charge ($25/seat) — states in its own parameter
-- description that it "cannot reconstruct a licence that has since been
-- deleted". Both are true at once, so re-deriving a closed period's seat count
-- after any delete silently returns a SMALLER number than the invoice was
-- written from, with nothing anywhere to explain the difference.
--
-- Four columns, all NULLable, no backfill, no constraint change:
--
--   deleted_at           WHEN the licence was deleted. NULL = not deleted, which
--                        is every existing row and every row a hard delete
--                        leaves behind. This is the column the time-aware seat
--                        predicate reads.
--   deleted_by           WHO asked (the actor's `sub` from the verified JWT).
--                        TEXT, not a FK to `subject`: the actor is an ADMIN or a
--                        service account, which need not have a `subject` row in
--                        this registry at all, and an FK would fail the write for
--                        the ordinary case.
--   delete_reason        free text from the caller, so the row says why.
--   deleted_from_status  the value `status` held at the moment of deletion.
--
-- ⛔ WHY `deleted_from_status` IS NOT REDUNDANT, AND WHY `status` CANNOT SIMPLY
-- BE LEFT ALONE. `uq_license_active_tenant_app_subject` is a PARTIAL unique index
-- `WHERE status = 'active'` on (tenant_id, app_id, subject_id). A soft delete
-- that left `status='active'` would keep occupying that slot, so re-provisioning
-- the same person for the same app after a delete would fail on a row the
-- operator believes is gone. The soft delete therefore sets `status='revoked'`
-- (freeing the slot; a deleted licence must also stop authorizing anything) and
-- preserves the pre-deletion value HERE. Without that column a past-instant seat
-- count could not tell a licence that was active when it was deleted from one
-- that had already been revoked, and would over-count the second.
--
-- So a seat AT `as_of` is: the window contains `as_of`, AND either the row is not
-- deleted and reads `status='active'`, OR it was deleted AFTER `as_of` and was
-- `active` at that point. `models.effectively_active_clauses` is the single
-- expression of that, shared by the seat count and `/internal/licenses`.
--
-- ⚠️ THIS MIGRATION CHANGES NO BEHAVIOUR ON ITS OWN, and that is deliberate. The
-- soft-delete path is gated behind `LICENSE_SOFT_DELETE` (default FALSE), so
-- until that is armed every delete is still a hard delete, every one of these
-- columns stays NULL on every row, and the predicate above reduces EXACTLY to
-- `status = 'active'`. Landing the columns first is what makes arming the flag a
-- one-line config change rather than a schema change under load.
--
-- ⚠️ NO INDEX ON `deleted_at`. `license` is a few hundred rows and every query
-- that reads the new column already filters on `tenant_id` (idx_license_tenant)
-- or `status` (idx_license_status) first. A partial index here would be an
-- unmeasured guess; add one when a plan shows it is needed.
--
-- ⛔ NO CHECK CONSTRAINT COUPLING THE FOUR COLUMNS EITHER. The obvious one —
-- "deleted_at IS NULL iff deleted_from_status IS NULL" — would be correct for
-- every row this code writes, and is still refused: a CHECK added here is
-- evaluated against rows written by code that may be rolled back to, and a
-- partial write during a rollback window would then be un-writable rather than
-- merely untidy. The invariant is enforced at the write path, in
-- `soft_delete_licenses_for_subject`, which sets all four in one UPDATE.
--
-- Idempotent: `ADD COLUMN IF NOT EXISTS` on all four, so a re-apply is a no-op.
--
-- BASELINE INTERACTION (scripts/apply_migrations.py). On a FRESH database this
-- file is stamped applied WITHOUT running — but nothing is lost, because
-- `create_all()` builds `license` from the ORM and the four columns are declared
-- on `models.License`. Verified by tests/unit/test_schema_parity.py, which is
-- what makes "the ORM and the SQL agree" a measurement rather than a hope.
--
-- DOWNGRADE: migrations/downgrades/036_license_soft_delete.sql

ALTER TABLE license ADD COLUMN IF NOT EXISTS deleted_at timestamptz NULL;
ALTER TABLE license ADD COLUMN IF NOT EXISTS deleted_by text NULL;
ALTER TABLE license ADD COLUMN IF NOT EXISTS delete_reason text NULL;
ALTER TABLE license ADD COLUMN IF NOT EXISTS deleted_from_status varchar(50) NULL;

COMMENT ON COLUMN license.deleted_at IS
    'When this licence was SOFT-deleted. NULL = not deleted. A row with a value '
    'here is retained solely so a historical seat count stays derivable: it is '
    'excluded from every present-tense answer, and included by '
    'models.effectively_active_clauses only for an as_of EARLIER than this '
    'timestamp. Written only when LICENSE_SOFT_DELETE is armed.';

COMMENT ON COLUMN license.deleted_by IS
    'The `sub` of the admin or service account that deleted this licence. TEXT, '
    'not an FK to subject: the actor is frequently a service account with no '
    'subject row in this registry.';

COMMENT ON COLUMN license.delete_reason IS
    'Caller-supplied reason for the deletion. Free text — the only place the '
    'WHY of a destructive request is recorded.';

COMMENT ON COLUMN license.deleted_from_status IS
    'The value `status` held at the instant of the soft delete. NOT redundant: '
    'the soft delete must set status=''revoked'' to free the partial unique index '
    'uq_license_active_tenant_app_subject (WHERE status = ''active''), so without '
    'this column a past-instant seat count could not tell a licence that was '
    'ACTIVE when deleted from one that had already been revoked, and would '
    'over-count the second.';

-- Verify. Expected immediately after apply: the four columns present, and
-- deleted = 0 on every existing row (no backfill, nothing reinterpreted).
SELECT count(*)                                        AS total_licences,
       count(*) FILTER (WHERE deleted_at IS NOT NULL)   AS soft_deleted,
       count(*) FILTER (WHERE deleted_from_status IS NOT NULL) AS with_prior_status
  FROM license;
