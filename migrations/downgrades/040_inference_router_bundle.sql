-- Downgrade 040. Removes the `inference-router` bundle and its membership rows —
-- but ONLY while nothing holds it.
--
-- ⛔ NEVER ORPHAN A GRANT. `license.bundle_id` and `tenant_bundle.bundle_id` both
-- reference `bundle(id)` ON DELETE RESTRICT (039), so deleting a held bundle would
-- fail anyway; this file checks FIRST and raises a message that says why, instead of
-- a bare FK violation. If any licence names the bundle or any tenant holds it, the
-- downgrade REFUSES and changes nothing: un-provisioning those grants is an operator
-- decision (each licence keeps its app and tokens; re-pointing it is not a schema
-- rollback's call).
--
-- The `inference-router` APP is not touched: 040 never created it (it was created in
-- prod via POST /apps), and dropping an `app` cascades to `license`.
DO $$
DECLARE
    v_bundle   UUID;
    n_licence  INT;
    n_holder   INT;
BEGIN
    SELECT id INTO v_bundle FROM bundle WHERE key = 'inference-router';
    IF v_bundle IS NULL THEN
        RAISE NOTICE '040 downgrade: no inference-router bundle — nothing to do';
        RETURN;
    END IF;

    SELECT count(*) INTO n_licence FROM license WHERE bundle_id = v_bundle;
    SELECT count(*) INTO n_holder FROM tenant_bundle WHERE bundle_id = v_bundle;
    IF n_licence > 0 OR n_holder > 0 THEN
        RAISE EXCEPTION '040 downgrade REFUSED: bundle inference-router is held (% licence(s) name it, % tenant_bundle row(s)). Un-provision those grants first; nothing was changed.',
            n_licence, n_holder;
    END IF;

    DELETE FROM bundle_app WHERE bundle_id = v_bundle;
    DELETE FROM bundle WHERE id = v_bundle;
    RAISE NOTICE '040 downgrade: inference-router bundle and its membership removed';
END $$;
