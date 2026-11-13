-- Downgrade 078: care-bus's realm_owner row, minter client, bundle, tool edges and the
-- strata apps go.
--
-- ⚠️ Roll back models.py (REALM_OWNERS, API_CLIENTS, APP_ISSUER_OVERRIDES,
-- BUNDLE_ENSURER_SEED, APP_REQUIRES_SEED) and scripts/seed_apps.py too, or the boot /
-- DR seed re-creates the rows.
--
-- ⛔ REFUSES (raises, changes nothing) while 078 is in use: any tenant holds the
-- `care-bus` bundle or any licence names it (ON DELETE RESTRICT), or anything references
-- a strata app — deleting an app CASCADE-deletes its licences silently: any licence,
-- dependency grant, offering, tenant_app_byo_mode, bundle_app, bundle_caller or
-- app_requires row other than 078's own edges, or an issuer row 078 did not write.
-- Only what 078 wrote is removed; an operator-changed client owner is kept.

BEGIN;

DO $$
DECLARE
    v_bundle   UUID;
    n_licence  INT := 0;
    n_holder   INT := 0;
    n_used     INT := 0;
BEGIN
    SELECT id INTO v_bundle FROM bundle WHERE key = 'care-bus';
    IF v_bundle IS NOT NULL THEN
        SELECT count(*) INTO n_licence FROM license WHERE bundle_id = v_bundle;
        SELECT count(*) INTO n_holder FROM tenant_bundle WHERE bundle_id = v_bundle;
    END IF;
    SELECT (SELECT count(*) FROM license l JOIN app a ON a.id = l.app_id
             WHERE a.key IN ('strata-rag', 'strata-ingest'))
         + (SELECT count(*) FROM tenant_dependency_grant g JOIN app a ON a.id = g.app_id
             WHERE a.key IN ('strata-rag', 'strata-ingest'))
         + (SELECT count(*) FROM offering o JOIN app a ON a.id = o.app_id
             WHERE a.key IN ('strata-rag', 'strata-ingest'))
         + (SELECT count(*) FROM tenant_app_byo_mode m JOIN app a ON a.id = m.app_id
             WHERE a.key IN ('strata-rag', 'strata-ingest'))
         + (SELECT count(*) FROM bundle_app b JOIN app a ON a.id = b.app_id
             WHERE a.key IN ('strata-rag', 'strata-ingest'))
         + (SELECT count(*) FROM bundle_caller c JOIN app a ON a.id = c.app_id
             WHERE a.key IN ('strata-rag', 'strata-ingest'))
         + (SELECT count(*) FROM app_requires r
              JOIN app a ON a.id = r.app_id
              JOIN app q ON q.id = r.required_app_id
             WHERE (a.key IN ('strata-rag', 'strata-ingest')
                    OR q.key IN ('strata-rag', 'strata-ingest'))
               AND NOT (a.key = 'care-bus' AND r.caller_owner_key = 'care-bus'))
         + (SELECT count(*) FROM app_issuer i JOIN app a ON a.id = i.app_id
             WHERE a.key IN ('strata-rag', 'strata-ingest') AND i.owner_key <> 'platform')
      INTO n_used;
    IF n_licence > 0 OR n_holder > 0 OR n_used > 0 THEN
        RAISE EXCEPTION '078 downgrade REFUSED: % licence(s) name the care-bus bundle, % tenant_bundle row(s) hold it, % row(s) reference the strata apps (licences, grants, offerings, byo modes, bundles, edges, other issuers). Remove them first; nothing was changed.',
            n_licence, n_holder, n_used;
    END IF;

    DELETE FROM app_requires r
     USING app a, app q
     WHERE a.id = r.app_id AND q.id = r.required_app_id
       AND a.key = 'care-bus' AND r.caller_owner_key = 'care-bus'
       AND q.key IN ('mcp', 'strata-rag', 'strata-ingest');

    IF v_bundle IS NOT NULL THEN
        DELETE FROM bundle WHERE id = v_bundle;  -- bundle_app / bundle_ensurer / bundle_caller cascade
    END IF;

    DELETE FROM app WHERE key IN ('strata-rag', 'strata-ingest');  -- plan / app_issuer cascade

    DELETE FROM api_client
     WHERE realm = 'secure-apps'
       AND client_id = 'care-bus-bundle-minter'
       AND owner_key = 'care-bus'
       AND cross_owner = false;

    DELETE FROM realm_owner WHERE realm = 'care-bus' AND owner_key = 'care-bus';
END $$;

-- Un-stamp 078 so scripts/apply_migrations.py re-applies it (up -> down -> up).
DO $$
BEGIN
    IF to_regclass('public.schema_migrations') IS NOT NULL THEN
        DELETE FROM schema_migrations WHERE filename = '078_care_bus_early_access.sql';
    END IF;
END $$;

COMMIT;
