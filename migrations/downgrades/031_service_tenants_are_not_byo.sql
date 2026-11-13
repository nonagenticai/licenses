-- Downgrade 031. Returns the 34 rows this file set back to 'byo_required'.
--
-- EXACT. Every one of the 34 held 'byo_required' before 031 ran — that is the
-- predicate 031 selected them with, measured on prod 2026-09-05 — so restoring
-- that single value restores the exact prior state, byte for byte. There is no
-- per-row prior value to remember.
--
-- SCOPED TWICE, deliberately, so a downgrade cannot erase somebody else's work:
--   * by ID — only the 34 rows 031 names, never "every service tenant". The 9
--     service tenants that were ALREADY 'local_allowed' before 031
--     (svc-cdmsfirst-n8n, svc-discovery-engine, svc-hirag, svc-kag-graph,
--     svc-karma, svc-media-discovery, svc-nlp-service, svc-scientific-processor,
--     svc-tts-synth) are untouched by construction: 031 never owned them, and 7
--     of them were corrected by hand on 2026-07-23. A predicate like
--     `WHERE kind='service'` would silently revert that correction too, and
--     re-arm the 402 it was made to stop.
--   * by VALUE — only where the row still holds 'local_allowed', the value 031
--     wrote. If an operator has since moved one of these to 'platform_only',
--     that is THEIR value, not 031's, and this file leaves it alone.
--
-- ⚠️ WHAT DOWNGRADING COSTS. This re-arms the refusal. Every id below is a
-- machine identity with no way to hold an API key, so 'byo_required' makes
-- worker_management 402 `byo_llm_not_configured` on the first call each one makes
-- to a BYO-gated path — and that 402 is raised WITHOUT LOGGING
-- (worker_management/src/api/message_endpoints.py:628), so the damage is not
-- greppable. Only roll back if 031 itself is the problem.
--
-- IDs, not names: tenants get renamed in this corpus (024, 026); the primary key
-- does not change.

UPDATE tenants
   SET byo_mode = 'byo_required'
 WHERE byo_mode = 'local_allowed'
   AND id IN (
     'e32db873-6197-5cbe-8d6f-f800ff015b37',  -- svc-coding-ai
     '3533c51e-19e6-5a29-a636-7269f941a256',  -- svc-coding-analysis
     '71189c26-d5c2-50ef-94b6-719db4af5f9f',  -- svc-coding-containers
     'e7b1d902-5bae-5bd8-9fe2-1c6ff29b7b63',  -- svc-coding-gateway
     '83ecc1ad-54bd-5de7-9b56-0f1707dcba8e',  -- svc-coding-github
     '41020e1b-8ff6-54dd-bb36-725da5046e5c',  -- svc-coding-realtime
     '91c77f12-c7fa-5ab1-bfd8-25f8e1874315',  -- svc-coding-repo-jobs
     '6aeb0926-6ffc-59ce-b668-59eeb318de78',  -- svc-coding-security
     '526378a8-0b20-5784-b5ce-d0fdc7a42d2b',  -- svc-coding-testing
     'd4804d48-2761-50d7-9302-fb366fbcad1c',  -- svc-fr-backend
     'dfb9ca84-05f9-510b-93ae-b8dda21ebd4a',  -- svc-fr-backend-kag
     'c361d84f-8a42-5dfa-aaa9-0339658f22ae',  -- svc-hirag-graph
     'dd4eb320-59d8-5359-bb4a-5adb8bb77a01',  -- svc-image-e2e-customer
     'd6cffb03-db1b-58c6-94ba-e8a1d9f3c507',  -- svc-image-gateway
     '0e2bc289-1cd5-5d82-a100-e4073f501058',  -- svc-ingestion
     'ef9735e5-0c71-56e0-852b-3ca601177bb6',  -- svc-ingestion-graph
     '291c4cf5-fc88-56d5-9254-ac02869728a8',  -- svc-kag
     '4dd78137-4f5f-531d-b58b-385bded3914c',  -- svc-kag-eval-r46
     '17c61bc0-027b-5964-8d0f-e0e9d32c3cd0',  -- svc-kag-graph-reconciler
     'f172bf91-2dd5-5386-ae73-704f31f4f0a9',  -- svc-kag-karma
     '15fc5490-690b-5b60-9449-d42b9548386c',  -- svc-kag-orchestrator-graph
     'a2ce50eb-f0d7-54e5-8ffe-975aece729c5',  -- svc-modality-gpuproof-e2e
     '65a6416c-a372-5a00-b50e-6ab5193f4a6d',  -- svc-modality-gw-e2e
     '74c2fd20-da0c-57ba-9020-dd4dba736a63',  -- svc-modality-phaseb-e2e
     '92e8d32c-850a-52ff-997e-b6f5fcb94231',  -- svc-n8n-aa
     '4eeb06ef-1312-5bc2-8a60-0945d30519cb',  -- svc-nlp-graph
     'd535bf4c-062b-5108-b992-a685b6ad59a4',  -- svc-probe-gwflip-20260813
     'eba3af2b-6d9b-5e24-8294-6eddca146d8f',  -- svc-probe-tenant-header-20260812
     '0fbaaead-1752-54e2-843a-e4bbd946bb48',  -- svc-probe-tenant-header-20260812b
     '593bc1cc-eb77-55b7-b8a7-20c469077609',  -- svc-probe-vouch-20260813
     '01a0138c-a6bc-75e7-9263-e3894f2eb94c',  -- svc-requirements
     'c9669d15-b85a-567f-b2dd-534ddad0c7c1',  -- svc-scientific-processor-graph
     'a3c1e697-6c05-5717-9612-f7fc8743d92d',  -- svc-vlm-smoke
     '4af92cd8-4d13-54f1-aaf9-94ee2caaf823'   -- svc-w2v-shared
   );
