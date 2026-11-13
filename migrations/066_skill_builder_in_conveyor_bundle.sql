-- 066: `skill-builder` joins the `conveyor` bundle, and fr may mint app=skill-builder
-- for every tenant holding conveyor (bundle_caller (conveyor, fr, skill-builder)).
--
-- WHY (George 2026-10-16, fr P1.5 "agent skills"). fr calls skill-builder for the
-- tenant and must prove the TENANT with a minted per-tenant app licence — the same
-- way it proves it to worker_management (fr#1579) and secrets (065):
-- getBundleTokenMinter('skill-builder') -> POST /internal/tenants/{t}/bundle-token
-- ?app=skill-builder as secure-apps/fr-bundle-minter. Nothing is stored in SOPS.
--
-- ⚠️ THIS REVERSES 065's EXCLUSION. 065's header ("this file supersedes that line for
-- `secrets` only — artifacts, tools, mcp, skill-builder, ... stay out") and 059's
-- Left-OUT list kept skill-builder out because no conveyor caller existed. George
-- ruled 2026-10-16 to add it: the caller is now fr itself, a service hop for the
-- tenant. 059 and 065 are left as written (an applied migration is history); this
-- file supersedes those lines for `skill-builder` only — artifacts, tools, mcp, ...
-- stay out.
--
-- MEMBER, not an app_requires edge: fr calls skill-builder DIRECTLY for the tenant.
-- Same shape as 065's (conveyor, fr, secrets): the bundle_caller makes ensure write
-- the APP-SCOPED tenant_dependency_grant (fr, skill-builder) on every conveyor tenant
-- fr does not own (on an fr-owned tenant fr's owner scope already covers it). No
-- tenant_app_grant is widened; fr's minter still gets 403 `dependency_grant_scope`
-- for coding / laya / ... on a tenant it does not own.
--
-- ENSURER (052 rule 1): fr ensures conveyor iff fr issues every member. skill-builder
-- is not in models.APP_ISSUER_OVERRIDES, so 050 gave it the default issuers
-- {platform, fr}; this file re-checks it on the database and, if fr does NOT issue
-- skill-builder, REFUSES the whole file rather than leave a bundle fr is the seeded
-- ensurer of but no longer issues.
--
-- REMAINING other_bundle CASE (live state, not this file's): skill-builder is a member
-- of `non-agentic-ai` (028:136). A tenant whose tenant-level `skill-builder` licence is
-- already bound to non-agentic-ai (or any other bundle) is REFUSED `other_bundle` for
-- the skill-builder member when conveyor is re-ensured (services/bundle_ensure.plan_app).
-- An UNBUNDLED tenant-level skill-builder licence is ADOPTED; none is INSERTED on
-- `standard`.
--
-- EXISTING CONVEYOR TENANTS ARE NOT TOUCHED HERE. No migration inserts licences or
-- grants (licences + metering are ensure's: services/bundle_ensure.py). After this
-- file is applied, each tenant holding conveyor is RE-ENSURED by an operator — the
-- ensure is idempotent: existing members answer `exists`, skill-builder is
-- inserted/adopted, and the (fr, skill-builder) dependency grant is written:
--     scripts/provision_tenant_bundle.py --tenant <uuid> --bundle conveyor \
--         --plan standard            # dry run: read the table first
--     ... --apply
-- (or POST /internal/tenants/{t}/bundles/conveyor/ensure by an ensurer of conveyor).
-- Holders: SELECT tenant_id FROM tenant_bundle tb JOIN bundle b ON b.id = tb.bundle_id
--          WHERE b.key = 'conveyor';
--
-- PLANS: ensure picks the first code of the caller's `plans` per app; skill-builder
-- must carry that code (e.g. `standard`) or the member is REFUSED. Nothing is created.
--
-- A missing app is ANNOUNCED AND SKIPPED (051's rule): no member row, no caller row.
-- Fresh database: this file is stamped without running; the caller seed is mirrored in
-- models.BUNDLE_CALLER_SEED (pinned by tests/unit/test_migration_066_*.py).
--
-- ONE TRANSACTION. Data only. Every INSERT ON CONFLICT DO NOTHING: an operator's row is
-- never overwritten, a re-run is a no-op.
--
-- DOWNGRADE: migrations/downgrades/066_skill_builder_in_conveyor_bundle.sql (REFUSES
-- while any licence holds skill-builder IN the conveyor bundle).

BEGIN;

DO $$
DECLARE
    v_bundle  UUID;
    v_app     UUID;
BEGIN
    SELECT id INTO v_bundle FROM bundle WHERE key = 'conveyor';
    SELECT id INTO v_app FROM app WHERE key = 'skill-builder';
    IF v_bundle IS NULL OR v_app IS NULL THEN
        RAISE NOTICE '066: skipping conveyor/skill-builder — %',
            CASE WHEN v_bundle IS NULL THEN 'no conveyor bundle' ELSE 'no such app' END;
        RETURN;
    END IF;

    -- 052 rule 1: fr is conveyor's ensurer, so fr must issue every member.
    IF EXISTS (SELECT 1 FROM bundle_ensurer WHERE bundle_id = v_bundle AND owner_key = 'fr')
       AND NOT EXISTS (SELECT 1 FROM app_issuer WHERE app_id = v_app AND owner_key = 'fr') THEN
        RAISE EXCEPTION '066: fr ensures conveyor but does not issue skill-builder — nothing was changed';
    END IF;

    INSERT INTO bundle_app (bundle_id, app_id)
    VALUES (v_bundle, v_app)
    ON CONFLICT (bundle_id, app_id) DO NOTHING;

    INSERT INTO bundle_caller (bundle_id, owner_key, app_id)
    VALUES (v_bundle, 'fr', v_app)
    ON CONFLICT (bundle_id, owner_key, app_id) DO NOTHING;

    RAISE NOTICE '066: conveyor/skill-builder member + (fr, skill-builder) caller ensured';
END $$;

COMMIT;
