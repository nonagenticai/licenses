-- Downgrade 045. Moves `book-fulfillment` BACK onto the id that is also the
-- Keycloak principal `service-account-book-fulfillment-svc`.
--
-- ⚠️ THIS RE-CREATES THE VIOLATION 045 REPAIRED: `LicenseTenantIdIsKeycloakPrincipal`
-- fires again, by design. It exists because the repo convention requires a way
-- back, not because going back is a good state.
--
-- ⚠️ Roll the CODE back first: scripts/backfill_tenants_byo_mode.py lists the NEW
-- id in PLATFORM_ONLY_KEEP, and with the old id live it would classify the
-- tenant `byo_required`.
--
-- EXACT mirror of 045 — same tables, same columns, same assertion, ids swapped.
-- No-op when the new-id row is absent. One DO block, so atomic.
--
-- ⛔ NOT the model's `ensure_tenant` path: that refuses an id that is a subject's
-- keycloak_sub, which the old id is. This is a raw INSERT on purpose.

DO $$
DECLARE
    old_id    constant uuid := '1d0090ac-adbf-4c07-b704-bc66061b572e';
    new_id    constant uuid := '01a1109c-f895-7766-ba28-6ea56e56a5f7';
    tmp_name  constant text := 'rekey-045-down-1d0090ac-adbf-4c07-b704-bc66061b572e';
    old_svc   constant text := 'svc:1d0090ac-adbf-4c07-b704-bc66061b572e:';
    new_svc   constant text := 'svc:01a1109c-f895-7766-ba28-6ea56e56a5f7:';
    orig_name text;
    n_left    bigint;
BEGIN
    SELECT name INTO orig_name FROM tenants WHERE id = new_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE NOTICE '045 downgrade: tenant % is absent — nothing to move back', new_id;
        RETURN;
    END IF;
    IF EXISTS (SELECT 1 FROM tenants WHERE id = old_id) THEN
        RAISE EXCEPTION '045 downgrade REFUSED: tenants holds BOTH % and %', old_id, new_id;
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
        RAISE EXCEPTION '045 downgrade FAILED: % row(s) still reference tenant %', n_left, new_id;
    END IF;
END
$$;
