-- 049: RE-KEY the `cdmsfirst` (CDMSFIRST / Atlific demo) tenant onto a fresh
-- organisation id.
--
-- ⛔ THE THIRD SANCTIONED EXCEPTION TO "NO UUID IS EVER RE-KEYED" (045 and 046
-- were the first two). Do not copy this file as a pattern. 037's header explains
-- why a re-key is a commercial decision and not a hygiene fix; this one was TAKEN,
-- by the owner, for exactly this tenant.
--
-- OWNER RULING 2026-10-07 (George): re-key the CDMSFIRST demo tenant, after the
-- demo was recorded, in the same coordinated-window pattern as 046 (unit "B2").
-- `0195f48b-d000-7000-8000-000000000100` is a hand-allocated reserved-slot id,
-- not a minted one; the tenant moves to a uuid7 minted for this file.
--
--   old tenants.id  0195f48b-d000-7000-8000-000000000100
--   NEW tenants.id  01a1117c-d9b0-72fc-aeda-a9d1187f2a36   (uuid7, minted for this file)
--
-- WHAT MOVES. Every column in this database that holds a tenant id — the same set
-- as 045/046 (re-checked for this file against models.py and the primary's
-- information_schema: `%tenant%` columns are exactly license / subject /
-- tenant_bundle / tenant_app_byo_mode `.tenant_id`), NOT rendered from live
-- metadata:
--   tenants.id                      (PK; the row is RE-CREATED, see below)
--   license.tenant_id               fk_license_tenant         ON DELETE RESTRICT
--   subject.tenant_id               fk_subject_tenant         ON DELETE RESTRICT
--   tenant_bundle.tenant_id         fk_tenant_bundle_tenant   ON DELETE RESTRICT
--   tenant_app_byo_mode.tenant_id   (unnamed FK)              ON DELETE CASCADE
--   subject.keycloak_sub            NO FK — a tenant-level service subject
--                                   `svc:<tenant-uuid>:<app-key>` EMBEDS the id
--                                   (models.service_subject_sub). Prod has none
--                                   for this tenant; the prefix rewrite is kept so
--                                   one minted between the census and the apply
--                                   still moves.
-- Prod, measured read-only 2026-10-07: 4 license (3 active), 3 subject (two
-- `portal` users + `svc-workers-cdmsfirst`, all name/sub-based), 1 tenant_bundle,
-- 0 tenant_app_byo_mode, 0 `svc:<old>:` subjects, 0 subjects whose keycloak_sub
-- IS or CONTAINS the old id (so, unlike 046, there is no user-sense occurrence of
-- the id in this database). 4 tokens hang off those licences and follow them:
-- introspection returns the NEW tenant id from the moment this commits.
--
-- ⛔ WHAT DELIBERATELY DOES NOT MOVE.
--   * `license.metadata`. No statement here touches that column (same rule as
--     046: it is free-form provenance).
--   * Migration 032 and its downgrade, which name the old id as the backfill
--     target of three subjects: stamped everywhere, never replayed, left
--     byte-identical. On a replayed corpus 032 writes the old id and this file
--     moves it.
--
-- HOW. Identical to 046: none of the four FKs is ON UPDATE CASCADE, so the row is
-- re-created — copy it to the new id with EVERY column under a temporary name
-- (`ix_tenants_name` is unique), repoint the children, delete the old row,
-- restore the name. The new id is refused if it already exists, so it holds no
-- rows and nothing can collide.
--
-- ONE TRANSACTION. scripts/apply_migrations.py runs each top-level statement on
-- an autocommit connection, so the whole re-key is ONE `DO` block: it commits
-- entirely or not at all, and the closing assertion aborts it.
--
-- IDEMPOTENT. Old tenant row absent => complete no-op (fresh / CI database, or a
-- second run). Old AND new both present is REFUSED rather than guessed at.
--
-- SEED. `cdmsfirst` is not in `models.PLATFORM_TENANTS` (it was registered
-- through the API), so no seed can re-create the old id.
--
-- ⚠️ OUTSIDE THIS DATABASE. billing, core_app, analytics_app, admin_app, … and
-- Keycloak's `tenant_id` user attributes carry this id with no FK to it; they are
-- moved in the same window by their own migrations and by the gitops runbook
-- `scripts/rekey/cdmsfirst-demo/`. This file cannot see or move them.
--
-- BASELINE INTERACTION: on a FRESH database the runner stamps this applied
-- without running it. Nothing is lost — no seed creates this tenant.
--
-- DOWNGRADE: migrations/downgrades/049_rekey_cdmsfirst_demo_tenant.sql

