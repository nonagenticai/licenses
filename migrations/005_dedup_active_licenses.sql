-- Migration: dedup active licenses + enforce one active per (tenant, app, subject)
-- Date: 2026-06-18
-- Description: license creation had no idempotency, so portal-bff minted a fresh
-- ACTIVE license on every provision — ~66 redundant active rows (one (tenant,app,
-- subject) group had 35). This (a) revokes the redundant actives, keeping the one
-- whose token was most recently used (else the newest created), and (b) adds a
-- partial unique index so duplicates are impossible at the DB level.
-- create_user_license is now idempotent against this same key.
--
-- ⚠️ DEPLOY ORDER: apply AFTER the idempotent create_user_license is live, so the
-- old (duplicate-minting) code path can't hit the new index and 500.

BEGIN;

-- Cleanup: revoke every active license except the keeper per (tenant, app, subject).
-- Keeper = most-recently-used token (else newest created). History (revoked/expired)
-- is untouched.
WITH ranked AS (
    SELECT l.id,
           row_number() OVER (
               PARTITION BY l.tenant_id, l.app_id, l.subject_id
               ORDER BY (SELECT max(t.last_used_at) FROM token t WHERE t.license_id = l.id)
                            DESC NULLS LAST,
                        l.created_at DESC
           ) AS rn
    FROM license l
    WHERE l.status = 'active'
)
UPDATE license
SET status = 'revoked', revoked_at = NOW()
WHERE id IN (SELECT id FROM ranked WHERE rn > 1);

-- Backstop: at most one active license per (tenant, app, subject).
CREATE UNIQUE INDEX IF NOT EXISTS uq_license_active_tenant_app_subject
    ON license (tenant_id, app_id, subject_id)
    WHERE status = 'active';

COMMIT;

-- Verify: no (tenant, app, subject) group has >1 active row.
SELECT count(*) AS dup_groups_remaining
FROM (
    SELECT 1 FROM license WHERE status = 'active'
    GROUP BY tenant_id, app_id, subject_id HAVING count(*) > 1
) x;
