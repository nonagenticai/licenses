-- 033: close three unconstrained-enum gaps in one migration.
--
-- 1. `license.status` is NOT NULL but had no CHECK. `effective_status()`'s own
--    docstring says a stored value outside the known set is returned VERBATIM
--    rather than normalised — a design that matters only because nothing
--    stopped an arbitrary value landing in the column in the first place.
--    Measured on prod 2026-09-07: every one of 258 rows holds 'active' (164),
--    'revoked' (92) or 'expired' (2). The vocabulary here is FOUR values, not
--    three: the column's own comment (models.py) and
--    tests/unit/test_effective_status_contract.py::TestStoredPendingIsTheIntendedImprovement
--    both name `pending` as an intended (if not yet written) stored state — 0
--    rows hold it and no code path writes it, but excluding it would foreclose
--    a documented direction for no operational benefit, and would break
--    tests/unit/test_models.py::TestLicenseModel::test_license_is_active_status_logic,
--    which persists it today to pin `is_active()`'s handling of an
--    unrecognised-but-anticipated status.
--
-- 2. `token.token_type` is NOT NULL, no CHECK. Measured on prod 2026-09-07:
--    'api' (176) and 'internal' (74), zero 'node' rows. 'node' is included
--    anyway: `main.TokenType` (a StrEnum) declares it and
--    `POST /licenses/{id}/tokens?token_type=node` is a LIVE, reachable route
--    that persists it (`create_license_token` -> `token_type.value` ->
--    `db_manager.create_token`). Zero rows today only means nobody has minted
--    one yet; constraining to two values would 500 the first caller who does.
--
-- 3. Migration 031's own header proposed exactly
--    `CHECK(NOT(kind='service' AND byo_mode='byo_required'))` and declined to
--    add it because `scripts/provision_service_token.py`'s insert path could
--    not set `byo_mode` — every service principal it minted was born
--    kind='service' + byo_mode='byo_required', which this very constraint would
--    have rejected on the live provisioning path (ext_authz, replicas: 2).
--
--    That ONE call site was fixed in #183: `DatabaseManager.ensure_tenant` now
--    accepts `byo_mode` and `provision_service_token.py` passes
--    `byo_mode="local_allowed"` alongside `kind="service"`. But `ensure_tenant`
--    itself did not COUPLE the two — `kind="service"` with no explicit
--    `byo_mode` still fell through to the column's own
--    `server_default('byo_required')`. Measured directly against an empty
--    schema (2026-09-07): `ensure_tenant(uuid4(), name=..., kind="service")` —
--    no `byo_mode` — persisted `byo_mode='byo_required'`. Several existing test
--    call sites relied on exactly that fallthrough
--    (tests/unit/test_tenant_rename_and_archive.py,
--    tests/unit/test_endpoints.py), so the incoherent combination was still
--    reachable through the shared primitive, not just through the one caller
--    031 named.
--
--    This migration's CHECK is therefore shipped in the SAME commit as the
--    primitive fix: `ensure_tenant(kind="service")` with no explicit
--    `byo_mode` now defaults to `'local_allowed'`, and an EXPLICIT
--    `byo_mode='byo_required'` for `kind='service'` is refused with a named
--    `ClientSafeError` before it ever reaches this constraint. See
--    `models.py::DatabaseManager.ensure_tenant`.
--
-- Measured live 2026-09-07: 0 tenants rows currently hold
-- `kind='service' AND byo_mode='byo_required'` (migration 031 already fixed
-- the 34 that had accumulated, and nothing has re-created the combination
-- since). The guarded EXISTS below re-checks that at apply time.
--
-- SAFETY. A failed migration raises out of `_run_db_bootstrap` and the pod
-- REFUSES TO SERVE — this service backs ext_authz for the whole platform at
-- replicas: 2. So, matching 021's shape:
--   * each ADD CONSTRAINT is guarded on `pg_constraint`, so a re-run is a no-op;
--   * each is preceded by a row count against the CHECK's own predicate, and a
--     violation is a NAMED `RAISE EXCEPTION` rather than a bare constraint
--     failure — an operator reading the CrashLoop knows what to fix;
--   * no DELETE, no UPDATE, no data is touched — this file is schema-only.
--
-- BASELINE INTERACTION (scripts/apply_migrations.py). On a FRESH database this
-- file never executes — the runner stamps it as applied without running it.
-- All three constraints are ALSO declared in the ORM (models.py: License,
-- Token, Tenant `__table_args__`), which is what puts them on a fresh
-- database; this file puts them on an EXISTING one. Mirrors 021's own note.
--
-- DOWNGRADE: migrations/downgrades/033_enum_checks_and_service_byo_guard.sql

