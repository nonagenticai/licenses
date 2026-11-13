-- 032: fill `subject.tenant_id` for the subjects whose parent the DATABASE ALREADY
-- ANSWERS — and deliberately leave the other 203 NULL.
--
-- WHERE THIS COMES FROM. Migration 029 added `subject.tenant_id` (nullable, no
-- default) and backfilled NOTHING, on the stated grounds that "infer the parent
-- from the licence rows" is the inverted inference the column exists to replace,
-- because that join returns a SET rather than a parent. That is true for SOME
-- subjects. It is not true for all of them, and 029's own test file pins the
-- no-op only for 029. Measured on prod 2026-09-05, `subject.tenant_id` is NULL on
-- all 254 rows — the column shipped and nothing has ever written to it.
--
-- The target model 029's header states, unchanged here:
--     tenant   = the org, the billable party, NEVER a machine
--     subject  = a real Keycloak human OR a service actor (`svc-*`)
--     licence  = (tenant, app, subject)
--
-- ======================== THE PARTITION, MEASURED (254) ========================
--
-- Every subject falls in exactly one of four buckets. Measured on prod
-- 2026-09-05 by `count(DISTINCT license.tenant_id)` per subject:
--
--     bucket                                              subjects   this file
--     -------------------------------------------------   --------   ---------
--     1  licences under EXACTLY ONE non-service tenant           51   SETS IT
--     4  licences under exactly one SERVICE tenant               41   leaves NULL
--     2  licences under MORE THAN ONE tenant (ambiguous)          8   leaves NULL
--     3  no licences at all                                     154   leaves NULL
--                                                              ----
--                                                               254
--
-- So this file writes 51 rows and refuses 203. That ratio is the finding, not a
-- shortfall: 154 of the 254 subjects have never held a licence, and nothing in
-- this database says who they belong to. Naming that in the header is cheaper
-- than the next reader re-deriving it.
--
-- ============================= THE ADMISSION RULE =============================
--
--     ⛔ SET `tenant_id` ONLY WHERE THE SUBJECT'S LICENCES NAME EXACTLY ONE
--        TENANT AND THAT TENANT IS NOT ITSELF A MACHINE. Anything else stays
--        NULL and goes to the operator.
--
-- Two independent halves, and both are needed:
--
--   (i) EXACTLY ONE. `count(DISTINCT tenant_id) = 1` over ALL of the subject's
--       licences, not just the active ones. Counting only active rows would
--       manufacture certainty: a subject with a revoked licence under tenant A
--       and an active one under tenant B reads as unambiguous while the database
--       plainly holds two different answers. Using all rows is the conservative
--       direction — it can only move a subject OUT of the backfill set, never in.
--
--  (ii) NOT A MACHINE. See "BUCKET 4" below. This is the half that is easy to
--       skip and expensive to get wrong.
--
-- ========================= BUCKET 4: THE 41 REFUSED =========================
--
-- 41 subjects are `svc-*` service actors whose single licence tenant is a
-- `kind='service'` tenant — 40 of them a tenant with the IDENTICAL NAME
-- (`svc-kag` the subject -> `svc-kag` the tenant), plus `svc-kag-svc-kag-graph`
-- -> `svc-kag-graph`. The derivation succeeds on these. THIS FILE REFUSES THEM
-- ANYWAY, and that refusal is the most important decision in it.
--
-- Writing `subject.tenant_id = <the svc-* tenant>` would be the exact opposite
-- of a repair. It would take the defect `docs/svc-tenant-migration-plan.md`
-- exists to undo — a machine sitting in the org table as a billable party — and
-- record it a SECOND time, in the very column added to express the corrected
-- shape. Worse, it would launder it: today "machine is a tenant" is legible as
-- an artefact of how `provision_service_token.py` mints rows; after such a
-- backfill it reads as a stated parentage, indistinguishable from one an
-- operator decided. Step 0 of that plan is an operator-supplied 43-row
-- `svc-name -> owning-tenant` table. This file does not have it, cannot derive
-- it, and must not pre-empt it with a self-reference.
--
-- ⚠️ DO NOT "COMPLETE" THIS FILE BY ADDING THOSE 41. They are not an oversight.
-- The correct parent for `svc-kag` is whatever org owns the KAG service, which is
-- a fact about the business that is not in this database.
--
-- ⚠️ NOTE WHAT IS *NOT* IN BUCKET 4. 17 of the 51 rows this file DOES set are
-- `svc-*` subjects too — `svc-workers-ahtapol`, `svc-kag-merqi`,
-- `svc-inference-router-prompt-improvement` and 14 more. Their licences already
-- sit under the REAL org tenant (`ahtapol`, `merqi`, `prompt-improvement`), which
-- is precisely the target model already in effect for them. The discriminator is
-- therefore NOT "is the subject named svc-*" — it is "is the resolved TENANT a
-- machine". A `name LIKE 'svc-%'` filter on the SUBJECT would have excluded all
-- 17 correct rows and included none of the 41 wrong ones. It is exactly backwards.
--
-- ========================= BUCKET 2: THE 8 AMBIGUOUS =========================
--
-- 8 subjects hold licences under more than one tenant. This file DOES NOT PICK,
-- by any rule — not most-licences, not most-recent, not "the non-test one":
--
--   * 3 portal humans spanning 5, 2 and 2 tenants (one of them `n8n-dev`,
--     `aa-agents`, `portal`, `portal-render-e2e` and `svc-w2v-shared` at once)
--   * 1 secure-apps human across 4 (`better-results-group`, `fr-test-user`,
--     `nonagentic`, `system`)
--   * `marketing-service` (marketing / nonagentic-gtm) and `sales-service`
--     (nonagentic-gtm / sales)
--   * `svc-kag-eval-r46` (fr-test-user / svc-kag-eval-r46) and `svc-n8n-aa`
--     (n8n-dev / svc-n8n-aa) — the two machine identities that hold BOTH a real
--     tenant's licence and their own machine tenant's. These are the shape the
--     svc-tenant plan is about, seen from the licence side.
--
-- A multi-tenant subject is a REAL state, not a data error: one human can be a
-- member of several orgs. `tenant_id` is a single column, so it cannot express
-- that, and the honest value for a question the schema cannot answer is NULL.
-- Guessing here would attribute a person to one org and silently un-attribute
-- them from the rest.
--
-- ======================= BUCKET 3: THE 154 WITHOUT LICENCES =======================
--
-- 154 subjects — 61% of the table — have never held a licence. There is no join
-- to derive from at all. Left NULL, necessarily.
--
-- ============================== WHAT THIS DOES ==============================
--
-- Sets `subject.tenant_id` on the 51 subjects named below BY ID, to the tenant
-- named BY ID.
--
-- BY ID, NOT BY NAME, on both sides. Tenants get renamed in this corpus (024 and
-- 026 both do it), so `tenants.name` is not stable across the window between
-- writing this file and running it. `subject.keycloak_sub` is stable but is not
-- the key. Both uuids are primary keys and are immutable; the names are carried
-- as comments so the list is reviewable.
--
-- EXPLICITLY, NOT BY PREDICATE. A `WHERE (SELECT count(DISTINCT ...)) = 1` would
-- re-run the derivation at APPLY time against whatever the registry looks like
-- then, sweeping every subject created between 2026-09-05 and the moment this
-- runs. Those rows may well deserve the same answer, but sweeping them makes the
-- downgrade set unknowable and hides the fact that they appeared. Same rule 027,
-- 030 and 031 all state: name the rows, never an else-branch. Rows that appear
-- later are REPORTED below, not swept.
--
-- SAFETY. A failed migration raises out of `_run_db_bootstrap` and the pod
-- REFUSES TO SERVE — this service backs ext_authz for the whole platform at
-- replicas: 2. So:
--   * The UPDATE is guarded `AND s.tenant_id IS NULL`. A re-run changes 0 rows,
--     and an operator's later correction is NEVER overwritten — once a value
--     exists the operator is the authority, not this file (027, 030, 031).
--   * Every tenant id below is an existing `tenants.id`, so `fk_subject_tenant`
--     (029, ON DELETE RESTRICT) cannot be violated. 6 of the 26 target tenants
--     are ARCHIVED (aa-tenant-proof, e2e-smoke-qe95guoq, e2e-smoke-xtzz3028,
--     fr-test, rmt-e2e, zz-s62-probe-a1). Archived is not a reason to withhold
--     parentage: the FK does not care, archiving blocks new provisioning rather
--     than existing rows, and a retired tenant still owns its historical
--     subjects. Same reasoning 030 applied to `subhankar-kag-2`.
--   * Drift from the 2026-09-05 measurement is a WARNING, never an EXCEPTION. A
--     NOTICE cannot crashloop ext_authz; a raise can.
--   * No schema change, no ALTER, no new constraint, no NOT NULL. The column
--     stays nullable and MUST: 203 rows are legitimately undecided, and making
--     it NOT NULL is only reachable after step 0 of the svc-tenant plan.
--
-- ⛔ WHAT THIS FILE DOES NOT DO. It sets no `kind`, no `cost_centre`, no
-- `byo_mode`; it moves no `tenants` row; it creates, deletes and re-points no
-- licence; it changes no uuid and no name. It does not execute any part of
-- `docs/svc-tenant-migration-plan.md`, whose step 0 (the operator's 43-row
-- svc-name -> owning-tenant table) remains BLOCKING and un-started.
--
-- WHAT READS THIS COLUMN TODAY: nothing gates on it. 029 added it so membership
-- becomes expressible; no authz, metering or pricing path branches on
-- `subject.tenant_id`. So a wrong value here cannot change an access decision
-- today — which is exactly why it must not be allowed to become a wrong value
-- that a later consumer starts trusting.
--
-- BASELINE INTERACTION (scripts/apply_migrations.py). On a FRESH database this
-- file NEVER EXECUTES — the runner stamps every .sql file as applied without
-- running it. That is CORRECT here and needs no ORM counterpart: this is DATA
-- about 51 specific rows that exist on prod, not schema. A fresh database has
-- neither the rows nor the question. (Contrast 029, whose COLUMN must also live
-- in models.py for exactly the opposite reason.)
--
-- VERIFIED 2026-09-05, NOT AGAINST PROD. Every claim above was measured against
-- the REAL 254 subject / 258 licence / 76 tenant rows, exported read-only from
-- prod into a throwaway postgres:18 container built from models.py's own schema:
--   * the upgrade set exactly 51 rows and reported exactly 41 / 8 / 154 in the
--     three refused buckets, with NO drift warning;
--   * a second run set 0 rows and fired only the already-applied warning;
--   * a subject inserted AFTER the measurement, derivable by the rule and not
--     named here, was REPORTED by the drift warning and NOT swept;
--   * the downgrade returned `subject` byte-identical to the snapshot it started
--     from (md5 e213011587447f1986e76b575e3b38fd both sides).
--
-- DOWNGRADE: migrations/downgrades/032_backfill_subject_tenant.sql

