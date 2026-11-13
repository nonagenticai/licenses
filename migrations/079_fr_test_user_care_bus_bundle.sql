-- 079: the tenant's OWNER (`platform`) ensures the `care-bus` bundle on fr-test-user.
--
-- WHY (diagnosed live 2026-10-23). care-bus's tenant licence-set reconcile calls
-- POST /internal/tenants/{id}/bundles/care-bus/ensure as owner care-bus. For the W3
-- bootstrap tenant fr-test-user 0195f48b-d000-7000-8000-000000000099 (owner
-- `platform`, kind `test`; care-bus holds only 069's tenant_app_grant) that is 403
-- `tenant_owner_only` (main._resolve_owned_tenant). That rule is CORRECT and stays:
-- ensure is how an OWNER sanctions a bundle's apps on its tenant, and a grantee must
-- never be able to. So the owner sanctions it here instead — exactly the act the
-- owner would perform through the route / scripts/provision_tenant_bundle.py.
--
-- WHAT THIS DOES (data only, no schema) — services/bundle_ensure.plan + apply,
-- transcribed rule for rule for ONE (tenant, bundle), plans=['standard']:
--   lock          pg_advisory_xact_lock(hashtextextended('bundle_ensure:<tenant>', 0))
--                 — the SAME key as _TenantEnsureLock, so a live ensure of this tenant
--                 and this file serialise.
--   hold          tenant_bundle (fr-test-user, care-bus).
--   issuer grants models.bundle_grantees per bundled app: care-bus = {care-bus}, the
--                 owner platform does not issue it -> (fr-test-user, care-bus), already
--                 069's row (ON CONFLICT on the active-grant index: left alone).
--   dependency    tenant_dependency_grant (care-bus, mcp|strata-rag|strata-ingest) — the
--   grants        app_requires edges walked from the bundle (078) plus any
--                 bundle_caller rows; never for the tenant's own owner.
--   licences      per bundled app (plan_app): exists | adopt (the ONE unbundled
--                 tenant-level licence; is_dependency cleared) | insert (subject
--                 svc:<tenant>:<app>, realm `service`, bundle_id = care-bus) | REFUSE.
--                 Per dependency app (plan_dependency, the closure minus the members):
--                 exists (any ONE tenant-level licence, shared) | insert (is_dependency,
--                 bundle_id NULL) | REFUSE. A REFUSED app is announced and skipped; the
--                 others are still ensured (the route's 409-with-the-rest-applied).
--                 A new subject's parent org is stated (attribute_subject_to_tenant).
--   audit         license.metadata {"provisioned_by": "migration-079", "bundle" |
--                 "dependency_of": "care-bus"}; grants granted_by
--                 'bundle:care-bus by migration-079' (the route's
--                 'bundle:<b> by <realm>/<azp>' shape).
-- Measured live 2026-10-23 (read-only): the tenant holds NO licence for care-bus, mcp,
-- strata-rag or strata-ingest, so all four are INSERTED (care-bus bundled, the three
-- tools as dependency licences).
--
-- METERING. An inserted licence is NOT pushed to billing here (SQL cannot call it):
-- metering_provisioned_at / metering_verified_at stay NULL — exactly an ensure whose
-- billing push did not complete ('pending'), which the daily
-- licenses-metering-reconcile CronJob repairs (selection: metering_verified_at NULL).
--
-- AFTER THIS, care-bus's own ensure on this tenant is STILL 403 `tenant_owner_only`
-- (unchanged, by design); care-bus reads the bundle instead (GET
-- /internal/tenants/{id}/bundles, licence-reader/admin as a grantee).
--
-- GUARDED + IDEMPOTENT. A no-op (NOTICE) when the tenant is absent (CI / test /
-- rebuilt databases), archived, no longer owned by `platform` (then it is that
-- owner's call), or the bundle is missing / revoked / empty, or app_requires holds a
-- cycle reachable from it (ensure: 409 dependency_cycle, nothing written). A re-run
-- answers `exists` everywhere and writes nothing.
--
-- FRESH DATABASE: stamped without running (no fr-test-user licences anywhere).
--
-- ONE TRANSACTION (BEGIN/COMMIT as statements).
--
-- DOWNGRADE: migrations/downgrades/079_fr_test_user_care_bus_bundle.sql.

BEGIN;

DO $$
DECLARE
    c_tenant      CONSTANT UUID := '0195f48b-d000-7000-8000-000000000099';  -- fr-test-user
    c_owner       CONSTANT TEXT := 'platform';
    c_bundle_key  CONSTANT TEXT := 'care-bus';
    c_plan        CONSTANT TEXT := 'standard';
    c_by          CONSTANT TEXT := 'migration-079';
    c_granted_by  CONSTANT TEXT := 'bundle:care-bus by migration-079';
    c_key_max     CONSTANT INT  := 214;  -- models.SERVICE_SUBJECT_APP_KEY_MAX_LEN
    v_tenant      RECORD;
    v_bundle      UUID;
    v_cycle       BOOLEAN;
    v_row         RECORD;
    v_n_mine      INT;
    v_n_rows      INT;
    v_n_other     INT;
    v_n_unbundled INT;
    v_lic         UUID;
    v_plan        UUID;
    v_subject     UUID;
    v_dep         BOOLEAN;
BEGIN
    -- The tenant lock first, as plan_and_apply takes it before planning.
    PERFORM pg_advisory_xact_lock(hashtextextended('bundle_ensure:' || c_tenant::text, 0));

    SELECT id, owner_key, kind, archived_at INTO v_tenant FROM tenants WHERE id = c_tenant;
    IF NOT FOUND THEN
        RAISE NOTICE '079: fr-test-user absent — nothing to do';
        RETURN;
    END IF;
    IF v_tenant.archived_at IS NOT NULL THEN
        RAISE NOTICE '079: fr-test-user is ARCHIVED — nothing written';
        RETURN;
    END IF;
    IF v_tenant.owner_key IS DISTINCT FROM c_owner THEN
        RAISE NOTICE '079: fr-test-user is owned by % (not %) — its owner decides; nothing written',
            v_tenant.owner_key, c_owner;
        RETURN;
    END IF;

    SELECT id INTO v_bundle FROM bundle WHERE key = c_bundle_key AND NOT revoked;
    IF v_bundle IS NULL THEN
        RAISE NOTICE '079: bundle care-bus missing or revoked — nothing written';
        RETURN;
    END IF;

    CREATE TEMP TABLE m079_member ON COMMIT DROP AS
        SELECT a.id, a.key FROM bundle_app ba JOIN app a ON a.id = ba.app_id
         WHERE ba.bundle_id = v_bundle AND NOT a.revoked;
    IF NOT EXISTS (SELECT 1 FROM m079_member) THEN
        RAISE NOTICE '079: bundle care-bus is empty — nothing written';
        RETURN;
    END IF;

    -- app_requires between non-revoked apps (DatabaseManager.list_app_requirements).
    CREATE TEMP TABLE m079_edge ON COMMIT DROP AS
        SELECT a.key AS app, q.key AS req, q.id AS req_id, ar.caller_owner_key AS caller
          FROM app_requires ar
          JOIN app a ON a.id = ar.app_id
          JOIN app q ON q.id = ar.required_app_id
         WHERE NOT a.revoked AND NOT q.revoked;

    -- services.app_requires.closure: refuse a cycle reachable from the members.
    WITH RECURSIVE walk(node, path, cyc) AS (
        SELECT key, ARRAY[key::TEXT], false FROM m079_member
        UNION ALL
        SELECT e.req, w.path || e.req::TEXT, e.req = ANY (w.path)
          FROM walk w JOIN m079_edge e ON e.app = w.node
         WHERE NOT w.cyc
    )
    SELECT coalesce(bool_or(cyc), false) INTO v_cycle FROM walk;
    IF v_cycle THEN
        RAISE NOTICE '079: app_requires holds a cycle reachable from care-bus — nothing written';
        RETURN;
    END IF;

    -- Every app reachable from the members (roots included) and the edges out of them.
    CREATE TEMP TABLE m079_reach ON COMMIT DROP AS
        WITH RECURSIVE reach(node) AS (
            SELECT key::TEXT FROM m079_member
            UNION
            SELECT e.req::TEXT FROM reach rc JOIN m079_edge e ON e.app = rc.node
        )
        SELECT node FROM reach;

    -- ── ensure_bundle_grants: hold + issuer grants + dependency grants, before licences.
    INSERT INTO tenant_bundle (tenant_id, bundle_id) VALUES (c_tenant, v_bundle)
    ON CONFLICT DO NOTHING;

    -- models.bundle_grantees per bundled app.
    FOR v_row IN
        SELECT DISTINCT g.grantee FROM (
            SELECT m.id,
                   CASE
                       WHEN bool_or(ai.owner_key = c_owner) THEN NULL
                       WHEN count(*) = 1 THEN min(ai.owner_key)
                       WHEN bool_or(ai.owner_key = 'platform') THEN 'platform'
                   END AS grantee
              FROM m079_member m JOIN app_issuer ai ON ai.app_id = m.id
             GROUP BY m.id
        ) g
         WHERE g.grantee IS NOT NULL
         ORDER BY 1
    LOOP
        INSERT INTO tenant_app_grant (tenant_id, grantee_owner_key, granted_by)
        VALUES (c_tenant, v_row.grantee, c_granted_by)
        ON CONFLICT (tenant_id, grantee_owner_key) WHERE revoked_at IS NULL DO NOTHING;
    END LOOP;

    INSERT INTO tenant_dependency_grant (tenant_id, owner_key, app_id, granted_by)
    SELECT DISTINCT c_tenant, d.caller, d.app_id, c_granted_by FROM (
        SELECT e.caller, e.req_id AS app_id
          FROM m079_edge e JOIN m079_reach rr ON rr.node = e.app
        UNION
        SELECT c.owner_key, c.app_id
          FROM bundle_caller c JOIN app a ON a.id = c.app_id
         WHERE c.bundle_id = v_bundle AND NOT a.revoked
    ) d
     WHERE d.caller <> c_owner
    ON CONFLICT DO NOTHING;

    -- ── Licences: the bundled apps (plan_app), then the dependencies (plan_dependency).
    FOR v_row IN
        SELECT id, key, false AS dep FROM m079_member
        UNION ALL
        SELECT a.id, a.key, true FROM m079_reach x JOIN app a ON a.key = x.node
         WHERE x.node NOT IN (SELECT key FROM m079_member)
        ORDER BY 3, 2
    LOOP
        v_dep := v_row.dep;
        IF length(v_row.key) > c_key_max THEN
            RAISE NOTICE '079: REFUSE % app_key_too_long', v_row.key;
            CONTINUE;
        END IF;

        -- The tenant's effectively-active licences for the app, externally held
        -- (cp-tenant) ones excluded (list_tenant_app_licences + is_externally_held_subject).
        DROP TABLE IF EXISTS m079_lic;
        CREATE TEMP TABLE m079_lic ON COMMIT DROP AS
            SELECT l.id, l.bundle_id,
                   (s.keycloak_sub LIKE 'svc:%' OR s.keycloak_realm = 'service') AS tenant_level
              FROM license l JOIN subject s ON s.id = l.subject_id
             WHERE l.tenant_id = c_tenant AND l.app_id = v_row.id
               AND l.status = 'active' AND l.deleted_at IS NULL
               AND l.starts_at <= now()
               AND (l.expires_at IS NULL OR l.expires_at >= now())
               AND s.keycloak_realm <> 'cp-tenant';

        SELECT count(*) FILTER (WHERE tenant_level),
               count(*) FILTER (WHERE tenant_level AND bundle_id = v_bundle),
               count(*) FILTER (WHERE tenant_level AND bundle_id IS NOT NULL
                                  AND bundle_id <> v_bundle),
               count(*) FILTER (WHERE tenant_level AND bundle_id IS NULL)
          INTO v_n_rows, v_n_mine, v_n_other, v_n_unbundled
          FROM m079_lic;

        IF v_dep THEN
            IF v_n_rows > 1 THEN
                RAISE NOTICE '079: REFUSE % several_candidates (dependency)', v_row.key;
                CONTINUE;
            ELSIF v_n_rows = 1 THEN
                RAISE NOTICE '079: % exists (dependency, shared)', v_row.key;
                CONTINUE;
            END IF;
        ELSE
            IF EXISTS (SELECT 1 FROM m079_lic WHERE NOT tenant_level AND bundle_id = v_bundle) THEN
                RAISE NOTICE '079: REFUSE % member_licence', v_row.key;
                CONTINUE;
            END IF;
            IF v_n_mine = 1 AND v_n_other = 0 AND v_n_unbundled = 0 THEN
                RAISE NOTICE '079: % exists', v_row.key;
                CONTINUE;
            END IF;
            IF v_n_rows > 1 THEN
                RAISE NOTICE '079: REFUSE % several_candidates', v_row.key;
                CONTINUE;
            END IF;
            IF v_n_other > 0 THEN
                RAISE NOTICE '079: REFUSE % other_bundle', v_row.key;
                CONTINUE;
            END IF;
            IF v_n_unbundled = 1 THEN
                -- adopt_license_into_bundle: compare-and-set, promotes a dependency.
                UPDATE license SET bundle_id = v_bundle, is_dependency = false, updated_at = now()
                 WHERE id = (SELECT id FROM m079_lic WHERE tenant_level AND bundle_id IS NULL)
                   AND bundle_id IS NULL;
                RAISE NOTICE '079: % adopt', v_row.key;
                CONTINUE;
            END IF;
        END IF;

        -- insert: choose_plan(['standard']) over the app's non-revoked plans.
        SELECT p.id INTO v_plan FROM plan p
         WHERE p.app_id = v_row.id AND p.code = c_plan AND NOT p.revoked
         ORDER BY p.region_id IS NOT NULL, p.id
         LIMIT 1;
        IF v_plan IS NULL THEN
            RAISE NOTICE '079: REFUSE % no_plan (no non-revoked %)', v_row.key, c_plan;
            CONTINUE;
        END IF;

        -- create_or_get_subject(svc:<tenant>:<app>, realm 'service').
        INSERT INTO subject (id, keycloak_sub, keycloak_realm)
        VALUES (uuidv7(), 'svc:' || c_tenant::text || ':' || v_row.key, 'service')
        ON CONFLICT (keycloak_sub) DO NOTHING;
        SELECT id INTO v_subject FROM subject
         WHERE keycloak_sub = 'svc:' || c_tenant::text || ':' || v_row.key;

        INSERT INTO license (
            id, subject_id, app_id, plan_id, tenant_id, bundle_id, is_dependency,
            status, starts_at, metadata
        ) VALUES (
            uuidv7(), v_subject, v_row.id, v_plan, c_tenant,
            CASE WHEN v_dep THEN NULL ELSE v_bundle END, v_dep,
            'active', now(),
            CASE WHEN v_dep
                 THEN jsonb_build_object('provisioned_by', c_by, 'dependency_of', c_bundle_key)
                 ELSE jsonb_build_object('provisioned_by', c_by, 'bundle', c_bundle_key)
            END
        )
        RETURNING id INTO v_lic;

        -- attribute_subject_to_tenant: only when unset, the tenant is not a machine and
        -- every licence of the subject names this one tenant.
        UPDATE subject s SET tenant_id = c_tenant
         WHERE s.id = v_subject AND s.tenant_id IS NULL
           AND v_tenant.kind IS DISTINCT FROM 'service'
           AND NOT EXISTS (SELECT 1 FROM license l
                            WHERE l.subject_id = s.id AND l.tenant_id <> c_tenant);

        RAISE NOTICE '079: % insert (licence %, %)', v_row.key, v_lic,
            CASE WHEN v_dep THEN 'dependency' ELSE 'bundled' END;
    END LOOP;
END $$;

COMMIT;
