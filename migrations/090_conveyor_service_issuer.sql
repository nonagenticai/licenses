-- 090: the conveyor's own backend may mint conveyor licences through provisioning.
--
-- WHY (the Rust conveyor's Platform wizard, 2026-11-09). Conveyor provisions a tenant by
-- POSTing /api/provision to the PROVISIONING service as the secure-apps client
-- `conveyor-backend` (owner fr, the same owner as provisioning-backend). Provisioning then
-- mints the licence with ITS OWN credential — `secure-apps/provisioning-backend`, owner fr —
-- so licences' `_authorize_issuance` checks `app_issuer(app=conveyor)` for owner `fr`.
-- Measured live 2026-11-09: app_issuer holds (conveyor, agent) ONLY, so every
-- app_keys:["conveyor"] provision would 403 `app_not_issuable_by_caller` after the tenant
-- was created — a half-provisioned tenant. This file adds the missing issuer row.
--
-- WHAT THIS ADDS (data only, no schema, one transaction):
--   1. app_issuer (conveyor -> fr), additive; (conveyor, agent) is left untouched.
--   2. api_client (secure-apps, conveyor-backend, fr, cross_owner=false) — the client the
--      conveyor service authenticates as. A client NOT registered here is refused 403
--      `unknown_api_client` on every tenant / licence / grant / bundle write (083's rule).
--
-- ⚠️ POLICY, stated plainly: after this file the owner `fr` may mint conveyor licences and
-- read fr-owned tenants (that is how licences' owner scoping works — the owner is keyed on
-- the verified (realm, azp), not on the app). This is the price of using the fr-owned
-- provisioning service: its SA mints as fr. It mirrors `fr` issuing its own apps (050) and
-- is NOT cross_owner — conveyor gets no break-glass power over other owners' tenants.
--
-- FRESH DATABASE: models.API_CLIENTS gains the client, so the boot seed writes it and this
-- file's insert is a no-op. The app table is NOT seeded by any migration — the `conveyor`
-- app is created at runtime (POST /apps) — so the app_issuer leg ANNOUNCES AND SKIPS when
-- the app is absent (051/089's rule). Re-running is a no-op; an operator's row is never
-- overwritten.
--
-- DOWNGRADE: migrations/downgrades/090_conveyor_service_issuer.sql (removes the two rows
-- and must be mirrored in models.API_CLIENTS). Existing tenant/licence rows stay.

BEGIN;

DO $$
DECLARE
    v_app  UUID;
    v_rows INT := 0;
BEGIN
    SELECT id INTO v_app FROM app WHERE key = 'conveyor';
    IF v_app IS NULL THEN
        RAISE NOTICE '090: app conveyor missing — app_issuer (conveyor, fr) skipped';
    ELSE
        INSERT INTO app_issuer (app_id, owner_key)
        VALUES (v_app, 'fr')
        ON CONFLICT (app_id, owner_key) DO NOTHING;
        SELECT count(*) INTO v_rows
          FROM app_issuer WHERE app_id = v_app AND owner_key = 'fr';
        RAISE NOTICE '090: app_issuer (conveyor, fr) present=%', v_rows;
    END IF;
END $$;

INSERT INTO api_client (realm, client_id, owner_key, cross_owner)
VALUES ('secure-apps', 'conveyor-backend', 'fr', false)
ON CONFLICT (realm, client_id) DO NOTHING;

COMMIT;
