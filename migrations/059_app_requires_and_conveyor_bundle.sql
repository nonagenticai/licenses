-- 059: app dependencies (app_requires), dependency licences, the `conveyor` bundle,
-- and the fr / w2v bundle-token minters.
--
-- WHY (George 2026-10-15; ~/.claude/plans/program/20-designs/
-- tenant-app-licence-migration-PLAN.md, "Decisions", "model dependency chains as APP
-- DEPENDENCIES", "Sync with the m4 sessions", "Input from the licences/billing
-- session"). Licences are tenant x app only: a service calling another service for a
-- tenant presents THAT tenant's licence for the callee, minted through
-- POST /internal/tenants/{t}/bundle-token?app=<callee>. That needs (a) the callee's
-- licence on the tenant and (b) the caller's minter admitted on the tenant (054's
-- owner scope). Chains like merqi -> w2v -> worker_management are APP DEPENDENCIES,
-- not nested bundles: a bundle lists only what is SOLD; `app_requires` says what a
-- sold app NEEDS, and ensure provisions the transitive closure as DEPENDENCY licences.
--
-- SCHEMA (mirrored in models.AppRequires / BundleCaller / License.is_dependency):
--   app_requires (app_id, required_app_id, caller_owner_key)
--       PK app_requires_pkey; ck_app_requires_not_self; FKs to app ON DELETE CASCADE,
--       caller_owner_key -> tenant_owner ON DELETE RESTRICT. caller_owner_key = the
--       owner whose bundle-token minter mints required_app for the tenant (its
--       api_client owner); ensure GRANTS it on the tenant. A cycle is refused by
--       services.app_requires.closure (409 dependency_cycle) and by this file's
--       final check; it is never walked.
--   bundle_caller (bundle_id, owner_key, app_id) — an owner whose service calls ONE of
--       the bundle's own apps for the holding tenant (fr -> workers for conveyor).
--   tenant_dependency_grant (tenant_id, owner_key, app_id, granted_by, created_at) —
--       an APP-SCOPED grant: that owner's minter may mint THAT app on THAT tenant
--       through bundle-token, and nothing else. Written by ensure for every edge walked
--       (caller_owner_key, required_app) and every bundle_caller (owner, app), never for
--       the tenant's own owner. A SEPARATE TABLE, not a nullable tenant_app_grant.app_id:
--       tenant_app_grant is read by every owner-scoped path (tenant reads, issuance,
--       grant listings, bundle-token), each of which would have to filter the scope —
--       one that forgot would widen a dependency grant to a full grant (fail open).
--       A separate table is fail closed: only bundle-token reads it. Existing
--       tenant_app_grant rows keep their meaning (all apps the grantee issues / mints).
--   license.is_dependency BOOLEAN NOT NULL DEFAULT false + ck_license_dependency_unbundled
--       (NOT is_dependency OR bundle_id IS NULL). A REAL COLUMN, not metadata JSON:
--       (1) `metadata` is caller-written on POST /licenses/user, and the token-route
--       guard must not read a caller's claim; (2) billing mirrors licences from
--       GET /internal/licenses and its #274 licence inference must filter dependency
--       licences out, which it cannot do on nested JSON (the route now emits
--       `is_dependency`). A dependency licence is ONE per (tenant, app), SHARED by
--       every bundle whose closure needs the app, so it names no bundle; a later
--       bundle that SELLS the app adopts it and the adoption clears the flag.
--       Dependency licences mint ONLY through bundle-token: POST /licenses/{id}/tokens
--       and /regenerate-token answer 403 `dependency_licence`.
--
-- EDGES — each evidenced in code on origin/main (read 2026-10-15), plus live logs:
--   w2v -> workers     caller w2v    w2v_common@a012eab src/w2v_common/clients/
--                                    wm_client.py:224 WMClient (default worker-management
--                                    .workers.svc:8887), used by w2v_orchestrator@e58ceed
--                                    src/services/stages/course/outline.py:201.
--   w2v -> kag         caller w2v    w2v_common clients/kag_scrape.py:64 KagScrapeClient
--                                    (tenant_id or licence token), forwarded by
--                                    w2v_orchestrator src/models/auth_config.py:5.
--   marketing -> workers caller merqi marketing@b8e4775 src/settings.py:1624
--                                    MARKETING_WORKER_MANAGEMENT_URL, src/integrations/
--                                    worker_management_client.py.
--   sales -> workers   caller merqi  sales_app@e9c39e5 src/core/config.py:469
--                                    SALES_WORKER_MANAGEMENT_URL, src/core/
--                                    worker_management_client.py:34.
--   kag -> workers     caller kag    kag src/core/config.py:282 "kag routes ALL LLM calls
--                                    through worker_management" (WORKER_MANAGEMENT_URL).
--   kag -> mlx-backend caller kag    kag src/core/config.py:162-170 ML_BACKEND_URL
--                                    (embeddings); live 2026-10-15 06:23Z kag /query/ask
--                                    for tenant 5f868d42 called mlx-backend embeddings.
--   kag -> mlx-ingestion-backend caller kag  kag src/core/config.py:164-177
--                                    RERANK_BACKEND_URL (rerank lives there).
--   workers -> inference-router caller platform  worker_management src/services/
--                                    ir_bundle_tokens.py:7-8,50 mints
--                                    bundle-token?app=inference-router per tenant as
--                                    worker-management-bundle-minter (platform, 054).
-- NOT SEEDED (not evidenced as a DIRECT call): kag -> inference-router. kag stamps
--   KAG_INFERENCE_ROUTER_URL (config.py:470) as each WORKER's base_url_override; the
--   call to inference-router is made by worker_management with ITS minted token. The
--   live 06:23Z chain (kag + inference-router under one correlation id) is that path.
--   The closure still reaches inference-router: kag -> workers -> inference-router.
--
-- BUNDLE `conveyor` (SOLD = the apps fr and the conveyor call DIRECTLY):
--   workers       fr integrations/workers.ts (the PLAN's Step 1)
--   coding        n8n-local conveyor workflows: CUSTOM.coding node / codingApi
--   laya          n8n-local: 235 TypeSafe nodes, credential "Laya (conveyor)"
--   kag           n8n-local v3 faces: kag.query.semanticSearch
--   requirements  n8n-local v3 cards: requirements.* (117 refs)
--   procedures    n8n-local v3 cards: procedures.agenticProcedures.* (12 refs)
-- Cross-check: tenant 5f868d42 (svc-n8n-local-conveyor) holds an active licence for
-- each of the six (the primary, BEGIN READ ONLY, 2026-10-15).
-- Left OUT, and why:
--   fr                audience=human (its UI app); the conveyor's fr node is the
--                     tenant's own fr licence, not a service hop.
--   inference-router  reached through the closure (workers -> inference-router):
--                     77 tenants already hold it in the `inference-router` bundle, and
--                     a member here would REFUSE other_bundle on every one of them.
--   kanban            the n8n kanban node authenticates to BYO Planka (PlankaApi), not
--                     to a licensed app.
--   artifacts, secrets, tools, mcp, skill-builder, licenses, keycloak, metering,
--   observability, meta_prompt_core_actions — licensed on 5f868d42 (2026-10-11) but
--   called by no conveyor workflow.
-- Plans: every member has `standard` (and fr's tenant licences sit on perpetual /
-- tenant-pooled); ensure picks the first code of the caller's `plans` per app.
--
-- BUNDLE `w2v` already exists ({w2v, w2v-image}, ensurers {w2v, merqi, commonpractice})
-- and is unchanged: w2v is what it sells; kag / workers / mlx-* / inference-router come
-- from the closure.
--
-- app_issuer: NONE added. fr issues every conveyor app (live: workers, coding, laya,
-- kag, requirements, procedures all list fr), so 052 rule 1 makes fr an ensurer;
-- w2v already issues w2v + w2v-image. Dependency apps are not issued by the ensurer —
-- licences itself mints them, internal-only, as it mints every bundle licence.
-- bundle_ensurer: conveyor -> {fr} (rule 1). NOT platform: platform does not issue kag
-- (issuers fr, kag). ⚠️ tenant 5f868d42 is platform-OWNED, so ensuring conveyor there
-- takes break-glass or moving the tenant to fr — an operator decision, not this file's.
-- w2v already ensures `w2v` (052).
--
-- api_client (054's shape; realm secure-apps, NOT cross-owner; each Keycloak client
-- holds `bundle-token-minter` ONLY — gitops realm-patch BUNDLE_MINTER_CLIENTS, a
-- separate change; until they exist the rows are inert):
--   fr-bundle-minter  -> fr     (fr integrations/workers.ts)
--   w2v-bundle-minter -> w2v    (w2v_common WMClient; m4 builds the caller)
-- marketing-bundle-minter / sales-bundle-minter -> merqi already exist (054).
--
-- GRANTS written by ensure (not by this file), APP-SCOPED (tenant_dependency_grant):
-- e.g. merqi ensuring `w2v` on a merqi tenant writes (w2v, workers), (w2v, kag),
-- (kag, workers), (kag, mlx-backend), (kag, mlx-ingestion-backend),
-- (platform, inference-router). No tenant_app_grant is widened.
--
-- A missing app is ANNOUNCED AND SKIPPED (051's rule). Fresh database: this file is
-- stamped without running; the schema comes from models.py (tests/unit/
-- test_schema_parity.py), the api_client rows from the boot seed (models.API_CLIENTS),
-- and the ensurer / edge / caller seeds are mirrored in models.BUNDLE_ENSURER_SEED /
-- APP_REQUIRES_SEED / BUNDLE_CALLER_SEED (pinned by tests/unit/test_migration_059_*.py).
--
-- ONE TRANSACTION. DDL guarded (IF NOT EXISTS / pg_constraint), every INSERT ON
-- CONFLICT DO NOTHING: an operator's row is never overwritten, a re-run is a no-op.
--
-- DOWNGRADE: migrations/downgrades/059_app_requires_and_conveyor_bundle.sql (REFUSES
-- while any dependency licence exists or any tenant / licence holds `conveyor`). Roll
-- back models.py too, or create_all re-adds the column and tables.

BEGIN;

CREATE TABLE IF NOT EXISTS app_requires (
    app_id            UUID NOT NULL
        CONSTRAINT fk_app_requires_app REFERENCES app(id) ON DELETE CASCADE,
    required_app_id   UUID NOT NULL
        CONSTRAINT fk_app_requires_required_app REFERENCES app(id) ON DELETE CASCADE,
    caller_owner_key  VARCHAR(64) NOT NULL
        CONSTRAINT fk_app_requires_caller_owner REFERENCES tenant_owner(key) ON DELETE RESTRICT,
    CONSTRAINT app_requires_pkey PRIMARY KEY (app_id, required_app_id),
    CONSTRAINT ck_app_requires_not_self CHECK (app_id <> required_app_id)
);

CREATE TABLE IF NOT EXISTS bundle_caller (
    bundle_id  UUID NOT NULL
        CONSTRAINT fk_bundle_caller_bundle REFERENCES bundle(id) ON DELETE CASCADE,
    owner_key  VARCHAR(64) NOT NULL
        CONSTRAINT fk_bundle_caller_owner REFERENCES tenant_owner(key) ON DELETE RESTRICT,
    app_id     UUID NOT NULL
        CONSTRAINT fk_bundle_caller_app REFERENCES app(id) ON DELETE CASCADE,
    CONSTRAINT bundle_caller_pkey PRIMARY KEY (bundle_id, owner_key, app_id)
);

CREATE TABLE IF NOT EXISTS tenant_dependency_grant (
    tenant_id   UUID NOT NULL
        CONSTRAINT fk_tenant_dependency_grant_tenant REFERENCES tenants(id) ON DELETE RESTRICT,
    owner_key   VARCHAR(64) NOT NULL
        CONSTRAINT fk_tenant_dependency_grant_owner REFERENCES tenant_owner(key)
        ON DELETE RESTRICT,
    app_id      UUID NOT NULL
        CONSTRAINT fk_tenant_dependency_grant_app REFERENCES app(id) ON DELETE CASCADE,
    granted_by  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT tenant_dependency_grant_pkey PRIMARY KEY (tenant_id, owner_key, app_id)
);

ALTER TABLE license ADD COLUMN IF NOT EXISTS is_dependency BOOLEAN NOT NULL DEFAULT false;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_license_dependency_unbundled'
    ) THEN
        ALTER TABLE license ADD CONSTRAINT ck_license_dependency_unbundled
            CHECK (NOT is_dependency OR bundle_id IS NULL);
    END IF;
END $$;

INSERT INTO api_client (realm, client_id, owner_key, cross_owner) VALUES
    ('secure-apps', 'fr-bundle-minter', 'fr', false),
    ('secure-apps', 'w2v-bundle-minter', 'w2v', false)
ON CONFLICT (realm, client_id) DO NOTHING;

INSERT INTO bundle (key, name, description)
VALUES (
    'conveyor',
    'Conveyor',
    'The apps fr and the n8n conveyor call directly for a tenant: workers, coding, '
    'laya, kag, requirements, procedures. Dependencies (inference-router, mlx-*) come '
    'from app_requires (migration 059, 2026-10-15)'
)
ON CONFLICT (key) DO NOTHING;

DO $$
DECLARE
    r         RECORD;
    v_bundle  UUID;
    v_app     UUID;
    linked    INT := 0;
    skipped   INT := 0;
BEGIN
    SELECT id INTO v_bundle FROM bundle WHERE key = 'conveyor';
    FOR r IN
        SELECT * FROM (VALUES
            ('workers'),
            ('coding'),
            ('laya'),
            ('kag'),
            ('requirements'),
            ('procedures')
        ) AS t(app_key)
    LOOP
        SELECT id INTO v_app FROM app WHERE key = r.app_key;
        IF v_app IS NULL THEN
            RAISE NOTICE '059: skipping conveyor/% — no such app', r.app_key;
            skipped := skipped + 1;
            CONTINUE;
        END IF;
        INSERT INTO bundle_app (bundle_id, app_id)
        VALUES (v_bundle, v_app)
        ON CONFLICT (bundle_id, app_id) DO NOTHING;
        linked := linked + 1;
    END LOOP;
    RAISE NOTICE '059: % conveyor membership rows ensured, % skipped', linked, skipped;
END $$;

-- 052 rule 1: fr ensures conveyor only if it issues every app in it.
DO $$
DECLARE
    v_bundle    UUID;
    unissued    TEXT;
BEGIN
    SELECT id INTO v_bundle FROM bundle WHERE key = 'conveyor';
    SELECT string_agg(a.key, ', ' ORDER BY a.key) INTO unissued
      FROM bundle_app ba JOIN app a ON a.id = ba.app_id
     WHERE ba.bundle_id = v_bundle
       AND NOT EXISTS (
            SELECT 1 FROM app_issuer ai WHERE ai.app_id = ba.app_id AND ai.owner_key = 'fr'
       );
    IF unissued IS NOT NULL THEN
        RAISE NOTICE '059: fr does not issue % — conveyor left WITHOUT fr as ensurer', unissued;
        RETURN;
    END IF;
    INSERT INTO bundle_ensurer (bundle_id, owner_key)
    VALUES (v_bundle, 'fr')
    ON CONFLICT (bundle_id, owner_key) DO NOTHING;
END $$;

INSERT INTO bundle_caller (bundle_id, owner_key, app_id)
SELECT b.id, 'fr', a.id FROM bundle b, app a WHERE b.key = 'conveyor' AND a.key = 'workers'
ON CONFLICT (bundle_id, owner_key, app_id) DO NOTHING;

DO $$
DECLARE
    r         RECORD;
    v_app     UUID;
    v_req     UUID;
    linked    INT := 0;
    skipped   INT := 0;
BEGIN
    FOR r IN
        SELECT * FROM (VALUES
            ('w2v', 'workers', 'w2v'),
            ('w2v', 'kag', 'w2v'),
            ('marketing', 'workers', 'merqi'),
            ('sales', 'workers', 'merqi'),
            ('kag', 'workers', 'kag'),
            ('kag', 'mlx-backend', 'kag'),
            ('kag', 'mlx-ingestion-backend', 'kag'),
            ('workers', 'inference-router', 'platform')
        ) AS t(app_key, required_key, caller)
    LOOP
        SELECT id INTO v_app FROM app WHERE key = r.app_key;
        SELECT id INTO v_req FROM app WHERE key = r.required_key;
        IF v_app IS NULL OR v_req IS NULL
           OR NOT EXISTS (SELECT 1 FROM tenant_owner WHERE key = r.caller) THEN
            RAISE NOTICE '059: skipping app_requires % -> % (caller %) — app or owner missing',
                r.app_key, r.required_key, r.caller;
            skipped := skipped + 1;
            CONTINUE;
        END IF;
        INSERT INTO app_requires (app_id, required_app_id, caller_owner_key)
        VALUES (v_app, v_req, r.caller)
        ON CONFLICT (app_id, required_app_id) DO NOTHING;
        linked := linked + 1;
    END LOOP;
    RAISE NOTICE '059: % app_requires edges ensured, % skipped', linked, skipped;
END $$;

-- No cycle may exist (an operator row included): refuse the whole migration if one does.
DO $$
DECLARE
    v_cycle TEXT;
BEGIN
    WITH RECURSIVE walk(start_id, cur_id, path) AS (
        SELECT app_id, required_app_id, ARRAY[app_id, required_app_id] FROM app_requires
        UNION ALL
        SELECT w.start_id, ar.required_app_id, w.path || ar.required_app_id
          FROM walk w JOIN app_requires ar ON ar.app_id = w.cur_id
         WHERE NOT ar.required_app_id = ANY (w.path[2:])
           AND w.cur_id <> w.start_id
    )
    SELECT string_agg(a.key, ' -> ' ORDER BY ord) INTO v_cycle
      FROM (SELECT path FROM walk WHERE cur_id = start_id LIMIT 1) c,
           unnest(c.path) WITH ORDINALITY AS u(app_id, ord)
      JOIN app a ON a.id = u.app_id;
    IF v_cycle IS NOT NULL THEN
        RAISE EXCEPTION '059: app_requires holds a cycle (%) — nothing was changed', v_cycle;
    END IF;
END $$;

COMMIT;
