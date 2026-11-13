-- 035: REPORT every tenant id that is also a Keycloak principal id. Report only.
--
-- THE INVARIANT: no `tenants.id` may equal any `subject.keycloak_sub` (a
-- Keycloak principal's `sub` claim) nor any `subject.id` (this registry's own
-- primary key for that principal). A tenant is an ORGANISATION; a subject is an
-- identity that AUTHENTICATES within one.
--
-- ⚠️ "PRINCIPAL", NOT "PERSON". Of the four known offending ids only
-- `portal-render-e2e`'s subject is a human; `book-fulfillment`
-- (`service-account-book-fulfillment-svc`) and `nonagentic-gtm`
-- (`service-account-control-service-account`) are SERVICE ACCOUNTS in realm
-- `secure-apps` with no email. The defect is identical for a machine, and
-- calling it "a person" would make three of the four look like false positives.
--
-- WHAT IS WRONG, MEASURED ON PROD 2026-09-10.
-- `e148e8af-3751-49bf-a0fa-56ab46d31ddc` is a `tenants` row named
-- `portal-render-e2e` (kind='internal', cost_centre='ci', created
-- 2026-07-20 21:08Z) AND the `keycloak_sub` of subject `8382f0b3-…` in realm
-- `portal`. It drew 1,399 billing rows / $86.02 in 24h. The Keycloak user was
-- DELETED on 2026-08-27 and the billing did not stop, because it is sustained
-- by this registry row plus 5 licences, not by anything in Keycloak.
--
-- ⚠️ NOTHING DOWNSTREAM CAN CATCH IT. `billing_cost_events` has no FK to any
-- tenant table, both ids are bare `uuid` on the wire, and UUID VERSION IS NOT A
-- DISCRIMINATOR — `george-dekker`, `merqi` and `nonagentic-gtm` are genuine
-- tenants and all v4, exactly like a Keycloak sub. The cross-table predicate
-- below is the only authoritative test, and until this change nothing ran it.
--
-- THREE ROWS MATCH THIS FILE'S PREDICATE TODAY, and every one is EXPECTED:
--   e148e8af-3751-49bf-a0fa-56ab46d31ddc  portal-render-e2e  kind='internal'
--   1d0090ac-adbf-4c07-b704-bc66061b572e  book-fulfillment   kind='customer'  ⚠️
--   00000000-0000-0000-0000-000000000000  default-anon       DELIBERATE SENTINEL
--
-- ⛔ BUT THE PROBLEM IS FOUR ROWS, NOT THREE, AND THIS FILE CAN ONLY SEE THREE.
-- A SINGLE-SOURCE DETECTOR IS BLIND BY CONSTRUCTION. Measured 2026-09-10 across
-- both doors — the licences DB and live Keycloak — the union is:
--
--   id          name               kind      in subject.keycloak_sub  live in KC
--   e148e8af-…  portal-render-e2e  internal  YES                      NO (deleted
--                                                                     2026-08-27)
--   e4f70a83-…  nonagentic-gtm     internal  NO  (in_subject=0)       YES (SA)
--   1d0090ac-…  book-fulfillment   customer  YES                      YES (SA)
--   00000000-…  default-anon       internal  YES                      n/a sentinel
--
-- So `e4f70a83-81ee-429a-abca-6999e3a0d24f` / `nonagentic-gtm` — a LIVE Keycloak
-- principal registered as a tenant — was never written to `subject` and is
-- therefore INVISIBLE to every predicate in this file. Symmetrically, a
-- Keycloak-only check would miss `portal-render-e2e`, the row actually spending
-- the money, because a DELETED principal cannot be joined to. The second door is
-- not implemented because this service holds no Keycloak admin capability at all
-- (`auth/config.py`: only an outbound SA client-credentials grant; the
-- `client_id`/`client_secret` reads were removed in 2026-08 when their sole
-- consumer, `auth/validator.py`, was deleted in PR #140). The gap is made
-- machine-readable rather than left in prose:
-- `nonagentic_license_tenant_is_subject_source_measured{source="keycloak_principal"}`
-- reports 0, and that source is deliberately ABSENT from the violation counts —
-- a seeded zero would claim "measured, found none" about a door nobody opened.
--
-- ────────────────────────────────────────────────────────────────────────────
-- ⛔ WHY THIS FILE ADDS NO CONSTRAINT, AND WHY IT CANNOT FAIL.
-- ────────────────────────────────────────────────────────────────────────────
-- 1. A CHECK cannot express it. PostgreSQL requires a CHECK to be a function of
--    the row alone; this is a JOIN between `tenants` and `subject`.
-- 2. The cast is not optional either. `subject.keycloak_sub` is a STRING
--    (`TEXT` here in init.sql:28, `String(255)` in models.py:751 — they
--    disagree, noted, not fixed here) and `tenants.id` is `uuid`, so an uncast
--    comparison does not compile at all:
--        ERROR:  operator does not exist: text = uuid
--    Hence `t.id::text`, and `lower()` on both sides because a free-text column
--    may hold an upper-case sub while `uuid::text` renders lower-case.
-- 3. A TRIGGER — the only DB-level mechanism left — was rejected on a measured
--    fact. `models.PLATFORM_TENANTS` contains BOTH `e148e8af-…` and the
--    `00000000-…0000` sentinel, and `seed_platform_tenants()` re-INSERTs every
--    one of them on EVERY boot under `ON CONFLICT DO NOTHING`. A BEFORE INSERT
--    row trigger fires BEFORE conflict arbitration, so a trigger that raised on
--    those ids would raise on every boot of the service that backs ext_authz
--    for the whole platform at replicas: 2. There is also not one CREATE
--    TRIGGER anywhere in this repo, and `create_all` would not reproduce one on
--    a fresh database.
-- 4. So this file RAISES NOTICE/WARNING and never EXCEPTION. Three rows violate
--    today; a migration that failed on them would put both replicas of the
--    platform auth path into CrashLoopBackOff over a billing-hygiene defect —
--    the exact fail-soft rule `seed_platform_tenants` states for itself. Unlike
--    033, whose CHECKs had zero violating rows to land on, there is nothing
--    here that can be made safely fatal.
--
-- WHERE THE ENFORCEMENT ACTUALLY LIVES, since it is not here:
--   * WRITE TIME — `DatabaseManager.ensure_tenant` refuses a NEW registration of
--     a subject id with `ClientSafeError` (models.py). Same shape as the
--     kind='service'/byo_mode coupling guard that migration 033 relies on.
--   * BOOT TIME — `services.tenant_subject_invariant.report_at_boot`, called
--     from `_run_db_bootstrap`. Logs each row at ERROR, never raises.
--   * CONTINUOUSLY — `nonagentic_license_tenant_is_subject_rows{class,match,source}`,
--     the only one of the three that keeps measuring after this ships. The
--     `class` label splits the deliberate `default-anon` sentinel off from the
--     real violations so the actionable series CAN reach 0; the predicate is
--     NOT loosened to achieve that — the sentinel still matches, still counts,
--     still appears in its own series.
--
-- ⚠️ A CLASS THIS FILE CANNOT SEE. A `subject.id` can be used as a tenant_id by
-- a DOWNSTREAM service without ever being registered here — measured:
-- `8382f0b3-c082-4f97-8d47-120ce5a18e05` carries 61 `billing.token_budgets`
-- rows, is a `subject.id`, is NOT a `keycloak_sub`, and is NOT in `tenants` at
-- all. No predicate rooted in `tenants` can match a row that does not exist,
-- and `token_budgets` lives in the billing service's database, which this
-- service has no connection to. That detector has to be written where the data
-- is. This file covers what IS in `tenants`, both classes of it.
--
-- BASELINE INTERACTION (scripts/apply_migrations.py). On a FRESH database this
-- file is stamped applied without running, like every other. Nothing is lost:
-- it creates no schema object, and the boot-time report + the gauge run on
-- every database regardless of how its schema was built.
--
-- DOWNGRADE: migrations/downgrades/035_tenant_id_must_not_be_a_subject.sql

DO $$
DECLARE
    r           record;
    n_violation bigint := 0;
    n_sentinel  bigint := 0;
BEGIN
    FOR r IN
        SELECT t.id::text       AS tenant_id,
               t.name           AS tenant_name,
               t.kind           AS tenant_kind,
               s.id::text       AS subject_id,
               s.keycloak_realm AS realm,
               'keycloak_sub'   AS match_kind
          FROM tenants t
          JOIN subject s ON lower(s.keycloak_sub) = lower(t.id::text)
        UNION ALL
        SELECT t.id::text       AS tenant_id,
               t.name           AS tenant_name,
               t.kind           AS tenant_kind,
               s.id::text       AS subject_id,
               s.keycloak_realm AS realm,
               'subject_id'     AS match_kind
          FROM tenants t
          JOIN subject s ON lower(s.id::text) = lower(t.id::text)
         ORDER BY 6, 2
    LOOP
        -- The sentinel is named LITERALLY and only here. It is not filtered out
        -- of the predicate: it still matches and is still reported — it is only
        -- classified, so the actionable count can legitimately reach zero.
        IF r.tenant_id = '00000000-0000-0000-0000-000000000000' THEN
            n_sentinel := n_sentinel + 1;
            RAISE NOTICE
                '035: sentinel (deliberate, permanent): tenants.id=% name=% is also '
                'subject.% (subject.id=%, realm=%)',
                r.tenant_id, r.tenant_name, r.match_kind, r.subject_id, r.realm;
        ELSE
            n_violation := n_violation + 1;
            RAISE WARNING
                '035: TENANT IS A KEYCLOAK PRINCIPAL: tenants.id=% name=% kind=% is '
                'also subject.% (subject.id=%, realm=%). An identity that '
                'authenticates is registered as the thing that pays, and nothing '
                'downstream can detect it.',
                r.tenant_id, r.tenant_name, r.tenant_kind, r.match_kind,
                r.subject_id, r.realm;
        END IF;
    END LOOP;

    -- ⛔ Announced on EVERY apply, including a clean one — a clean reading is
    -- exactly when a reader is most likely to conclude "no problem".
    RAISE WARNING
        '035: BLIND SPOT — this file evaluates only the licences-DB door and '
        'cannot see e4f70a83-81ee-429a-abca-6999e3a0d24f (nonagentic-gtm, '
        'service-account-control-service-account, realm secure-apps): a LIVE '
        'Keycloak principal registered as a tenant that was never written to '
        '`subject`. Measured 2026-09-10.';

    IF n_violation = 0 THEN
        RAISE NOTICE
            '035: no tenant id in this database is a recorded subject identity '
            '(% sentinel exempt) — see the BLIND SPOT warning above before reading '
            'that as "clean"',
            n_sentinel;
    ELSE
        RAISE WARNING
            '035: % tenant row(s) are also recorded subject identities (+% '
            'deliberate sentinel; the licences-DB door only). REPORTED, NOT FATAL — '
            'see this file''s header for why a '
            'constraint cannot hold this and where the write-time guard lives. '
            'Watch nonagentic_license_tenant_is_subject_rows{class="violation"}.',
            n_violation, n_sentinel;
    END IF;
END $$;

-- Verify. Both classes, one row per offending identity. Expected on prod at the
-- time of writing: three `keycloak_sub` rows, zero `subject_id` rows.
SELECT t.id::text AS tenant_id, t.name AS tenant_name, t.kind, 'keycloak_sub' AS match_kind
  FROM tenants t JOIN subject s ON lower(s.keycloak_sub) = lower(t.id::text)
UNION ALL
SELECT t.id::text, t.name, t.kind, 'subject_id'
  FROM tenants t JOIN subject s ON lower(s.id::text) = lower(t.id::text)
 ORDER BY 4, 2;
