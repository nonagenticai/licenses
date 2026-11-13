-- Downgrade 054: the bundle-token minters lose their owners; the merqi / w2v service
-- tenants go back to 'platform'.
--
-- ⚠️ Roll back the CODE as well (the owner scope in
-- main.internal_mint_tenant_bundle_token, the four rows in models.API_CLIENTS), or
-- every bundle-token mint by these clients 403s `unknown_api_client` — and the boot
-- seed re-inserts the rows on the next start anyway.
--
-- LOSSY. A tenant is moved back only while it is still owned by the app 054 gave it
-- and no live 'platform' tenant has its name. Grants are re-derived with 050's rule 2
-- (the SOLE issuer of an app with a live licence on the tenant gets a grant), which is
-- how they existed before 054; a grant an operator added since is not recreated. The
-- 'platform' grants on the moved-back tenants are revoked (platform owns them again).

BEGIN;

DELETE FROM api_client
WHERE realm = 'secure-apps'
  AND cross_owner = false
  AND (client_id, owner_key) IN (
      ('marketing-bundle-minter', 'merqi'),
      ('sales-bundle-minter', 'merqi'),
      ('worker-management-bundle-minter', 'platform'),
      ('n8n-bundle-resolver', 'platform')
  );

CREATE TEMP TABLE m054_back ON COMMIT DROP AS
SELECT t.id, t.owner_key AS was_owner
FROM tenants t
JOIN (VALUES
    ('svc-w2v-cell-m4-gate-tenant', 'w2v'),
    ('svc-w2v-cell-m4-orchestrator', 'w2v'),
    ('svc-w2v-cell-m4-partner', 'w2v'),
    ('svc-control-service-w2v-partner', 'merqi'),
    ('svc-marketing', 'merqi'),
    ('svc-marketing-t-c46c9378', 'merqi'),
    ('svc-sales', 'merqi'),
    ('svc-growth', 'merqi'),
    ('svc-control-relay', 'merqi'),
    ('svc-platform-console', 'merqi')
) AS m(name, owner_key) ON t.name = m.name AND t.owner_key = m.owner_key
WHERE t.archived_at IS NULL
  AND NOT EXISTS (
      SELECT 1 FROM tenants o
      WHERE o.owner_key = 'platform' AND o.name = m.name AND o.archived_at IS NULL
  );

UPDATE tenants t SET owner_key = 'platform' FROM m054_back b WHERE t.id = b.id;

UPDATE tenant_app_grant g
SET revoked_at = now()
FROM m054_back b
WHERE g.tenant_id = b.id AND g.grantee_owner_key = 'platform' AND g.revoked_at IS NULL;

INSERT INTO tenant_app_grant (tenant_id, grantee_owner_key, granted_by)
SELECT DISTINCT l.tenant_id, sole.owner_key, 'downgrade-054'
FROM license l
JOIN m054_back b ON b.id = l.tenant_id
JOIN (
    SELECT app_id, min(owner_key) AS owner_key
    FROM app_issuer
    GROUP BY app_id
    HAVING count(*) = 1
) sole ON sole.app_id = l.app_id
WHERE l.status = 'active'
  AND l.deleted_at IS NULL
  AND (l.expires_at IS NULL OR l.expires_at > now())
  AND sole.owner_key <> 'platform'
ON CONFLICT (tenant_id, grantee_owner_key) WHERE revoked_at IS NULL DO NOTHING;

-- Un-stamp 054 so scripts/apply_migrations.py re-applies it (up -> down -> up).
DO $$
BEGIN
    IF to_regclass('public.schema_migrations') IS NOT NULL THEN
        DELETE FROM schema_migrations
        WHERE filename = '054_bundle_minter_owners_and_service_tenant_owners.sql';
    END IF;
END $$;

COMMIT;
