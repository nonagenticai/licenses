-- 030: fill `tenants.cost_centre` for the rows where the answer is NOT a judgement
-- call — and DELIBERATELY LEAVE THE REST NULL.
--
-- WHERE THIS COMES FROM. Migration 027 added `cost_centre` and left it NULL for
-- every row, on the stated grounds that "inventing values before the apportionment
-- basis exists would bake in a guess that later reads as measurement". That basis
-- now exists: 55 of the 76 rows have since been classified by hand, and they use a
-- closed six-value vocabulary. Measured on prod 2026-09-05:
--
--     service      34   platform-core + 33 svc-* rows
--     customers     5   ahtapol, better-results-group, book-fulfillment,
--                       cdmsfirst, subhankar-kag
--     ci            4   n8n-dev, n8n-dev-portal-realm, portal-render-e2e, rmt-e2e
--     improvement   4   aa-agents, claude, procedure-improvement, prompt-improvement
--     merqi         4   marketing, merqi, nonagentic-gtm, sales
--     tests         4   fr-test-user, n8n-test, ola-armatys, tenant-dev
--     (NULL)       21
--
-- This file does NOT invent a vocabulary. It extends that measured one, and only
-- to rows where the already-classified population answers the question with ZERO
-- counter-examples. That is the whole admission rule, stated once:
--
--     ⛔ ASSIGN ONLY WHERE 100% OF ALREADY-CLASSIFIED ROWS SHARING THE
--        DISCRIMINATING FEATURE AGREE. Anything else stays NULL and goes to the
--        operator.
--
-- WHY THE BAR IS THAT HIGH. A `cost_centre` is a CHARGEBACK JOIN KEY. A guessed
-- value does not stay a guess: it is joined against, summed, and rendered as a
-- per-centre cost figure, at which point it is indistinguishable from a measured
-- one. NULL is legible as "undecided". A wrong bucket is not legible as anything.
-- This is the same failure 027's header describes — four sessions hand-deriving a
-- classification from name prefixes and disagreeing — and the fix is not a better
-- heuristic, it is refusing to answer where the data does not.
--
-- ============================ WHAT THIS FILE SETS (12) ============================
--
-- (a) 10 x `svc-%`  ->  'service'
--     Evidence: 33 of 43 svc-* rows are already classified and ALL 33 carry
--     'service'. Zero svc-* row carries any other value (verified: the
--     "cost_centre IS NOT NULL AND <> 'service'" query returns 0 rows). Migration
--     027 also states the precedence explicitly — "`svc-` prefix wins over every
--     other rule" — and applies it to `kind`, which is why every one of these 10
--     already reads kind='service'. This is not a heuristic; it is the same rule
--     already applied to their 33 siblings.
--     Rows: svc-image-e2e-customer, svc-kag-eval-r46, svc-modality-gpuproof-e2e,
--           svc-modality-gw-e2e, svc-modality-phaseb-e2e, svc-probe-gwflip-20260813,
--           svc-probe-tenant-header-20260812, svc-probe-tenant-header-20260812b,
--           svc-probe-vouch-20260813, svc-vlm-smoke
--     ⚠️ Note svc-image-e2e-customer: the name matches the service, test AND
--     customer greps at once — it is 027's own worked example of why prefixes are
--     not a partition. The `svc-` precedence rule is what settles it, and it
--     settles it as 'service'. Do not "correct" this to 'customers'.
--
-- (b) 1 x subhankar-kag-2  ->  'customers'
--     Evidence: TWO independent agreements. All 5 already-classified kind='customer'
--     rows carry 'customers', zero exceptions. And its exact name sibling
--     `subhankar-kag` — of which this is the `-2` duplicate — is one of them.
--     (It is archived. Archived is not a reason to leave it NULL: a retired tenant
--     still owns historical cost rows, which is precisely when a backfill over an
--     old window needs to be able to bucket it — the same reasoning
--     /internal/tenants uses for include_archived.)
--
-- (c) 1 x fr-test  ->  'tests'
--     Evidence: its name siblings `fr-test-user` (the fr browser-test user tenant,
--     same fr test identity pair) and `n8n-test` both carry 'tests'. No row named
--     `<product>-test*` carries anything else.
--
-- ======================= WHAT THIS FILE REFUSES TO SET (9) =======================
--
-- Each is left NULL with the reason, so the operator is answering a stated
-- question rather than re-deriving the whole problem:
--
--   * 5 x kind='internal': default-anon, george-dekker, nonagentic, portal, system
--     The classified internal rows are split across FIVE different values —
--     ci(3), improvement(4), merqi(4), service(1), tests(1). `kind='internal'`
--     therefore predicts nothing, and there is no 'internal' or 'platform' bucket
--     in the vocabulary at all, so any answer here MINTS A NEW VOCABULARY VALUE.
--     That is a decision about the chart of accounts, not a transcription.
--     ⚠️ `portal` carries 13 active licences and `system` 7 — these are the two
--     that most change a per-centre total, and the two most worth deciding.
--
--   * 2 x e2e-smoke-qe95guoq, e2e-smoke-xtzz3028 (kind='test')
--     kind='test' is SPLIT between 'ci' (rmt-e2e) and 'tests' (fr-test-user,
--     n8n-test, ola-armatys), so the kind does not decide it. Both classified
--     e2e-suffixed rows are 'ci', but these are `e2e-smoke-`PREFIXED disposable
--     fixtures with random suffixes, which is a different shape. Genuine coin-flip
--     between 'ci' and 'tests'.
--
--   * aa-tenant-proof (kind='test') — a proof fixture. No classified sibling of
--     any shape. Could be 'tests' or 'improvement' (the aa-* family: `aa-agents`
--     is 'improvement').
--
--   * zz-s62-probe-a1 (kind='test') — a probe. The other probes in the registry
--     are `svc-probe-*`, which take 'service' by the svc- rule; this one has no
--     svc- prefix, so that rule does not reach it.
--
--   To see the remainder at any time:
--     SELECT name, kind FROM tenants WHERE cost_centre IS NULL ORDER BY kind, name;
--
-- ⛔ WHAT THIS FILE DOES NOT DO. It sets no `kind`, moves no row, touches no
-- licence, and changes no uuid or name. `cost_centre` is read today only by
-- billing's reporting layer (`billing_service/src/services/tenant_names.py`), which
-- resolves a centre for display; nothing gates, meters or prices on it. So this is
-- a reporting improvement and cannot change an authz or billing outcome.
--
-- BASELINE INTERACTION (scripts/apply_migrations.py). On a FRESH database this
-- file NEVER EXECUTES — the runner stamps every .sql file as applied without
-- running it. That is CORRECT here and needs no ORM counterpart: this is DATA
-- about 12 specific rows that exist on prod, not schema. A fresh database has
-- neither the rows nor the question. (Contrast 029, whose column MUST also be in
-- models.py for exactly the opposite reason.)
--
-- SAFETY. A failed migration raises out of `_run_db_bootstrap` and the pod REFUSES
-- TO SERVE — this service backs ext_authz for the whole platform at replicas: 2.
-- So:
--   * Every UPDATE is guarded `WHERE cost_centre IS NULL`. A re-run is a no-op,
--     and — more importantly — an operator's later correction is NEVER overwritten.
--     Same principle as 027: once a value exists, the operator is the authority,
--     not this file.
--   * The rows are named EXPLICITLY, never "whatever is left". A tenant created
--     after 2026-09-05 must not be swept into a bucket by an else-branch.
--   * `cost_centre` is VARCHAR(64) and nullable; every value written here is a
--     known-good existing vocabulary member, so nothing can fail on length or a
--     constraint. There is no CHECK on this column and this file does not add one
--     — pinning the vocabulary while 9 rows are still undecided would force the
--     operator's hand.
--   * Counts are RAISEd, and a drift from the measured expectation is a WARNING,
--     never fatal. A NOTICE cannot crashloop ext_authz; a raise can.
--
-- DOWNGRADE: migrations/downgrades/030_backfill_tenant_cost_centre.sql

