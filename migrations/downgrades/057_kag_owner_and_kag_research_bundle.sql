-- Downgrade 057: kag stops being a tenant owner; the kag-research bundle goes.
--
-- ⚠️ Roll back models.py too (TENANT_OWNERS, API_CLIENTS, APP_ISSUER_OVERRIDES,
-- BUNDLE_ENSURER_SEED), or the boot seed re-inserts the kag owner and its client on
-- the next start.
--
-- ⛔ REFUSES (raises, changes nothing) while kag is in use:
--   * any tenant is owned by kag (tenants.owner_key ON DELETE RESTRICT; moving a
--     customer is an operator decision, not a rollback's);
--   * any licence names kag-research or any tenant holds it (ON DELETE RESTRICT);
--   * kag holds anything 057 did not write: a grant, an api_client other than
--     secure-apps/kag-licenses-admin, an issuer row on another app, an ensurer row
--     on another bundle.
-- Only what 057 wrote is removed: the four app_issuer rows (fr / platform /
-- commonpractice issuers are untouched), the bundle + its members + its ensurer,
-- the api_client row, the owner.

BEGIN;

DO $$
DECLARE
    v_bundle   UUID;
    n_tenant   INT;
    n_licence  INT := 0;
    n_holder   INT := 0;
    n_other    INT;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM tenant_owner WHERE key = 'kag') THEN
        RAISE NOTICE '057 downgrade: no kag owner — nothing to remove';
    END IF;

    SELECT count(*) INTO n_tenant FROM tenants WHERE owner_key = 'kag';
    SELECT id INTO v_bundle FROM bundle WHERE key = 'kag-research';
    IF v_bundle IS NOT NULL THEN
        SELECT count(*) INTO n_licence FROM license WHERE bundle_id = v_bundle;
        SELECT count(*) INTO n_holder FROM tenant_bundle WHERE bundle_id = v_bundle;
    END IF;
    IF n_tenant > 0 OR n_licence > 0 OR n_holder > 0 THEN
        RAISE EXCEPTION '057 downgrade REFUSED: % tenant(s) owned by kag, % licence(s) name kag-research, % tenant_bundle row(s) hold it. Move / un-provision them first; nothing was changed.',
            n_tenant, n_licence, n_holder;
    END IF;

    SELECT (SELECT count(*) FROM tenant_app_grant WHERE grantee_owner_key = 'kag')
         + (SELECT count(*) FROM api_client
             WHERE owner_key = 'kag'
               AND NOT (realm = 'secure-apps' AND client_id = 'kag-licenses-admin'))
         + (SELECT count(*) FROM app_issuer ai JOIN app a ON a.id = ai.app_id
             WHERE ai.owner_key = 'kag'
               AND a.key NOT IN ('kag', 'kag-graph', 'kag-orchestrator', 'inference-router'))
         + (SELECT count(*) FROM bundle_ensurer e JOIN bundle b ON b.id = e.bundle_id
             WHERE e.owner_key = 'kag' AND b.key <> 'kag-research')
      INTO n_other;
    IF n_other > 0 THEN
        RAISE EXCEPTION '057 downgrade REFUSED: kag holds % row(s) 057 did not write (grants, other api_clients, issuer rows on other apps, ensurer rows on other bundles); nothing was changed.',
            n_other;
    END IF;

    IF v_bundle IS NOT NULL THEN
        DELETE FROM bundle_ensurer WHERE bundle_id = v_bundle;
        DELETE FROM bundle_app WHERE bundle_id = v_bundle;
        DELETE FROM bundle WHERE id = v_bundle;
    END IF;

    DELETE FROM app_issuer ai
     USING app a
     WHERE a.id = ai.app_id
       AND ai.owner_key = 'kag'
       AND a.key IN ('kag', 'kag-graph', 'kag-orchestrator', 'inference-router');

    DELETE FROM api_client
     WHERE realm = 'secure-apps' AND client_id = 'kag-licenses-admin' AND owner_key = 'kag';

    DELETE FROM tenant_owner WHERE key = 'kag';
END $$;

-- Un-stamp 057 so scripts/apply_migrations.py re-applies it (up -> down -> up).
DO $$
BEGIN
    IF to_regclass('public.schema_migrations') IS NOT NULL THEN
        DELETE FROM schema_migrations
         WHERE filename = '057_kag_owner_and_kag_research_bundle.sql';
    END IF;
END $$;

COMMIT;
