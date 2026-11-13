-- 040: the `inference-router` bundle — the bundle every tenant's inference-router
-- licence is held in.
--
-- WHY (George's provider-refactor rulings §I Q1/Q3, 2026-10-01; licenses #239).
-- Every non-archived tenant — INCLUDING the dormant service actors (kind='service')
-- — holds exactly ONE (tenant, app=inference-router) licence, and that licence sits
-- in the tenant's BUNDLE (license.bundle_id, migration 039), not loose. A tenant
-- later pastes a token minted on it through the portal (the EXISTING route
-- `POST /licenses/{id}/tokens?expires_days=180`; no new route). A licence can only
-- be held in a bundle that exists, so this file creates that bundle and links the
-- one app it covers.
--
-- THE APP IS NOT IN THIS REPO'S SEEDS. `inference-router` was created in PROD via
-- `POST /apps` (plan code `standard`); no seed script or migration here creates it.
-- So the membership follows the 028/039 contract exactly: the app is linked ONLY if
-- the key already exists in `app`, and an absent key is ANNOUNCED AND SKIPPED. No
-- `app` row is created here — creating one is a catalogue decision, not a
-- membership one, and inventing it here would fork prod's id from every other
-- environment's.
--
-- ⛔ WHAT THIS DOES NOT DO. It mints nothing and gives NO tenant a licence or a
-- bundle: no `license`, `tenant_bundle`, `token` or `subject` row is written, and no
-- existing row of any kind is modified. Granting is the job of
-- scripts/provision_bundle_all_tenants.py (dry-run by default), which runs
-- scripts/provision_tenant_bundle.py's per-tenant decision — and that ADOPTS an
-- existing unbundled tenant-level inference-router licence rather than minting a
-- second one beside it.
--
-- BASELINE INTERACTION (scripts/apply_migrations.py). On a FRESH database this file
-- is stamped without running, exactly like 028/039's data: the bundle row and its
-- membership exist only on databases that ran it. It is DATA ONLY — no schema object
-- is created, so the ORM arm and the corpus arm of tests/unit/test_schema_parity.py
-- are structurally unchanged and only the migration-file count moves.
--
-- SAFETY. A failed migration makes the pod refuse to serve (ext_authz, replicas: 2),
-- so: the INSERTs are ON CONFLICT DO NOTHING (a re-run is a no-op) and a missing app
-- is a NOTICE, never an error.
--
-- DOWNGRADE: migrations/downgrades/040_inference_router_bundle.sql

INSERT INTO bundle (key, name, description)
VALUES (
    'inference-router',
    'Inference Router',
    'The ONE per-tenant inference-router licence (provider-refactor §I, licenses #239)'
)
ON CONFLICT (key) DO NOTHING;

DO $$
DECLARE
    r         RECORD;
    v_bundle  UUID;
    v_app     UUID;
    linked    INT := 0;
    skipped   INT := 0;
BEGIN
    FOR r IN
        SELECT * FROM (VALUES
            ('inference-router', 'inference-router')
        ) AS t(bundle_key, app_key)
    LOOP
        SELECT id INTO v_bundle FROM bundle WHERE key = r.bundle_key;
        SELECT id INTO v_app    FROM app    WHERE key = r.app_key;

        IF v_bundle IS NULL OR v_app IS NULL THEN
            RAISE NOTICE '040: skipping %/% — %',
                r.bundle_key, r.app_key,
                CASE WHEN v_bundle IS NULL THEN 'no such bundle' ELSE 'no such app' END;
            skipped := skipped + 1;
            CONTINUE;
        END IF;

        INSERT INTO bundle_app (bundle_id, app_id)
        VALUES (v_bundle, v_app)
        ON CONFLICT (bundle_id, app_id) DO NOTHING;
        linked := linked + 1;
    END LOOP;

    RAISE NOTICE '040: % membership rows ensured, % skipped', linked, skipped;
END $$;

-- Verify
SELECT b.key AS bundle, count(ba.app_id) AS apps
  FROM bundle b
  LEFT JOIN bundle_app ba ON ba.bundle_id = b.id
 WHERE b.key = 'inference-router'
 GROUP BY b.key;
