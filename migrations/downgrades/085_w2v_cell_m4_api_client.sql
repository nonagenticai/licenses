-- Downgrade 085: secure-apps/w2v-cell-m4 loses its owner.
--
-- ⚠️ Remove the row from models.API_CLIENTS as well, or the boot seed re-inserts it on
-- the next start. After this the cell's X-Tenant-Id retarget is ignored again, so with
-- `license-reader` every cross-tenant read 401/403s — only safe while the Keycloak
-- client still holds `service-internal` (revert the gitops swap FIRST).
-- Only the exact row 085 inserts is removed; an operator-changed owner is kept.

BEGIN;

DELETE FROM api_client
WHERE realm = 'secure-apps'
  AND client_id = 'w2v-cell-m4'
  AND owner_key = 'w2v'
  AND cross_owner = false;

-- Un-stamp 085 so scripts/apply_migrations.py re-applies it (up -> down -> up).
DO $$
BEGIN
    IF to_regclass('public.schema_migrations') IS NOT NULL THEN
        DELETE FROM schema_migrations
        WHERE filename = '085_w2v_cell_m4_api_client.sql';
    END IF;
END $$;

COMMIT;
