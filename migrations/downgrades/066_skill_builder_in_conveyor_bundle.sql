-- Downgrade 066: skill-builder leaves the conveyor bundle; fr's (conveyor,
-- skill-builder) caller goes.
--
-- ⚠️ Roll back models.py too (BUNDLE_CALLER_SEED["conveyor"] loses ("fr", "skill-builder")).
--
-- ⛔ REFUSES (raises, changes nothing) while any licence holds skill-builder IN the
-- conveyor bundle: such a licence would name a bundle that no longer sells its app.
-- Re-bind or revoke those licences first.
-- NOT removed: tenant_dependency_grant (fr, skill-builder) rows written by ensure — 059
-- owns that table, and a grant is ensure's, not this file's. After this downgrade a
-- re-ensure of conveyor no longer writes them; remove existing ones deliberately if
-- fr must stop minting skill-builder.
-- Only what 066 wrote is removed: the member row and the caller row.

BEGIN;

DO $$
DECLARE
    v_bundle   UUID;
    v_app      UUID;
    n_licence  INT := 0;
BEGIN
    SELECT id INTO v_bundle FROM bundle WHERE key = 'conveyor';
    SELECT id INTO v_app FROM app WHERE key = 'skill-builder';
    IF v_bundle IS NULL OR v_app IS NULL THEN
        RAISE NOTICE '066 downgrade: no conveyor bundle or no skill-builder app — nothing to remove';
        RETURN;
    END IF;
    SELECT count(*) INTO n_licence FROM license
     WHERE bundle_id = v_bundle AND app_id = v_app;
    IF n_licence > 0 THEN
        RAISE EXCEPTION '066 downgrade REFUSED: % skill-builder licence(s) carry the conveyor bundle. Re-bind or revoke them first; nothing was changed.',
            n_licence;
    END IF;

    DELETE FROM bundle_caller
     WHERE bundle_id = v_bundle AND owner_key = 'fr' AND app_id = v_app;
    DELETE FROM bundle_app WHERE bundle_id = v_bundle AND app_id = v_app;
END $$;

-- Un-stamp 066 so scripts/apply_migrations.py re-applies it (up -> down -> up).
DO $$
BEGIN
    IF to_regclass('public.schema_migrations') IS NOT NULL THEN
        DELETE FROM schema_migrations WHERE filename = '066_skill_builder_in_conveyor_bundle.sql';
    END IF;
END $$;

COMMIT;