DO $$
DECLARE
    old_id    constant uuid := '0195f48b-d000-7000-8000-000000000100';
    new_id    constant uuid := '01a1117c-d9b0-72fc-aeda-a9d1187f2a36';
    tmp_name  constant text := 'rekey-049-tmp-01a1117c-d9b0-72fc-aeda-a9d1187f2a36';
    old_svc   constant text := 'svc:0195f48b-d000-7000-8000-000000000100:';
    new_svc   constant text := 'svc:01a1117c-d9b0-72fc-aeda-a9d1187f2a36:';
    orig_name text;
    n_license bigint;
    n_subject bigint;
    n_bundle  bigint;
    n_byo     bigint;
    n_svc     bigint;
    n_left    bigint;
BEGIN
    SELECT name INTO orig_name FROM tenants WHERE id = old_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE NOTICE '049: tenant % is absent — nothing to re-key (fresh database, or already applied)', old_id;
        RETURN;
    END IF;

    IF EXISTS (SELECT 1 FROM tenants WHERE id = new_id) THEN
        RAISE EXCEPTION
            '049 REFUSED: tenants holds BOTH the old id % and the new id %. This file is atomic and cannot have produced that; resolve it by hand before re-running.',
            old_id, new_id;
    END IF;

    -- 1. The new row: EVERY column of `tenants`, the id and name excepted.
    INSERT INTO tenants (id, name, keycloak_realm, byo_mode, archived_at, kind,
                         cost_centre, budget_currency, created_at)
    SELECT new_id, tmp_name, keycloak_realm, byo_mode, archived_at, kind,
           cost_centre, budget_currency, created_at
      FROM tenants
     WHERE id = old_id;

    -- 2. Repoint every column that holds the tenant id.
    UPDATE license SET tenant_id = new_id WHERE tenant_id = old_id;
    GET DIAGNOSTICS n_license = ROW_COUNT;

    UPDATE subject SET tenant_id = new_id WHERE tenant_id = old_id;
    GET DIAGNOSTICS n_subject = ROW_COUNT;

    UPDATE tenant_bundle SET tenant_id = new_id WHERE tenant_id = old_id;
    GET DIAGNOSTICS n_bundle = ROW_COUNT;

    UPDATE tenant_app_byo_mode SET tenant_id = new_id WHERE tenant_id = old_id;
    GET DIAGNOSTICS n_byo = ROW_COUNT;

    -- 3. The id EMBEDDED in a tenant-level service subject's keycloak_sub. Only
    --    the `svc:<old>:` PREFIX — never the bare principal sub (see header).
    UPDATE subject
       SET keycloak_sub = new_svc || substr(keycloak_sub, length(old_svc) + 1)
     WHERE lower(left(keycloak_sub, length(old_svc))) = old_svc;
    GET DIAGNOSTICS n_svc = ROW_COUNT;

    -- 4. Nothing references the old row now (every FK is RESTRICT or already
    --    moved), so it can go, and the name is free again.
    DELETE FROM tenants WHERE id = old_id;
    UPDATE tenants SET name = orig_name WHERE id = new_id;

    -- 5. HARD ASSERTION. Aborts — and so rolls back — the whole re-key.
    SELECT (SELECT count(*) FROM tenants             WHERE id        = old_id)
         + (SELECT count(*) FROM license             WHERE tenant_id = old_id)
         + (SELECT count(*) FROM subject             WHERE tenant_id = old_id)
         + (SELECT count(*) FROM tenant_bundle       WHERE tenant_id = old_id)
         + (SELECT count(*) FROM tenant_app_byo_mode WHERE tenant_id = old_id)
         + (SELECT count(*) FROM subject
             WHERE lower(left(keycloak_sub, length(old_svc))) = old_svc)
      INTO n_left;
    IF n_left <> 0 THEN
        RAISE EXCEPTION '049 FAILED: % row(s) still reference tenant % after the re-key', n_left, old_id;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM tenants WHERE id = new_id AND name = orig_name) THEN
        RAISE EXCEPTION '049 FAILED: tenant % does not carry the original name %', new_id, orig_name;
    END IF;

    RAISE NOTICE
        '049: re-keyed tenant % (%) -> %: % license, % subject, % tenant_bundle, % tenant_app_byo_mode, % svc: keycloak_sub',
        old_id, orig_name, new_id, n_license, n_subject, n_bundle, n_byo, n_svc;
END
$$;
