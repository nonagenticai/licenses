-- 085: register the m4 Studio cell's own S2S client secure-apps/w2v-cell-m4 (owner w2v).
--
-- WHY. w2v-cell-m4 (gitops #4594, secure-apps realm) is the m4 w2v cell's OWN service
-- account — never the fleet's w2v-backend. It reads OTHER tenants' licences: the cell's
-- licence gate calls GET /licenses/active?tenant_id=<served tenant> and the provider
-- resolver GET /tenants/{id}/apps/w2v/byo-mode, each with X-Tenant-Id = the served
-- tenant. The middleware honours that X-Tenant-Id retarget ONLY for a client whose
-- api_client owner owns the tenant or holds an active grant on it (050). With no row
-- the header is ignored, so a least-privilege `license-reader` token can read nothing
-- but its own (non-existent) tenant: every tenant route 401/403s and the gate goes
-- INDETERMINATE (503). That is why gitops #4935 had to give the client
-- `service-internal` (an unscoped cross-tenant read on every internal route).
--
-- With this row + `license-reader` the cell reads exactly w2v's own and granted
-- tenants, nothing else. gitops then swaps service-internal -> license-reader.
-- ⛔ ORDER: this migration must be LIVE before that gitops swap, or the cell's gate
-- 503s for every tenant in the window between them.
--
-- SAME OWNER as w2v-backend (w2v): the cell is a w2v deployment. Not cross-owner.
-- Data only, idempotent (ON CONFLICT DO NOTHING — an operator's row is never
-- overwritten), one transaction. A FRESH database gets the row from the boot seed
-- (models.API_CLIENTS, pinned equal at head by tests/unit/test_migration_085_*.py).
--
-- DOWNGRADE: migrations/downgrades/085_w2v_cell_m4_api_client.sql (deletes the row).
-- Remove it from models.API_CLIENTS too, or the boot seed re-inserts it.

BEGIN;

INSERT INTO api_client (realm, client_id, owner_key, cross_owner) VALUES
    ('secure-apps', 'w2v-cell-m4', 'w2v', false)
ON CONFLICT (realm, client_id) DO NOTHING;

COMMIT;
