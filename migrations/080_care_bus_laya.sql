-- 080: care-bus requires `laya` — every care-bus tenant gets its own metered laya licence.
--
-- WHY (owner ruling 2026-10-23). care-bus calls GPU services with its OWN application
-- licences, per tenant: the laya System-1 classifier (pgx, behind its Jev bridge) is
-- reached at api.nonagentic.ai `/laya/`, where license-authz meters it per licence and
-- `_check_app_route` admits app_key `laya` exactly. So each care-bus tenant needs a
-- tenant-level `laya` licence, minted through the existing care-bus bundle
-- (POST /internal/tenants/{id}/bundle-token?app=laya as care-bus-bundle-minter).
-- `vllm` is deliberately NOT added: it uses a per-service token (owner decision).
--
-- WHAT THIS DOES (data only, no schema):
--   app_requires  care-bus -> laya, caller care-bus (078's shape, mirrored in
--                 models.APP_REQUIRES_SEED). laya requires nothing, so the care-bus
--                 closure becomes {mcp, strata-rag, strata-ingest, laya}. A missing app
--                 or owner is ANNOUNCED AND SKIPPED (051's rule; a database seed_apps.py
--                 never ran on has no laya) — and then nothing below is written.
--   cycle         the whole app_requires graph is refused if it holds a cycle (078's
--                 check), the migration changing nothing.
--   re-ensure     for EVERY non-archived tenant that already HOLDS the `care-bus` bundle
--                 (tenant_bundle), the part of services/bundle_ensure.plan + apply this
--                 edge changes — transcribed rule for rule from 079, plans=['standard']:
--                 lock          pg_advisory_xact_lock(hashtextextended('bundle_ensure:<t>', 0))
--                               (the _TenantEnsureLock key: a live ensure serialises).
--                 dependency    tenant_dependency_grant (caller, app) for every edge out
--                 grants        of care-bus reaching laya and out of laya's own closure,
--                               never for the tenant's own owner (a care-bus-owned
--                               tenant gets none: the minter is its owner).
--                 licences      plan_dependency per app in laya's closure that is not a
--                               bundle member: exists (any ONE tenant-level licence,
--                               shared — a conveyor-held laya satisfies it) | insert
--                               (subject svc:<tenant>:laya, realm `service`,
--                               is_dependency, bundle_id NULL) | REFUSE (several
--                               candidates / no non-revoked `standard` plan / key too
--                               long; announced, the tenant's other rows untouched).
--   audit         license.metadata {"provisioned_by": "migration-080", "dependency_of":
--                 "care-bus"}; grants granted_by 'bundle:care-bus by migration-080'.
--
-- WHY A DATA STEP AND NOT "the next ensure". Ensure walks app_requires at run time, so
-- a care-bus-OWNED tenant's next ensure would add laya by itself. But care-bus ensures
-- a tenant ONCE (care-bus-agents reconcile_bundles: `ok` is final) and on approve /
-- licence-on-login only, and a tenant another owner holds (fr-test-user, platform —
-- 079) is never ensured by care-bus at all (403 tenant_owner_only, by rule). Without
-- this step those tenants would answer bundle-token?app=laya 404 `no_app_licence`.
-- Everything else of the bundle is unchanged, so the result is exactly what a fresh
-- ensure of the bundle would write for each holder (pinned by a differential test).
--
-- ISSUERS. laya's issuers stay ("platform", "fr") (models.DEFAULT_APP_ISSUERS). A
-- dependency licence is minted by licences itself on ensure and is bundle-token only:
-- no issuer rule applies to it, and bundle-token checks owner/grantee scope, not
-- issuers (main.internal_mint_tenant_bundle_token).
--
-- METERING. An inserted licence is NOT pushed to billing here (SQL cannot call it):
-- metering_provisioned_at / metering_verified_at stay NULL — an ensure whose billing
-- push did not complete; the daily licenses-metering-reconcile CronJob repairs it.
--
-- GUARDED + IDEMPOTENT. A re-run answers `exists` everywhere and writes nothing. No
-- holder, a missing / revoked / empty bundle, or a missing edge: a NOTICE, nothing
-- written.
--
-- ONE TRANSACTION (BEGIN/COMMIT as statements).
--
-- DOWNGRADE: migrations/downgrades/080_care_bus_laya.sql.

BEGIN;

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
            ('care-bus', 'laya', 'care-bus')
        ) AS t(app_key, required_key, caller)
    LOOP
        SELECT id INTO v_app FROM app WHERE key = r.app_key;
        SELECT id INTO v_req FROM app WHERE key = r.required_key;
        IF v_app IS NULL OR v_req IS NULL
           OR NOT EXISTS (SELECT 1 FROM tenant_owner WHERE key = r.caller) THEN
            RAISE NOTICE '080: skipping app_requires % -> % (caller %) — app or owner missing',
                r.app_key, r.required_key, r.caller;
            skipped := skipped + 1;
            CONTINUE;
        END IF;
        INSERT INTO app_requires (app_id, required_app_id, caller_owner_key)
        VALUES (v_app, v_req, r.caller)
        ON CONFLICT (app_id, required_app_id) DO NOTHING;
        linked := linked + 1;
    END LOOP;
    RAISE NOTICE '080: % app_requires edges ensured, % skipped', linked, skipped;
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
        RAISE EXCEPTION '080: app_requires holds a cycle (%) — nothing was changed', v_cycle;
    END IF;
