-- Downgrade 079: undo the care-bus bundle ensure on fr-test-user — only what 079 wrote.
--
-- Removed: licences on fr-test-user whose metadata.provisioned_by = 'migration-079',
-- grants granted_by 'bundle:care-bus by migration-079', the svc:<tenant>:<app> subjects
-- those licences were minted to once nothing else references them, and the
-- tenant_bundle (fr-test-user, care-bus) hold once no licence carries the bundle.
-- NOT reversed: an ADOPTION (079 adopting an existing unbundled licence leaves no
-- marker; measured live 2026-10-23 there was nothing to adopt).
--
-- ⛔ REFUSES (raises, changes nothing) while any token was minted on one of 079's
-- licences (deleting the licence would CASCADE-delete live tokens). Revoke them first.
-- Billing rows the reconcile sweep may have created for those licences are NOT
-- touched here (billing's database) — retire them there.

BEGIN;

DO $$
DECLARE
    c_tenant  CONSTANT UUID := '0195f48b-d000-7000-8000-000000000099';
    n_tokens  INT;
BEGIN
    SELECT count(*) INTO n_tokens
      FROM token t JOIN license l ON l.id = t.license_id
     WHERE l.tenant_id = c_tenant AND l.metadata->>'provisioned_by' = 'migration-079';
    IF n_tokens > 0 THEN
        RAISE EXCEPTION '079 downgrade REFUSED: % token(s) were minted on migration-079 licences. Revoke them first; nothing was changed.',
            n_tokens;
    END IF;

    CREATE TEMP TABLE m079_down_subject ON COMMIT DROP AS
        SELECT DISTINCT subject_id FROM license
         WHERE tenant_id = c_tenant AND metadata->>'provisioned_by' = 'migration-079';

    DELETE FROM license
     WHERE tenant_id = c_tenant AND metadata->>'provisioned_by' = 'migration-079';

    DELETE FROM subject s
     USING m079_down_subject d
     WHERE s.id = d.subject_id
       AND s.keycloak_sub LIKE 'svc:' || c_tenant::text || ':%'
       AND NOT EXISTS (SELECT 1 FROM license l WHERE l.subject_id = s.id);

    DELETE FROM tenant_dependency_grant
     WHERE tenant_id = c_tenant AND granted_by = 'bundle:care-bus by migration-079';
    DELETE FROM tenant_app_grant
     WHERE tenant_id = c_tenant AND granted_by = 'bundle:care-bus by migration-079';

    DELETE FROM tenant_bundle tb
     USING bundle b
     WHERE b.id = tb.bundle_id AND b.key = 'care-bus' AND tb.tenant_id = c_tenant
       AND NOT EXISTS (SELECT 1 FROM license l
                        WHERE l.tenant_id = c_tenant AND l.bundle_id = b.id);
END $$;

-- Un-stamp 079 so scripts/apply_migrations.py re-applies it (up -> down -> up).
DO $$
BEGIN
    IF to_regclass('public.schema_migrations') IS NOT NULL THEN
        DELETE FROM schema_migrations WHERE filename = '079_fr_test_user_care_bus_bundle.sql';
    END IF;
END $$;

COMMIT;
