-- 050: tenants and licences are OWNED PER APP.
--
-- WHY. `tenants.name` was globally UNIQUE and `POST /tenants` was idempotent BY
-- NAME, so when commonpractice had already registered `acme` and fr onboarded
-- `acme`, fr was handed CP's tenant UUID — fr's users, licences and billing landed
-- inside CP's customer. There was also no issuer rule: any license-admin /
-- license-manager client could mint any app on any tenant (fr's super-admin
-- console minted `commonpractice` on netcare). Plan:
-- ~/.claude/plans/pasted-content-id-bbd6-licences-still-noble-stearns.md.
--
-- WHAT THIS ADDS.
--   tenant_owner      the apps that own tenants
--   api_client        verified (token iss realm, azp) -> owner; the ONLY source of a
--                     caller's owner. cross_owner = break-glass (licenses-admin only)
--   app_issuer        which owners may issue (mint/revoke) an app's licences
--   tenant_app_grant  the tenant's owner lets another owner act on the tenant
--   tenants.owner_key NOT NULL FK, NO default: the API sets it from the caller and
--                     every operator script / the boot seed names it explicitly
--   tenants.external_ref  the owner's own id for the tenant; unique per owner
--   names: global UNIQUE (tenants_name_key + unique ix_tenants_name) DROPPED,
--          UNIQUE (owner_key, name) ADDED; ix_tenants_name stays as a plain index.
--
-- api_client SEED (Keycloak DB census 2026-10-13, read-only; all 12 licence-role
-- mappings are service accounts):
--   portal/fr-backend, secure-apps/fr-backend, portal/provisioning-backend  -> fr
--   portal/w2v-backend                                                      -> w2v
--   secure-apps/commonpractice-licenses-admin                               -> commonpractice
--   secure-apps/control-service-account                                     -> merqi
--   secure-apps/admin-app-service-account  (admin_app = RaiseMyTickets'
--     tenant control plane, gitops keycloak realm-patch)                    -> merqi
--   secure-apps/book-fulfillment-svc                                        -> book-fulfillment
--   portal/portal-bff-service  (NonAgentic customer-portal BFF; proxies
--     portal users' own licence CRUD, portal-bff/src/config.py)            -> platform
--   secure-apps/licenses-admin                                    -> platform, cross_owner
--
-- app_issuer SEED (by app key; every app not listed -> {platform, fr}, because fr's
-- super-admin console mints across the whole catalogue via provisioning with
-- CALLER_ALLOWED_APP_KEYS="*"):
--   commonpractice                                   -> {commonpractice}
--   fr, fr-frontend, kag                             -> {fr}
--   inference-router                                 -> {fr, commonpractice}
--   w2v, w2v-image                                   -> {w2v}
--   raise-my-tickets, raise-my-tickets-admin/-analytics/-core, raisemytickets,
--   merqi, marketing, sales, core-app, chat-bridge-merqi, chat-bridge-rmt -> {merqi}
--   book-fulfillment                                 -> {book-fulfillment}
-- Mirrored in models.APP_ISSUER_OVERRIDES / scripts/seed_apps.py (fresh databases).
--
-- OWNER BACKFILL. Step 0 (2026-10-13) refuted "CP ids -> commonpractice": almost
-- every existing tenant is a platform tenant. So EVERY existing tenant -> 'platform'
-- EXCEPT this curated list, matched by ID (prod ids, measured read-only on the primary
-- 2026-10-14):
--   netcare            01a0ef72-31af-7561-936a-b8383b5fb6c8 -> commonpractice
--                      (George 2026-10-13: CP's only real customer. It stays
--                      ARCHIVED here; un-archiving is a post-deploy API step.)
--   cp-e2e-2026-10-13  01a135dd-698e-7000-864d-205ec171ad33 -> commonpractice
--   ola-armatys        019edc48-6060-76e9-8d8f-2e720e12c609 -> merqi
--                      (merqi control_service's tenant_lifecycle tenant; merqi
--                      also provisions it into w2v via its w2v_tenant step, so
--                      w2v reaches it through a grant: its live `w2v` licence
--                      makes it the SOLE issuer of a live app -> grant rule 2.)
--   tenant-dev         3806d6e5-4698-571e-9bb2-22ffd076ac73 stays platform; merqi
--                      references it and keeps a grant via rule 2 (live
--                      raise-my-tickets / marketing / sales licences, measured
--                      read-only on the primary 2026-10-14).
--   merqi              639c333a-1613-4fbc-aa14-a7b1430dc0db -> merqi
--   fr provisioning-created tenants (provisioning DB provisioning_runs.tenant_id,
--   read-only) -> fr, external_ref = name — provisioning sends external_ref =
--   fr's tenant_slug and is idempotent on (fr, external_ref), so without this a
--   re-provision would mint a DUPLICATE fr tenant beside the platform one:
--     isolation-probe-test 01a0fdb8-f785-7785-843d-0799f306cbbd
--     claude               01a03dd9-941a-7300-ad03-f099dcf93864
--     ahtapol              0195f48b-d000-7000-8000-000000000101
--     cdmsfirst            01a1117c-d9b0-72fc-aeda-a9d1187f2a36
--     aa-tenant-proof      019fa340-a5e4-709d-8047-f555f063068a (archived)
--     n8n-test             019de842-c92b-752e-b39c-30d64950e1cc
--     fr-test              019efaa0-512b-733e-9e98-b1ac82a6f7ee (archived)
--   (provisioning also onboarded netcare; it STAYS commonpractice per George.)
--
-- SERVICE TENANTS (kind 'service', svc-<x>; all 61 listed read-only on the primary
-- 2026-10-14). A svc tenant is owned by the app whose OWN code creates or uses it;
-- every other one — operator-provisioned service identities, probes, e2e, the
-- license-authz service-principal tenants — stays 'platform'. Matched by ID:
--   svc-commonpractice  b8e94a81-dbd3-5f75-8a58-45deceaef000 -> commonpractice
--       CP's own inference-router holder (commonpractice config.ts /
--       tenant-inference.ts, migration 0100); CP minted its cp-tenant licence
--       there via POST /licenses/user on 2026-10-13 (Loki).
--   svc-w2v-shared      4af92cd8-4d13-54f1-aaf9-94ee2caaf823 -> w2v
--       the w2v deployments' DOWNSTREAM_LICENSE_TOKEN subject
--       (gitops services/w2v/deployments.yaml).
--   svc-tts-synth       333c001e-03eb-5508-901c-b05409b0df85 -> w2v
--       the subject w2v's mini1 narration worker (tts-gateway-worker) presents
--       (gitops external-services/tts-backend.yaml, w2v_common middleware).
--   svc-fr-backend      d4804d48-2761-50d7-9302-fb366fbcad1c -> fr
--       fr-backend's CODING_SERVICE_TOKEN (fr service-credential.ts).
--   svc-fr-backend-kag  dfb9ca84-05f9-510b-93ae-b8dda21ebd4a -> fr
--       fr-backend's KAG_ONBEHALF_TOKEN (fr service-credential.ts, gitops kag).
--   Stay 'platform' (no app code creates or uses them; operator-script
--   provisioned): svc-w2v-cell-m4-{gate-tenant,orchestrator,partner},
--   svc-control-service-w2v-partner, svc-core-app-mlx, svc-marketing-t-c46c9378,
--   svc-fr-backend-pm, svc-kag-frontend-pm, svc-n8n-local-conveyor; and
--   svc-marketing / svc-sales / svc-growth / svc-control-relay /
--   svc-platform-console, which only license-authz (platform) resolves by name.
--
-- GRANT BACKFILL — least privilege. granted_by = 'migration-050' unless noted.
--   1. PLATFORM IS INFRASTRUCTURE: every tenant NOT owned by platform gets an
--      active 'platform' grant (granted_by 'platform-infrastructure'), because
--      portal-bff-service (owner platform) serves portal users of every tenant.
--      The API writes the same grant whenever a non-platform owner creates one.
--   2. An owner O gets a grant on tenant T iff O is the SOLE issuer of some app
--      holding an ACTIVE licence on T (status 'active', not soft-deleted, not
--      expired). An app with SEVERAL issuers (inference-router: fr+commonpractice;
--      the long tail: platform+fr) grants nobody on its own: an issuer of it only
--      gets in through an app it alone issues. (This is the rule "issuer of the
--      app AND (single issuer OR sole issuer of another live app on T)" — the
--      second branch already yields the grant through that other app.) Before
--      this, every issuer of every live app got a grant, so commonpractice got 77
--      tenants through inference-router alone.
--   3. commonpractice's REGISTERED shared platform tenants (CP's tenant table,
--      step 0 2026-10-13): CP re-registers (PUT /tenants/{id}) before every mint
--      on these, so it keeps a grant even where no live commonpractice licence
--      remains (raisemytickets: both CP licences revoked):
--        system 00000000-0000-0000-0000-000000000001, default-anon
--        00000000-0000-0000-0000-000000000000, aa-agents
--        0195f48b-d000-7000-8000-000000000003, fr-test-user
--        0195f48b-d000-7000-8000-000000000099, nonagentic
--        019fe708-424a-7a31-bce8-da19020dc62b, raisemytickets
--        01a0ae64-3703-76a6-8e05-9a4826428d32
--
-- ONE TRANSACTION. The runner executes each top-level statement on an autocommit
-- connection, so BEGIN/COMMIT run as statements (same pattern as 005 / 016).
-- IDEMPOTENT: every CREATE / ADD is guarded and every INSERT is ON CONFLICT DO
-- NOTHING, so a re-run (or a database whose create_all already built the new,
-- empty tables before this ran — the boot order) is safe.
--
-- DOWNGRADE: migrations/downgrades/050_tenant_ownership_per_app.sql. LOSSY (owners,
-- grants, external refs) and it FAILS if two tenants now share a name. It also
-- deletes 050's schema_migrations stamp, so the runner re-applies 050 afterwards.

