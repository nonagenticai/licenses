-- Downgrade 080: the care-bus -> laya edge and what 080's re-ensure wrote go.
--
-- ⚠️ Roll back models.APP_REQUIRES_SEED too, or the boot seed re-creates the edge.
--
-- Removed: licences whose metadata.provisioned_by = 'migration-080', the
-- svc:<tenant>:<app> subjects those licences were minted to once nothing else references
-- them, dependency grants granted_by 'bundle:care-bus by migration-080', and the
-- app_requires edge care-bus -> laya (caller care-bus).
-- NOT removed: laya licences a later live ensure wrote (provisioned_by the route) —
-- they are that ensure's, released by licences' own cascade once nothing requires them.
--
-- ⛔ REFUSES (raises, changes nothing) while any token was minted on one of 080's
-- licences (deleting the licence would CASCADE-delete live tokens). Revoke them first.
-- Billing rows the reconcile sweep may have created for those licences are NOT
-- touched here (billing's database) — retire them there.

BEGIN;

DO $$
DECLARE
    n_tokens  INT;
BEGIN
    SELECT count(*) INTO n_tokens
      FROM token t JOIN license l ON l.id = t.license_id
     WHERE l.metadata->>'provisioned_by' = 'migration-080';
    IF n_tokens > 0 THEN
        RAISE EXCEPTION '080 downgrade REFUSED: % token(s) were minted on migration-080 licences. Revoke them first; nothing was changed.',
            n_tokens;
    END IF;

    CREATE TEMP TABLE m080_down_subject ON COMMIT DROP AS
        SELECT DISTINCT subject_id FROM license
         WHERE metadata->>'provisioned_by' = 'migration-080';

    DELETE FROM license WHERE metadata->>'provisioned_by' = 'migration-080';

    DELETE FROM subject s
     USING m080_down_subject d
     WHERE s.id = d.subject_id
       AND s.keycloak_sub LIKE 'svc:%'
       AND NOT EXISTS (SELECT 1 FROM license l WHERE l.subject_id = s.id);

    DELETE FROM tenant_dependency_grant
     WHERE granted_by = 'bundle:care-bus by migration-080';

    DELETE FROM app_requires r
     USING app a, app q
     WHERE a.id = r.app_id AND q.id = r.required_app_id
       AND a.key = 'care-bus' AND q.key = 'laya' AND r.caller_owner_key = 'care-bus';
END $$;

-- Un-stamp 080 so scripts/apply_migrations.py re-applies it (up -> down -> up).
DO $$
BEGIN
    IF to_regclass('public.schema_migrations') IS NOT NULL THEN
        DELETE FROM schema_migrations WHERE filename = '080_care_bus_laya.sql';
    END IF;
END $$;

COMMIT;