END $$;

-- The re-ensure of laya's closure on every tenant holding the care-bus bundle.
DO $$
DECLARE
    c_bundle_key  CONSTANT TEXT := 'care-bus';
    c_new_app     CONSTANT TEXT := 'laya';
    c_plan        CONSTANT TEXT := 'standard';
    c_by          CONSTANT TEXT := 'migration-080';
    c_granted_by  CONSTANT TEXT := 'bundle:care-bus by migration-080';
    c_key_max     CONSTANT INT  := 214;  -- models.SERVICE_SUBJECT_APP_KEY_MAX_LEN
    v_bundle      UUID;
    v_tenant      RECORD;
    v_row         RECORD;
    v_n_rows      INT;
    v_lic         UUID;
    v_plan        UUID;
    v_subject     UUID;
BEGIN
    SELECT id INTO v_bundle FROM bundle WHERE key = c_bundle_key AND NOT revoked;
    IF v_bundle IS NULL THEN
        RAISE NOTICE '080: bundle care-bus missing or revoked — nothing written';
        RETURN;
    END IF;

    CREATE TEMP TABLE m080_member ON COMMIT DROP AS
        SELECT a.id, a.key FROM bundle_app ba JOIN app a ON a.id = ba.app_id
         WHERE ba.bundle_id = v_bundle AND NOT a.revoked;
    IF NOT EXISTS (SELECT 1 FROM m080_member) THEN
        RAISE NOTICE '080: bundle care-bus is empty — nothing written';
        RETURN;
    END IF;

    -- app_requires between non-revoked apps (DatabaseManager.list_app_requirements).
    CREATE TEMP TABLE m080_edge ON COMMIT DROP AS
        SELECT a.key::TEXT AS app, q.key::TEXT AS req, q.id AS req_id, ar.caller_owner_key AS caller
          FROM app_requires ar
          JOIN app a ON a.id = ar.app_id
          JOIN app q ON q.id = ar.required_app_id
         WHERE NOT a.revoked AND NOT q.revoked;

    -- Every app reachable from the members (roots included); the cycle check above
    -- already refused any cycle, so the walk terminates.
    CREATE TEMP TABLE m080_reach ON COMMIT DROP AS
        WITH RECURSIVE reach(node) AS (
            SELECT key::TEXT FROM m080_member
            UNION
            SELECT e.req FROM reach rc JOIN m080_edge e ON e.app = rc.node
        )
        SELECT node FROM reach;
    IF NOT EXISTS (SELECT 1 FROM m080_reach WHERE node = c_new_app) THEN
        RAISE NOTICE '080: laya is not in the care-bus closure (edge skipped) — nothing written';
        RETURN;
    END IF;

    -- What this edge adds: laya and whatever laya itself requires (the sub-closure).
    CREATE TEMP TABLE m080_new ON COMMIT DROP AS
        WITH RECURSIVE sub(node) AS (
            SELECT c_new_app
            UNION
            SELECT e.req FROM sub s JOIN m080_edge e ON e.app = s.node
        )
        SELECT node FROM sub;

    -- The edges whose dependency grants this adds: into laya from the closure, and out
    -- of laya's sub-closure.
    CREATE TEMP TABLE m080_new_edge ON COMMIT DROP AS
        SELECT e.caller, e.req_id
          FROM m080_edge e
         WHERE (e.req = c_new_app AND e.app IN (SELECT node FROM m080_reach))
            OR e.app IN (SELECT node FROM m080_new);

    CREATE TEMP TABLE m080_lic (id UUID, bundle_id UUID, tenant_level BOOLEAN) ON COMMIT DROP;

    FOR v_tenant IN
        SELECT t.id, t.owner_key, t.kind
          FROM tenant_bundle tb JOIN tenants t ON t.id = tb.tenant_id
         WHERE tb.bundle_id = v_bundle AND t.archived_at IS NULL
         ORDER BY t.id
    LOOP
        PERFORM pg_advisory_xact_lock(hashtextextended('bundle_ensure:' || v_tenant.id::text, 0));

        INSERT INTO tenant_dependency_grant (tenant_id, owner_key, app_id, granted_by)
        SELECT DISTINCT v_tenant.id, ne.caller, ne.req_id, c_granted_by
          FROM m080_new_edge ne
         WHERE ne.caller <> v_tenant.owner_key
        ON CONFLICT DO NOTHING;

        FOR v_row IN
            SELECT a.id, a.key FROM m080_new n JOIN app a ON a.key = n.node
             WHERE n.node NOT IN (SELECT key FROM m080_member) AND NOT a.revoked
             ORDER BY a.key
        LOOP
            IF length(v_row.key) > c_key_max THEN
                RAISE NOTICE '080: tenant % REFUSE % app_key_too_long', v_tenant.id, v_row.key;
                CONTINUE;
            END IF;

            -- shared_tenant_licences: effectively-active tenant-level licences, cp-tenant
            -- (externally held) excluded.
            TRUNCATE m080_lic;
            INSERT INTO m080_lic
            SELECT l.id, l.bundle_id, true
              FROM license l JOIN subject s ON s.id = l.subject_id
             WHERE l.tenant_id = v_tenant.id AND l.app_id = v_row.id
               AND l.status = 'active' AND l.deleted_at IS NULL
               AND l.starts_at <= now()
               AND (l.expires_at IS NULL OR l.expires_at >= now())
               AND s.keycloak_realm <> 'cp-tenant'
               AND (s.keycloak_sub LIKE 'svc:%' OR s.keycloak_realm = 'service');
            SELECT count(*) INTO v_n_rows FROM m080_lic;

            IF v_n_rows > 1 THEN
                RAISE NOTICE '080: tenant % REFUSE % several_candidates (dependency)',
                    v_tenant.id, v_row.key;
                CONTINUE;
            ELSIF v_n_rows = 1 THEN
                RAISE NOTICE '080: tenant % % exists (dependency, shared)', v_tenant.id, v_row.key;
                CONTINUE;
            END IF;

            -- insert: choose_plan(['standard']) over the app's non-revoked plans.
            SELECT p.id INTO v_plan FROM plan p
             WHERE p.app_id = v_row.id AND p.code = c_plan AND NOT p.revoked
             ORDER BY p.region_id IS NOT NULL, p.id
             LIMIT 1;
            IF v_plan IS NULL THEN
                RAISE NOTICE '080: tenant % REFUSE % no_plan (no non-revoked %)',
                    v_tenant.id, v_row.key, c_plan;
                CONTINUE;
            END IF;

            -- create_or_get_subject(svc:<tenant>:<app>, realm 'service').
            INSERT INTO subject (id, keycloak_sub, keycloak_realm)
            VALUES (uuidv7(), 'svc:' || v_tenant.id::text || ':' || v_row.key, 'service')
            ON CONFLICT (keycloak_sub) DO NOTHING;
            SELECT id INTO v_subject FROM subject
             WHERE keycloak_sub = 'svc:' || v_tenant.id::text || ':' || v_row.key;

            INSERT INTO license (
                id, subject_id, app_id, plan_id, tenant_id, bundle_id, is_dependency,
                status, starts_at, metadata
            ) VALUES (
                uuidv7(), v_subject, v_row.id, v_plan, v_tenant.id, NULL, true,
                'active', now(),
                jsonb_build_object('provisioned_by', c_by, 'dependency_of', c_bundle_key)
            )
            RETURNING id INTO v_lic;

            -- attribute_subject_to_tenant: only when unset, the tenant is not a machine
            -- and every licence of the subject names this one tenant.
            UPDATE subject s SET tenant_id = v_tenant.id
             WHERE s.id = v_subject AND s.tenant_id IS NULL
               AND v_tenant.kind IS DISTINCT FROM 'service'
               AND NOT EXISTS (SELECT 1 FROM license l
                                WHERE l.subject_id = s.id AND l.tenant_id <> v_tenant.id);

            RAISE NOTICE '080: tenant % % insert (licence %, dependency)',
                v_tenant.id, v_row.key, v_lic;
        END LOOP;
    END LOOP;
END $$;

COMMIT;
