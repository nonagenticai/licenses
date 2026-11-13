-- Downgrade 082: the mcp owner, both minter clients, the mcp + agent edges and the
-- `agent` bundle go.
--
-- ⚠️ Roll back models.py (TENANT_OWNERS, API_CLIENTS, BUNDLE_ENSURER_SEED,
-- APP_REQUIRES_SEED) too, or the boot seed re-creates the owner + client rows.
--
-- ⛔ REFUSES (raises, changes nothing) while 082 is in use or an operator built on it:
--   * any tenant holds the `agent` bundle or any licence names it (ON DELETE RESTRICT);
--   * the mcp owner holds anything 082 did not write — a tenant, a tenant_app_grant or
--     tenant_dependency_grant (ensure writes those once an mcp edge is walked), another
--     api_client, an issuer / ensurer / bundle_caller / realm_owner row, or an
--     app_requires edge other than 082's four.
-- Only what 082 wrote is removed; an operator-changed client owner is kept.

BEGIN;

DO $$
DECLARE
    v_bundle   UUID;
    n_licence  INT := 0;
    n_holder   INT := 0;
    n_mcp      INT := 0;
BEGIN
    SELECT id INTO v_bundle FROM bundle WHERE key = 'agent';
    IF v_bundle IS NOT NULL THEN
        SELECT count(*) INTO n_licence FROM license WHERE bundle_id = v_bundle;
        SELECT count(*) INTO n_holder FROM tenant_bundle WHERE bundle_id = v_bundle;
    END IF;
    SELECT (SELECT count(*) FROM tenants WHERE owner_key = 'mcp')
         + (SELECT count(*) FROM tenant_app_grant WHERE grantee_owner_key = 'mcp')
         + (SELECT count(*) FROM tenant_dependency_grant WHERE owner_key = 'mcp')
         + (SELECT count(*) FROM api_client
             WHERE owner_key = 'mcp'
               AND NOT (realm = 'secure-apps' AND client_id = 'mcp-bundle-minter'))
         + (SELECT count(*) FROM app_issuer WHERE owner_key = 'mcp')
         + (SELECT count(*) FROM bundle_ensurer WHERE owner_key = 'mcp')
         + (SELECT count(*) FROM bundle_caller WHERE owner_key = 'mcp')
         + (SELECT count(*) FROM realm_owner WHERE owner_key = 'mcp')
         + (SELECT count(*) FROM app_requires r
              JOIN app a ON a.id = r.app_id
              JOIN app q ON q.id = r.required_app_id
             WHERE r.caller_owner_key = 'mcp'
               AND NOT (a.key = 'mcp'
                        AND q.key IN ('coding', 'strata-rag', 'strata-ingest', 'laya')))
      INTO n_mcp;
    IF n_licence > 0 OR n_holder > 0 OR n_mcp > 0 THEN
        RAISE EXCEPTION '082 downgrade REFUSED: % licence(s) name the agent bundle, % tenant_bundle row(s) hold it, % row(s) are keyed on owner mcp beyond what 082 wrote (tenants, grants, dependency grants, clients, issuers, ensurers, callers, realms, edges). Remove them first; nothing was changed.',
            n_licence, n_holder, n_mcp;
    END IF;

    DELETE FROM app_requires r
     USING app a, app q
     WHERE a.id = r.app_id AND q.id = r.required_app_id
       AND ((a.key = 'mcp' AND r.caller_owner_key = 'mcp'
             AND q.key IN ('coding', 'strata-rag', 'strata-ingest', 'laya'))
         OR (a.key = 'agent' AND r.caller_owner_key = 'agent' AND q.key = 'mcp'));

    IF v_bundle IS NOT NULL THEN
        DELETE FROM bundle WHERE id = v_bundle;  -- bundle_app / bundle_ensurer / bundle_caller cascade
    END IF;

    DELETE FROM api_client
     WHERE realm = 'secure-apps' AND cross_owner = false
       AND ((client_id = 'mcp-bundle-minter' AND owner_key = 'mcp')
         OR (client_id = 'agent-bundle-minter' AND owner_key = 'agent'));

    DELETE FROM tenant_owner WHERE key = 'mcp';  -- nothing references it (checked above)
END $$;

-- Un-stamp 082 so scripts/apply_migrations.py re-applies it (up -> down -> up).
DO $$
BEGIN
    IF to_regclass('public.schema_migrations') IS NOT NULL THEN
        DELETE FROM schema_migrations WHERE filename = '082_mcp_toolkit_entitlements.sql';
    END IF;
END $$;

COMMIT;
