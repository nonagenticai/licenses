-- 031: a machine identity cannot "bring its own" API key — stop 34 service
-- tenants claiming it must.
--
-- WHAT IS WRONG. `tenants.byo_mode` answers "where does this tenant's LLM
-- inference come from", and worker_management enforces the answer. Measured on
-- prod 2026-09-05, the full (kind, byo_mode) matrix over all 76 rows:
--
--     customer  byo_required   4      internal  byo_required   4
--     customer  local_allowed  1      internal  local_allowed  8
--     customer  platform_only  1      internal  platform_only  6
--     service   byo_required  34  <-- THIS
--     service   local_allowed  9      test      byo_required   4
--                                     test      local_allowed  5
--
-- `byo_required` means, per models.py:890, "commercial: must bring their own
-- provider; worker_management hard-fails a run with no configured one". A
-- `kind='service'` row is not a commercial party. It is a machine identity — an
-- `svc-*` service principal minted by scripts/provision_service_token.py for one
-- of our own backends. There is no human behind it, no account, and no place for
-- it to have put an API key. Requiring it to bring one is not a strict policy; it
-- is a statement that cannot be satisfied, so the only reachable outcome is the
-- refusal.
--
-- The 9 service tenants already on `local_allowed` — svc-cdmsfirst-n8n,
-- svc-discovery-engine, svc-hirag, svc-kag-graph, svc-karma, svc-media-discovery,
-- svc-nlp-service, svc-scientific-processor, svc-tts-synth — show what the
-- correct value is, and are the reason this is a transcription rather than a
-- judgement call. 7 of those 9 were corrected out-of-band on 2026-07-23 with
-- exactly this reasoning; the note in scripts/backfill_tenants_byo_mode.py says
-- so in as many words: "byo_required would 402 byo_llm_not_configured ...
-- (verified live 2026-07-23)". That correction was applied to the 7 tenants that
-- were hurting at the time and never generalised to the other 34.
--
-- THE MEASURED CONSEQUENCE. `svc-kag` — in the list below — took a **402** on
-- POST /workers/kag_synthesize/message at 2026-09-03T04:16:53Z, 60 ms after the
-- BYO gate evaluated.
--
-- ⚠️ BLAST RADIUS IS SMALL TODAY, AND THAT IS NOT REASSURANCE. `byo_required`
-- reached the resolver ONCE in 7 days against 125,908 resolve calls. These 34
-- rows are a loaded gun, not an active fire: the refusal fires only on the paths
-- that actually reach worker_management's BYO gate, and most of these principals
-- have not exercised one yet. Every one of them will refuse the first time it
-- does.
--
-- ⚠️ THE REFUSAL IS INVISIBLE. worker_management raises this 402 at
-- src/api/message_endpoints.py:628 WITHOUT LOGGING. Grepping 7 days of logs for
-- `BYORequiredError` or `byo_llm_not_configured` returns 0 — that is a BLIND
-- result, not a negative one. The only signature that exists is the
-- `402 POST /workers/*/message` pair in the ingress access log. Do not read
-- "no refusals found" as "no refusals".
--
-- ============================ WHY local_allowed ============================
--
-- Of the three values, `local_allowed` is the one that describes these rows:
--   * `byo_required`  — must bring their own. Cannot be satisfied by a machine.
--   * `local_allowed` — "may use platform GPU OR BYO" (models.py:892). Permits
--                       the platform default AND still honours a configured
--                       external provider, so a service principal that DOES
--                       delegate (svc-kag-graph forwards X-Effective-Tenant-Id
--                       and resolves under the customer tenant) keeps working.
--   * `platform_only` — would LOCK these principals to platform inference and
--                       make WM ignore any stored external row. That is a
--                       narrowing, and it would break exactly the delegating
--                       case above. Not used here.
-- `local_allowed` is therefore both the correct value and the least destructive
-- of the two candidates. It is also, independently, what all 9 already-correct
-- siblings carry — zero counter-examples.
--
-- ⚠️ THIS FILE FIXES THE ACCUMULATED ROWS, NOT THE GENERATOR. The root cause is
-- upstream and is NOT addressed here:
--     models.py:896                    byo_mode server_default 'byo_required'
--     provision_service_token.py:243   ensure_tenant(..., kind="service")
-- The provisioning script passes `kind` but has no way to pass `byo_mode` —
-- `ensure_tenant` does not accept it — so every service principal is BORN
-- kind='service' + byo_mode='byo_required'. Until `ensure_tenant` takes a
-- `byo_mode` and applies it ON INSERT ONLY (exactly as it already does for
-- `kind`), the next `svc-*` tenant minted re-creates this state. This file
-- therefore REPORTS any such row instead of silently sweeping it — see the
-- RAISE WARNING below. Follow-up, deliberately not bundled into a data migration.
--
-- ============================== WHAT THIS DOES ==============================
--
-- Sets `byo_mode = 'local_allowed'` on the 34 rows named below BY ID.
--
-- BY ID, NOT BY NAME. Tenants get renamed in this corpus — migrations 024 and 026
-- both rename registry rows — so `name` is not a stable key across the window
-- between writing this file and running it. `id` is the primary key and is
-- immutable. (030 used names; it was written the same day it ran.)
--
-- EXPLICITLY, NOT BY PREDICATE. `WHERE kind='service' AND byo_mode='byo_required'`
-- would also sweep any service tenant created between 2026-09-05 and the moment
-- this runs. Those rows may well deserve the same fix, but sweeping them makes
-- the downgrade set unknowable and hides the generator defect. Same rule 027 and
-- 030 both state: name the rows, never use an else-branch.
--
-- SAFETY. A failed migration raises out of _run_db_bootstrap and the pod REFUSES
-- TO SERVE — and this service backs ext_authz for the whole platform at
-- replicas: 2. So:
--   * The UPDATE is guarded `AND byo_mode = 'byo_required'`. A re-run changes 0
--     rows, and an operator who has since set one of these to something else
--     keeps their value — the operator is the authority, not this file (027, 030).
--   * 'local_allowed' is a member of ck_tenants_byo_mode (migration 021), so the
--     write cannot violate a constraint.
--   * Drift from the 2026-09-05 measurement is a WARNING, never an EXCEPTION. A
--     NOTICE cannot crashloop ext_authz; a raise can.
--   * No schema change, no ALTER, no new constraint. See "NO CHECK CONSTRAINT".
--
-- NO CHECK CONSTRAINT, DELIBERATELY. A `CHECK (NOT (kind='service' AND
-- byo_mode='byo_required'))` is the obvious guard and is the wrong one:
--   1. It would break the live provisioning path. Because the server_default IS
--      'byo_required' and ensure_tenant cannot override it, the INSERT in
--      provision_service_token.py would violate the constraint on every future
--      service-token mint — turning a data-coherence problem into a hard write
--      failure on a path that runs against the ext_authz backing store.
--   2. It forbids a legitimate future state. An operator reclassifying a tenant
--      INTO kind='service' would have to set both columns in one statement or hit
--      an opaque constraint error; and a service principal fronting a single
--      customer's own key is a coherent thing to want.
--   3. `kind` is itself a backfilled classification (027), not a fact the writer
--      supplies. Constraining one backfilled column against another turns a
--      future re-classification into an outage.
-- The durable control is the generator fix described above, not a constraint that
-- fails closed on the wrong actor.
--
-- BASELINE INTERACTION (scripts/apply_migrations.py). On a FRESH database this
-- file NEVER EXECUTES — the runner stamps every .sql file as applied without
-- running it. That is correct and needs no ORM counterpart: this is DATA about 34
-- specific rows that exist on prod, not schema. A fresh database has neither the
-- rows nor the question. (Contrast 029, whose column must also be in models.py.)
--
-- DOWNGRADE: migrations/downgrades/031_service_tenants_are_not_byo.sql

