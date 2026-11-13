-- 022: give `app` an `audience` column, so "who is this licensed to" is DATA.
--
-- WHY. fr's super-admin provisioning picker offered all 47 registry rows as
-- checkboxes, service-to-service modules included. On 2026-08-26 an operator
-- selected three of them (coding, n8n, observability) because they were offered;
-- the run then reached an app with no active plan, 404'd after 10.8s, and left
-- five licences and five tokens behind. fr now filters — but from a hardcoded
-- list of seven keys duplicated in its own frontend AND backend. That is a
-- classification about THIS service's data living in another repo, in two
-- copies. This column is its home; fr deletes both copies and reads `audience`.
--
-- THE THREE VALUES, and the rule that assigns them:
--   human      end-user product a person is licensed to use               (7)
--   automation n8n action module; never a person                          (4)
--   platform   service-to-service; fr drives it on the user's behalf     (36)
-- `scripts/seed_apps.py` has always stated the platform rule in prose:
-- "fr then drives kanban/n8n/kag/requirements on their behalf over the trusted
-- network (those need no per-user license)."
--
-- DEFAULT IS `platform`, DELIBERATELY. A newly registered app is not offered to
-- people until someone declares it. The failure this column prevents is
-- over-offering, so the default must fail closed.
--
-- WHY THE BACKFILL LISTS EVERY HUMAN/AUTOMATION KEY EXPLICITLY rather than
-- deriving from seed_apps.py: the seed declares 34 of the registry's 47 apps
-- (10 explicit + 24 generated). The other 13 — including all four automation
-- modules — were created by other paths and appear in no seed file. A backfill
-- driven off the seed would leave them at the default; correct for the platform
-- ones, WRONG for the four automation modules, which would then be silently
-- indistinguishable from platform. Counts verified against prod 2026-08-26.
--
-- BASELINE INTERACTION (scripts/apply_migrations.py). On a database with the app
-- tables but no stamps, apply_all() stamps every current file WITHOUT running it,
-- so on a FRESH database this file never executes: the column arrives from
-- create_all (models.py App.audience + ck_app_audience) and the values from
-- seed_apps.py. This file is for the databases that already exist. The guard
-- below makes that explicit rather than accidental.
--
-- SAFETY. A failed migration raises out of _run_db_bootstrap and the pod REFUSES
-- TO SERVE — and this service backs ext_authz for the whole platform at
-- replicas: 2. So: one DO block (atomic — column, backfill and constraint land
-- together or not at all), guarded on the column so a re-run is a no-op, and no
-- statement that can fail on data, because every row is assigned a value here
-- before the CHECK is added. A re-run deliberately does NOT re-assert the
-- backfill: once the column exists, an operator's later correction is the
-- authority, not this file.

DO $$
DECLARE
    n_human      bigint;
    n_automation bigint;
    n_platform   bigint;
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'app' AND column_name = 'audience'
    ) THEN
        RAISE NOTICE 'app.audience already present — nothing to do';
        RETURN;
    END IF;

    -- VARCHAR(16), NOT text: models.py declares String(16) and create_all builds a
    -- fresh database from that. An existing database and a fresh one must end up with
    -- the SAME column type, or the two paths quietly diverge — which is exactly the
    -- class of drift 021's header warns about.
    ALTER TABLE app
        ADD COLUMN audience VARCHAR(16) NOT NULL DEFAULT 'platform';

    UPDATE app SET audience = 'human'
    WHERE key IN (
        'fr',
        'fr-frontend',
        'commonpractice',
        'w2v',
        'signal',
        'raise-my-tickets',
        'ingestion'
    );

    UPDATE app SET audience = 'automation'
    WHERE key IN (
        'kaizen_core_actions',
        'kaizen_discover_actions',
        'meta_prompt_core_actions',
        'meta_prompt_discover_actions'
    );

    ALTER TABLE app
        ADD CONSTRAINT ck_app_audience
        CHECK (audience IN ('human', 'platform', 'automation'));

    SELECT count(*) FILTER (WHERE audience = 'human'),
           count(*) FILTER (WHERE audience = 'automation'),
           count(*) FILTER (WHERE audience = 'platform')
      INTO n_human, n_automation, n_platform
      FROM app;

    RAISE NOTICE 'app.audience added: % human, % automation, % platform',
        n_human, n_automation, n_platform;

    -- Loud but non-fatal. A key that matched nothing means the registry has
    -- drifted from this list (renamed or not yet created); the operator should
    -- see it, but a NOTICE cannot crashloop ext_authz for the whole platform.
    IF n_human <> 7 THEN
        RAISE WARNING 'expected 7 human apps, found % — check for renamed keys', n_human;
    END IF;
    IF n_automation <> 4 THEN
        RAISE WARNING 'expected 4 automation apps, found % — check for renamed keys',
            n_automation;
    END IF;
END $$;

-- Verify
SELECT audience, count(*) AS apps, string_agg(key, ', ' ORDER BY key) AS keys
FROM app
WHERE audience <> 'platform'
GROUP BY audience
ORDER BY audience;
