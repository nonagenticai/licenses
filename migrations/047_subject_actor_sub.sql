-- 047: subject.actor_sub — the Keycloak sub that ACTS when a subject's licence is presented.
--
-- WHY. license-authz stamps every metering event with actor_id. In licence-token mode
-- that is the licence subject's Keycloak sub, which introspection only emits when
-- `subject.keycloak_sub` IS a Keycloak sub (models.keycloak_sub_as_identity). Measured
-- read-only on prod 2026-10-07: 281 of 388 active licences belong to subjects whose
-- keycloak_sub is a registry LABEL (`svc-kag`, `svc:<tenant>:<app>`, `tenant-merqi`) —
-- so those events carried no actor (license_authz_actor_missing_total
-- {reason="licence_no_subject_sub"}).
--
-- OWNER RULE (2026-10-07): a service's actor is its OWN Keycloak service-account sub.
--
-- ⛔ `keycloak_sub` IS NOT REWRITTEN. It is the runtime lookup key
-- (get_subject_by_keycloak_sub, provision_service_token.py --subject …) and ingestion
-- compares `subject_keycloak_sub` against recorded owners. This file only ADDS a
-- nullable column and states it for the subjects below; introspection returns it as
-- `subject_actor_sub` (keycloak_sub wins when it is itself a sub).
--
-- WHICH SUBJECTS. Exactly the service subjects that are a mapped service's OWN identity:
-- named in the 2026-10-07 actor map (service -> SA sub, KC GET
-- clients/<id>/service-account-user), or attributed to that service by the gitops
-- manifest that mounts its token. Matched by exact `keycloak_sub` — no prefix rule.
-- DELIBERATELY NOT HERE (owner follow-up, no sub is invented):
--   * services with no Keycloak SA: svc-coding-*, svc-discovery-engine,
--     svc-image-gateway, svc-modality-gateway;
--   * per-tenant APP licences (`svc-inference-router-<tenant>`, `svc-kag-<tenant>`,
--     `svc-workers-<tenant>`, `svc-marketing-<tenant>`, …): the licence is the
--     TENANT's for that app, presented by whichever caller acts for the tenant
--     (core-app presents svc-inference-router-cdmsfirst), so the app's SA is not
--     the actor;
--   * `svc:<tenant>:<app>` bundle subjects, `tenant-*`, test subjects.
--
-- SAFETY. ADD COLUMN … NULL with no default is catalogue-only. The UPDATE fills only
-- rows whose actor_sub IS NULL, so a re-run is a no-op and a value an operator set by
-- hand is never overwritten. A name absent from the database (fresh / CI) matches
-- nothing. One statement: commits entirely or not at all.
--
-- DOWNGRADE: migrations/downgrades/047_subject_actor_sub.sql (drops the column).

ALTER TABLE subject
    ADD COLUMN IF NOT EXISTS actor_sub uuid NULL;