DO $$
DECLARE
    n_fixed  bigint;
    n_leaked bigint;
    leaked   text;
BEGIN
    UPDATE tenants
       SET byo_mode = 'local_allowed'
     WHERE byo_mode = 'byo_required'
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
        'dd4eb320-59d8-5359-bb4a-5adb8bb77a01',  -- svc-image-e2e-customer (archived)
        'd6cffb03-db1b-58c6-94ba-e8a1d9f3c507',  -- svc-image-gateway
        '0e2bc289-1cd5-5d82-a100-e4073f501058',  -- svc-ingestion
        'ef9735e5-0c71-56e0-852b-3ca601177bb6',  -- svc-ingestion-graph
        '291c4cf5-fc88-56d5-9254-ac02869728a8',  -- svc-kag           <-- the measured 402
        '4dd78137-4f5f-531d-b58b-385bded3914c',  -- svc-kag-eval-r46 (archived)
        '17c61bc0-027b-5964-8d0f-e0e9d32c3cd0',  -- svc-kag-graph-reconciler
        'f172bf91-2dd5-5386-ae73-704f31f4f0a9',  -- svc-kag-karma
        '15fc5490-690b-5b60-9449-d42b9548386c',  -- svc-kag-orchestrator-graph
        'a2ce50eb-f0d7-54e5-8ffe-975aece729c5',  -- svc-modality-gpuproof-e2e (archived)
        '65a6416c-a372-5a00-b50e-6ab5193f4a6d',  -- svc-modality-gw-e2e (archived)
        '74c2fd20-da0c-57ba-9020-dd4dba736a63',  -- svc-modality-phaseb-e2e (archived)
        '92e8d32c-850a-52ff-997e-b6f5fcb94231',  -- svc-n8n-aa
        '4eeb06ef-1312-5bc2-8a60-0945d30519cb',  -- svc-nlp-graph
        'd535bf4c-062b-5108-b992-a685b6ad59a4',  -- svc-probe-gwflip-20260813 (archived)
        'eba3af2b-6d9b-5e24-8294-6eddca146d8f',  -- svc-probe-tenant-header-20260812 (archived)
        '0fbaaead-1752-54e2-843a-e4bbd946bb48',  -- svc-probe-tenant-header-20260812b (archived)
        '593bc1cc-eb77-55b7-b8a7-20c469077609',  -- svc-probe-vouch-20260813 (archived)
        '01a0138c-a6bc-75e7-9263-e3894f2eb94c',  -- svc-requirements
        'c9669d15-b85a-567f-b2dd-534ddad0c7c1',  -- svc-scientific-processor-graph
        'a3c1e697-6c05-5717-9612-f7fc8743d92d',  -- svc-vlm-smoke (archived)
        '4af92cd8-4d13-54f1-aaf9-94ee2caaf823'   -- svc-w2v-shared
       );
    GET DIAGNOSTICS n_fixed = ROW_COUNT;

    RAISE NOTICE '031: % service tenant(s) moved byo_required -> local_allowed', n_fixed;

    -- Loud but non-fatal. Fewer than 34 means the registry drifted since the
    -- 2026-09-05 measurement, OR this file has already run (a re-run is a no-op
    -- and reports 0, which is the expected second-run output).
    IF n_fixed <> 34 THEN
        RAISE WARNING '031: expected 34 rows, updated % — either already applied (0 is normal on a re-run) or the registry drifted since 2026-09-05',
            n_fixed;
    END IF;

    -- THE GENERATOR CHECK. Any kind='service' row still on byo_required after
    -- this file ran was created AFTER 2026-09-05 — i.e. minted by
    -- provision_service_token.py while ensure_tenant still cannot set byo_mode.
    -- This file deliberately does NOT sweep it (see header: name the rows, never
    -- an else-branch). It reports it, so the operator sees the generator is still
    -- producing incoherent rows rather than the symptom being silently mopped up.
    SELECT count(*), string_agg(name, ', ' ORDER BY name)
      INTO n_leaked, leaked
      FROM tenants
     WHERE kind = 'service' AND byo_mode = 'byo_required';

    IF n_leaked > 0 THEN
        RAISE WARNING '031: % service tenant(s) STILL byo_required and not named by this file (created since 2026-09-05): % — the generator is unfixed: ensure_tenant() cannot set byo_mode, so provision_service_token.py mints kind=service + byo_mode=byo_required. Fix upstream, do not widen this migration.',
            n_leaked, leaked;
    END IF;
END $$;

-- Verify. After this file, the service row should read 43 local_allowed / 0
-- byo_required — and every other kind must be UNCHANGED.
SELECT kind, byo_mode, count(*) AS tenants
FROM tenants
GROUP BY kind, byo_mode
ORDER BY kind, byo_mode;