DO $$
DECLARE
    n_set        bigint;
    n_svc_parent bigint;
    n_ambiguous  bigint;
    n_no_licence bigint;
    n_leaked     bigint;
    leaked       text;
BEGIN
    UPDATE subject s
       SET tenant_id = v.tenant_id
      FROM (VALUES
        ('019ff6db-3654-7208-9c7f-ae551d37be64'::uuid, '0195f48b-d000-7000-8000-000000000003'::uuid),  -- 22560966-8ac0-4b56-af59-b646325d95d1        (secure-apps) -> aa-agents [internal]
        ('01a06ff3-8c55-703e-87bc-debf10f3cff0'::uuid, '0195f48b-d000-7000-8000-000000000003'::uuid),  -- svc-kag-aa-agents                           (service) -> aa-agents [internal]
        ('01a06ff1-e959-7534-bbb3-41b9dca71334'::uuid, '0195f48b-d000-7000-8000-000000000003'::uuid),  -- svc-workers-aa-agents                       (service) -> aa-agents [internal]
        ('019ed1d7-0306-739b-8197-f24b981477aa'::uuid, '0195f48b-d000-7000-8000-000000000003'::uuid),  -- tenant-0195f48b-d000-7000-8000-000000000003 (secure-apps) -> aa-agents [internal]
        ('019fa340-ae45-761a-9a7d-b42604f320b2'::uuid, '019fa340-a5e4-709d-8047-f555f063068a'::uuid),  -- 5836a145-1d1e-4890-a0fc-3cddeaf111cf        (portal) -> aa-tenant-proof [test]
        ('019ff0a5-9052-7315-8a37-c696dff12509'::uuid, '0195f48b-d000-7000-8000-000000000101'::uuid),  -- 2b08c2e6-a1b2-4eff-9f1c-4366e2636a9e        (portal) -> ahtapol [customer]
        ('019ff0a5-63db-772d-a2b7-8ae2cc802cdb'::uuid, '0195f48b-d000-7000-8000-000000000101'::uuid),  -- 4e0daa8a-252f-4203-a8aa-0f2b6a7d5e27        (portal) -> ahtapol [customer]
        ('019ff0a4-b4dd-7445-87db-986d16df5736'::uuid, '0195f48b-d000-7000-8000-000000000101'::uuid),  -- aad9fff9-fcd6-4a02-83f3-cf133f15d384        (portal) -> ahtapol [customer]
        ('01a06ff2-d1b6-70da-9ab5-ca95047b5627'::uuid, '0195f48b-d000-7000-8000-000000000101'::uuid),  -- svc-workers-ahtapol                         (service) -> ahtapol [customer]
        ('019e97ad-ee93-7609-bdc2-3a11a054b1c2'::uuid, '1d0090ac-adbf-4c07-b704-bc66061b572e'::uuid),  -- 1d0090ac-adbf-4c07-b704-bc66061b572e        (secure-apps) -> book-fulfillment [customer]
        ('019fe9ca-81cb-71dc-9da0-f7cd6e87d8d4'::uuid, '0195f48b-d000-7000-8000-000000000100'::uuid),  -- 9897e262-100a-4487-adc3-121b2705568d        (portal) -> cdmsfirst [customer]
        ('019fe700-4d80-70c7-8a9b-77c44eaeb65a'::uuid, '0195f48b-d000-7000-8000-000000000100'::uuid),  -- c98824db-e650-4429-a4f4-1bb3442c9fce        (portal) -> cdmsfirst [customer]
        ('01a06ff2-a74c-7039-b273-3e3975166dbe'::uuid, '0195f48b-d000-7000-8000-000000000100'::uuid),  -- svc-workers-cdmsfirst                       (service) -> cdmsfirst [customer]
        ('01a03dd9-a386-7731-b6a5-6fa01df183db'::uuid, '01a03dd9-941a-7300-ad03-f099dcf93864'::uuid),  -- 19141de3-ca06-4c04-84dc-d3ceb4b33ba4        (portal) -> claude [internal]
        ('019ec6e8-53e9-7522-a66f-11795b884625'::uuid, '00000000-0000-0000-0000-000000000000'::uuid),  -- 00000000-0000-0000-0000-000000000000        (secure-apps) -> default-anon [internal]
        ('019dcaf1-b4a3-7798-853d-2d9383aa98a5'::uuid, '019de842-c92b-7579-a7e6-5b79642d6c80'::uuid),  -- e2e-subject-qe95guoq                        (secure-apps) -> e2e-smoke-qe95guoq [test]
        ('019dcaf3-3f6e-762b-9073-6507b0d9ca20'::uuid, '019de842-c92b-75b0-9502-88a436832a67'::uuid),  -- e2e-subject-xtzz3028                        (secure-apps) -> e2e-smoke-xtzz3028 [test]
        ('019ed54e-a826-71da-ba73-b0036ca34c57'::uuid, '019efaa0-512b-733e-9e98-b1ac82a6f7ee'::uuid),  -- smoke-operator-3806d6e5                     (secure-apps) -> fr-test [test]
        ('019f5237-90cc-758d-9b5c-14853a85c156'::uuid, '0195f48b-d000-7000-8000-000000000099'::uuid),  -- 019ec527-8219-7564-99a5-81b181715630        (secure-apps) -> fr-test-user [test]
        ('01a04ce2-64d3-71b6-9a67-4d5b4ae5d2e6'::uuid, '0195f48b-d000-7000-8000-000000000099'::uuid),  -- svc-kag-eval                                (service) -> fr-test-user [test]
        ('01a06ff2-0d83-7074-85af-57d84495d5ad'::uuid, '0195f48b-d000-7000-8000-000000000099'::uuid),  -- svc-workers-fr-test-user                    (service) -> fr-test-user [test]
        ('01a06ff3-d969-75fe-bb31-af7511f33131'::uuid, '639c333a-1613-4fbc-aa14-a7b1430dc0db'::uuid),  -- svc-kag-merqi                               (service) -> merqi [internal]
        ('019f0929-c88f-71ea-989f-c86318267e3c'::uuid, '639c333a-1613-4fbc-aa14-a7b1430dc0db'::uuid),  -- tenant-merqi                                (secure-apps) -> merqi [internal]
        ('06992eff-2805-7dd5-accf-ffd5ad61614b'::uuid, '019de842-c92a-7e37-8fdd-eb0d8c5396f9'::uuid),  -- test-user                                   (secure-apps) -> n8n-dev [internal]
        ('09e52820-1a96-4ba0-8164-0f361f295324'::uuid, '0195f48b-d000-7000-8000-000000000001'::uuid),  -- c82077b7-08c2-46cd-886d-b0fa6a69f732        (portal) -> n8n-dev-portal-realm [internal]
        ('019ede27-b577-7640-a6f3-2b91ced833ec'::uuid, '0195f48b-d000-7000-8000-000000000001'::uuid),  -- f204a489-5cc5-41ac-82e7-4b0becc78827        (portal) -> n8n-dev-portal-realm [internal]
        ('01a06ff2-fcdf-72be-8ffd-881784ee4d82'::uuid, '0195f48b-d000-7000-8000-000000000001'::uuid),  -- svc-kag-n8n-dev-portal-realm                (service) -> n8n-dev-portal-realm [internal]
        ('01a06ff1-c225-722e-b482-8c98fa637886'::uuid, '0195f48b-d000-7000-8000-000000000001'::uuid),  -- svc-workers-n8n-dev-portal-realm            (service) -> n8n-dev-portal-realm [internal]
        ('019fa076-7e9e-776f-b99c-2137aae20282'::uuid, '019de842-c92b-752e-b39c-30d64950e1cc'::uuid),  -- 00000000-0000-7000-8000-0000deadbeef        (portal) -> n8n-test [test]
        ('9144c7dc-e9ef-491e-96b2-d3b69b11e21e'::uuid, '019de842-c92b-752e-b39c-30d64950e1cc'::uuid),  -- n8n-test-perpetual                          (secure-apps) -> n8n-test [test]
        ('01a06ff3-5ad9-746a-8786-c7340b7f8260'::uuid, 'e4f70a83-81ee-429a-abca-6999e3a0d24f'::uuid),  -- svc-kag-nonagentic-gtm                      (service) -> nonagentic-gtm [internal]
        ('019edc49-7c65-7602-8a72-6d185a75b0bf'::uuid, '019edc48-6060-76e9-8d8f-2e720e12c609'::uuid),  -- d71bc8cc-ad71-4836-8f75-32e29980a3aa        (secure-apps) -> ola-armatys [test]
        ('019edee5-03a6-77c1-bd30-f726e9cf0fa1'::uuid, '019edc48-6060-76e9-8d8f-2e720e12c609'::uuid),  -- tenant-019edc48-6060-76e9-8d8f-2e720e12c609 (secure-apps) -> ola-armatys [test]
        ('019dc19a-ab50-757c-bce5-2f4990b78ff9'::uuid, '0195f48b-d000-7000-8000-000000000002'::uuid),  -- 70640a77-153d-403b-95bc-0f3b3291b2ef        (portal) -> platform-core [internal]
        ('019fe83a-8b15-7661-a79a-3854ef09bc65'::uuid, 'e148e8af-3751-49bf-a0fa-56ab46d31ddc'::uuid),  -- 019fe83a-8590-7439-90ac-ce5a88800e4d        (secure-apps) -> portal-render-e2e [internal]
        ('019ede27-b71a-7516-b603-6ad9ce563e48'::uuid, '0195f48b-d000-7000-8000-000000000005'::uuid),  -- 155ffb93-e49a-4662-aa36-57c1f2385126        (portal) -> procedure-improvement [internal]
        ('01a06d0d-50b7-7238-8e44-e16b03f1ce89'::uuid, '0195f48b-d000-7000-8000-000000000005'::uuid),  -- svc-inference-router-procedure-improvement  (service) -> procedure-improvement [internal]
        ('01a06ff2-59e9-76aa-a6f5-71c6c546b931'::uuid, '0195f48b-d000-7000-8000-000000000005'::uuid),  -- svc-workers-procedure-improvement           (service) -> procedure-improvement [internal]
        ('019ede27-b69b-75c1-b029-d8a933c4266f'::uuid, '0195f48b-d000-7000-8000-000000000004'::uuid),  -- 4e2c7635-e1b9-498d-9c3e-b084838fa02a        (portal) -> prompt-improvement [internal]
        ('01a06d0d-0b0a-7791-88ac-5330f3eb703a'::uuid, '0195f48b-d000-7000-8000-000000000004'::uuid),  -- svc-inference-router-prompt-improvement     (service) -> prompt-improvement [internal]
        ('01a06ff2-3172-7126-9ce9-e226cb084059'::uuid, '0195f48b-d000-7000-8000-000000000004'::uuid),  -- svc-workers-prompt-improvement              (service) -> prompt-improvement [internal]
        ('019fef81-708b-73b1-9685-f48517a4b188'::uuid, '019fb1f4-7342-7bb7-8ac6-2ff2fcda645f'::uuid),  -- f66eb059-029f-40c3-957d-e059300d07ae        (secure-apps) -> rmt-e2e [test]
        ('019edc10-d1c4-71c4-a854-bdcea801ca0e'::uuid, '019edc1e-313d-779d-9330-fe013c705be8'::uuid),  -- 1e3b6058-192c-4939-9b67-898a9923b3fb        (secure-apps) -> subhankar-kag [customer]
        ('019ec6e8-5610-773e-b4da-20bc0d8aec27'::uuid, '00000000-0000-0000-0000-000000000001'::uuid),  -- 4a191861-b30e-430d-9660-975ae8dcdf52        (secure-apps) -> system [internal]
        ('01a06ff3-2a1a-7635-adcf-1bb14e0b6884'::uuid, '00000000-0000-0000-0000-000000000001'::uuid),  -- svc-kag-system                              (service) -> system [internal]
        ('01a06ff2-8057-7038-8f22-c4f9666be226'::uuid, '00000000-0000-0000-0000-000000000001'::uuid),  -- svc-workers-system                          (service) -> system [internal]
        ('019ed470-9b21-7232-a6a8-2cd08f027fe2'::uuid, '00000000-0000-0000-0000-000000000001'::uuid),  -- tenant-00000000-0000-0000-0000-000000000001 (secure-apps) -> system [internal]
        ('01a06ff3-b21d-71bc-9982-d4230b79d793'::uuid, '3806d6e5-4698-571e-9bb2-22ffd076ac73'::uuid),  -- svc-kag-tenant-dev                          (service) -> tenant-dev [internal]
        ('019ed9f6-3b58-776f-bf9a-8b4abef22112'::uuid, '3806d6e5-4698-571e-9bb2-22ffd076ac73'::uuid),  -- tenant-3806d6e5-4698-571e-9bb2-22ffd076ac73 (secure-apps) -> tenant-dev [internal]
        ('019fef99-e8bc-719a-934d-756389783fb3'::uuid, '019fef97-bc1a-74b1-89e6-5d0574210203'::uuid),  -- 019fef97-c061-73d4-a80c-29df91b3ffdd        (secure-apps) -> zz-s62-probe-a1 [test]
        ('019fef97-c061-73d4-a80c-29df91b3ffdd'::uuid, '019fef97-bc1a-74b1-89e6-5d0574210203'::uuid)   -- zz-s62-probe-subject                        (secure-apps) -> zz-s62-probe-a1 [test]
      ) AS v(subject_id, tenant_id)
     WHERE s.id = v.subject_id
       AND s.tenant_id IS NULL;
    GET DIAGNOSTICS n_set = ROW_COUNT;

    RAISE NOTICE '032: subject.tenant_id set on % subject(s)', n_set;

    -- Loud but non-fatal. Fewer than 51 means the registry drifted since the
    -- 2026-09-05 measurement, OR this file has already run (a re-run is a no-op
    -- and reports 0, which is the expected second-run output).
    IF n_set <> 51 THEN
        RAISE WARNING '032: expected 51 rows, updated % — either already applied (0 is normal on a re-run) or the registry drifted since 2026-09-05',
            n_set;
    END IF;

    -- ===================== THE THREE DELIBERATE NULL BUCKETS =====================
    -- Counted and reported, so "still NULL" is a stated OUTPUT of this file rather
    -- than something the next reader has to decide was intentional.

    -- Bucket 4 — one tenant, but that tenant IS a machine. Refused on purpose;
    -- the answer belongs to step 0 of docs/svc-tenant-migration-plan.md.
    SELECT count(*) INTO n_svc_parent
      FROM subject s
     WHERE s.tenant_id IS NULL
       AND (SELECT count(DISTINCT l.tenant_id) FROM license l WHERE l.subject_id = s.id) = 1
       AND EXISTS (
            SELECT 1 FROM license l JOIN tenants t ON t.id = l.tenant_id
             WHERE l.subject_id = s.id AND t.kind = 'service');

    -- Bucket 2 — the licences name more than one tenant. `tenant_id` is a single
    -- column and cannot hold the truth, so NULL is the honest value.
    SELECT count(*) INTO n_ambiguous
      FROM subject s
     WHERE s.tenant_id IS NULL
       AND (SELECT count(DISTINCT l.tenant_id) FROM license l WHERE l.subject_id = s.id) > 1;

    -- Bucket 3 — nothing to derive from.
    SELECT count(*) INTO n_no_licence
      FROM subject s
     WHERE s.tenant_id IS NULL
       AND NOT EXISTS (SELECT 1 FROM license l WHERE l.subject_id = s.id);

    RAISE NOTICE '032: left NULL on purpose — % machine-parented (svc-* tenant, operator step 0), % ambiguous (>1 tenant), % with no licence',
        n_svc_parent, n_ambiguous, n_no_licence;

    IF n_svc_parent <> 41 OR n_ambiguous <> 8 OR n_no_licence <> 154 THEN
        RAISE WARNING '032: expected 41/8/154 in the three refused buckets, found %/%/% — the registry has drifted since 2026-09-05',
            n_svc_parent, n_ambiguous, n_no_licence;
    END IF;

    -- ============================ THE DRIFT REPORT ============================
    -- A subject that IS derivable by this file's own rule (exactly one licence
    -- tenant, and that tenant is not a machine) but is NOT named above was
    -- created after 2026-09-05. This file deliberately does NOT sweep it — see
    -- the header: name the rows, never an else-branch. It reports it, so the
    -- operator sees the population moved rather than the movement being silently
    -- absorbed. The remedy is a new migration naming those rows, not widening
    -- this one.
    SELECT count(*), string_agg(s.keycloak_sub, ', ' ORDER BY s.keycloak_sub)
      INTO n_leaked, leaked
      FROM subject s
     WHERE s.tenant_id IS NULL
       AND (SELECT count(DISTINCT l.tenant_id) FROM license l WHERE l.subject_id = s.id) = 1
       AND NOT EXISTS (
            SELECT 1 FROM license l JOIN tenants t ON t.id = l.tenant_id
             WHERE l.subject_id = s.id AND t.kind = 'service');

    IF n_leaked > 0 THEN
        RAISE WARNING '032: % subject(s) are derivable but not named by this file (created since 2026-09-05): % — write a follow-up migration naming them; do not widen this one.',
            n_leaked, leaked;
    END IF;
END $$;

-- Verify. The NULL rows are a DELIBERATE output of this file, not a leftover.
SELECT CASE
           WHEN s.tenant_id IS NOT NULL                                              THEN 'parented by 032'
           WHEN NOT EXISTS (SELECT 1 FROM license l WHERE l.subject_id = s.id)       THEN 'NULL — no licence to derive from'
           WHEN (SELECT count(DISTINCT l.tenant_id)
                   FROM license l WHERE l.subject_id = s.id) > 1                     THEN 'NULL — ambiguous, >1 tenant'
           WHEN EXISTS (SELECT 1 FROM license l JOIN tenants t ON t.id = l.tenant_id
                         WHERE l.subject_id = s.id AND t.kind = 'service')           THEN 'NULL — machine tenant, operator step 0'
           ELSE                                                                           'NULL — derivable, appeared after 2026-09-05'
       END       AS outcome,
       count(*)  AS subjects
FROM subject s
GROUP BY outcome
ORDER BY outcome;
