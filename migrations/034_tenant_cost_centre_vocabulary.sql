-- 034: pin `tenants.cost_centre` to the vocabulary actually in use.
--
-- WHERE THIS COMES FROM. `cost_centre` (migration 027) is the chargeback join
-- key. It arrived with NO CHECK — measured 2026-09-08, `pg_constraint` on
-- `tenants` carries `ck_tenants_byo_mode`, `ck_tenants_kind`,
-- `ck_tenants_name_format` and `ck_tenants_service_not_byo_required`, and NOT
-- ONE of them mentions `cost_centre`. The column's own Field description in
-- main.py said so out loud: "No enforced vocabulary". So any string up to 64
-- characters was accepted, including a typo of an existing centre — and a typo
-- in a join key does not fail, it silently produces a chargeback bucket of one.
--
-- MEASURED ON THE PRIMARY (2026-09-08), all 76 tenant rows:
--
--   service       45
--   tests          9
--   customers      6
--   ci             4
--   improvement    4
--   merqi          4
--   NULL           4   <- default-anon, george-dekker, nonagentic, portal
--
-- Six values, and they are the whole population. This constraint admits exactly
-- those six plus NULL.
--
-- ⛔ NULL IS ADMITTED DELIBERATELY, AND THE FOUR NULLS ARE LEFT ALONE. They are
-- `default-anon`, `george-dekker`, `nonagentic` and `portal`, all `kind='internal'`.
-- Which cost centre each belongs to is an APPORTIONMENT decision — it decides
-- whose budget carries them — and this service must never guess it. Migration
-- 030 established the same rule when it backfilled 12 rows and left these four:
-- a cost centre is stated by an operator, never derived. What this migration's
-- sibling commit adds is the ability to state it over HTTP (`PATCH /tenants/{id}`
-- now carries `cost_centre`), which is what makes leaving them NULL a choice
-- rather than a dead end — before it, the only way to fill them was direct SQL.
--
-- ⚠️ AN INCONSISTENCY THIS FILE DELIBERATELY DOES NOT FIX. `platform-core` and
-- `system` are `kind='internal'` but carry `cost_centre='service'`. That is
-- either a mis-set cost centre or a mis-set kind, and the two have different
-- consequences (`kind` drives BYO posture via `ck_tenants_service_not_byo_required`;
-- `cost_centre` drives chargeback). Deciding which is wrong is an operator's
-- call, so this migration ADMITS the combination rather than rejecting it — a
-- CHECK correlating kind and cost_centre would refuse these two rows at apply
-- time and CrashLoop the pod. Reported, not silently repaired.
--
-- WHAT THIS COSTS. Adding a new cost centre now requires a migration. That is
-- the intended trade: a join key with an open vocabulary cannot distinguish "a
-- new centre" from "a typo of an old one", and this column exists to be joined
-- on. The remedy is one line in this CHECK, and the failure mode is a loud 400
-- at the edge instead of a silent bucket of one.
--
-- BASELINE INTERACTION (scripts/apply_migrations.py). On a FRESH database this
-- file never executes — the runner stamps it as applied. The constraint is ALSO
-- declared in the ORM (models.py: `Tenant.__table_args__`), which is what puts
-- it on a fresh database; this file puts it on an EXISTING one. Mirrors 021/033.
--
-- SAFETY. A failed migration raises out of `_run_db_bootstrap` and the pod
-- REFUSES TO SERVE. Matching 033's shape: the ADD is guarded on `pg_constraint`
-- so a re-run is a no-op, and it is preceded by a count against the CHECK's own
-- predicate whose violation is a NAMED `RAISE EXCEPTION` listing the offending
-- values — an operator reading the CrashLoop knows exactly what to fix.
--
-- DOWNGRADE: migrations/downgrades/034_tenant_cost_centre_vocabulary.sql

DO $$
DECLARE
    bad_rows bigint;
    offenders text;
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_tenants_cost_centre' AND conrelid = 'tenants'::regclass
    ) THEN
        RAISE NOTICE '034: ck_tenants_cost_centre already present — nothing to do';
    ELSE
        SELECT count(*), string_agg(DISTINCT quote_literal(cost_centre), ', ')
          INTO bad_rows, offenders
          FROM tenants
         WHERE cost_centre IS NOT NULL
           AND cost_centre NOT IN ('service', 'tests', 'customers', 'ci', 'improvement', 'merqi');

        IF bad_rows > 0 THEN
            RAISE EXCEPTION
                'refusing to add ck_tenants_cost_centre: % tenant row(s) hold a '
                'cost_centre outside the measured vocabulary (offending values: %). '
                'Either correct the rows or add the value to this CHECK — '
                'SELECT id, name, cost_centre FROM tenants WHERE cost_centre IS NOT NULL '
                'AND cost_centre NOT IN (''service'', ''tests'', ''customers'', ''ci'', '
                '''improvement'', ''merqi'');',
                bad_rows, offenders;
        END IF;

        ALTER TABLE tenants
            ADD CONSTRAINT ck_tenants_cost_centre
            CHECK (
                cost_centre IS NULL
                OR cost_centre IN ('service', 'tests', 'customers', 'ci', 'improvement', 'merqi')
            );
        RAISE NOTICE '034: ck_tenants_cost_centre added';
    END IF;
END $$;

-- Verify
SELECT conname, pg_get_constraintdef(oid)
  FROM pg_constraint
 WHERE conname = 'ck_tenants_cost_centre';
