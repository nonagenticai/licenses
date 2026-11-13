-- 046: RE-KEY the `portal-render-e2e` tenant onto a fresh organisation id.
--
-- ⛔ THE SECOND SANCTIONED EXCEPTION TO "NO UUID IS EVER RE-KEYED" (045 was the
-- first). Do not copy this file as a pattern. 037's header explains why a re-key
-- is a commercial decision and not a hygiene fix; this one was TAKEN, by the
-- owner, for exactly this tenant.
--
-- WHY. `tenants.id` e148e8af-3751-49bf-a0fa-56ab46d31ddc (`portal-render-e2e`) is
-- ALSO the Keycloak `sub` of a (since deleted, 2026-08) user in realm `portal`,
-- still recorded here as `subject.keycloak_sub` of subject `8382f0b3-…`: an
-- identity that authenticates, registered as the thing that pays. That is the
-- invariant 035 / 037 report.
--
-- OWNER RULING 2026-10-06 (George): re-key `portal-render-e2e`. This SUPERSEDES
-- the same-day text in 045's header and in the retired-principal register of
-- `services.tenant_subject_invariant` ("NOT re-keyed … recorded exception"): the
-- register entry is removed in the same change, so this id on a tenant row is a
-- plain `class="violation"` again.
--
--   old tenants.id  e148e8af-3751-49bf-a0fa-56ab46d31ddc
--   NEW tenants.id  01a1113b-674f-72b0-a84b-dc746d39afca   (uuid7, minted for this file)
--
-- WHAT MOVES. Every column in this database that holds a tenant id — enumerated
-- from models.py + the corpus (re-checked for this file: the set is unchanged
-- since 045), NOT rendered from live metadata:
--   tenants.id                      (PK; the row is RE-CREATED, see below)
--   license.tenant_id               fk_license_tenant         ON DELETE RESTRICT
--   subject.tenant_id               fk_subject_tenant         ON DELETE RESTRICT
--   tenant_bundle.tenant_id         fk_tenant_bundle_tenant   ON DELETE RESTRICT
--   tenant_app_byo_mode.tenant_id   (unnamed FK)              ON DELETE CASCADE
--   subject.keycloak_sub            NO FK — a tenant-level service subject
--                                   `svc:<tenant-uuid>:<app-key>` EMBEDS the id
--                                   (models.service_subject_sub). Prod has none
--                                   for this tenant (its service subject is the
--                                   name-based `svc-inference-router-portal-render-e2e`);
--                                   the prefix rewrite is kept so one minted
--                                   between the census and the apply still moves.
-- No other table carries a tenant id: token, plan, app, bundle, bundle_app,
-- offering and region have none (token reaches its tenant through license_id).
-- There is no history / audit table in this schema, so — exactly as in 045 —
-- there is none to move or to exclude.
-- Prod, measured read-only 2026-10-06: 11 license (6 active, 5 revoked),
-- 1 subject, 1 tenant_bundle, 0 tenant_app_byo_mode, 0 `svc:<old>:` subjects.
-- 8 tokens hang off those licences and follow them: introspection returns the
-- NEW tenant id from the moment this commits.
--
-- ⛔ WHAT DELIBERATELY DOES NOT MOVE.
--   * The subject whose `keycloak_sub` IS e148e8af-… (subject `8382f0b3-…`,
--     tenant_id NULL). That value is the PRINCIPAL's `sub`, not a reference to
--     the tenant; `get_subject_by_keycloak_sub` and every provisioning call
--     (`provision_service_token.py --subject e148e8af-…`) look it up by it.
--     Leaving it is the whole point: after this file the principal still exists
--     as a subject and NO tenant shares its id. (It also keeps the old id
--     un-registerable: `ensure_tenant` and `seed_platform_tenants` both refuse a
--     tenant id that is a subject's `keycloak_sub`.)
--   * `license.metadata`. Two rows name e148e8af-… as the provisioning ACTOR —
--     the user sense again. No statement here touches that column.
--
-- HOW. None of the four FKs is ON UPDATE CASCADE, so `UPDATE tenants SET id`
-- fails. The row is re-created: copy it to the new id with EVERY column under a
-- temporary name (`ix_tenants_name` is unique), repoint the children, delete the
-- old row, restore the name. Moving a licence between tenant ids cannot collide
-- on `uq_license_active_tenant_app_subject`, nor a grant on `tenant_bundle_pkey`:
-- the new id is refused if it already exists, so it holds no rows.
--
-- ONE TRANSACTION. scripts/apply_migrations.py runs each top-level statement on
-- an autocommit connection, so the whole re-key is ONE `DO` block: it commits
-- entirely or not at all, and the closing assertion aborts it.
--
-- IDEMPOTENT. Old tenant row absent => complete no-op. That covers a fresh / CI
-- database and a second run. Old AND new both present cannot be produced by this
-- file (it is atomic) and is REFUSED rather than guessed at.
--
-- ⚠️ THE SEED MUST MOVE WITH IT. `models.PLATFORM_TENANTS` names the NEW id as of
-- this change. Boot order is create -> migrate -> seed, so this file runs before
-- the seed looks. 013 (which INSERTs the old id) is stamped everywhere and is
-- never replayed by the runner, so it is left byte-identical.
--
-- ⚠️ OUTSIDE THIS DATABASE. `tenants.id` is what billing keys on and what w2v / fr
-- hard-code; those rows carry no FK to it (037's header). This file cannot see or
-- move them; whatever was written against the old id there stays under the old id.
--
-- BASELINE INTERACTION: on a FRESH database the runner stamps this applied
-- without running it. Nothing is lost — the seed creates the tenant on the new id.
--
-- DOWNGRADE: migrations/downgrades/046_rekey_portal_render_e2e_tenant.sql

DO $$
DECLARE
    old_id    constant uuid := 'e148e8af-3751-49bf-a0fa-56ab46d31ddc';
    new_id    constant uuid := '01a1113b-674f-72b0-a84b-dc746d39afca';
    tmp_name  constant text := 'rekey-046-tmp-01a1113b-674f-72b0-a84b-dc746d39afca';
    old_svc   constant text := 'svc:e148e8af-3751-49bf-a0fa-56ab46d31ddc:';
    new_svc   constant text := 'svc:01a1113b-674f-72b0-a84b-dc746d39afca:';
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
        RAISE NOTICE '046: tenant % is absent — nothing to re-key (fresh database, or already applied)', old_id;
        RETURN;
    END IF;

    IF EXISTS (SELECT 1 FROM tenants WHERE id = new_id) THEN
        RAISE EXCEPTION
            '046 REFUSED: tenants holds BOTH the old id % and the new id %. This file is atomic and cannot have produced that; resolve it by hand before re-running.',
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
        RAISE EXCEPTION '046 FAILED: % row(s) still reference tenant % after the re-key', n_left, old_id;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM tenants WHERE id = new_id AND name = orig_name) THEN
        RAISE EXCEPTION '046 FAILED: tenant % does not carry the original name %', new_id, orig_name;
    END IF;

    RAISE NOTICE
        '046: re-keyed tenant % (%) -> %: % license, % subject, % tenant_bundle, % tenant_app_byo_mode, % svc: keycloak_sub',
        old_id, orig_name, new_id, n_license, n_subject, n_bundle, n_byo, n_svc;
END
$$;
