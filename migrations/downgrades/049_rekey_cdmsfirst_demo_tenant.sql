-- Downgrade 049. Moves `cdmsfirst` BACK onto
-- 0195f48b-d000-7000-8000-000000000100.
--
-- ⚠️ Roll back the OTHER stores of the window first (billing, core_app, the
-- rekey.py databases, Keycloak attributes), or tokens resolve to an id nothing
-- else uses.
--
-- EXACT mirror of 049 — same tables, same columns, same assertion, ids swapped.
-- No-op when the new-id row is absent. One DO block, so atomic.

DO $$
DECLARE
    old_id    constant uuid := '0195f48b-d000-7000-8000-000000000100';
    new_id    constant uuid := '01a1117c-d9b0-72fc-aeda-a9d1187f2a36';
    tmp_name  constant text := 'rekey-049-down-0195f48b-d000-7000-8000-000000000100';
    old_svc   constant text := 'svc:0195f48b-d000-7000-8000-000000000100:';
    new_svc   constant text := 'svc:01a1117c-d9b0-72fc-aeda-a9d1187f2a36:';
    orig_name text;
    n_left    bigint;
BEGIN
    SELECT name INTO orig_name FROM tenants WHERE id = new_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE NOTICE '049 downgrade: tenant % is absent — nothing to move back', new_id;
        RETURN;
    END IF;
    IF EXISTS (SELECT 1 FROM tenants WHERE id = old_id) THEN
        RAISE EXCEPTION '049 downgrade REFUSED: tenants holds BOTH % and %', old_id, new_id;
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
        RAISE EXCEPTION '049 downgrade FAILED: % row(s) still reference tenant %', n_left, new_id;
    END IF;
END
$$;
