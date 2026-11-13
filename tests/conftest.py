"""
Pytest configuration and fixtures for licenses service tests.

Requires PostgreSQL. Order: TEST_DATABASE_URL -> DATABASE_URL -> a throwaway
postgres:18 container started by tests/support/ephemeral_postgres.py (random
127.0.0.1 port, removed at session end) -> no docker: the run exits. There is no
host-database default.
Authentication is mocked by patching the validator to return mock users.
"""

import os
import sys
import time
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import jwt

from tests.support.jwt_keys import TEST_ISSUER, private_key_pem, public_key

# Force development mode BEFORE main / auth.config get imported anywhere in this
# test session — main.py + auth/config.py contain a production fail-fast
# validator that requires service URLs to be set via gitops env, and we don't
# (and shouldn't) ship those in the unit-test environment.
os.environ.setdefault("ENVIRONMENT", "development")

# Resolve the test DB URL in priority order:
#   1. TEST_DATABASE_URL  — explicit local/dev override
#   2. DATABASE_URL        — provided by the CI archetype (python-service.yml);
#                            without this the suite silently SKIPPED every DB
#                            test in CI.
#   3. an ephemeral postgres:18 container (tests/support/ephemeral_postgres.py),
#      removed by its own id at session end (pytest_unconfigure + atexit).
#   No docker either ⇒ pytest.exit. NEVER a host localhost default.
from tests.support.ephemeral_postgres import (  # noqa: E402
    remove_ephemeral_postgres,
    resolve_database_url,
)

TEST_DATABASE_URL = resolve_database_url("licenses", label="licenses")
# main.py refuses to import without DATABASE_URL (no localhost default), and the
# integration conftest reads it — point both at the same database.
os.environ.setdefault("DATABASE_URL", TEST_DATABASE_URL)


def pytest_unconfigure(config):
    """Remove the ephemeral container (if this process started one), pass or fail."""
    remove_ephemeral_postgres()


import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth.models import KeycloakUser  # noqa: E402
from models import App, Base, DatabaseManager, License, Plan, Region, Subject, Token  # noqa: E402

# ---------------------------------------------------------------------------
# Original model methods exposed for test_models.py
# With PostgreSQL, these are the actual production methods (no monkey-patching).
# ---------------------------------------------------------------------------


def _original_license_is_active(lic: License) -> bool:
    """Delegate to License.is_active -- used by TestOriginalIsActiveMethods."""
    return License.is_active(lic)


def _original_token_is_valid(tok: Token) -> bool:
    """Delegate to Token.is_valid -- used by TestOriginalIsActiveMethods."""
    return Token.is_valid(tok)


# =============================================================================
# Database Fixtures
# =============================================================================


def _hard_reset_schema(engine) -> None:
    """Drop and recreate the public schema.

    This is more reliable than ``Base.metadata.drop_all`` for test isolation:

    - ``drop_all`` only drops tables it knows about; PostgreSQL TYPE objects
      (custom enums, composite types) and any leftover tables from a
      previously-crashed session survive, producing ``DuplicateTable`` /
      ``pg_type_typname_nsp_index`` errors on the next run.
    - Open transactions from leaked sessions can block DROP TABLE; resetting
      the whole schema in a single AUTOCOMMIT statement after terminating
      other backends sidesteps the lock contention entirely.
    """
    # Use AUTOCOMMIT so DROP/CREATE SCHEMA isn't wrapped in an implicit tx
    # that other lingering sessions could deadlock against.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        # Terminate any leftover backends (other than ours) holding locks
        # on the licenses DB. Without this, a session leaked from a previous
        # test can block DROP SCHEMA forever.
        conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) "
                "FROM pg_stat_activity "
                "WHERE datname = current_database() "
                "AND pid <> pg_backend_pid()"
            )
        )
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
        conn.execute(text("GRANT ALL ON SCHEMA public TO public"))


# The well-known tenant_ids the seed fixtures / unit tests mint licenses under.
# Since migration 016, license.tenant_id is a real FK to tenants.id, so these MUST
# exist before any license insert. Registered by _truncate_all after each per-test
# reset (the TRUNCATE wipes tenants), coupled to the unit db_engine lifecycle so
# integration tests — which manage their own DB — are unaffected.
SEED_TENANT_ID = "01000000-0000-7000-8000-000000000001"
SEED_OTHER_TENANT_ID = "01000000-0000-7000-8000-000000000002"