BEGIN;

CREATE TABLE IF NOT EXISTS tenant_owner (
    key           VARCHAR(64) PRIMARY KEY,
    display_name  VARCHAR(255) NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_tenant_owner_key_format CHECK (key ~ '^[a-z0-9][a-z0-9-]*$')
);

INSERT INTO tenant_owner (key, display_name) VALUES
    ('platform', 'Platform (internal, service and test tenants)'),
    ('fr', 'FR'),
    ('commonpractice', 'Common Practice'),
    ('w2v', 'W2V'),
    ('merqi', 'merqi / RaiseMyTickets'),
    ('book-fulfillment', 'Book fulfillment')
ON CONFLICT (key) DO NOTHING;

CREATE TABLE IF NOT EXISTS api_client (
    realm        VARCHAR(255) NOT NULL,
    client_id    VARCHAR(255) NOT NULL,
    owner_key    VARCHAR(64) NOT NULL
        CONSTRAINT fk_api_client_owner REFERENCES tenant_owner(key) ON DELETE RESTRICT,
    cross_owner  BOOLEAN NOT NULL DEFAULT false,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT api_client_pkey PRIMARY KEY (realm, client_id)
);

INSERT INTO api_client (realm, client_id, owner_key, cross_owner) VALUES
    ('portal', 'fr-backend', 'fr', false),
    ('secure-apps', 'fr-backend', 'fr', false),
    ('portal', 'provisioning-backend', 'fr', false),
    ('portal', 'w2v-backend', 'w2v', false),
    ('secure-apps', 'commonpractice-licenses-admin', 'commonpractice', false),
    ('secure-apps', 'control-service-account', 'merqi', false),
    ('secure-apps', 'book-fulfillment-svc', 'book-fulfillment', false),
    ('portal', 'portal-bff-service', 'platform', false),
    ('secure-apps', 'admin-app-service-account', 'merqi', false),
    ('secure-apps', 'licenses-admin', 'platform', true)
