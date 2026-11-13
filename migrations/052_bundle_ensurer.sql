-- 052: WHO may ensure each bundle — a per-bundle ensurer allowlist.
--
-- THE HOLE. Since the bundle-ensure route admitted the owner-write roles
-- (POST /internal/tenants/{id}/bundles/{bundle}/ensure; owner-scoped since 050), ANY
-- tenant owner could ensure ANY bundle on its own tenant — and licences mints a
-- licence for every app in it. So fr could ensure the `commonpractice` bundle on an
-- fr tenant and get a CommonPractice licence minted, although George's core rule is
-- that ONLY CommonPractice issues CommonPractice licences and fr never mints CP, w2v
-- or merqi apps. Requiring the caller to ISSUE every bundled app is wrong the other
-- way: it would break merqi -> `w2v` and CP -> `non-agentic-ai` / `n8n-nodes`.
--
-- THE FIX. `bundle_ensurer(bundle_id, owner_key)`: the ensure route requires the
-- caller's owner to be listed for the bundle; otherwise 403
-- `bundle_not_ensurable_by_caller`. The break-glass client (api_client.cross_owner,
-- secure-apps/licenses-admin) bypasses it, and the bypass is audited
-- (cross_owner_action). A bundle with NO ensurer row can be ensured only through
-- break-glass (fail closed). The DAL refuses to create a bundle without at least
-- one ensurer (models.DatabaseManager.create_bundle), the same rule as app_issuer.
--
-- SEED RULE. An owner is an ensurer of bundle B iff
--   (1) it ISSUES every app in B (app_issuer): ensuring B gives it nothing it could
--       not already mint with POST /licenses/user; or
--   (2) it is a listed cross-app EXCEPTION below, each with its evidence.
-- Evidence sources (all read 2026-10-14):
--   (a) prod tenant_bundle holders by tenant owner (the primary, read-only):
--         commonpractice  -> commonpractice, inference-router, knowledge, merqi,
--                            non-agentic-ai, raisemytickets, research, w2v — ALL on
--                            the legacy netcare 01a0ef72… (archived; 051 makes it
--                            platform's netcare-legacy)
--         inference-router-> fr 7, merqi 2, w2v 2, platform 62 (61 live)
--         non-agentic-ai  -> platform 5 (aa-agents, n8n-dev-portal-realm,
--                            platform-core, procedure-improvement, prompt-improvement)
--         raisemytickets  -> merqi (tenant merqi)
--         w2v             -> merqi (tenant merqi); fr (isolation-probe-test)
--       Every one of these rows was written by the operator scripts
--       (provision_tenant_bundle / provision_bundle_all_tenants): Loki holds ZERO
--       HTTP `/ensure` calls in the licenses namespace from 2026-09-14 (the
--       start of its retention) to 2026-10-14.
--   (b) merqi control_service origin/main (m4): LICENCE_BUNDLE_KEYS default
--       "raisemytickets,w2v", ensured as secure-apps/control-service-account
--       (owner merqi) by provisioning/licence_bundles.py.
--   (c) CommonPractice's create flow (commonpractice feat/netcare-onboarding-path,
--       backend/src/services/tenant-licence-bundles.ts; George 2026-10-14 netcare
--       onboarding): non-agentic-ai, knowledge, research, w2v, merqi,
--       raisemytickets, inference-router, n8n-nodes.
--   (d) provisioning origin/main src/tenant_inference.py ensures `inference-router`
--       (fr's provisioning path). fr origin/main makes NO bundle-ensure call.
--
-- SEED (bundle -> ensurers) and why:
--   commonpractice    {commonpractice}                       rule 1 only (HARD)
--   book-fulfillment  {book-fulfillment}                     rule 1
--   knowledge         {fr, commonpractice}                   fr rule 1; CP (c)+(a)
--   research          {fr, commonpractice}                   fr rule 1; CP (c)+(a)
--   non-agentic-ai    {fr, commonpractice, platform}         fr rule 1; CP (c)+(a);
--                                                            platform (a)
--   merqi             {merqi, commonpractice}                merqi rule 1; CP (c)+(a)
--   raisemytickets    {merqi, commonpractice}                merqi rule 1+(a)+(b);
--                                                            CP (c)+(a)
--   w2v               {w2v, merqi, commonpractice}           w2v rule 1; merqi (b)+(a);
--                                                            CP (c)+(a)
--   inference-router  {fr, commonpractice, merqi, w2v, platform}
--                                                            fr+CP rule 1 (its two
--                                                            issuers), (c), (d);
--                                                            merqi/w2v/platform (a)
--   n8n-nodes         {commonpractice, platform, fr}         platform+fr rule 1;
--                                                            CP (c) (George 10-14)
--
-- CROSS-APP EXCEPTIONS for bundles holding a SINGLE-ISSUER app (an owner that does
-- not issue it may ensure it):
--   commonpractice -> knowledge, research (fr, kag: issuer fr), non-agentic-ai
--     (fr, fr-frontend, kag: fr), merqi (marketing, sales: merqi), raisemytickets
--     (raise-my-tickets*: merqi), w2v (w2v, w2v-image: w2v). Evidence (c), and (a):
--     the legacy netcare held every one of them.
--   merqi -> w2v. Evidence (b), and (a): tenant merqi holds w2v.
--   platform -> non-agentic-ai (fr, fr-frontend, kag: fr). Evidence (a): five
--     platform-owned tenants hold it. platform's only non-break-glass client is
--     portal/portal-bff-service.
-- REJECTED: fr -> w2v. (a) shows fr's isolation-probe-test holding w2v (2026-10-03),
--   but that is a probe tenant written by an operator script, no fr code ensures a
--   bundle (d), and George 2026-10-14: fr never mints w2v apps. Nobody but
--   commonpractice may ensure `commonpractice`.
--
-- Mirrored in models.BUNDLE_ENSURER_SEED. A bundle key absent on a database (e.g.
-- n8n-nodes before 051) seeds nothing; a bundle left with no ensurer is NOTICEd.
--
-- SCHEMA: +1 table (bundle_ensurer: 2 columns, bundle_ensurer_pkey,
-- fk_bundle_ensurer_bundle ON DELETE CASCADE, fk_bundle_ensurer_owner ON DELETE
-- RESTRICT), mirrored in models.BundleEnsurer.
--
-- ONE transaction; CREATE guarded, INSERT ON CONFLICT DO NOTHING: a re-run is a no-op.
-- DOWNGRADE: migrations/downgrades/052_bundle_ensurer.sql

BEGIN;

CREATE TABLE IF NOT EXISTS bundle_ensurer (
    bundle_id  UUID NOT NULL
        CONSTRAINT fk_bundle_ensurer_bundle REFERENCES bundle(id) ON DELETE CASCADE,
    owner_key  VARCHAR(64) NOT NULL
        CONSTRAINT fk_bundle_ensurer_owner REFERENCES tenant_owner(key) ON DELETE RESTRICT,
    CONSTRAINT bundle_ensurer_pkey PRIMARY KEY (bundle_id, owner_key)
);

INSERT INTO bundle_ensurer (bundle_id, owner_key)
SELECT b.id, s.owner_key
FROM (VALUES
    ('commonpractice', 'commonpractice'),
    ('book-fulfillment', 'book-fulfillment'),
    ('knowledge', 'fr'),
    ('knowledge', 'commonpractice'),
    ('research', 'fr'),
    ('research', 'commonpractice'),
    ('non-agentic-ai', 'fr'),
    ('non-agentic-ai', 'commonpractice'),
    ('non-agentic-ai', 'platform'),
    ('merqi', 'merqi'),
    ('merqi', 'commonpractice'),
    ('raisemytickets', 'merqi'),
    ('raisemytickets', 'commonpractice'),
    ('w2v', 'w2v'),
    ('w2v', 'merqi'),
    ('w2v', 'commonpractice'),
    ('inference-router', 'fr'),
    ('inference-router', 'commonpractice'),
    ('inference-router', 'merqi'),
    ('inference-router', 'w2v'),
    ('inference-router', 'platform'),
    ('n8n-nodes', 'commonpractice'),
    ('n8n-nodes', 'platform'),
    ('n8n-nodes', 'fr')
) AS s(bundle_key, owner_key)
JOIN bundle b ON b.key = s.bundle_key
ON CONFLICT (bundle_id, owner_key) DO NOTHING;

DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT b.key FROM bundle b
         WHERE NOT EXISTS (SELECT 1 FROM bundle_ensurer e WHERE e.bundle_id = b.id)
         ORDER BY b.key
    LOOP
        RAISE NOTICE '052: bundle % has NO ensurer — only break-glass can ensure it', r.key;
    END LOOP;
END $$;

COMMIT;
