-- Downgrade 068: remove care-bus's licences client.
--
-- ⚠️ Roll back models.py (API_CLIENTS) too, or the boot / DR seed re-creates it.
--
-- Deletes only the exact row 068 wrote (owner care-bus, not cross-owner); an operator
-- row re-pointed at another owner or flipped to cross-owner is left alone. No tenant,
-- licence or token references api_client, so nothing cascades.

BEGIN;

DELETE FROM api_client
 WHERE realm = 'secure-apps' AND client_id = 'care-bus-licenses-admin'
   AND owner_key = 'care-bus' AND cross_owner = false;

-- Un-stamp 068 so scripts/apply_migrations.py re-applies it (up -> down -> up).
DO $$
BEGIN
    IF to_regclass('public.schema_migrations') IS NOT NULL THEN
        DELETE FROM schema_migrations
         WHERE filename = '068_care_bus_licenses_admin_client.sql';
    END IF;
END $$;

COMMIT;