UPDATE subject AS s
   SET actor_sub = m.actor_sub
  FROM (VALUES
        -- core-app (secure-apps core-service-account)
        ('svc-core-app-mlx',               'efaff3a9-e87a-4026-a8ca-372c8ea03297'::uuid),
        -- worker_management (secure-apps svc-worker-management)
        ('svc-workers-system',             'f2e2957c-7bfb-4836-9230-648a068a571e'::uuid),
        -- marketing (secure-apps marketing-service-account)
        ('marketing-service',              '53704bc4-654c-48a8-9205-8cc2530b6213'::uuid),
        -- sales (secure-apps sales-service-account)
        ('sales-service',                  '105ca2a2-7501-4f18-93e4-35386138efd6'::uuid),
        -- fr (secure-apps fr-backend)
        ('svc-fr-backend',                 '31f62c60-bef6-46a0-b0be-9a8f823fd987'::uuid),
        ('svc-fr-backend-kag',             '31f62c60-bef6-46a0-b0be-9a8f823fd987'::uuid),
        ('svc-fr-backend-pm',              '31f62c60-bef6-46a0-b0be-9a8f823fd987'::uuid),
        -- kag-frontend (secure-apps svc-kag-frontend)
        ('svc-kag-frontend-pm',            'c9f587e6-8e41-4671-a54e-bfcd5dcfe8e3'::uuid),
        -- kag gateway (secure-apps svc-kag)
        ('svc-kag',                        'a52d3c9a-11ea-46a3-aa49-8651ae166391'::uuid),
        ('svc-kag-karma',                  'a52d3c9a-11ea-46a3-aa49-8651ae166391'::uuid),
        -- kag_graph (secure-apps svc-kag-graph)
        ('svc-kag-graph',                  'be35ab47-6a1a-4e05-9788-8fa129827fac'::uuid),
        ('svc-kag-graph-reconciler',       'be35ab47-6a1a-4e05-9788-8fa129827fac'::uuid),
        -- kag_orchestrator (secure-apps svc-kag-orchestrator)
        ('svc-kag-orchestrator-graph',     'c3f265f3-6947-4411-b7f6-60b8e7045d4e'::uuid),
        -- w2v (portal w2v-backend, shared by every w2v deploy)
        ('svc-w2v-shared',                 'e7330ae1-7464-49ff-8c99-43fb8a5cc523'::uuid),
        ('svc-w2v-tenant-dev',             'e7330ae1-7464-49ff-8c99-43fb8a5cc523'::uuid),
        ('svc-tts-synth',                  'e7330ae1-7464-49ff-8c99-43fb8a5cc523'::uuid),
        -- media-discovery (secure-apps svc-media-discovery)
        ('svc-media-discovery',            'c01849c0-f3ef-45bf-b2a6-869f6432443e'::uuid),
        -- ingestion (secure-apps svc-ingestion)
        ('svc-ingestion',                  '5c7b3997-9d14-4650-bf7f-e47b88b32974'::uuid),
        ('svc-ingestion-graph',            '5c7b3997-9d14-4650-bf7f-e47b88b32974'::uuid),
        -- scientific-processor (secure-apps svc-scientific-processor)
        ('svc-scientific-processor',       '3c68200a-7a0d-4114-89a9-9f5b3fe180ba'::uuid),
        ('svc-scientific-processor-graph', '3c68200a-7a0d-4114-89a9-9f5b3fe180ba'::uuid),
        -- karma (secure-apps svc-karma)
        ('svc-karma',                      '9cf23fa6-a564-427c-9e37-e4160d7811b9'::uuid),
        -- hirag (secure-apps svc-hirag)
        ('svc-hirag',                      'fd7e102c-6b1d-4a3d-9a87-248fb9081aa1'::uuid),
        ('svc-hirag-graph',                'fd7e102c-6b1d-4a3d-9a87-248fb9081aa1'::uuid),
        -- nlp-service (secure-apps svc-nlp-service)
        ('svc-nlp-service',                '91451911-fb08-4412-b905-6687aea1385c'::uuid),
        ('svc-nlp-graph',                  '91451911-fb08-4412-b905-6687aea1385c'::uuid),
        -- inference-router's OWN identity (its vllm hop) — secure-apps svc-inference-router
        ('svc-inference-router',           '98a72edd-06ee-408f-9f08-f88ce35897a5'::uuid),
        -- n8n (secure-apps svc-n8n): the AA dispatch credential of the n8n/n8n deploy
        ('svc-n8n-aa',                     '1a149521-7845-41fc-a468-1d6ce0dca096'::uuid),
        -- cdmsfirst-n8n (secure-apps svc-cdmsfirst-n8n)
        ('svc-cdmsfirst-n8n',              '06196f5b-8cda-44a5-bfbc-87e14247976a'::uuid),
        -- requirements (secure-apps requirements-service)
        ('svc-requirements',               '7b9b72f8-2ad9-4add-8e20-5b103882aee9'::uuid),
        ('svc-requirements-pm',            '7b9b72f8-2ad9-4add-8e20-5b103882aee9'::uuid)
       ) AS m(keycloak_sub, actor_sub)
 WHERE s.keycloak_sub = m.keycloak_sub
   AND s.actor_sub IS NULL;
