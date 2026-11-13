-- Downgrade 059: drop app dependencies, dependency licences' marker, the conveyor
-- bundle and the fr / w2v bundle-token minter rows.
--
-- ⚠️ Roll back models.py too (AppRequires, BundleCaller, License.is_dependency,
-- API_CLIENTS, BUNDLE_ENSURER_SEED, APP_REQUIRES_SEED, BUNDLE_CALLER_SEED), or
-- create_all re-creates the tables and the boot seed re-inserts the two clients.
--
-- ⛔ REFUSES (raises, changes nothing) while 059 is in use:
--   * any licence is a dependency licence (dropping the column would turn an
--     internal-only licence into one a user token can be minted on);
--   * any licence names `conveyor` or any tenant holds it (ON DELETE RESTRICT).
-- tenant_dependency_grant goes with the table: every row was derived by an ensure
-- from 059's edges, and none can exist without a dependency it serves. No
-- tenant_app_grant is touched.

BEGIN;

DO $$
DECLARE
    v_bundle   UUID;
    n_dep      INT := 0;
    n_licence  INT := 0;
    n_holder   INT := 0;
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'license' AND column_name = 'is_dependency'
    ) THEN
        EXECUTE 'SELECT count(*) FROM license WHERE is_dependency' INTO n_dep;
    END IF;
    SELECT id INTO v_bundle FROM bundle WHERE key = 'conveyor';
    IF v_bundle IS NOT NULL THEN
        SELECT count(*) INTO n_licence FROM license WHERE bundle_id = v_bundle;
        SELECT count(*) INTO n_holder FROM tenant_bundle WHERE bundle_id = v_bundle;
    END IF;
    IF n_dep > 0 OR n_licence > 0 OR n_holder > 0 THEN
        RAISE EXCEPTION '059 downgrade REFUSED: % dependency licence(s), % licence(s) name conveyor, % tenant_bundle row(s) hold it. Revoke / un-provision them first; nothing was changed.',
            n_dep, n_licence, n_holder;
    END IF;

    IF v_bundle IS NOT NULL THEN
        DELETE FROM bundle WHERE id = v_bundle;  -- bundle_app / bundle_ensurer / bundle_caller cascade
    END IF;

    DELETE FROM api_client
     WHERE realm = 'secure-apps'
       AND (client_id, owner_key) IN (('fr-bundle-minter', 'fr'), ('w2v-bundle-minter', 'w2v'));
END $$;

DROP TABLE IF EXISTS tenant_dependency_grant;
DROP TABLE IF EXISTS bundle_caller;
DROP TABLE IF EXISTS app_requires;
ALTER TABLE license DROP CONSTRAINT IF EXISTS ck_license_dependency_unbundled;
ALTER TABLE license DROP COLUMN IF EXISTS is_dependency;

-- Un-stamp 059 so scripts/apply_migrations.py re-applies it (up -> down -> up).
DO $$
BEGIN
    IF to_regclass('public.schema_migrations') IS NOT NULL THEN
        DELETE FROM schema_migrations
         WHERE filename = '059_app_requires_and_conveyor_bundle.sql';
    END IF;
END $$;

COMMIT;
