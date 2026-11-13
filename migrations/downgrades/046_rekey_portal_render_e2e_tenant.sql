-- Downgrade 046. Moves `portal-render-e2e` BACK onto the id that is also the
-- `keycloak_sub` of subject `8382f0b3-…` (a deleted `portal`-realm user).
--
-- ⚠️ THIS RE-CREATES THE VIOLATION 046 REPAIRED: `LicenseTenantIdIsKeycloakPrincipal`
-- fires again, by design (the retired-principal register entry is gone). It
-- exists because the repo convention requires a way back, not because going back
-- is a good state.
--
-- ⚠️ Roll the CODE back first: `models.PLATFORM_TENANTS` and
-- scripts/backfill_tenants_byo_mode.py name the NEW id. With the old id live and
-- the new code running, the next boot's seed re-inserts NOTHING (the name is
-- taken) but the backfill would classify the old id `byo_required`.
--
-- EXACT mirror of 046 — same tables, same columns, same assertion, ids swapped.
-- No-op when the new-id row is absent. One DO block, so atomic.
--
-- ⛔ NOT the model's `ensure_tenant` path: that refuses an id that is a subject's
-- keycloak_sub, which the old id is. This is a raw INSERT on purpose.

DO $$
DECLARE
    old_id    constant uuid := 'e148e8af-3751-49bf-a0fa-56ab46d31ddc';
    new_id    constant uuid := '01a1113b-674f-72b0-a84b-dc746d39afca';
    tmp_name  constant text := 'rekey-046-down-e148e8af-3751-49bf-a0fa-56ab46d31ddc';
    old_svc   constant text := 'svc:e148e8af-3751-49bf-a0fa-56ab46d31ddc:';
    new_svc   constant text := 'svc:01a1113b-674f-72b0-a84b-dc746d39afca:';
    orig_name text;
    n_left    bigint;
BEGIN
    SELECT name INTO orig_name FROM tenants WHERE id = new_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE NOTICE '046 downgrade: tenant % is absent — nothing to move back', new_id;
        RETURN;
    END IF;
    IF EXISTS (SELECT 1 FROM tenants WHERE id = old_id) THEN
        RAISE EXCEPTION '046 downgrade REFUSED: tenants holds BOTH % and %', old_id, new_id;
    END IF;

    INSERT INTO tenants (id, name, keycloak_realm, byo_mode, archived_at, kind,
                         cost_centre, budget_currency, created_at)
    SELECT old_id, tmp_name, keycloak_realm, byo_mode, archived_at, kind,
           cost_centre, budget_currency, created_at
      FROM tenants
     WHERE id = new_id;

    UPDATE license             SET tenant_id = old_id WHERE tenant_id = new_id;
    UPDATE subject             SET tenant_id = old_id WHERE tenant_id = new_id;
    UPDATE tenant_bundle       SET tenant_id = old_id WHERE tenant_id = new_id;
    UPDATE tenant_app_byo_mode SET tenant_id = old_id WHERE tenant_id = new_id;
    UPDATE subject
       SET keycloak_sub = old_svc || substr(keycloak_sub, length(new_svc) + 1)
     WHERE lower(left(keycloak_sub, length(new_svc))) = new_svc;

    DELETE FROM tenants WHERE id = new_id;
    UPDATE tenants SET name = orig_name WHERE id = old_id;

    SELECT (SELECT count(*) FROM tenants             WHERE id        = new_id)
         + (SELECT count(*) FROM license             WHERE tenant_id = new_id)
         + (SELECT count(*) FROM subject             WHERE tenant_id = new_id)
         + (SELECT count(*) FROM tenant_bundle       WHERE tenant_id = new_id)
         + (SELECT count(*) FROM tenant_app_byo_mode WHERE tenant_id = new_id)
         + (SELECT count(*) FROM subject
             WHERE lower(left(keycloak_sub, length(new_svc))) = new_svc)
      INTO n_left;
    IF n_left <> 0 THEN
        RAISE EXCEPTION '046 downgrade FAILED: % row(s) still reference tenant %', n_left, new_id;
    END IF;
END
$$;
