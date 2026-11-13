-- 027: give `tenants` a `kind` and a `cost_centre`, so "who is this row FOR" is DATA.
--
-- WHY. The registry holds 75 tenant rows and the question "which of these are
-- customers?" has no answer in the schema. It has been re-derived by hand, from
-- name prefixes, in at least four sessions, and the derivations disagreed. Name
-- prefixes are not a partition: `svc-image-e2e-customer`, `svc-modality-gw-e2e`
-- and `svc-probe-*` match BOTH the service filter and the test filter, so any
-- two people classifying by grep get two different answers.
--
-- It matters commercially. Measured on prod over the 30 days to 2026-09-02,
-- billable spend by tenant:
--     n8n-dev-portal-realm    $2,602.67   internal dev
--     n8n-dev                   $908.51   internal dev
--     portal-render-e2e         $748.23   e2e test loop
--     george-dev-portal-realm   $200.91   internal dev
--     fr-test-user              $189.08   test
--     platform-core              $54.05   internal
--     ahtapol + cdmsfirst + merqi + nonagentic-gtm, COMBINED   $4.33
-- i.e. ~99.9% of measured spend is dev/test/e2e, not customers. Nothing in the
-- data said so; it had to be reconstructed by joining two databases by hand.
--
-- THE FIVE VALUES:
--   customer     an external party we sell to
--   internal     our own staff, product and platform tenants
--   test         e2e loops, probes, fixtures -- disposable by design
--   service      a service-mesh auth principal, NOT a tenant in the commercial
--                sense; these should be excluded from any tenant inventory
--   unclassified not yet decided
--
-- PRECEDENCE, stated once so it cannot drift: `svc-` prefix wins over every
-- other rule. `svc-image-e2e-customer` is a SERVICE principal that happens to
-- exercise an e2e path; it is not a customer and not a test tenant. Classifying
-- it any other way is what made the overlapping filters produce contradictory
-- counts before.
--
-- DEFAULT IS `unclassified`, DELIBERATELY -- fail closed. A newly registered
-- tenant must not be silently counted as a customer (inflating revenue) or as
-- internal (hiding cost). It shows up as undecided until somebody decides.
--
-- WHY NOT `archived_at` FOR THE svc-* ROWS: archiving blocks provisioning
-- (models.py list filter `archived_at.is_(None)`), and these principals are
-- load-bearing -- 58 lines across 27 gitops files pin their subject ids in
-- *_ONBEHALF_SUBJECTS allowlists. Reclassifying changes no name and no UUID, so
-- every one of those lines stays byte-identical. That is the whole point.
--
-- `cost_centre` is left NULL here on purpose. It is the join key for CI and
-- infrastructure chargeback, and inventing values before the apportionment basis
-- exists would bake in a guess that later reads as measurement.
--
-- BASELINE INTERACTION (scripts/apply_migrations.py). On a database with the
-- tables but no stamps, apply_all() stamps every current file WITHOUT running
-- it, so on a FRESH database this file never executes: the columns arrive from
-- create_all (models.py Tenant.kind/cost_centre + ck_tenants_kind) and stay at
-- the default. This file is for databases that already exist.
--
-- SAFETY. A failed migration raises out of _run_db_bootstrap and the pod REFUSES
-- TO SERVE -- and this service backs ext_authz for the whole platform at
-- replicas: 2. So: ONE DO block (columns, backfill and constraint land together
-- or not at all), guarded on the column so a re-run is a no-op, and no statement
-- that can fail on data -- every row is assigned a value before the CHECK is
-- added. A re-run deliberately does NOT re-assert the backfill: once the column
-- exists, an operator's later correction is the authority, not this file.

