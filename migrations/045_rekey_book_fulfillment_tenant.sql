-- 045: RE-KEY the `book-fulfillment` tenant onto a fresh organisation id.
--
-- ⛔ THIS IS THE SINGLE SANCTIONED EXCEPTION TO "NO UUID IS EVER RE-KEYED".
-- Do not copy this file as a pattern. 037's header explains why a re-key is a
-- commercial decision and not a hygiene fix; this one was TAKEN, by the owner,
-- for exactly one tenant.
--
-- WHY. `tenants.id` 1d0090ac-adbf-4c07-b704-bc66061b572e (`book-fulfillment`) is
-- ALSO the Keycloak `sub` of the LIVE service account
-- `service-account-book-fulfillment-svc` (realm `secure-apps`): an identity that
-- authenticates, registered as the thing that pays. That is the invariant 035 /
-- 037 report and `nonagentic_license_tenant_is_subject_rows{class="violation"}`
-- counts, and it is what keeps `LicenseTenantIdIsKeycloakPrincipal` firing.
--
-- OWNER RULING 2026-10-06 (George): re-key `book-fulfillment` — it is only his own
-- website tenant. The other offender, `e148e8af-…` `portal-render-e2e`, is NOT
-- re-keyed (~18k rows in 26 databases, hard-coded in w2v workflows and fr; its
-- Keycloak user was deleted 2026-08): it is a recorded exception in
-- `services.tenant_subject_invariant.RETIRED_PRINCIPAL_TENANT_IDS` instead.
--
--   old tenants.id  1d0090ac-adbf-4c07-b704-bc66061b572e
--   NEW tenants.id  01a1109c-f895-7766-ba28-6ea56e56a5f7   (minted for this file)
--
-- WHAT MOVES. Every column in this database that holds a tenant id — enumerated
-- from models.py + the corpus, NOT rendered from live metadata:
--   tenants.id                      (PK; the row is RE-CREATED, see below)
--   license.tenant_id               fk_license_tenant         ON DELETE RESTRICT
--   subject.tenant_id               fk_subject_tenant         ON DELETE RESTRICT
--   tenant_bundle.tenant_id         fk_tenant_bundle_tenant   ON DELETE RESTRICT
--   tenant_app_byo_mode.tenant_id   (unnamed FK)              ON DELETE CASCADE
--   subject.keycloak_sub            NO FK — the tenant-level service subject
--                                   `svc:<tenant-uuid>:<app-key>` EMBEDS the id
--                                   (models.service_subject_sub). Left alone,
--                                   scripts/provision_tenant_bundle.py would mint
--                                   a DUPLICATE subject under the new id.
-- No other table carries a tenant id: token, plan, app, bundle, bundle_app,
-- offering and region have none (token reaches its tenant through license_id).
-- Prod, measured read-only 2026-10-06: 2 license, 2 subject, 1 tenant_bundle.
--
-- ⛔ WHAT DELIBERATELY DOES NOT MOVE: the subject whose `keycloak_sub` IS
-- 1d0090ac-… . That value is the Keycloak PRINCIPAL's `sub` claim, not a
-- reference to the tenant — it is what a live token carries, and rewriting it
-- would lock the service account out. Leaving it is the whole point: after this
-- file the principal still exists as a subject and NO tenant shares its id.
-- (It also keeps the old id un-registerable: `ensure_tenant` refuses a tenant id
-- that is a subject's `keycloak_sub`.)
--
-- HOW. None of the four FKs is ON UPDATE CASCADE, so `UPDATE tenants SET id`
-- fails. The row is re-created: copy it to the new id with EVERY column under a
-- temporary name (`ix_tenants_name` is unique), repoint the children, delete the
-- old row, restore the name.
--
-- ONE TRANSACTION. scripts/apply_migrations.py runs each top-level statement on
-- an autocommit connection, so the whole re-key is ONE `DO` block: it commits
-- entirely or not at all, and the closing assertion aborts it.
--
-- IDEMPOTENT. Old tenant row absent => complete no-op. That covers a fresh / CI
-- database and a second run. Old AND new both present cannot be produced by this
-- file (it is atomic) and is REFUSED rather than guessed at.
--
-- ⚠️ OUTSIDE THIS DATABASE. `tenants.id` is what the billing service keys on, and
-- billing rows carry no FK to it (037's header). This file cannot see or move
-- them; whatever was written against the old id there stays under the old id.
--
-- BASELINE INTERACTION: on a FRESH database the runner stamps this applied
-- without running it. Nothing is lost — a fresh database has no such tenant.
--
-- DOWNGRADE: migrations/downgrades/045_rekey_book_fulfillment_tenant.sql

DO $$
DECLARE
    old_id    constant uuid := '1d0090ac-adbf-4c07-b704-bc66061b572e';
    new_id    constant uuid := '01a1109c-f895-7766-ba28-6ea56e56a5f7';
    tmp_name  constant text := 'rekey-045-tmp-01a1109c-f895-7766-ba28-6ea56e56a5f7';
    old_svc   constant text := 'svc:1d0090ac-adbf-4c07-b704-bc66061b572e:';
    new_svc   constant text := 'svc:01a1109c-f895-7766-ba28-6ea56e56a5f7:';
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
        RAISE NOTICE '045: tenant % is absent — nothing to re-key (fresh database, or already applied)', old_id;
        RETURN;
    END IF;

    IF EXISTS (SELECT 1 FROM tenants WHERE id = new_id) THEN
        RAISE EXCEPTION
            '045 REFUSED: tenants holds BOTH the old id % and the new id %. This file is atomic and cannot have produced that; resolve it by hand before re-running.',
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
        RAISE EXCEPTION '045 FAILED: % row(s) still reference tenant % after the re-key', n_left, old_id;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM tenants WHERE id = new_id AND name = orig_name) THEN
        RAISE EXCEPTION '045 FAILED: tenant % does not carry the original name %', new_id, orig_name;
    END IF;

    RAISE NOTICE
        '045: re-keyed tenant % (%) -> %: % license, % subject, % tenant_bundle, % tenant_app_byo_mode, % svc: keycloak_sub',
        old_id, orig_name, new_id, n_license, n_subject, n_bundle, n_byo, n_svc;
END
$$;