def _truncate_all(engine) -> None:
    """Fast per-test reset: empty every table and restart its sequences in a
    single statement. CASCADE handles FK ordering; RESTART IDENTITY mirrors the
    fresh-ids behaviour of the old per-test drop/create cycle. One round-trip
    instead of ~dozens of DDL statements — see ``db_engine``."""
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    if not tables:
        return
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        # Migration 050 reference rows: every tenant row FKs an owner (default
        # 'platform'), and the auth middleware resolves each caller's owner from
        # api_client. Same rows the boot seed writes on a fresh database.
        _seed_ownership_reference(conn)
        # Register the standard test tenants so license inserts satisfy the FK
        # (license.tenant_id -> tenants.id, migration 016). Done here (coupled to
        # the unit db_engine lifecycle) rather than an autouse fixture, so it does
        # NOT drag this truncate machinery into integration tests, which manage
        # their own DB + tenant registration via their own conftest.
        conn.execute(
            text(
                "INSERT INTO tenants (id, name, byo_mode, owner_key, created_at) VALUES "
                "(:p, 'seed-tenant-primary', 'byo_required', 'platform', now()), "
                "(:o, 'seed-tenant-other', 'byo_required', 'platform', now()) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"p": SEED_TENANT_ID, "o": SEED_OTHER_TENANT_ID},
        )
        # schema_migrations is NOT a SQLAlchemy model (apply_migrations creates it
        # via raw DDL), so TRUNCATE above misses it. Drop it each test so the next
        # apply_migrations run (in a TestClient lifespan) BASELINES cleanly against
        # the create_all'd schema instead of inheriting stray stamps left by
        # test_apply_migrations (which apply_all's a tmp-dir set). Without this, a
        # polluted tracking table makes the runner skip baseline and REPLAY the
        # historical migrations (e.g. 004's text-column backfill) against the
        # already-final UUID schema — a hard failure since migration 016.
        conn.execute(text("DROP TABLE IF EXISTS schema_migrations"))


def _seed_ownership_reference(conn) -> None:
    """Insert models.TENANT_OWNERS + models.API_CLIENTS (migration 050)."""
    from models import API_CLIENTS, REALM_OWNERS, TENANT_OWNERS

    for key, display in TENANT_OWNERS:
        conn.execute(
            text(
                "INSERT INTO tenant_owner (key, display_name) VALUES (:k, :d) "
                "ON CONFLICT (key) DO NOTHING"
            ),
            {"k": key, "d": display},
        )
    for realm, client_id, owner, cross in API_CLIENTS:
        conn.execute(
            text(
                "INSERT INTO api_client (realm, client_id, owner_key, cross_owner) "
                "VALUES (:r, :c, :o, :x) ON CONFLICT (realm, client_id) DO NOTHING"
            ),
            {"r": realm, "c": client_id, "o": owner, "x": cross},
        )
    for realm, owner in REALM_OWNERS:
        conn.execute(
            text(
                "INSERT INTO realm_owner (realm, owner_key) VALUES (:r, :o) "
                "ON CONFLICT (realm) DO NOTHING"
            ),
            {"r": realm, "o": owner},
        )


def _unreachable_database(exc: Exception):
    """Skip locally, but FAIL in CI, when Postgres cannot be reached.

    A skip is right on a laptop with no database running. It is wrong in CI, and
    the coverage floor cannot save us: measured on this suite with the DSN pointed
    at a closed port, every DB-backed test skips and the run reports

        491 passed, 678 skipped ... Total coverage: 44.33%
        Required test coverage of 42.0% reached.

    — exit 0, green, with 58% of the suite silently gone. That is exactly the
    historical incident recorded at the top of this file (DATABASE_URL absent in
    CI ⇒ every DB test skipped). `fail_under = 42` is BELOW the no-database
    coverage level, so it can never fire on it; raising the number is the wrong
    instrument for "did the database exist". This is the right one.
    """
    if os.getenv("CI"):
        pytest.fail(
            f"PostgreSQL is unreachable at {TEST_DATABASE_URL!r} and CI is set, so this "
            f"is a FAILURE, not a skip: {exc}\n"
            "Skipping here is how the whole DB-backed suite once vanished from CI "
            "while the build stayed green. If the CI database is genuinely optional "
            "for this job, unset CI for it — do not soften this back to a skip.",
            pytrace=False,
        )
    pytest.skip(f"PostgreSQL not available: {exc}")


@pytest.fixture(scope="session", autouse=True)
def _session_schema_reset():
    """Wipe leftover state from any previous pytest run before tests start.

    Without this, re-running ``uv run pytest`` after a crashed/aborted run
    fails with ``DuplicateTable: relation "region" already exists`` and
    ``pg_type_typname_nsp_index ... Key (typname, typnamespace)=...
    already exists`` because ``Base.metadata.drop_all`` only drops tables
    it currently knows about and skips any orphan tables / PostgreSQL
    TYPE objects left behind by a previous interpreter.

    Doing a hard ``DROP SCHEMA public CASCADE`` once at session start is
    cheap (one statement) and guarantees the per-test fixture's
    ``drop_all`` / ``create_all`` cycle starts from a clean slate.
    """
    engine = create_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        engine.dispose()
        # Per-test fixture will pytest.skip with a clear message
        yield
        return
    try:
        _hard_reset_schema(engine)
        # Create the full schema ONCE per session. Per-test isolation is a fast
        # TRUNCATE (see db_engine), not a per-test drop/create.
        Base.metadata.create_all(bind=engine)
    finally:
        engine.dispose()
    yield
    # Session teardown: drop everything so the next run starts clean.
    teardown_engine = create_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
    try:
        Base.metadata.drop_all(bind=teardown_engine)
    except Exception:
        pass
    finally:
        teardown_engine.dispose()


@pytest.fixture(scope="function")
def db_engine(_session_schema_reset):
    """Create a PostgreSQL engine for each test function.

    Tables are created once per session (``_session_schema_reset``); each test
    starts from a clean slate via a single TRUNCATE. The previous approach did
    ``drop_all`` + ``create_all`` per test (~dozens of DDL round-trips) — fine
    on localhost but enough to blow the CI job timeout against the
    network-latency CI postgres (this suite has 270+ DB-backed tests).
    """
    engine = create_engine(TEST_DATABASE_URL, echo=False)

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as e:
        engine.dispose()
        _unreachable_database(e)

    _truncate_all(engine)

    yield engine

    engine.dispose()


@pytest.fixture
def db_manager(db_engine) -> DatabaseManager:
    """Create DatabaseManager with test database."""
    manager = DatabaseManager.__new__(DatabaseManager)
    manager.engine = db_engine
    manager.SessionLocal = sessionmaker(
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
        bind=db_engine,
    )
    return manager


# =============================================================================
# Mock User Fixtures
# =============================================================================


@pytest.fixture
def admin_user() -> KeycloakUser:
    """Admin user with license-admin role."""
    return KeycloakUser(
        sub="admin-user-uuid",
        preferred_username="admin",
        email="admin@test.com",
        realm_access={"roles": ["license-admin"]},
        resource_access={},
        iss="https://keycloak.example.com/realms/secure-apps",
    )


@pytest.fixture
def manager_user() -> KeycloakUser:
    """Manager user with license-manager role."""
    return KeycloakUser(
        sub="manager-user-uuid",
        preferred_username="manager",
        email="manager@test.com",
        realm_access={"roles": ["license-manager"]},
        resource_access={},
        iss="https://keycloak.example.com/realms/secure-apps",
    )


@pytest.fixture
def reader_user() -> KeycloakUser:
    """Reader user with license-reader role."""
    return KeycloakUser(
        sub="reader-user-uuid",
        preferred_username="reader",
        email="reader@test.com",
        realm_access={"roles": ["license-reader"]},
        resource_access={},
        iss="https://keycloak.example.com/realms/secure-apps",
    )


@pytest.fixture
def no_role_user() -> KeycloakUser:
    """User with no license roles."""
    return KeycloakUser(
        sub="norole-user-uuid",
        preferred_username="norole",
        email="norole@test.com",
        realm_access={"roles": []},
        resource_access={},
        iss="https://keycloak.example.com/realms/secure-apps",
    )


# =============================================================================
# Module-Global Safety Net
# =============================================================================
#
# Several tests directly mutate module-level globals (main.db_manager,
# service_auth._provider) instead of using monkeypatch. If such a test errors
# between the "save original" and "restore original" lines, the global stays
# clobbered and contaminates every subsequent test in the session.
#
# This autouse fixture snapshots-and-restores those globals around every test
# unconditionally, so even a leak is bounded to a single test.


@pytest.fixture(autouse=True)
def _restore_module_globals():
    """Snapshot+restore mutable module globals around every test.

    Defensive against direct assignments like `main.db_manager = ...` or
    `service_auth._provider = ...` that may not be restored on test error.
    """
    import main
    import service_auth

    orig_db_manager = main.db_manager
    orig_provider = service_auth._provider
    try:
        yield
    finally:
        main.db_manager = orig_db_manager
        service_auth._provider = orig_provider


# =============================================================================
# Application and Client Fixtures
# =============================================================================

# Global reference to current test's db_manager for patching
_current_db_manager: DatabaseManager | None = None


def _get_test_db_manager() -> DatabaseManager:
    """Get the current test's db_manager (used in patch)."""
    if _current_db_manager is None:
        raise RuntimeError("No db_manager set for test")
    return _current_db_manager


@pytest.fixture
def client(db_manager) -> Generator[TestClient]:
    """Create test client with mocked database (no auth mocking)."""
    global _current_db_manager
    _current_db_manager = db_manager

    # Import main and directly replace db_manager
    import main

    original_db_manager = main.db_manager
    main.db_manager = db_manager

    try:
        with TestClient(main.app, raise_server_exceptions=False) as test_client:
            yield test_client
    finally:
        main.db_manager = original_db_manager

    _current_db_manager = None


@pytest.fixture
def mock_no_auth():
    """Marker fixture indicating no auth."""
    yield


@pytest.fixture(autouse=True)
def _trust_test_signing_key(monkeypatch):
    """Make the middleware's JWT verification accept locally-signed tokens.

    The middleware now verifies signatures (auth/jwt_verify.py), so the old
    `header.payload.mock_signature` tokens would 401 every authenticated test.
    Rather than edit hundreds of call sites, this autouse fixture points the
    JWKS seam at the in-process test key and trusts the fixtures' issuer.

    NOTE this deliberately does NOT disable verification — tokens must still be
    genuinely signed by `tests/support/jwt_keys`. That is what keeps
    tests/unit/test_jwt_signature_verification.py meaningful: a forged token
    fails here exactly as it would in production.
    """
    import auth.jwt_verify as jv

    monkeypatch.setenv("KEYCLOAK_TRUSTED_ISSUERS", TEST_ISSUER)
    jv._jwks_client.cache_clear()
    monkeypatch.setattr(jv, "_signing_key", lambda token, issuer: public_key())
    yield
    jv._jwks_client.cache_clear()


@pytest.fixture
def no_realm_owner(monkeypatch):
    """For DB-less minimal-app middleware tests: no realm has a realm_owner row.

    The real resolver (registered by ``import main``) queries the database and a
    fault is a fail-closed 503 ``realm_owner_unresolved`` — correct in production,
    but a minimal app with no schema would then 503 every JWT for a reason
    unrelated to what the test exercises. Same seam as ``_owner_resolver``.
    """
    import auth.middleware as mw

    monkeypatch.setattr(mw, "_realm_owner_resolver", lambda realm: None)


def _make_mock_jwt(
    user: KeycloakUser, *, azp: str = "licenses-admin", iss: str = TEST_ISSUER
) -> str:
    """Create a REAL RS256-signed JWT with the user's claims.

    Signed with the throwaway keypair in tests/support/jwt_keys so it passes
    the middleware's signature + issuer + expiry checks. The issuer is forced
    to TEST_ISSUER because that is what `_trust_test_signing_key` trusts.

    ``azp`` defaults to ``licenses-admin`` — with TEST_ISSUER's ``secure-apps``
    realm that is the ONE cross_owner (break-glass) api_client (migration 050),
    so the generic fixtures keep exercising their own feature, not ownership:
    they may retarget any tenant via X-Tenant-Id and issue any app. Ownership is
    tested with real owner clients (e.g. ``azp="fr-backend"`` -> owner ``fr``,
    ``"commonpractice-licenses-admin"`` -> ``commonpractice``) in
    tests/unit/test_tenant_ownership.py. Pass an azp absent from api_client to
    exercise the unknown-client / ignored-header path. Pass ``iss`` to mint as another issuer; the test must
    then also trust it (KEYCLOAK_TRUSTED_ISSUERS) or the token 401s.
    """
    payload = {
        "sub": user.sub,
        "preferred_username": user.preferred_username,
        "email": user.email,
        "email_verified": user.email_verified,
        "name": user.name,
        "realm_access": user.realm_access,
        "resource_access": user.resource_access,
        "azp": azp,
        "exp": int(time.time()) + 3600,
        "iat": int(time.time()),
        "iss": iss,
    }
    return jwt.encode(payload, private_key_pem(), algorithm="RS256")


def _make_authenticated_client(
    db_manager: DatabaseManager,
    user: KeycloakUser,
    tenant_id: str = "01000000-0000-7000-8000-000000000001",
) -> Generator[TestClient]:
    """Create authenticated test client.

    Args:
        db_manager: Test database manager
        user: KeycloakUser with roles for RBAC tests
        tenant_id: Tenant ID header value (must match seed data tenant_id)
    """
    global _current_db_manager
    _current_db_manager = db_manager

    # Import main and directly replace db_manager
    import main

    original_db_manager = main.db_manager
    main.db_manager = db_manager

    # Register the client's tenant so POST /licenses/user/{sub} passes the mint
    # guard ("tenant_id must be a REGISTERED tenant" — the license.tenant_id ->
    # tenants.id FK invariant). Idempotent; the name is derived from the tenant_id
    # so distinct client tenants never collide on the unique name. Guard tests that
    # need an UNREGISTERED tenant override X-Tenant-Id per-request, so they are
    # unaffected.
    db_manager.ensure_tenant(tenant_id, name=f"test-tenant-{tenant_id[:8]}", owner_key="platform")

    try:
        with TestClient(main.app, raise_server_exceptions=False) as test_client:
            # Create a mock JWT with user claims for role-based tests
            # The middleware decodes JWT payload without validation
            mock_jwt = _make_mock_jwt(user)
            test_client.headers["Authorization"] = f"Bearer {mock_jwt}"
            # Add X-Tenant-Id header to match seed data (default: tenant-1)
            test_client.headers["X-Tenant-Id"] = tenant_id
            yield test_client
    finally:
        main.db_manager = original_db_manager

    _current_db_manager = None


@pytest.fixture
def auth_client_admin(db_manager, admin_user) -> Generator[TestClient]:
    """Test client authenticated as admin."""
    yield from _make_authenticated_client(db_manager, admin_user)


@pytest.fixture
def auth_client_manager(db_manager, manager_user) -> Generator[TestClient]:
    """Test client authenticated as manager."""
    yield from _make_authenticated_client(db_manager, manager_user)


@pytest.fixture
def auth_client_reader(db_manager, reader_user) -> Generator[TestClient]:
    """Test client authenticated as reader."""
    yield from _make_authenticated_client(db_manager, reader_user)


@pytest.fixture
def auth_client_no_role(db_manager, no_role_user) -> Generator[TestClient]:
    """Test client authenticated as user with no roles."""
    yield from _make_authenticated_client(db_manager, no_role_user)


# =============================================================================
# Seed Data Fixtures
# =============================================================================


@pytest.fixture
def seed_region(db_manager) -> Region:
    """Create a test region."""
    return db_manager.create_region(code="us", name="United States", active=True)


@pytest.fixture
def seed_app(db_manager) -> App:
    """Create a test app."""
    return db_manager.create_app(
        key="test-app",
        name="Test Application",
        description="A test application",
        issuers=("platform",),
    )


@pytest.fixture
def seed_plan(db_manager, seed_app) -> Plan:
    """Create a test plan."""
    return db_manager.create_plan(
        app_id=UUID(str(seed_app.id)),
        code="trial",
        name="Trial Plan",
        description="30-day trial",
        duration_days=30,
    )


@pytest.fixture
def seed_subject(db_manager) -> Subject:
    """Create a test subject."""
    return db_manager.create_or_get_subject(
        keycloak_sub="test-user-sub", keycloak_realm="secure-apps"
    )


@pytest.fixture
def seed_license(db_manager, seed_subject, seed_app, seed_plan) -> License:
    """Create a test license."""
    return db_manager.create_license(
        subject_id=UUID(str(seed_subject.id)),
        app_id=UUID(str(seed_app.id)),
        plan_id=UUID(str(seed_plan.id)),
        tenant_id="01000000-0000-7000-8000-000000000001",
        status="active",
        starts_at=datetime.now(UTC) - timedelta(days=1),
        expires_at=datetime.now(UTC) + timedelta(days=30),
    )


@pytest.fixture
def seed_second_subject(db_manager) -> Subject:
    """A SECOND subject — i.e. a second employee of the same customer org.

    ``fr``/``kanban``/``n8n`` are sold per-tenant (whole customer org) but
    provisioning mints one ``license`` row per onboarded user, so a tenant with
    two staff legitimately holds two active licenses for the same app. This
    fixture is the second seat.
    """
    return db_manager.create_or_get_subject(
        keycloak_sub="test-user-sub-2", keycloak_realm="secure-apps"
    )


@pytest.fixture
def seed_second_license(db_manager, seed_second_subject, seed_app, seed_plan) -> License:
    """A second ACTIVE license for the SAME (tenant, app), held by another subject.

    Legal under ``uq_license_active_tenant_app_subject`` — the partial unique
    index keys on ``subject_id`` too, so one active row per SEAT is exactly what
    the schema permits.

    Expires sooner (+5d) than ``seed_license`` (+30d) on purpose: under the
    deterministic pick (``expires_at DESC NULLS FIRST``) the tenant-level
    fallback resolves to ``seed_license``, so a test that asks for THIS subject
    and gets THIS row has actually proven subject resolution rather than
    accidentally agreeing with the fallback.
    """
    return db_manager.create_license(
        subject_id=UUID(str(seed_second_subject.id)),
        app_id=UUID(str(seed_app.id)),
        plan_id=UUID(str(seed_plan.id)),
        tenant_id="01000000-0000-7000-8000-000000000001",
        status="active",
        starts_at=datetime.now(UTC) - timedelta(days=1),
        expires_at=datetime.now(UTC) + timedelta(days=5),
    )


def insert_legacy_perpetual_token(
    db_manager: DatabaseManager, license_id: UUID | str, token_type: str = "api"
) -> tuple[Token, str]:
    """A token row exactly as the pre-licenses#244 perpetual tokens are stored.

    ``create_token`` can no longer mint an UNREGISTERED perpetual token (an ``api``
    mint without an expiry gets the 365-day default), but ~199 such rows exist in
    production and must keep working. Tests that exercise how those legacy rows
    behave (rotation inheritance, introspection) insert one directly.
    """
    raw = Token.generate_token()
    session = db_manager.get_session()
    try:
        token = Token(
            license_id=UUID(str(license_id)),
            token_hash=Token.hash_token(raw),
            token_type=token_type,
            expires_at=None,
        )
        session.add(token)
        session.commit()
        session.refresh(token)
        return token, raw
    finally:
        session.close()


@pytest.fixture
def seed_token(db_manager, seed_license) -> tuple[Token, str]:
    """Create a test token. Returns (Token, raw_token)."""
    return db_manager.create_token(
        license_id=UUID(str(seed_license.id)),
        token_type="api",
        expires_at=datetime.now(UTC) + timedelta(days=30),
    )


@pytest.fixture
def seed_expired_token(db_manager, seed_license) -> tuple[Token, str]:
    """Create an expired test token. Returns (Token, raw_token)."""
    return db_manager.create_token(
        license_id=UUID(str(seed_license.id)),
        token_type="api",
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )


@pytest.fixture
def seed_revoked_license(db_manager, seed_subject, seed_app, seed_plan) -> License:
    """Create a revoked test license."""
    return db_manager.create_license(
        subject_id=UUID(str(seed_subject.id)),
        app_id=UUID(str(seed_app.id)),
        plan_id=UUID(str(seed_plan.id)),
        tenant_id="01000000-0000-7000-8000-000000000001",
        status="revoked",
        starts_at=datetime.now(UTC) - timedelta(days=10),
        expires_at=datetime.now(UTC) + timedelta(days=20),
    )


@pytest.fixture
def seed_other_tenant_license(db_manager, seed_subject, seed_app, seed_plan) -> License:
    """Create a license for a different tenant."""
    return db_manager.create_license(
        subject_id=UUID(str(seed_subject.id)),
        app_id=UUID(str(seed_app.id)),
        plan_id=UUID(str(seed_plan.id)),
        tenant_id="01000000-0000-7000-8000-000000000002",
        status="active",
        starts_at=datetime.now(UTC) - timedelta(days=1),
        expires_at=datetime.now(UTC) + timedelta(days=30),
    )


# =============================================================================
# Full Seed Data
# =============================================================================


@pytest.fixture
def full_seed_data(
    seed_region, seed_app, seed_plan, seed_subject, seed_license, seed_token
) -> dict[str, Any]:
    """Create all seed data and return as dict."""
    token_obj, raw_token = seed_token
    return {
        "region": seed_region,
        "app": seed_app,
        "plan": seed_plan,
        "subject": seed_subject,
        "license": seed_license,
        "token": token_obj,
        "raw_token": raw_token,
    }
