-- Downgrade 063: the kag-service bundle goes.
--
-- ⚠️ Roll back models.py too (BUNDLE_ENSURER_SEED["kag-service"]).
--
-- ⛔ REFUSES (raises, changes nothing) while 063 is in use: any licence names
-- kag-service or any tenant holds it (ON DELETE RESTRICT). Dependency licences an
-- ensure of kag-service inserted name no bundle and are SHARED with every other bundle
-- whose closure needs the app, so they are not this file's to remove; neither are the
-- tenant_dependency_grant rows (059 owns that table).
-- Only what 063 wrote is removed: the bundle, its member row, its ensurer row.

BEGIN;

DO $$
DECLARE
    v_bundle   UUID;
    n_licence  INT := 0;
    n_holder   INT := 0;
BEGIN
    SELECT id INTO v_bundle FROM bundle WHERE key = 'kag-service';
    IF v_bundle IS NULL THEN
        RAISE NOTICE '063 downgrade: no kag-service bundle — nothing to remove';
        RETURN;
    END IF;
    SELECT count(*) INTO n_licence FROM license WHERE bundle_id = v_bundle;
    SELECT count(*) INTO n_holder FROM tenant_bundle WHERE bundle_id = v_bundle;
    IF n_licence > 0 OR n_holder > 0 THEN
        RAISE EXCEPTION '063 downgrade REFUSED: % licence(s) name kag-service, % tenant_bundle row(s) hold it. Un-provision them first; nothing was changed.',
            n_licence, n_holder;
    END IF;

    DELETE FROM bundle_ensurer WHERE bundle_id = v_bundle;
    DELETE FROM bundle_app WHERE bundle_id = v_bundle;
    DELETE FROM bundle WHERE id = v_bundle;
END $$;

-- Un-stamp 063 so scripts/apply_migrations.py re-applies it (up -> down -> up).
DO $$
BEGIN
    IF to_regclass('public.schema_migrations') IS NOT NULL THEN
        DELETE FROM schema_migrations WHERE filename = '063_kag_service_bundle.sql';
    END IF;
END $$;

COMMIT;
