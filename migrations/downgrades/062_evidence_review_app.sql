-- Downgrade 062: drop the kag -> evidence-review edge and the evidence-review app
-- (its plans and app_issuer rows cascade).
--
-- ⚠️ Roll back models.py (APP_ISSUER_OVERRIDES, APP_REQUIRES_SEED) and
-- scripts/seed_apps.py too, or the DR seed re-creates the app.
--
-- ⛔ REFUSES (raises, changes nothing) while 062 is in use or an operator has built on
-- the app — deleting the app would CASCADE-delete every one of these silently:
--   * any licence for evidence-review;
--   * any tenant_dependency_grant for it;
--   * any operator row referencing it in offering, tenant_app_byo_mode, bundle_app
--     (ER sold inside a bundle) or bundle_caller (062 itself writes none of these).

BEGIN;

DO $$
DECLARE
    v_app      UUID;
    n_licence  INT := 0;
    n_grant    INT := 0;
    n_offering INT := 0;
    n_byo      INT := 0;
    n_bundle   INT := 0;
    n_caller   INT := 0;
BEGIN
    SELECT id INTO v_app FROM app WHERE key = 'evidence-review';
    IF v_app IS NULL THEN
        RETURN;
    END IF;
    SELECT count(*) INTO n_licence FROM license WHERE app_id = v_app;
    SELECT count(*) INTO n_grant FROM tenant_dependency_grant WHERE app_id = v_app;
    SELECT count(*) INTO n_offering FROM offering WHERE app_id = v_app;
    SELECT count(*) INTO n_byo FROM tenant_app_byo_mode WHERE app_id = v_app;
    SELECT count(*) INTO n_bundle FROM bundle_app WHERE app_id = v_app;
    SELECT count(*) INTO n_caller FROM bundle_caller WHERE app_id = v_app;
    IF n_licence + n_grant + n_offering + n_byo + n_bundle + n_caller > 0 THEN
        RAISE EXCEPTION '062 downgrade REFUSED: % evidence-review licence(s), % dependency grant(s), % offering(s), % tenant_app_byo_mode row(s), % bundle_app row(s), % bundle_caller row(s). Revoke / remove them first; nothing was changed.',
            n_licence, n_grant, n_offering, n_byo, n_bundle, n_caller;
    END IF;
    DELETE FROM app_requires WHERE required_app_id = v_app OR app_id = v_app;
    DELETE FROM app WHERE id = v_app;  -- plan / app_issuer cascade
END $$;

-- Un-stamp 062 so scripts/apply_migrations.py re-applies it (up -> down -> up).
DO $$
BEGIN
    IF to_regclass('public.schema_migrations') IS NOT NULL THEN
        DELETE FROM schema_migrations WHERE filename = '062_evidence_review_app.sql';
    END IF;
END $$;

COMMIT;
