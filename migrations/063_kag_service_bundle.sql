-- 063: the `kag-service` bundle = {kag}, so a tenant can hold kag (plus its app_requires
-- closure as dependency licences) WITHOUT the other apps kag-research sells.
--
-- WHY (George 2026-10-15: licences are tenant x app only, provisioned via bundles;
-- ~/.claude/plans/program/20-designs/tenant-app-licence-migration-PLAN.md). kag's
-- service tenant (svc-kag, 291c4cf5…) needs a `kag` licence and the licences kag's
-- outbound hops mint through bundle-token (061's kag-bundle-minter). Ensuring
-- `kag-research` there was refused `other_bundle`: kag-research SELLS inference-router,
-- and the tenant already holds its inference-router licence in the `inference-router`
-- bundle (services/bundle_ensure.plan_app: a tenant-level licence bound to ANOTHER
-- bundle is refused for a MEMBER app). A bundle that sells only kag avoids that: every
-- other app the tenant needs is a DEPENDENCY, and plan_dependency SHARES an existing
-- tenant-level licence whatever bundle it sits in ("exists"), never refusing it.
--
-- CLOSURE (059's + 062's app_requires seed = models.APP_REQUIRES_SEED, not assumed):
--   kag -> workers                (caller kag)
--   kag -> mlx-backend            (caller kag)
--   kag -> mlx-ingestion-backend  (caller kag)
--   kag -> evidence-review        (caller kag)        — 062
--   workers -> inference-router   (caller platform)   — transitive, via workers
-- So ensuring kag-service inserts the kag MEMBER licence and, for each closure app the
-- tenant does not already hold, ONE dependency licence (is_dependency, bundle_id NULL);
-- inference-router held in the `inference-router` bundle is shared ("exists"). Ensure
-- writes the APP-SCOPED tenant_dependency_grant rows (kag, workers / mlx-backend /
-- mlx-ingestion-backend / evidence-review) and (platform, inference-router) — each
-- skipped when that owner OWNS the tenant (059: never the owner). No tenant_app_grant
-- is widened.
--
-- REMAINING other_bundle CASE (live state, not this file's): if the tenant already
-- holds a tenant-level `kag` licence bound to another bundle (conveyor, kag-research,
-- knowledge, research, non-agentic-ai all sell kag), the kag member is still refused
-- `other_bundle`. An UNBUNDLED kag licence is adopted.
--
-- ENSURER (052 rule 1: an owner ensures B iff it issues every app in B). kag's issuers
-- are {fr, kag} (057). Seeded: {kag} — kag's own service bundle, as 057 seeded
-- kag-research with {kag} only. NOT platform: platform does not issue kag, and no
-- cross-app exception is evidenced; a platform-owned tenant (svc-kag) is ensured
-- through break-glass (secure-apps/licenses-admin, cross_owner, audited) or the
-- operator script, exactly as 052 leaves every bundle without a platform ensurer.
-- No bundle_caller: kag's callers reach it through their own bundles.
--
-- PLANS: kag carries tenant-pooled, standard, perpetual, portal-n8n-dev, trial (057's
-- measurement); workers / mlx-* / evidence-review / inference-router carry standard. Ensure picks the
-- first code of the caller's `plans` per app; nothing is created here.
--
-- A missing app is ANNOUNCED AND SKIPPED (051's rule). Fresh database: this file is
-- stamped without running; the ensurer seed is mirrored in models.BUNDLE_ENSURER_SEED
-- (pinned by tests/unit/test_migration_063_kag_service_bundle.py).
--
-- ONE TRANSACTION. Data only. Every INSERT ON CONFLICT DO NOTHING: an operator's row is
-- never overwritten, a re-run is a no-op.
--
-- DOWNGRADE: migrations/downgrades/063_kag_service_bundle.sql (REFUSES while any
-- tenant holds kag-service or any licence names it).

BEGIN;

INSERT INTO bundle (key, name, description)
VALUES (
    'kag-service',
    'KAG Service',
    'kag alone; its dependencies (workers, mlx-backend, mlx-ingestion-backend, '
    'evidence-review, inference-router) come from app_requires as dependency licences '
    '(migration 063, 2026-10-15)'
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
    SELECT id INTO v_bundle FROM bundle WHERE key = 'kag-service';
    FOR r IN
        SELECT * FROM (VALUES
            ('kag')
        ) AS t(app_key)
    LOOP
        SELECT id INTO v_app FROM app WHERE key = r.app_key;
        IF v_app IS NULL THEN
            RAISE NOTICE '063: skipping kag-service/% — no such app', r.app_key;
            skipped := skipped + 1;
            CONTINUE;
        END IF;
        INSERT INTO bundle_app (bundle_id, app_id)
        VALUES (v_bundle, v_app)
        ON CONFLICT (bundle_id, app_id) DO NOTHING;
        linked := linked + 1;
    END LOOP;
    RAISE NOTICE '063: % kag-service membership rows ensured, % skipped', linked, skipped;
END $$;

-- 052 rule 1: kag ensures kag-service only if it issues every app in it.
DO $$
DECLARE
    v_bundle    UUID;
    unissued    TEXT;
BEGIN
    SELECT id INTO v_bundle FROM bundle WHERE key = 'kag-service';
    IF NOT EXISTS (SELECT 1 FROM tenant_owner WHERE key = 'kag') THEN
        RAISE NOTICE '063: no kag owner — kag-service left WITHOUT an ensurer';
        RETURN;
    END IF;
    SELECT string_agg(a.key, ', ' ORDER BY a.key) INTO unissued
      FROM bundle_app ba JOIN app a ON a.id = ba.app_id
     WHERE ba.bundle_id = v_bundle
       AND NOT EXISTS (
            SELECT 1 FROM app_issuer ai WHERE ai.app_id = ba.app_id AND ai.owner_key = 'kag'
       );
    IF unissued IS NOT NULL THEN
        RAISE NOTICE '063: kag does not issue % — kag-service left WITHOUT kag as ensurer', unissued;
        RETURN;
    END IF;
    INSERT INTO bundle_ensurer (bundle_id, owner_key)
    VALUES (v_bundle, 'kag')
    ON CONFLICT (bundle_id, owner_key) DO NOTHING;
END $$;

COMMIT;
