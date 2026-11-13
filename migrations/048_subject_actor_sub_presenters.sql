-- 048: subject.actor_sub for 12 newly-minted service SAs + 11 presenter-verified licences.
--
-- WHY. 047 stated the acting Keycloak sub for 31 service subjects. Two groups were left
-- NULL, so license-authz still emitted metering events with no actor_id
-- (license_authz_actor_missing_total{reason="licence_no_subject_sub"}).
--
-- OWNER RULE (2026-10-07): when a request uses a licence and no human acted, the actor
-- is the Keycloak service account of the app/service that PRESENTS that licence. No sub
-- is invented.
--
-- PART A: services that had no SA. gitops-production#4527 created 12 identity-only
-- clients in secure-apps (clientId == the subject's keycloak_sub). The subs below were
-- read back on 2026-10-07 with KC GET clients/<id>/service-account-user.
--
-- PART B: tenant-scoped licences whose PRESENTER was verified from live traffic
-- (2026-10-07, 7-day window). Sources: inference-router logs (license_id + client_host
-- -> pod at request time) and the license-authz audit joined to Tempo traces and
-- ztunnel source workloads. Each subject below holds exactly ONE active licence, and
-- every observed presentation of it came from ONE service.
--
-- WHY PER SUBJECT, NOT PER APP. The plan was an app_key -> presenter table. The
-- evidence rules it out: `inference-router` licences are presented by five different
-- services (core-app, worker-management, chat-bridge, chat-bridge-merqi, chat-bridge-rmt),
-- and `raise-my-tickets` licences by sales, marketing, merqi-edge and twenty. A per-app
-- actor would be wrong for most of their licences. The grain that holds is the licence,
-- and for these subjects (one licence each) that is the subject, which 047's column
-- already carries.
--
-- DELIBERATELY NOT HERE (reported, not guessed):
--   * tenant-merqi, tenant-3806d6e5-…: presented by sales, marketing, merqi-edge (no SA)
--     and twenty-server, so there is no single presenter;
--   * every licence with no presentation in the 7-day window (most svc:<tenant>:<app>,
--     svc-kag-<tenant>, svc-marketing/sales-<tenant>, svc-workers-cdmsfirst/-ahtapol/
--     -fr-test-user, n8n-test-perpetual, svc:01a0ef72…:raise-my-tickets with 2 requests).
--
-- SAFETY. Fills only rows whose actor_sub IS NULL, matched by exact keycloak_sub, so a
-- re-run does nothing and a value set by hand is never overwritten. keycloak_sub is not
-- touched. A name absent from the database (fresh / CI) matches nothing. One statement.
--
-- DOWNGRADE: none needed. This is data only, in a column 047 owns, and 047's downgrade
-- drops that column.

UPDATE subject AS s
   SET actor_sub = m.actor_sub
  FROM (VALUES
        -- ---- PART A: the service's own new SA (secure-apps, gitops #4527) ----
        ('svc-coding-ai',                  '164b6569-d92c-4726-a22a-146e19d71c0e'::uuid),
        ('svc-coding-analysis',            '97275ffe-d690-400d-a0db-acc10a151b50'::uuid),
        ('svc-coding-containers',          '8d3b0e36-d137-4882-81af-59179248c5f8'::uuid),
        ('svc-coding-gateway',             'b9e9e9fb-12aa-4e72-b583-26a485250c6b'::uuid),
        ('svc-coding-github',              '9d90aa45-6d9f-4497-babc-aade66038e8d'::uuid),
        ('svc-coding-realtime',            '977cda39-faf8-4bef-88d6-3011deba8906'::uuid),
        ('svc-coding-repo-jobs',           '9c3807b0-db08-483c-9480-14c3ebe8eefa'::uuid),
        ('svc-coding-security',            '8607cbb9-80d4-4680-97b6-a3e5cf012674'::uuid),
        ('svc-coding-testing',             '59786b65-9271-4b57-8999-5fe25c4f502a'::uuid),
        ('svc-discovery-engine',           '778c2f4e-d229-43c0-9fe0-a5bd668611bf'::uuid),
        -- w2v ai-content-generation-service's local_image client -> modality-gateway
        ('svc-image-gateway',              '054815b0-dd89-46c3-bb0f-4e6b1ad4ac8b'::uuid),
        ('svc-modality-gateway',           '062123a4-bf06-4c74-b8bf-f811b98614a1'::uuid),
        -- ---- PART B: the verified presenter of the subject's one licence ----
        -- sales (secure-apps sales-service-account): ~10k/7d at control-service,
        -- Tempo 40/40 sampled traces are sales-service client spans.
        ('svc:639c333a-1613-4fbc-aa14-a7b1430dc0db:raise-my-tickets', '105ca2a2-7501-4f18-93e4-35386138efd6'::uuid),
        -- core-app (secure-apps core-service-account): router logs, 4 core-app pods.
        ('svc-inference-router-cdmsfirst', 'efaff3a9-e87a-4026-a8ca-372c8ea03297'::uuid),
        -- worker-management (secure-apps svc-worker-management): router logs.
        ('svc-inference-router-marketing', 'f2e2957c-7bfb-4836-9230-648a068a571e'::uuid),
        ('svc:4af92cd8-4d13-54f1-aaf9-94ee2caaf823:inference-router', 'f2e2957c-7bfb-4836-9230-648a068a571e'::uuid),
        ('svc:97d33e0a-fc56-5ad6-8c32-1ccc565ff327:inference-router', 'f2e2957c-7bfb-4836-9230-648a068a571e'::uuid),
        ('svc-workers-aa-agents',          'f2e2957c-7bfb-4836-9230-648a068a571e'::uuid),
        ('svc-workers-n8n-dev-portal-realm', 'f2e2957c-7bfb-4836-9230-648a068a571e'::uuid),
        ('svc:0195f48b-d000-7000-8000-000000000005:workers', 'f2e2957c-7bfb-4836-9230-648a068a571e'::uuid),
        -- chat-bridge-merqi (secure-apps svc-chat-bridge-merqi): router logs.
        ('svc-inference-router-merqi',     'a637dad4-bbb7-4c40-8aea-0f0d1d7f47c0'::uuid),
        -- chat-bridge (secure-apps svc-chat-bridge): router logs.
        ('svc:019fe708-424a-7a31-bce8-da19020dc62b:inference-router', '29eeb0b4-5b65-450a-8cff-d76063bd026f'::uuid),
        -- chat-bridge-rmt (secure-apps svc-chat-bridge-rmt): router logs.
        ('svc:01a0ae64-3703-76a6-8e05-9a4826428d32:inference-router', 'fb89fc70-78a3-4c01-9060-49a9a2f81af9'::uuid)
       ) AS m(keycloak_sub, actor_sub)
 WHERE s.keycloak_sub = m.keycloak_sub
   AND s.actor_sub IS NULL;