ON CONFLICT (realm, client_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS app_issuer (
    app_id     UUID NOT NULL
        CONSTRAINT fk_app_issuer_app REFERENCES app(id) ON DELETE CASCADE,
    owner_key  VARCHAR(64) NOT NULL
        CONSTRAINT fk_app_issuer_owner REFERENCES tenant_owner(key) ON DELETE RESTRICT,
    CONSTRAINT app_issuer_pkey PRIMARY KEY (app_id, owner_key)
);

INSERT INTO app_issuer (app_id, owner_key)
SELECT a.id, o.owner_key
FROM app a
CROSS JOIN LATERAL unnest(
    CASE a.key
        WHEN 'commonpractice' THEN ARRAY['commonpractice']
        WHEN 'fr' THEN ARRAY['fr']
        WHEN 'fr-frontend' THEN ARRAY['fr']
        WHEN 'kag' THEN ARRAY['fr']
        WHEN 'inference-router' THEN ARRAY['fr', 'commonpractice']
        WHEN 'w2v' THEN ARRAY['w2v']
        WHEN 'w2v-image' THEN ARRAY['w2v']
        WHEN 'raise-my-tickets' THEN ARRAY['merqi']
        WHEN 'raise-my-tickets-admin' THEN ARRAY['merqi']
        WHEN 'raise-my-tickets-analytics' THEN ARRAY['merqi']
        WHEN 'raise-my-tickets-core' THEN ARRAY['merqi']
        WHEN 'raisemytickets' THEN ARRAY['merqi']
        WHEN 'merqi' THEN ARRAY['merqi']
        WHEN 'marketing' THEN ARRAY['merqi']
        WHEN 'sales' THEN ARRAY['merqi']
        WHEN 'core-app' THEN ARRAY['merqi']
        WHEN 'chat-bridge-merqi' THEN ARRAY['merqi']
        WHEN 'chat-bridge-rmt' THEN ARRAY['merqi']
        WHEN 'book-fulfillment' THEN ARRAY['book-fulfillment']
        ELSE ARRAY['platform', 'fr']
    END
) AS o(owner_key)
ON CONFLICT (app_id, owner_key) DO NOTHING;

ALTER TABLE tenants ADD COLUMN IF NOT EXISTS owner_key VARCHAR(64);
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS external_ref VARCHAR(255);

UPDATE tenants t
SET owner_key = c.owner_key,
    external_ref = CASE WHEN c.ref_is_name THEN t.name END
FROM (VALUES
    ('01a0ef72-31af-7561-936a-b8383b5fb6c8'::uuid, 'commonpractice', false),  -- netcare
    ('01a135dd-698e-7000-864d-205ec171ad33'::uuid, 'commonpractice', false),  -- cp-e2e-2026-10-13
    ('019edc48-6060-76e9-8d8f-2e720e12c609'::uuid, 'merqi', false),           -- ola-armatys
    ('639c333a-1613-4fbc-aa14-a7b1430dc0db'::uuid, 'merqi', false),           -- merqi
    ('01a0fdb8-f785-7785-843d-0799f306cbbd'::uuid, 'fr', true),  -- isolation-probe-test
    ('01a03dd9-941a-7300-ad03-f099dcf93864'::uuid, 'fr', true),  -- claude
    ('0195f48b-d000-7000-8000-000000000101'::uuid, 'fr', true),  -- ahtapol
    ('01a1117c-d9b0-72fc-aeda-a9d1187f2a36'::uuid, 'fr', true),  -- cdmsfirst
    ('019fa340-a5e4-709d-8047-f555f063068a'::uuid, 'fr', true),  -- aa-tenant-proof
    ('019de842-c92b-752e-b39c-30d64950e1cc'::uuid, 'fr', true),  -- n8n-test
    ('019efaa0-512b-733e-9e98-b1ac82a6f7ee'::uuid, 'fr', true),  -- fr-test
    ('b8e94a81-dbd3-5f75-8a58-45deceaef000'::uuid, 'commonpractice', false),  -- svc-commonpractice
    ('4af92cd8-4d13-54f1-aaf9-94ee2caaf823'::uuid, 'w2v', false),  -- svc-w2v-shared
    ('333c001e-03eb-5508-901c-b05409b0df85'::uuid, 'w2v', false),  -- svc-tts-synth
    ('d4804d48-2761-50d7-9302-fb366fbcad1c'::uuid, 'fr', false),   -- svc-fr-backend
    ('dfb9ca84-05f9-510b-93ae-b8dda21ebd4a'::uuid, 'fr', false)    -- svc-fr-backend-kag
) AS c(id, owner_key, ref_is_name)
WHERE t.id = c.id AND t.owner_key IS NULL;

UPDATE tenants SET owner_key = 'platform' WHERE owner_key IS NULL;

-- No default: a writer that forgets the owner fails NOT NULL instead of silently
-- landing in 'platform'. DROP DEFAULT also clears the default an earlier draft of
-- this migration set, on a database that ran it.
ALTER TABLE tenants ALTER COLUMN owner_key DROP DEFAULT;
ALTER TABLE tenants ALTER COLUMN owner_key SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_tenants_owner') THEN
        ALTER TABLE tenants
            ADD CONSTRAINT fk_tenants_owner
            FOREIGN KEY (owner_key) REFERENCES tenant_owner(key) ON DELETE RESTRICT;
    END IF;
END $$;

-- Swap the uniqueness: global name -> (owner, name).
ALTER TABLE tenants DROP CONSTRAINT IF EXISTS tenants_name_key;
DROP INDEX IF EXISTS ix_tenants_name;
CREATE INDEX IF NOT EXISTS ix_tenants_name ON tenants (name);
CREATE UNIQUE INDEX IF NOT EXISTS uq_tenants_owner_name ON tenants (owner_key, name);
CREATE UNIQUE INDEX IF NOT EXISTS uq_tenants_owner_external_ref
    ON tenants (owner_key, external_ref) WHERE external_ref IS NOT NULL;

CREATE TABLE IF NOT EXISTS tenant_app_grant (
    id                 UUID PRIMARY KEY DEFAULT uuidv7(),
    tenant_id          UUID NOT NULL
        CONSTRAINT fk_tenant_app_grant_tenant REFERENCES tenants(id) ON DELETE RESTRICT,
    grantee_owner_key  VARCHAR(64) NOT NULL
        CONSTRAINT fk_tenant_app_grant_owner REFERENCES tenant_owner(key) ON DELETE RESTRICT,
    granted_by         TEXT NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at         TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_tenant_app_grant_active
    ON tenant_app_grant (tenant_id, grantee_owner_key) WHERE revoked_at IS NULL;

-- 1. platform is infrastructure.
INSERT INTO tenant_app_grant (tenant_id, grantee_owner_key, granted_by)
SELECT t.id, 'platform', 'platform-infrastructure'
FROM tenants t
WHERE t.owner_key <> 'platform'
ON CONFLICT (tenant_id, grantee_owner_key) WHERE revoked_at IS NULL DO NOTHING;

-- 2. the SOLE issuer of an app with a live licence on the tenant.
INSERT INTO tenant_app_grant (tenant_id, grantee_owner_key, granted_by)
SELECT DISTINCT l.tenant_id, sole.owner_key, 'migration-050'
FROM license l
JOIN tenants t ON t.id = l.tenant_id
JOIN (
    SELECT app_id, min(owner_key) AS owner_key
    FROM app_issuer
    GROUP BY app_id
    HAVING count(*) = 1
) sole ON sole.app_id = l.app_id
WHERE l.status = 'active'
  AND l.deleted_at IS NULL
  AND (l.expires_at IS NULL OR l.expires_at > now())
  AND sole.owner_key <> t.owner_key
ON CONFLICT (tenant_id, grantee_owner_key) WHERE revoked_at IS NULL DO NOTHING;

-- 3. commonpractice's registered shared platform tenants.
INSERT INTO tenant_app_grant (tenant_id, grantee_owner_key, granted_by)
SELECT t.id, 'commonpractice', 'migration-050'
FROM tenants t
WHERE t.owner_key <> 'commonpractice'
  AND t.id IN (
    '00000000-0000-0000-0000-000000000001',  -- system
    '00000000-0000-0000-0000-000000000000',  -- default-anon
    '0195f48b-d000-7000-8000-000000000003',  -- aa-agents
    '0195f48b-d000-7000-8000-000000000099',  -- fr-test-user
    '019fe708-424a-7a31-bce8-da19020dc62b',  -- nonagentic
    '01a0ae64-3703-76a6-8e05-9a4826428d32'   -- raisemytickets
  )
ON CONFLICT (tenant_id, grantee_owner_key) WHERE revoked_at IS NULL DO NOTHING;

COMMIT;
