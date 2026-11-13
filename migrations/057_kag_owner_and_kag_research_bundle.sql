-- 057: kag becomes a tenant owner, with its own licences client and bundle.
--
-- WHY (kag-frontend separation, ~/.claude/plans/kag-frontend-separation.md,
-- decision 5 + review round 1 R1 / R8, 2026-10-15). The kag app today runs as fr
-- (its backend authenticates as fr-backend in both realms) and owns nothing in
-- licences: app `kag` issuer {fr}, no `kag` owner, no kag api_client. The separated
-- kag app registers ITS OWN tenants (owner kag), ensures its own bundle on them and
-- mints its own apps there, as CommonPractice does since 050/052.
--
-- WHAT THIS ADDS (all data, no schema):
--   tenant_owner   kag
--   api_client     ('secure-apps', 'kag-licenses-admin') -> kag, NOT cross-owner.
--                  The Keycloak SA is created by the kag realm-patch (gitops, P3);
--                  until it exists the row is inert.
--   app_issuer     kag              {fr}                 -> {fr, kag}
--                  kag-graph        {fr, platform}       -> {fr, platform, kag}
--                  kag-orchestrator {fr, platform}       -> {fr, platform, kag}
--                  inference-router {fr, commonpractice} -> {fr, commonpractice, kag}
--                  ADD only. fr KEEPS kag for the transition: direct mint/revoke
--                  checks app_issuer (main._authorize_issuance) and fr's admin
--                  console mints kag through provisioning — dropping fr would 403
--                  it. fr leaves kag's issuers only when fr is narrowed (a later
--                  migration). inference-router: R8, needed by kag-research and
--                  kag's AG-UI chat.
--   bundle         kag-research = {kag, kag-graph, kag-orchestrator, inference-router}
--   bundle_ensurer kag-research -> {kag}
--
-- 052's SEED RULE, rule 1: an owner is an ensurer of bundle B iff it ISSUES every
-- app in B. kag-research has no cross-app exception, so the app_issuer rows above
-- are written BEFORE the ensurer row, and the ensurer row is written only when kag
-- issues every app the bundle holds on this database (else NOTICEd, left without
-- an ensurer: break-glass only — fail closed, as 052).
--
-- NOT ADDED (R1): kag does NOT ensure `knowledge` or `research`. Those hold fr,
-- hirag, ingestion, karma, n8n … which kag does not issue; their ensurers stay
-- {fr, commonpractice}.
--
-- PLANS. A bundle carries no plan: bundle ensure picks, per app, the FIRST code of
-- the caller's `plans` list that exists (non-revoked) on that app
-- (services/bundle_ensure.choose_plan) and never invents one. Measured read-only on
-- the primary 2026-10-15 (licenses DB, BEGIN READ ONLY):
--   kag               tenant-pooled, standard, perpetual, portal-n8n-dev, trial
--   kag-graph         tenant-pooled, standard
--   kag-orchestrator  tenant-pooled, standard
--   inference-router  standard ONLY (all 77 active + 4 revoked inference-router
--                     bundle licences sit on `standard`)
-- knowledge / research / n8n-nodes bundle licences all sit on `tenant-pooled`. So
-- kag ensures kag-research with `plans: ["tenant-pooled", "standard"]`: tenant-pooled
-- for the three KAG apps, standard for inference-router — exactly the plans those
-- apps already carry in their existing bundles. No plan is created here. The
-- `offering` table holds only the four `kag-pipeline` offerings (ingestion, karma,
-- hirag, kag-graph), none of which a bundle reads; nothing to add.
--
-- GRANTS. None written. kag ensures kag-research on ITS OWN tenants, where
-- bundle_grantees grants nobody (the owner is an issuer of every app). Side effect
-- of kag gaining a second issuer: an ensure of knowledge / research / non-agentic-ai
-- by commonpractice no longer grants fr THROUGH kag (multi-issuer apps grant only
-- platform); fr still gets in through `fr`, which it issues alone.
--
-- A missing app is ANNOUNCED AND SKIPPED (051's rule: a catalogue that differs
-- between environments is normal). BASELINE INTERACTION (scripts/apply_migrations.py):
-- data only, so a FRESH database stamps it without running; there the owner + client
-- come from the boot seed (models.TENANT_OWNERS / API_CLIENTS), the issuers from
-- scripts/seed_apps.py (models.APP_ISSUER_OVERRIDES), and the ensurers are mirrored in
-- models.BUNDLE_ENSURER_SEED — pinned equal by tests/unit/test_migration_057_*.py.
--
-- ONE TRANSACTION (BEGIN/COMMIT run as statements, as in 050). Every INSERT is
-- ON CONFLICT DO NOTHING: an operator's row is never overwritten, a re-run is a no-op.
--
-- DOWNGRADE: migrations/downgrades/057_kag_owner_and_kag_research_bundle.sql (REFUSES
-- while any tenant is owned by kag or holds kag-research). Roll back models.py's
-- seed constants too, or the boot seed re-inserts the owner and client.