DO $$
DECLARE
    bad_rows bigint;
BEGIN
    -- ck_license_status ------------------------------------------------------
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_license_status' AND conrelid = 'license'::regclass
    ) THEN
        RAISE NOTICE '033: ck_license_status already present — nothing to do';
    ELSE
        SELECT count(*) INTO bad_rows
        FROM license
        WHERE status NOT IN ('active', 'revoked', 'expired', 'pending');

        IF bad_rows > 0 THEN
            RAISE EXCEPTION
                'refusing to add ck_license_status: % license row(s) hold a status '
                'outside (active, revoked, expired, pending). Fix the data first — '
                'SELECT id, status FROM license WHERE status NOT IN '
                '(''active'', ''revoked'', ''expired'', ''pending'');',
                bad_rows;
        END IF;

        ALTER TABLE license
            ADD CONSTRAINT ck_license_status
            CHECK (status IN ('active', 'revoked', 'expired', 'pending'));
        RAISE NOTICE '033: ck_license_status added';
    END IF;

    -- ck_token_token_type ------------------------------------------------------
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_token_token_type' AND conrelid = 'token'::regclass
    ) THEN
        RAISE NOTICE '033: ck_token_token_type already present — nothing to do';
    ELSE
        SELECT count(*) INTO bad_rows
        FROM token
        WHERE token_type NOT IN ('api', 'node', 'internal');

        IF bad_rows > 0 THEN
            RAISE EXCEPTION
                'refusing to add ck_token_token_type: % token row(s) hold a '
                'token_type outside (api, node, internal). Fix the data first — '
                'SELECT id, token_type FROM token WHERE token_type NOT IN '
                '(''api'', ''node'', ''internal'');',
                bad_rows;
        END IF;

        ALTER TABLE token
            ADD CONSTRAINT ck_token_token_type
            CHECK (token_type IN ('api', 'node', 'internal'));
        RAISE NOTICE '033: ck_token_token_type added';
    END IF;

    -- ck_tenants_service_not_byo_required ---------------------------------
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_tenants_service_not_byo_required'
          AND conrelid = 'tenants'::regclass
    ) THEN
        RAISE NOTICE '033: ck_tenants_service_not_byo_required already present — nothing to do';
    ELSE
        SELECT count(*) INTO bad_rows
        FROM tenants
        WHERE kind = 'service' AND byo_mode = 'byo_required';

        IF bad_rows > 0 THEN
            RAISE EXCEPTION
                'refusing to add ck_tenants_service_not_byo_required: % tenant '
                'row(s) hold kind=''service'' AND byo_mode=''byo_required''. Fix '
                'the data first (see migration 031) — SELECT id, name FROM '
                'tenants WHERE kind = ''service'' AND byo_mode = ''byo_required'';',
                bad_rows;
        END IF;

        ALTER TABLE tenants
            ADD CONSTRAINT ck_tenants_service_not_byo_required
            CHECK (NOT (kind = 'service' AND byo_mode = 'byo_required'));
        RAISE NOTICE '033: ck_tenants_service_not_byo_required added';
    END IF;
END $$;

-- Verify
SELECT conrelid::regclass AS table_name, conname, pg_get_constraintdef(oid)
FROM pg_constraint
WHERE conname IN (
    'ck_license_status', 'ck_token_token_type', 'ck_tenants_service_not_byo_required'
)
ORDER BY conname;
