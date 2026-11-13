-- 068: care-bus's licences client.
--
-- WHY (2026-10-18). 067 registered the `care-bus` owner and app but deliberately gave
-- it NO api_client ("none exists yet (break-glass only)"). care-bus now needs to
-- register its tenants, issue / revoke its own `care-bus` licences and mint the
-- licence tokens it introspects — exactly what 067 gave strata_configurator through
-- ('secure-apps', 'strata-configurator-licenses-admin'). Without a row here every one of
-- those calls from care-bus's client-credentials SA is refused (unknown client).
--
-- WHAT THIS ADDS (data only, no schema):
--   api_client  ('secure-apps', 'care-bus-licenses-admin') -> care-bus, NOT cross-owner.
--               Holds `license-admin` once the gitops keycloak realm-patch creates it;
--               until then the row is inert. Mirrors 067's
--               ('secure-apps', 'strata-configurator-licenses-admin') and
--               ('secure-apps', 'commonpractice-licenses-admin').
--
-- Owner `care-bus`, app `care-bus`, its standard plan and its issuer row
-- (care-bus -> {care-bus}) already exist from 067; main._authorize_issuance then admits
-- this SA to issue / revoke `care-bus` licences on tenants it owns, and nobody else.
--
-- FRESH DATABASE: this file is stamped without running; the client comes from the boot
-- seed (models.API_CLIENTS) — pinned equal at head by
-- tests/unit/test_migration_068_care_bus_licenses_admin_client.py.
--
-- ONE TRANSACTION. ON CONFLICT DO NOTHING: an operator's row is never overwritten, a
-- re-run is a no-op.
--
-- DOWNGRADE: migrations/downgrades/068_care_bus_licenses_admin_client.sql. Roll back
-- models.py too, or the boot / DR seed re-creates the row.

BEGIN;

INSERT INTO api_client (realm, client_id, owner_key, cross_owner) VALUES
    ('secure-apps', 'care-bus-licenses-admin', 'care-bus', false)
ON CONFLICT (realm, client_id) DO NOTHING;

COMMIT;
