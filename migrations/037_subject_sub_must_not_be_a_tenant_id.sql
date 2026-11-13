-- 037: REPORT the SECOND door onto the tenant/subject invariant. Report only.
--
-- 035 states the invariant: no `tenants.id` may equal any `subject.keycloak_sub`
-- nor any `subject.id`. This file does not change it. It reports the SAME
-- invariant from the other side, because 035 and every guard built on it ask
-- only one of the two questions the invariant implies:
--
--   DOOR 1 (guarded since #191/035)  "is this NEW TENANT id already a principal?"
--                                    — ensure_tenant, seed_platform_tenants,
--                                      scripts/backfill_tenants_byo_mode.
--   DOOR 2 (UNGUARDED until 2026-09-23)
--                                    "is this NEW SUBJECT's keycloak_sub already
--                                      a registered tenant id?"
--                                    — DatabaseManager.create_or_get_subject.
--
-- A collision created through door 2 is INDISTINGUISHABLE from one created
-- through door 1: same two tables, same join, same `tenants` × `subject` row
-- pair, same `nonagentic_license_tenant_is_subject_rows{class="violation"}`. But
-- no tenant row is inserted, so nothing on the tenant side can fire, and until
-- this change nothing refused it, logged it or counted it.
--
-- ⚠️ THE UNGUARDED DOOR WAS THE LESS PRIVILEGED ONE. `ensure_tenant` is reached
-- from `PUT /tenants/{id}` (`license-admin`). `create_or_get_subject` is reached
-- from `POST /licenses/user/{subject_id}` — where the sub is a caller-supplied
-- PATH PARAMETER — under `license-admin` OR `license-manager`, and from five
-- `scripts/provision_*` entry points via `--owner-sub` / `--subject`.
--
-- ────────────────────────────────────────────────────────────────────────────
-- ⭐ WHY THIS IS THE DOOR THAT MATTERED, MEASURED ON PROD 2026-09-23
-- ────────────────────────────────────────────────────────────────────────────
--   * 85 tenants. 82 of them have NO `subject` row carrying their id as a
--     `keycloak_sub`. Each of those 82 was ONE `create_or_get_subject` call away
--     from becoming a new `class="violation"` row, with nothing in its way.
--   * Among the 82 is `e4f70a83-81ee-429a-abca-6999e3a0d24f` `nonagentic-gtm`
--     (kind='internal', cost_centre='merqi', 5 licences, 3 tokens) — the KNOWN
--     FOURTH OFFENDER. It is a LIVE `secure-apps` service-account principal
--     already registered as a tenant, and 035 and the gauge are blind to it for
--     exactly one reason: it was never written to `subject` (measured: 0 rows).
--
-- ⛔ SO THE GAUGE'S BLIND SPOT AND THE UNGUARDED DOOR ARE ONE HOLE SEEN FROM TWO
-- SIDES. The single event that would make `nonagentic-gtm` VISIBLE to the gauge
-- — minting a licence for that principal, which writes the `subject` row — is
-- precisely the event nothing was refusing. The detector would have announced
-- the violation at the moment it became unfixable in silence, having been unable
-- to prevent it. That is now a REFUSAL at `create_or_get_subject`, counted by
-- `nonagentic_license_subject_principal_refusals`.
--
-- ⛔ THE REFUSAL IS NARROW, AND THIS REPORT MUST NOT BE READ AS "DOOR 2 IS SHUT".
-- It refuses only a GENUINELY NEW principal-as-subject registration. Two shapes
-- are ADMITTED (logged at ERROR, left to the gauge, NOT counted as refusals):
--   * the tenant already holds licences — `e4f70a83-…` nonagentic-gtm holds 5 and
--     has NO `subject` row, so an unnarrowed guard would refuse its very next
--     mint, and the first mint of every identity in that shape after a DR rebuild;
--   * the documented self-tenant mint (`POST /licenses/user/{id}` with
--     `X-Tenant-Id: {id}`), which `test_guard_does_not_fire_for_real_self_tenant`
--     pins at 201 for `e148e8af-…` portal-render-e2e.
-- So the count below is the population door 2 could still convert MINUS nothing:
-- an already-trading tenant in that list can still gain a `subject` row. Neither
-- exemption is a list of ids — both are derived from live state on every call.
--
-- ⚠️ This does NOT resolve `LicenseTenantIdIsKeycloakPrincipal`, and must not be
-- read as doing so. The gauge still reads 2 violations + 1 sentinel. It closes
-- the door through which it would have read 3, 4 or more.
--
-- ────────────────────────────────────────────────────────────────────────────
-- ⛔ WHY NO DATA IS REPAIRED HERE. READ THIS BEFORE "FINISHING THE JOB".
-- ────────────────────────────────────────────────────────────────────────────
-- The obvious repair — re-key the offending tenants onto fresh organisation ids
-- — is NOT a hygiene fix. It is a decision about who pays, and it is not this
-- file's to make. Measured on prod 2026-09-23:
--
--   tenant id    name               licences  tokens  subjects whose org it is
--   e148e8af-…   portal-render-e2e     6         5      1
--   1d0090ac-…   book-fulfillment      1         1      1  (its OWN principal)
--   e4f70a83-…   nonagentic-gtm        5         3      1
--   00000000-…   default-anon          1         1      — sentinel, never touch
--
-- Every one of those ids is a LIVE serving identity, and `tenants.id` is the
-- value the rest of the fleet bills against. Changing it would:
--   1. move `license.tenant_id` and `subject.tenant_id` (both FK, ON DELETE
--      RESTRICT — so the old row cannot even be deleted until they move), and
--   2. SILENTLY DETACH the billing history. `billing_cost_events` and
--      `token_budgets` live in the BILLING service's database, which this
--      service holds no connection to, and they carry NO FOREIGN KEY to
--      `tenants`. Rows written against the old id would simply stop joining —
--      no error, no orphan report, no way for this migration to fix or even
--      count them. `portal-render-e2e` alone drew 1,399 such rows / $86.02 in
--      24h (035:16-21).
-- A migration that quietly re-points money at a new id, in a database it cannot
-- see, is not a cleanup. Whoever owns that spend decides it first; #217 is where
-- that decision is being asked for, and it is the half this change does not take.
--
-- ⛔ AND THE SEED WOULD HAVE UNDONE A NAIVE ROW FIX ANYWAY. `models.PLATFORM_TENANTS`
-- still names `e148e8af-…`, and `013_seed_render_e2e_tenant.sql` re-asserts it
-- with `ON CONFLICT (id) DO UPDATE`. `seed_platform_tenants` now REFUSES to
-- re-insert an id the guard flags, so the boot path no longer reborns it — but a
-- DELETE here, with `PLATFORM_TENANTS` unedited, would be a data change whose
-- durability depends on a `subject` row continuing to exist. That is not a fix.
--
-- ────────────────────────────────────────────────────────────────────────────
-- WHY THIS FILE ADDS NO CONSTRAINT EITHER. 035's reasoning applies UNCHANGED and
-- is not re-litigated here: a CHECK cannot express a cross-table predicate, and
-- a trigger was rejected on a measured fact plus the fact that `create_all()`
-- would not reproduce one on a fresh database — which would make the guarantee
-- exist on prod and silently not exist in CI and in a DR rebuild. The enforcement
-- for BOTH doors therefore lives where 031/033/035 put it — at the shared
-- primitive, in code that every database runs:
--   * DOOR 1 — `DatabaseManager.ensure_tenant` (+ seed + backfill), via
--              `tenant_subject_invariant.subject_for_tenant_id`.
--   * DOOR 2 — `DatabaseManager.create_or_get_subject`, via
--              `tenant_subject_invariant.tenant_for_subject_sub`.  ← NEW
--   * BOTH   — reported at boot, and measured continuously on /metrics.
--
-- BASELINE INTERACTION: on a FRESH database `scripts/apply_migrations.py` stamps
-- this applied without running it, like every other file. Nothing is lost — it
-- creates no schema object, and both guards plus the gauge run on every database
-- regardless of how its schema was built.
--
-- DOWNGRADE: migrations/downgrades/037_subject_sub_must_not_be_a_tenant_id.sql

DO $$
DECLARE
    r            record;
    n_violation  bigint := 0;
    n_sentinel   bigint := 0;
    n_exposed    bigint := 0;
    n_tenants    bigint := 0;
BEGIN
    -- Door 2's view of the SAME rows 035 reports: a subject whose `keycloak_sub`
    -- is a registered tenant id. On a database where only door 1 was ever used
    -- this returns exactly 035's `keycloak_sub` class — that agreement is the
    -- point, not a redundancy. A row appearing HERE and not in 035 would mean the
    -- two predicates had drifted.
    FOR r IN
        SELECT s.id::text       AS subject_id,
               s.keycloak_sub   AS keycloak_sub,
               s.keycloak_realm AS realm,
               t.id::text       AS tenant_id,
               t.name           AS tenant_name
          FROM subject s
          JOIN tenants t ON lower(t.id::text) = lower(s.keycloak_sub)
         ORDER BY t.name
    LOOP
        IF r.tenant_id = '00000000-0000-0000-0000-000000000000' THEN
            n_sentinel := n_sentinel + 1;
            RAISE NOTICE
                '037: sentinel (deliberate, permanent): subject % (realm %) has '
                'keycloak_sub = tenants.id % (%)',
                r.subject_id, r.realm, r.tenant_id, r.tenant_name;
        ELSE
            n_violation := n_violation + 1;
            RAISE WARNING
                '037: SUBJECT SUB IS A TENANT ID: subject % (realm %) has '
                'keycloak_sub = tenants.id % (%). Same defect as 035, seen from the '
                'door that creates it — an identity that authenticates registered as '
                'the thing that pays. EXISTING row: reported, never refused. A '
                'genuinely NEW one is refused by create_or_get_subject — but NOT if '
                'the tenant already holds licences or the caller is minting that '
                'tenant its own licence; those are admitted and only reported.',
                r.subject_id, r.realm, r.tenant_id, r.tenant_name;
        END IF;
    END LOOP;

    -- THE EXPOSED SURFACE: registry tenants that no `subject` row names yet. This
    -- is the population door 2 could have converted into violations, and it is
    -- reported as a COUNT rather than a list precisely because it is nearly every
    -- tenant — the number is the scale of what was unguarded, not a worklist.
    SELECT count(*) INTO n_tenants FROM tenants;
    SELECT count(*) INTO n_exposed
      FROM tenants t
     WHERE NOT EXISTS (
        SELECT 1 FROM subject s WHERE lower(s.keycloak_sub) = lower(t.id::text)
     );

    RAISE WARNING
        '037: EXPOSED SURFACE — % of % tenant id(s) have no subject row naming '
        'them, so each was ONE create_or_get_subject call away from a new '
        'violation before this change. That set INCLUDES '
        'e4f70a83-81ee-429a-abca-6999e3a0d24f (nonagentic-gtm, '
        'service-account-control-service-account, realm secure-apps) — the known '
        'FOURTH offender, invisible to 035 and to the gauge for exactly the reason '
        'it appears here: it was never written to `subject`. It is NOT repaired by '
        'this migration; see this file''s header for why re-keying is a commercial '
        'decision and not a hygiene fix.',
        n_exposed, n_tenants;

    IF n_violation = 0 THEN
        RAISE NOTICE
            '037: no subject keycloak_sub is a registered tenant id (% sentinel '
            'exempt). ⛔ That is NOT a clean bill: it says door 2 has not been '
            'used, not that the invariant holds — read the EXPOSED SURFACE warning '
            'above and 035''s own BLIND SPOT warning before concluding anything.',
            n_sentinel;
    ELSE
        RAISE WARNING
            '037: % subject row(s) carry a registered tenant id as their '
            'keycloak_sub (+% deliberate sentinel). REPORTED, NOT FATAL — the same '
            'rule as 035: a guard that refused to serve on rows that have violated '
            'for months would CrashLoop the platform auth path over a billing-hygiene '
            'defect. Watch nonagentic_license_tenant_is_subject_rows{class="violation"} '
            'and nonagentic_license_subject_principal_refusals.',
            n_violation, n_sentinel;
    END IF;
END $$;

-- Verify. Door 2's view; expected on prod at the time of writing: two violations
-- (portal-render-e2e, book-fulfillment) + the default-anon sentinel — i.e. the
-- same identities 035 reports under match_kind='keycloak_sub'.
SELECT s.id::text AS subject_id, s.keycloak_realm AS realm,
       t.id::text AS tenant_id, t.name AS tenant_name
  FROM subject s
  JOIN tenants t ON lower(t.id::text) = lower(s.keycloak_sub)
 ORDER BY t.name;
