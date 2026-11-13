-- Migration: rename app key `skill_builder` -> `skill-builder`
-- Date: 2026-07-17
-- Description: The catalog registered the Skill Builder app as `skill_builder`
-- (underscore), but the edge enforces the key from
-- gitops-production/clusters/strato/route-registry.yaml, which spells it
-- `skill-builder` (hyphen) for `/skills/`. license-authz compares the app_key
-- byte-for-byte, so every license minted under `skill_builder` fails the edge
-- check while looking perfectly valid in the DB and in any admin UI — a silent
-- entitlement hole, not a cosmetic naming nit. 5 licenses were affected on prod
-- at the time of writing (audited 2026-07-17).
--
-- Rename IN PLACE rather than inserting a corrected row: `license.app_id` is a
-- FK to `app.id`, so mutating the key carries the existing licenses (and their
-- tokens) with it. Creating a second app row would instead strand them under a
-- key nothing enforces.
--
-- `app.key` is UNIQUE, so this must tolerate scripts/seed_apps.py — which now
-- also declares `skill-builder` — having run FIRST (fresh DB, or a re-run of
-- the seed Job before this migration lands). Both orderings converge on one
-- canonical `skill-builder` app carrying every license:
--   * legacy only          -> plain rename
--   * legacy + canonical   -> repoint licenses onto the canonical app's
--                             same-code plan, then drop the legacy row
--   * canonical only / neither -> no-op
-- Idempotent: re-running after success hits the no-op branch.

DO $$
DECLARE
    legacy_id    uuid;
    canonical_id uuid;
    moved        integer;
    stranded     integer;
BEGIN
    SELECT id INTO legacy_id    FROM app WHERE key = 'skill_builder';
    SELECT id INTO canonical_id FROM app WHERE key = 'skill-builder';

    IF legacy_id IS NULL THEN
        RAISE NOTICE '015: no `skill_builder` row — nothing to rename (no-op)';
        RETURN;
    END IF;

    -- Plain rename: the canonical key is free, so the row (and every license
    -- and token hanging off it) simply adopts the enforced spelling.
    IF canonical_id IS NULL THEN
        UPDATE app
           SET key  = 'skill-builder',
               name = 'Skill Builder'
         WHERE id = legacy_id;
        RAISE NOTICE '015: renamed skill_builder -> skill-builder (app_id=%)', legacy_id;
        RETURN;
    END IF;

    -- Merge: the seed already created the canonical app. Repoint each legacy
    -- license onto the canonical app's plan with the SAME code + region, so a
    -- license keeps the commercial terms it was sold under.
    UPDATE license l
       SET app_id  = canonical_id,
           plan_id = COALESCE(
               (SELECT p_new.id
                  FROM plan p_new
                  JOIN plan p_old ON p_old.id = l.plan_id
                 WHERE p_new.app_id = canonical_id
                   AND p_new.code   = p_old.code
                   AND p_new.region_id IS NOT DISTINCT FROM p_old.region_id
                 LIMIT 1),
               l.plan_id)
     WHERE l.app_id = legacy_id;
    GET DIAGNOSTICS moved = ROW_COUNT;

    -- Fail loud rather than orphan: any license still pointing at a legacy plan
    -- has no same-code counterpart on the canonical app, and dropping the
    -- legacy row would violate the FK (or, worse, cascade the license away).
    SELECT count(*) INTO stranded
      FROM license
     WHERE plan_id IN (SELECT id FROM plan WHERE app_id = legacy_id);
    IF stranded > 0 THEN
        RAISE EXCEPTION
            '015: % license(s) still reference legacy skill_builder plans with no '
            'same-code plan on skill-builder — resolve by hand, refusing to strand them',
            stranded;
    END IF;

    DELETE FROM plan WHERE app_id = legacy_id;
    DELETE FROM app  WHERE id = legacy_id;
    RAISE NOTICE '015: merged legacy skill_builder into skill-builder (%), % license(s) moved',
        canonical_id, moved;
END $$;

-- Verify: exactly one Skill Builder app, spelled as route-registry enforces,
-- and zero licenses left under the legacy key.
SELECT a.key, count(l.id) AS licenses
  FROM app a
  LEFT JOIN license l ON l.app_id = a.id
 WHERE a.key IN ('skill_builder', 'skill-builder')
 GROUP BY a.key
 ORDER BY a.key;
