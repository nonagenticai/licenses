-- 054: owners for the bundle-token minters; merqi / w2v service tenants to their app.
--
-- THE HOLE (C1). POST /internal/tenants/{id}/bundle-token checked only the realm role
-- `bundle-token-minter` and loaded the tenant unscoped, so every holder could mint a
-- live bearer on ANY tenant's bundle licence — merqi's marketing could mint on an fr
-- customer. Since this migration the route is OWNER-SCOPED (main.py
-- internal_mint_tenant_bundle_token -> _resolve_tenant_for_caller): the caller's app
-- (api_client) must own the tenant or hold an active grant on it, and an unregistered
-- client is 403 `unknown_api_client`. None of the four holders had an api_client row,
-- so without the seed below every live mint would 403.
--
-- THE HOLDERS (gitops-production origin/main, clusters/strato/services/keycloak/
-- realm-patch-script-configmap.yaml: declare_n8n_bundle_resolver +
-- declare_service_bundle_minters; all realm secure-apps, each holds
-- `bundle-token-minter` and NOTHING else) and the tenants they actually mint on
-- (Loki, licenses namespace, 2026-10-07..14, `bundle_token_minted|refused`):
--   marketing-bundle-minter          -> merqi   merqi (w2v 180x, raise-my-tickets 4x)
--   sales-bundle-minter              -> merqi   merqi (raise-my-tickets 177x),
--                                               netcare (CP-owned, merqi grant:
--                                               raise-my-tickets 2x, w2v 1x),
--                                               tenant-dev (platform, merqi grant:
--                                               404 app_not_in_bundle 1813x)
--   worker-management-bundle-minter  -> platform inference-router on svc-cdmsfirst-n8n,
--                                               svc-kag-karma, marketing, aa-agents
--                                               (platform) and svc-w2v-shared (w2v)
--   n8n-bundle-resolver              -> platform no mint in the window; serves the n8n
--                                               nodes of every tenant
-- platform holds an active grant on every non-platform tenant (050 grant rule 1;
-- measured 0 exceptions 2026-10-14), so the two platform clients keep their reach
-- without break-glass. Every call above stays admitted under the owner/grantee rule.
--
-- SERVICE TENANTS (C2). 050 left these on 'platform' as "operator-provisioned". Their
-- users are now known, and 050's own rule is "a svc tenant is owned by the app whose
-- own code uses it":
--   w2v:   svc-w2v-cell-m4-gate-tenant  4cc78f13-456c-5243-a026-5c4c1b67aa77
--          svc-w2v-cell-m4-orchestrator 396f3448-ccc1-584a-92f8-a1a3de3fbe1d
--          svc-w2v-cell-m4-partner      baad6bac-7541-5d9f-b209-423b94bcdc5f
--            the w2v cell deployed on m4 (w2v_orchestrator deploy/compose, keycloak
--            client w2v-cell-m4, gitops #4594); w2v already holds a grant on all 3.
--   merqi: svc-control-service-w2v-partner c44cf5dd-c721-57e3-acb2-64305acb3dc0
--            merqi control-service's w2v PARTNER licence 01a13515-… (gitops
--            services/w2v/configmaps.yaml W2V_PARTNER_LICENSE_IDS,
--            control-service W2V_PARTNER_KEY). w2v KEEPS its active grant here, so w2v
--            (sole issuer of `w2v`) can still issue / revoke that licence.
--          svc-marketing / svc-sales / svc-growth / svc-control-relay /
--          svc-platform-console
--            the OWN tenants of merqi's marketing-, sales-, growth-service-account,
--            control-relay-account and platform-console-backend, resolved BY NAME by
--            license-authz (_resolve_service_principal_tenant via licenses
--            GET /internal/tenants, which is NOT owner-scoped — so the move does not
--            change that lookup; names stay unique among live tenants, 053).
--          svc-marketing-t-c46c9378 c46c9378-eef5-57d6-b5f9-307e86db915d
--            marketing's on-behalf-of token subject; merqi already holds a grant.
--   STAY platform: tenant-dev (shared dev tenant: fr, merqi, w2v all hold grants) and
--   svc-core-app-mlx (mlx-ingestion-backend; not a merqi/w2v identity).
-- Matched by NAME, LIVE rows, owner currently 'platform' only — a no-op anywhere else,
-- and skipped where the target owner already has a live tenant of that name.
--
-- GRANTS after the move (the 050 invariants, restored generically, so this is
-- idempotent and touches nothing that already satisfies them):
--   * every non-platform tenant holds an active 'platform' grant (portal + WM + n8n);
--   * no owner holds a grant on its own tenant (the w2v / merqi grants on the moved
--     tenants become redundant and are revoked, revoked_at = now()).
--
-- ONE TRANSACTION; every INSERT is ON CONFLICT DO NOTHING. A FRESH database baselines
-- this file without running it: the api_client rows come from the boot seed
-- (models.API_CLIENTS, pinned equal by tests/unit/test_migration_054_*.py) and there
-- are no service tenants to move.
--
-- DOWNGRADE: migrations/downgrades/054_bundle_minter_owners_and_service_tenant_owners.sql
-- (moves the tenants back, re-derives 050's sole-issuer grants on them, deletes the
-- four api_client rows). Roll the CODE back too or every mint 403s.

BEGIN;

INSERT INTO api_client (realm, client_id, owner_key, cross_owner) VALUES
    ('secure-apps', 'marketing-bundle-minter', 'merqi', false),
    ('secure-apps', 'sales-bundle-minter', 'merqi', false),
    ('secure-apps', 'worker-management-bundle-minter', 'platform', false),
    ('secure-apps', 'n8n-bundle-resolver', 'platform', false)
ON CONFLICT (realm, client_id) DO NOTHING;

UPDATE tenants t
SET owner_key = m.owner_key
FROM (VALUES
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
) AS m(name, owner_key)
WHERE t.name = m.name
  AND t.owner_key = 'platform'
  AND t.archived_at IS NULL
  AND NOT EXISTS (
      SELECT 1 FROM tenants o
      WHERE o.owner_key = m.owner_key AND o.name = m.name AND o.archived_at IS NULL
  );

-- platform is infrastructure (050 rule 1).
INSERT INTO tenant_app_grant (tenant_id, grantee_owner_key, granted_by)
SELECT t.id, 'platform', 'platform-infrastructure'
FROM tenants t
WHERE t.owner_key <> 'platform'
ON CONFLICT (tenant_id, grantee_owner_key) WHERE revoked_at IS NULL DO NOTHING;

-- an owner never holds a grant on its own tenant.
UPDATE tenant_app_grant g
SET revoked_at = now()
FROM tenants t
WHERE t.id = g.tenant_id
  AND g.grantee_owner_key = t.owner_key
  AND g.revoked_at IS NULL;

COMMIT;