BEGIN;

INSERT INTO tenant_owner (key, display_name) VALUES
    ('kag', 'KAG')
ON CONFLICT (key) DO NOTHING;

INSERT INTO api_client (realm, client_id, owner_key, cross_owner) VALUES
    ('secure-apps', 'kag-licenses-admin', 'kag', false)
ON CONFLICT (realm, client_id) DO NOTHING;

INSERT INTO app_issuer (app_id, owner_key)
SELECT a.id, 'kag'
FROM app a
WHERE a.key IN ('kag', 'kag-graph', 'kag-orchestrator', 'inference-router')
ON CONFLICT (app_id, owner_key) DO NOTHING;

INSERT INTO bundle (key, name, description)
VALUES (
    'kag-research',
    'KAG Research',
    'The separated kag app: kag + kag-graph + kag-orchestrator + inference-router, '
    'ensured by kag on its own tenants (migration 057, 2026-10-15)'
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
    SELECT id INTO v_bundle FROM bundle WHERE key = 'kag-research';
    FOR r IN
        SELECT * FROM (VALUES
            ('kag'),
            ('kag-graph'),
            ('kag-orchestrator'),
            ('inference-router')
        ) AS t(app_key)
    LOOP
        SELECT id INTO v_app FROM app WHERE key = r.app_key;
        IF v_app IS NULL THEN
            RAISE NOTICE '057: skipping kag-research/% — no such app', r.app_key;
            skipped := skipped + 1;
            CONTINUE;
        END IF;
        INSERT INTO bundle_app (bundle_id, app_id)
        VALUES (v_bundle, v_app)
        ON CONFLICT (bundle_id, app_id) DO NOTHING;
        linked := linked + 1;
    END LOOP;
    RAISE NOTICE '057: % kag-research membership rows ensured, % skipped', linked, skipped;
END $$;

-- 052 rule 1: kag ensures kag-research only if it issues every app in it.
DO $$
DECLARE
    v_bundle    UUID;
    unissued    TEXT;
BEGIN
    SELECT id INTO v_bundle FROM bundle WHERE key = 'kag-research';
    SELECT string_agg(a.key, ', ' ORDER BY a.key) INTO unissued
      FROM bundle_app ba JOIN app a ON a.id = ba.app_id
     WHERE ba.bundle_id = v_bundle
       AND NOT EXISTS (
            SELECT 1 FROM app_issuer ai WHERE ai.app_id = ba.app_id AND ai.owner_key = 'kag'
       );
    IF unissued IS NOT NULL THEN
        RAISE NOTICE '057: kag does not issue % — kag-research left WITHOUT kag as ensurer', unissued;
        RETURN;
    END IF;
    INSERT INTO bundle_ensurer (bundle_id, owner_key)
    VALUES (v_bundle, 'kag')
    ON CONFLICT (bundle_id, owner_key) DO NOTHING;
END $$;

COMMIT;
