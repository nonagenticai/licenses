-- 065: `secrets` joins the `conveyor` bundle, and fr may mint app=secrets for every
-- tenant holding conveyor (bundle_caller (conveyor, fr, secrets)).
--
-- WHY (George 2026-10-16). fr must prove the TENANT to the secrets service with a
-- minted per-tenant app licence — the same way it already proves it to
-- worker_management (fr#1579, getBundleTokenMinter ->
-- POST /internal/tenants/{t}/bundle-token?app=<app> as secure-apps/fr-bundle-minter).
-- A tokenless X-Tenant-Id to secrets is being closed estate-wide, so fr's secrets hop
-- needs (a) a `secrets` licence on the tenant and (b) fr's minter admitted for exactly
-- that app on tenants fr does not own.
--
-- ⚠️ THIS REVERSES 059. 059's header (Left OUT: "artifacts, secrets, tools, ... —
-- licensed on 5f868d42 (2026-10-11) but called by no conveyor workflow") left secrets
-- out because NO CONVEYOR WORKFLOW called it. That was true and still is for n8n; the
-- caller now is fr itself, a service hop for the tenant, which is what a conveyor
-- member is (059: "SOLD = the apps fr and the conveyor call DIRECTLY"). 059's text is
-- left as written (an applied migration is history); this file supersedes that line
-- for `secrets` only — artifacts, tools, mcp, skill-builder, ... stay out.
--
-- MEMBER, not an app_requires edge: fr calls secrets DIRECTLY for the tenant, it is
-- not something another conveyor app needs. Same shape as 059's (conveyor, fr,
-- workers) caller row: the bundle_caller makes ensure write the APP-SCOPED
-- tenant_dependency_grant (fr, secrets) on every conveyor tenant fr does not own (on
-- an fr-owned tenant fr's owner scope already covers it; ensure skips the owner).
-- No tenant_app_grant is widened; fr's minter still gets 403 `dependency_grant_scope`
-- for coding / laya / ... on a tenant it does not own.
--
-- ENSURER (052 rule 1): fr ensures conveyor iff fr issues every member. secrets is
-- not in models.APP_ISSUER_OVERRIDES, so 050 gave it the default issuers
-- {platform, fr}; this file re-checks it on the database and, if fr does NOT issue
-- secrets, REFUSES the whole file rather than leave a bundle fr is the seeded
-- ensurer of but no longer issues (052 rule 1 would be broken silently).
--
-- REMAINING other_bundle CASE (live state, not this file's): secrets is a member of
-- `non-agentic-ai` (039:102). A tenant whose tenant-level `secrets` licence is already
-- bound to non-agentic-ai (or any other bundle) is REFUSED `other_bundle` for the
-- secrets member when conveyor is re-ensured (services/bundle_ensure.plan_app). An
-- UNBUNDLED tenant-level secrets licence is ADOPTED; none is INSERTED on `standard`.
--
-- EXISTING CONVEYOR TENANTS ARE NOT TOUCHED HERE. No migration inserts licences or
-- grants (licences + metering are ensure's: services/bundle_ensure.py). After this
-- file is applied, each tenant holding conveyor is RE-ENSURED by an operator — the
-- ensure is idempotent: the six existing members answer `exists`, secrets is
-- inserted/adopted, and the (fr, secrets) dependency grant is written:
--     scripts/provision_tenant_bundle.py --tenant <uuid> --bundle conveyor \
--         --plan standard            # dry run: read the table first
--     ... --apply
-- (or POST /internal/tenants/{t}/bundles/conveyor/ensure by an ensurer of conveyor).
-- Holders: SELECT tenant_id FROM tenant_bundle tb JOIN bundle b ON b.id = tb.bundle_id
--          WHERE b.key = 'conveyor';
--
-- PLANS: ensure picks the first code of the caller's `plans` per app; secrets must
-- carry that code (e.g. `standard`) or the member is REFUSED. Nothing is created here.
--
-- A missing app is ANNOUNCED AND SKIPPED (051's rule): no member row, no caller row.
-- Fresh database: this file is stamped without running; the caller seed is mirrored in
-- models.BUNDLE_CALLER_SEED (pinned by tests/unit/test_migration_065_*.py).
--
-- ONE TRANSACTION. Data only. Every INSERT ON CONFLICT DO NOTHING: an operator's row is
-- never overwritten, a re-run is a no-op.
--
-- DOWNGRADE: migrations/downgrades/065_secrets_in_conveyor_bundle.sql (REFUSES while
-- any licence holds secrets IN the conveyor bundle).

BEGIN;

DO $$
DECLARE
    v_bundle  UUID;
    v_app     UUID;
BEGIN
    SELECT id INTO v_bundle FROM bundle WHERE key = 'conveyor';
    SELECT id INTO v_app FROM app WHERE key = 'secrets';
    IF v_bundle IS NULL OR v_app IS NULL THEN
        RAISE NOTICE '065: skipping conveyor/secrets — %',
            CASE WHEN v_bundle IS NULL THEN 'no conveyor bundle' ELSE 'no such app' END;
        RETURN;
    END IF;

    -- 052 rule 1: fr is conveyor's ensurer, so fr must issue every member.
    IF EXISTS (SELECT 1 FROM bundle_ensurer WHERE bundle_id = v_bundle AND owner_key = 'fr')
       AND NOT EXISTS (SELECT 1 FROM app_issuer WHERE app_id = v_app AND owner_key = 'fr') THEN
        RAISE EXCEPTION '065: fr ensures conveyor but does not issue secrets — nothing was changed';
    END IF;

    INSERT INTO bundle_app (bundle_id, app_id)
    VALUES (v_bundle, v_app)
    ON CONFLICT (bundle_id, app_id) DO NOTHING;

    INSERT INTO bundle_caller (bundle_id, owner_key, app_id)
    VALUES (v_bundle, 'fr', v_app)
    ON CONFLICT (bundle_id, owner_key, app_id) DO NOTHING;

    RAISE NOTICE '065: conveyor/secrets member + (fr, secrets) caller ensured';
END $$;

COMMIT;
