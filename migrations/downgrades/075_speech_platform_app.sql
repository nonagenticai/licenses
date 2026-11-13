-- Downgrade 075: remove the `speech` app (TTS / ASR platform services).
--
-- ⚠️ Roll back models.py (APP_ISSUER_OVERRIDES) and scripts/seed_apps.py too, or the DR
-- seed re-creates it.
--
-- ⛔ REFUSES (raises, changes nothing) while anything references the app — deleting an
-- app CASCADE-deletes its licences silently: any licence, dependency grant, offering,
-- tenant_app_byo_mode, bundle_app, bundle_caller or app_requires row for it, or an
-- issuer row 075 did not write.

BEGIN;

DO $$
DECLARE
    n_used   INT;
BEGIN
    SELECT (SELECT count(*) FROM license l JOIN app a ON a.id = l.app_id
             WHERE a.key = 'speech')
         + (SELECT count(*) FROM tenant_dependency_grant g JOIN app a ON a.id = g.app_id
             WHERE a.key = 'speech')
         + (SELECT count(*) FROM offering o JOIN app a ON a.id = o.app_id
             WHERE a.key = 'speech')
         + (SELECT count(*) FROM tenant_app_byo_mode m JOIN app a ON a.id = m.app_id
             WHERE a.key = 'speech')
         + (SELECT count(*) FROM bundle_app b JOIN app a ON a.id = b.app_id
             WHERE a.key = 'speech')
         + (SELECT count(*) FROM bundle_caller c JOIN app a ON a.id = c.app_id
             WHERE a.key = 'speech')
         + (SELECT count(*) FROM app_requires r JOIN app a
                    ON a.id = r.app_id OR a.id = r.required_app_id
             WHERE a.key = 'speech')
         + (SELECT count(*) FROM app_issuer i JOIN app a ON a.id = i.app_id
             WHERE a.key = 'speech' AND i.owner_key <> 'platform')
      INTO n_used;
    IF n_used > 0 THEN
        RAISE EXCEPTION '075 downgrade REFUSED: % row(s) reference the speech app (licences, grants, offerings, byo modes, bundles, other issuers). Remove them first; nothing was changed.',
            n_used;
    END IF;

    DELETE FROM app WHERE key = 'speech';  -- plan / app_issuer cascade
END $$;

-- Un-stamp 075 so scripts/apply_migrations.py re-applies it (up -> down -> up).
DO $$
BEGIN
    IF to_regclass('public.schema_migrations') IS NOT NULL THEN
        DELETE FROM schema_migrations WHERE filename = '075_speech_platform_app.sql';
    END IF;
END $$;

COMMIT;