DO $$
DECLARE
    n_service   bigint;
    n_customers bigint;
    n_tests     bigint;
    n_remaining bigint;
BEGIN
    -- (a) Every remaining svc-* row. Guarded on NULL, so the 33 already-classified
    -- ones are not rewritten even though they hold the same value.
    UPDATE tenants SET cost_centre = 'service'
     WHERE cost_centre IS NULL
       AND name LIKE 'svc-%';
    GET DIAGNOSTICS n_service = ROW_COUNT;

    -- (b) The archived duplicate of an existing customer.
    UPDATE tenants SET cost_centre = 'customers'
     WHERE cost_centre IS NULL
       AND name = 'subhankar-kag-2';
    GET DIAGNOSTICS n_customers = ROW_COUNT;

    -- (c) The fr test tenant, alongside fr-test-user.
    UPDATE tenants SET cost_centre = 'tests'
     WHERE cost_centre IS NULL
       AND name = 'fr-test';
    GET DIAGNOSTICS n_tests = ROW_COUNT;

    SELECT count(*) INTO n_remaining FROM tenants WHERE cost_centre IS NULL;

    RAISE NOTICE '030: cost_centre set — % service, % customers, % tests (% left NULL for the operator)',
        n_service, n_customers, n_tests, n_remaining;

    -- Loud but non-fatal. A count that does not match means the registry drifted
    -- from the 2026-09-05 measurement (rows added, renamed, or classified by hand
    -- in the meantime). The operator must see it; it must not take authz down.
    IF n_service <> 10 OR n_customers <> 1 OR n_tests <> 1 THEN
        RAISE WARNING '030: expected 10/1/1 rows, updated %/%/% — registry has drifted since 2026-09-05',
            n_service, n_customers, n_tests;
    END IF;
    IF n_remaining <> 9 THEN
        RAISE WARNING '030: expected 9 rows left undecided, found % — list them: SELECT name, kind FROM tenants WHERE cost_centre IS NULL',
            n_remaining;
    END IF;
END $$;

-- Verify. The NULL bucket is a DELIBERATE output of this file, not a leftover.
SELECT COALESCE(cost_centre, '(undecided — operator)') AS cost_centre,
       count(*)                                        AS tenants,
       string_agg(name, ', ' ORDER BY name)            AS names
FROM tenants
GROUP BY cost_centre
ORDER BY cost_centre NULLS LAST;
