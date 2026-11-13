-- Downgrade 067: remove the strata apps, their owners and the configurator's client.
--
-- ⚠️ Roll back models.py (TENANT_OWNERS, API_CLIENTS, APP_ISSUER_OVERRIDES) and
-- scripts/seed_apps.py / seed_offerings.py too, or the boot / DR seed re-creates them.
--
-- ⛔ REFUSES (raises, changes nothing) while 067 is in use or an operator built on it —
-- deleting an app CASCADE-deletes its licences silently, and tenants.owner_key is
-- ON DELETE RESTRICT:
--   * any tenant owned by strata-configurator or care-bus;
--   * any licence, dependency grant, offering, tenant_app_byo_mode, bundle_app or
--     bundle_caller row for either app;
--   * either owner holding anything 067 did not write (a grant, another api_client,
--     an issuer row on another app, an ensurer row).

BEGIN;

DO $$
DECLARE
    n_tenant  INT;
    n_used    INT;
    n_other   INT;
BEGIN
    SELECT count(*) INTO n_tenant FROM tenants
     WHERE owner_key IN ('strata-configurator', 'care-bus');
    SELECT (SELECT count(*) FROM license l JOIN app a ON a.id = l.app_id
             WHERE a.key IN ('strata-configurator', 'care-bus'))
         + (SELECT count(*) FROM tenant_dependency_grant g JOIN app a ON a.id = g.app_id
             WHERE a.key IN ('strata-configurator', 'care-bus'))
         + (SELECT count(*) FROM offering o JOIN app a ON a.id = o.app_id
             WHERE a.key IN ('strata-configurator', 'care-bus'))
         + (SELECT count(*) FROM tenant_app_byo_mode m JOIN app a ON a.id = m.app_id
             WHERE a.key IN ('strata-configurator', 'care-bus'))
         + (SELECT count(*) FROM bundle_app b JOIN app a ON a.id = b.app_id
             WHERE a.key IN ('strata-configurator', 'care-bus'))
         + (SELECT count(*) FROM bundle_caller c JOIN app a ON a.id = c.app_id
             WHERE a.key IN ('strata-configurator', 'care-bus'))
      INTO n_used;
    IF n_tenant > 0 OR n_used > 0 THEN
        RAISE EXCEPTION '067 downgrade REFUSED: % tenant(s) owned by a strata owner, % row(s) reference a strata app (licences, grants, offerings, byo modes, bundles). Remove them first; nothing was changed.',
            n_tenant, n_used;
    END IF;

    SELECT (SELECT count(*) FROM tenant_app_grant
             WHERE grantee_owner_key IN ('strata-configurator', 'care-bus'))
         + (SELECT count(*) FROM api_client
             WHERE owner_key IN ('strata-configurator', 'care-bus')
               AND NOT (realm = 'secure-apps'
                        AND client_id = 'strata-configurator-licenses-admin'))
         + (SELECT count(*) FROM app_issuer ai JOIN app a ON a.id = ai.app_id
             WHERE ai.owner_key IN ('strata-configurator', 'care-bus')
               AND a.key NOT IN ('strata-configurator', 'care-bus'))
         + (SELECT count(*) FROM bundle_ensurer
             WHERE owner_key IN ('strata-configurator', 'care-bus'))
      INTO n_other;
    IF n_other > 0 THEN
        RAISE EXCEPTION '067 downgrade REFUSED: the strata owners hold % row(s) 067 did not write (grants, other api_clients, issuer rows on other apps, ensurer rows); nothing was changed.',
            n_other;
    END IF;

    DELETE FROM app WHERE key IN ('strata-configurator', 'care-bus');  -- plan / app_issuer cascade
    DELETE FROM api_client
     WHERE realm = 'secure-apps' AND client_id = 'strata-configurator-licenses-admin'
       AND owner_key = 'strata-configurator';
    DELETE FROM tenant_owner WHERE key IN ('strata-configurator', 'care-bus');
END $$;

-- Un-stamp 067 so scripts/apply_migrations.py re-applies it (up -> down -> up).
DO $$
BEGIN
    IF to_regclass('public.schema_migrations') IS NOT NULL THEN
        DELETE FROM schema_migrations
         WHERE filename = '067_strata_configurator_and_care_bus_apps.sql';
    END IF;
END $$;

COMMIT;