DO $$
DECLARE
    n_customer     bigint;
    n_internal     bigint;
    n_test         bigint;
    n_service      bigint;
    n_unclassified bigint;
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'tenants' AND column_name = 'kind'
    ) THEN
        RAISE NOTICE 'tenants.kind already present — nothing to do';
        RETURN;
    END IF;

    -- VARCHAR(16)/VARCHAR(64), not text: models.py declares String(16)/String(64)
    -- and create_all builds a fresh database from that. An existing database and
    -- a fresh one must end up with the SAME column types, or the two paths
    -- quietly diverge — the drift class 021's header warns about.
    ALTER TABLE tenants
        ADD COLUMN kind VARCHAR(16) NOT NULL DEFAULT 'unclassified';
    ALTER TABLE tenants
        ADD COLUMN cost_centre VARCHAR(64);

    -- SERVICE FIRST. This runs before every other rule so the `svc-` prefix wins
    -- the overlap, per the precedence note above.
    UPDATE tenants SET kind = 'service' WHERE name LIKE 'svc-%';

    UPDATE tenants SET kind = 'test'
    WHERE kind = 'unclassified'
      AND name IN (
        'aa-tenant-proof',
        'e2e-smoke-qe95guoq',
        'e2e-smoke-xtzz3028',
        'fr-test',
        'fr-test-user',
        'n8n-test',
        'portal-render-e2e',
        'rmt-e2e',
        'tenant-dev',
        'zz-s62-probe-a1'
      );

    UPDATE tenants SET kind = 'internal'
    WHERE kind = 'unclassified'
      AND name IN (
        'claude',
        'default-anon',
        'george-dekker',
        'george-dev-portal-realm',
        'marketing',
        'n8n-dev',
        'n8n-dev-portal-realm',
        'nonagentic',
        'nonagentic-gtm',
        'platform-core',
        'portal',
        'procedure-improvement',
        'prompt-improvement',
        'sales',
        'system'
      );

    -- Listed explicitly rather than "whatever is left". A tenant nobody has
    -- classified must land in `unclassified`, not be promoted to `customer` by
    -- an else-branch — over-counting revenue is the failure this column exists
    -- to prevent.
    UPDATE tenants SET kind = 'customer'
    WHERE kind = 'unclassified'
      AND name IN (
        'ahtapol',
        'better-results-group',
        'book-fulfillment',
        'cdmsfirst',
        'merqi',
        'ola-armatys',
        'subhankar-kag',
        'subhankar-kag-2'
      );

    ALTER TABLE tenants
        ADD CONSTRAINT ck_tenants_kind
        CHECK (kind IN ('customer', 'internal', 'service', 'test', 'unclassified'));

    -- Mirrors Index("ix_tenants_kind") in models.py. Without this line an
    -- EXISTING database gets the column and the constraint but no index, while a
    -- fresh create_all'd one has all three -- the exact ORM/corpus divergence the
    -- schema-parity gate exists to catch, and it did catch it here.
    CREATE INDEX ix_tenants_kind ON tenants (kind);

    SELECT count(*) FILTER (WHERE kind = 'customer'),
           count(*) FILTER (WHERE kind = 'internal'),
           count(*) FILTER (WHERE kind = 'test'),
           count(*) FILTER (WHERE kind = 'service'),
           count(*) FILTER (WHERE kind = 'unclassified')
      INTO n_customer, n_internal, n_test, n_service, n_unclassified
      FROM tenants;

    RAISE NOTICE 'tenants.kind added: % customer, % internal, % test, % service, % unclassified',
        n_customer, n_internal, n_test, n_service, n_unclassified;

    -- Loud but non-fatal. A name that matched nothing means the registry drifted
    -- from these lists (renamed, or created since 2026-09-02). The operator must
    -- see it, but a NOTICE cannot crashloop ext_authz for the whole platform.
    IF n_unclassified <> 0 THEN
        RAISE WARNING '% tenant(s) left unclassified — classify them: SELECT name FROM tenants WHERE kind = ''unclassified''',
            n_unclassified;
    END IF;
    IF n_service <> 42 THEN
        RAISE WARNING 'expected 42 svc-* tenants, found % — registry has drifted', n_service;
    END IF;
END $$;

-- Verify
SELECT kind, count(*) AS tenants, string_agg(name, ', ' ORDER BY name) AS names
FROM tenants
WHERE kind <> 'service'
GROUP BY kind
ORDER BY kind;
