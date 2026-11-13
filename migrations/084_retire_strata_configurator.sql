-- 084: retire the strata-configurator owner and app (agent merge, licences PR B).
--
-- WHY (owner approved the decommission 2026-10-24; agent-merge spec 08 §2.6 row B,
-- §1.10). strata_configurator (configurator.nonagentic.ai) is merged into the `agent`
-- app (070). Its tenants move to owner `agent` (C2); its app, licences client and
-- fr-test-user grant are retired. The spec named this file 070; 070..080 were taken
-- on main by then.
--
-- PRECONDITION (operator, step 9 — NOT SQL): every active `strata-configurator`
-- licence is revoked through the licences admin API with the configurator's OWN
-- client first (billing quota teardown runs there). This file deletes that client, so
-- it REFUSES (raises, changes nothing) while any `strata-configurator` licence is
-- still active or pending. Licences are never written here.
--
-- WHAT THIS DOES (data only, no schema):
--   1. tenant_app_grant  delete the (tenant, agent) grants, active or revoked, on the
--                        tenants about to move: after the move agent OWNS them, and
--                        no grant row ever names its tenant's own owner (050).
--   2. tenants           owner_key strata-configurator -> agent (C2). Live: 3 tenants.
--                        REFUSES on a name / external_ref clash among agent's live
--                        tenants (053's per-owner unique indexes) rather than half-move.
--   3. app               strata-configurator revoked = true (plan, issuer, offering
--                        rows stay as history; a revoked app is not offered).
--   4. api_client        delete ('secure-apps', 'strata-configurator-licenses-admin').
--                        api_client has no disable flag; an unknown azp is refused.
--   5. tenant_app_grant  soft-revoke (revoked_at = now()) every active grant held by
--                        strata-configurator — live: 069's grant on fr-test-user
--                        0195f48b-d000-7000-8000-000000000099.
--   6. tenant_owner      delete strata-configurator ONLY if no row of any table with
--                        a foreign key to tenant_owner still names it (checked from
--                        pg_constraint, so a future FK is covered). Live it stays:
--                        the app's issuer row (067) and 069's revoked grant name it.
--
-- NOT TOUCHED: licences (operator, above); every `care-bus` row (care-bus-agents keeps
-- running, D53); the strata_configurator database (operator step 12).
--
-- FRESH DATABASE: this file is stamped without running. models.API_CLIENTS no longer
-- lists the configurator's client (or the boot seed would re-create it every boot) and
-- scripts/seed_apps.py / seed_offerings.py no longer declare the app, so a rebuilt
-- database has no strata-configurator app, client or offering. The owner stays in
-- models.TENANT_OWNERS: live keeps the row (step 6) and 067/069 still reference it.
-- Pinned by tests/unit/test_migration_084_retire_strata_configurator.py.
--
-- ONE TRANSACTION, IDEMPOTENT: a re-run finds nothing to move, revoke or delete.
--
-- DOWNGRADE: migrations/downgrades/084_retire_strata_configurator.sql restores what is
-- reversible (owner, client, app not revoked, 069's grant). The ownership move and the
-- deleted redundant grants are NOT reversed — which agent tenants were the
-- configurator's is not recorded once moved.

BEGIN;

DO $$
DECLARE
    n_live_licences INT;
    n_clash         INT;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM tenant_owner WHERE key = 'agent') THEN
        RAISE EXCEPTION '084 REFUSED: owner `agent` (070) is missing; nothing was changed.';
    END IF;

    SELECT count(*) INTO n_live_licences
      FROM license l JOIN app a ON a.id = l.app_id
     WHERE a.key = 'strata-configurator'
       AND l.status IN ('active', 'pending')
       AND l.deleted_at IS NULL;
    IF n_live_licences > 0 THEN
        RAISE EXCEPTION '084 REFUSED: % strata-configurator licence(s) are still active or pending. Revoke them through the licences admin API with the configurator''s own client first (billing teardown); nothing was changed.',
            n_live_licences;
    END IF;

    SELECT count(*) INTO n_clash
      FROM tenants s
      JOIN tenants a
        ON a.owner_key = 'agent' AND a.archived_at IS NULL
       AND (a.name = s.name OR a.external_ref = s.external_ref)
     WHERE s.owner_key = 'strata-configurator' AND s.archived_at IS NULL;
    IF n_clash > 0 THEN
        RAISE EXCEPTION '084 REFUSED: % strata-configurator tenant(s) clash by name or external_ref with a live agent tenant; nothing was changed.',
            n_clash;
    END IF;
END $$;

-- 1. The (tenant, agent) grants the move makes redundant.
DELETE FROM tenant_app_grant g
 USING tenants t
 WHERE g.tenant_id = t.id
   AND t.owner_key = 'strata-configurator'
   AND g.grantee_owner_key = 'agent';

-- 2. C2: the configurator's tenants move to agent.
UPDATE tenants SET owner_key = 'agent' WHERE owner_key = 'strata-configurator';

-- 3. The app is retired.
UPDATE app SET revoked = true WHERE key = 'strata-configurator' AND NOT revoked;

-- 4. The configurator's licences client.
DELETE FROM api_client
 WHERE realm = 'secure-apps'
   AND client_id = 'strata-configurator-licenses-admin'
   AND owner_key = 'strata-configurator';

-- 5. Grants held by the retired owner (live: 069's on fr-test-user), history kept.
UPDATE tenant_app_grant SET revoked_at = now()
 WHERE grantee_owner_key = 'strata-configurator' AND revoked_at IS NULL;

-- 6. The owner row, only when nothing references it any more.
DO $$
DECLARE
    fk     RECORD;
    n_refs BIGINT := 0;
    n      BIGINT;
BEGIN
    FOR fk IN
        SELECT c.conrelid::regclass AS tbl, a.attname AS col
          FROM pg_constraint c
          JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
         WHERE c.contype = 'f'
           AND c.confrelid = 'public.tenant_owner'::regclass
           AND array_length(c.conkey, 1) = 1
    LOOP
        EXECUTE format('SELECT count(*) FROM %s WHERE %I = $1', fk.tbl, fk.col)
           INTO n USING 'strata-configurator';
        n_refs := n_refs + n;
    END LOOP;
    IF n_refs = 0 THEN
        DELETE FROM tenant_owner WHERE key = 'strata-configurator';
    ELSE
        RAISE NOTICE '084: tenant_owner strata-configurator kept, % row(s) still reference it.',
            n_refs;
    END IF;
END $$;

COMMIT;
