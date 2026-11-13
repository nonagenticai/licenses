"""Migration 079: the owner (platform) ensures the ``care-bus`` bundle on fr-test-user.

Run for REAL against databases built FROM EMPTY (init.sql -> shim -> 001..078, ``mcp``
seeded as scripts/seed_apps.py does), then:

* DIFFERENTIAL: the same world is built twice; one copy gets the REAL
  ``services.bundle_ensure.plan`` + ``apply`` (billing push failing, as 079 cannot push),
  the other gets 079. The resulting tenant_bundle / licence / subject / grant /
  dependency-grant rows are IDENTICAL (ids and timestamps aside) — for the live shape
  (nothing held: four inserts) and for an adopt / exists / refuse / member-licence mix;
* idempotent; a no-op when fr-test-user is absent or no longer owned by platform; the
  downgrade restores the 078 state;
* THROUGH THE API afterwards: care-bus-licenses-admin reads
  ``GET /licenses/active?tenant_id=<t>&app_key=care-bus&subject_id=svc:<t>:care-bus``
  -> 200 active on the bundle licence (subject_match), its own ensure is STILL 403
  ``tenant_owner_only``, and care-bus-bundle-minter mints each tool on the tenant.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import main
from auth.models import KeycloakUser
from models import DatabaseManager
from scripts.apply_migrations import split_statements
from scripts.build_schema_arms import _drop_database, _recreate_database, _run_sql_file
from services import bundle_ensure
from tests.conftest import TEST_DATABASE_URL, _make_mock_jwt
from tests.unit.test_migration_078_care_bus_early_access import (
    NAME as NAME_078,
)
from tests.unit.test_migration_078_care_bus_early_access import (
    _build_before as _build_077,
)
from tests.unit.test_migration_078_care_bus_early_access import (
    _seed_mcp,
)

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "migrations"
NAME = "079_fr_test_user_care_bus_bundle.sql"
UP = MIGRATIONS / NAME
DOWN = MIGRATIONS / "downgrades" / NAME
DB_MIG = "licenses_m079_test"
DB_ENSURE = "licenses_m079_ensure_test"
T = "0195f48b-d000-7000-8000-000000000099"  # fr-test-user
TOOLS = ("mcp", "strata-ingest", "strata-rag")
BY = "migration-079"
GRANTED_BY = "bundle:care-bus by migration-079"


def _libpq_dsn() -> str:
    return TEST_DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1)


def _build_078(dsn: str) -> None:
    _build_077(dsn)
    with psycopg.connect(dsn, autocommit=True) as c:
        _seed_mcp(c)
        # Live mcp carries `standard` (seed_apps.py's edge-gate plan); 078's helper
        # seeds the app only.
        c.execute(
            "INSERT INTO plan (app_id, code, name) SELECT id, 'standard', 'Standard' "
            "FROM app WHERE key = 'mcp'"
        )
        _run_sql_file(c, MIGRATIONS / NAME_078, "078")


def _db_manager(dsn: str) -> DatabaseManager:
    engine = create_engine(
        dsn.replace("postgresql://", "postgresql+psycopg://", 1), poolclass=NullPool
    )
    manager = DatabaseManager.__new__(DatabaseManager)
    manager.engine = engine
    manager.SessionLocal = sessionmaker(
        autocommit=False, autoflush=False, expire_on_commit=False, bind=engine
    )
    return manager


@contextmanager
def _database(name: str) -> Iterator[str]:
    dsn = _recreate_database(_libpq_dsn(), name)
    try:
        _build_078(dsn)
        yield dsn
    finally:
        _drop_database(_libpq_dsn(), name)


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    with _database(DB_MIG) as dsn, psycopg.connect(dsn, autocommit=True) as c:
        yield c


def _held(c, sub: str, realm: str, app: str, bundle: str | None = None) -> None:
    """A pre-existing active licence on fr-test-user (an operator / earlier mint)."""
    c.execute(
        "INSERT INTO subject (keycloak_sub, keycloak_realm) VALUES (%s, %s) "
        "ON CONFLICT (keycloak_sub) DO NOTHING",
        (sub, realm),
    )
    c.execute(
        "INSERT INTO license (subject_id, app_id, plan_id, tenant_id, bundle_id, status) "
        "SELECT s.id, a.id, p.id, %s, (SELECT id FROM bundle WHERE key = %s), 'active' "
        "FROM subject s, app a JOIN plan p ON p.app_id = a.id AND p.code = 'standard' "
        "WHERE s.keycloak_sub = %s AND a.key = %s",
        (T, bundle, sub, app),
    )


def _mixed_world(c) -> None:
    # care-bus: ONE unbundled tenant-level licence -> adopt; a person's care-bus
    # licence beside it is never adopted and never a candidate.
    _held(c, "ops-care-bus", "service", "care-bus")
    _held(c, "0c6f7a1e-1111-4a2b-9c3d-000000000001", "care-bus", "care-bus")
    # mcp: one tenant-level licence -> dependency satisfied (exists, shared).
    _held(c, f"svc:{T}:mcp", "service", "mcp")
    # strata-rag: two tenant-level licences -> REFUSE several_candidates.
    _held(c, "rag-a", "service", "strata-rag")
    _held(c, "rag-b", "service", "strata-rag")
    # strata-ingest: nothing -> dependency insert.


def _snapshot(c) -> dict[str, set[tuple]]:
    q = {
        "hold": "SELECT b.key FROM tenant_bundle tb JOIN bundle b ON b.id = tb.bundle_id "
        "WHERE tb.tenant_id = %(t)s",
        "licence": "SELECT a.key, s.keycloak_sub, s.keycloak_realm, b.key, l.is_dependency, "
        "l.status, p.code, l.expires_at, l.revoked_at, l.deleted_at, l.token_budget_override, "
        "l.quota_override, l.metering_provisioned_at, l.metering_verified_at, "
        "l.metadata::text, l.region_id "
        "FROM license l JOIN app a ON a.id = l.app_id JOIN subject s ON s.id = l.subject_id "
        "JOIN plan p ON p.id = l.plan_id LEFT JOIN bundle b ON b.id = l.bundle_id "
        "WHERE l.tenant_id = %(t)s",
        "subject": "SELECT keycloak_sub, keycloak_realm, tenant_id::text, actor_sub FROM subject",
        "grant": "SELECT grantee_owner_key, granted_by, revoked_at FROM tenant_app_grant "
        "WHERE tenant_id = %(t)s",
        "dep_grant": "SELECT g.owner_key, a.key, g.granted_by FROM tenant_dependency_grant g "
        "JOIN app a ON a.id = g.app_id WHERE g.tenant_id = %(t)s",
        "owner": "SELECT owner_key FROM tenants WHERE id = %(t)s",
    }
    return {k: {tuple(r) for r in c.execute(sql, {"t": T})} for k, sql in q.items()}


def _ensure_for_real(dsn: str) -> bundle_ensure.EnsurePlan:
    """The route's decision + writes (services.bundle_ensure), billing push failing."""
    db = _db_manager(dsn)
    ep = bundle_ensure.plan(db, T, "care-bus", ["standard"])
    bundle_ensure.apply(
        db,
        ep,
        provisioned_by=BY,
        granted_by=GRANTED_BY,
        provision=lambda *a, **k: "billing down (test)",
    )
    db.engine.dispose()
    return ep


class TestText:
    def test_one_transaction(self):
        for path in (UP, DOWN):
            stmts = [
                "\n".join(ln for ln in st.splitlines() if not ln.lstrip().startswith("--")).strip()
                for st in split_statements(path.read_text(encoding="utf-8"))
            ]
            assert stmts[0] == "BEGIN" and stmts[-1] == "COMMIT", path.name

    def test_the_runner_cannot_auto_apply_the_downgrade(self):
        assert DOWN.parent != MIGRATIONS


class TestIndistinguishableFromEnsure:
    @pytest.mark.parametrize("world", ["live_shape", "mixed"])
    def test_same_rows_as_the_real_ensure(self, world):
        with _database(DB_ENSURE) as dsn_e, _database(DB_MIG) as dsn_m:
            with (
                psycopg.connect(dsn_e, autocommit=True) as ce,
                psycopg.connect(dsn_m, autocommit=True) as cm,
            ):
                if world == "mixed":
                    _mixed_world(ce)
                    _mixed_world(cm)
                assert _snapshot(ce) == _snapshot(cm)  # control: same start
                before = _snapshot(cm)
                ep = _ensure_for_real(dsn_e)
                _run_sql_file(cm, UP, NAME)
                after_e, after_m = _snapshot(ce), _snapshot(cm)
            assert after_m == after_e
            assert after_m != before
            decisions = {p.app_key: p.action for p in ep.apps}
            if world == "live_shape":
                assert decisions == {"care-bus": "insert", **{t: "insert" for t in TOOLS}}
            else:
                assert decisions == {
                    "care-bus": "adopt",
                    "mcp": "exists",
                    "strata-rag": "REFUSE",
                    "strata-ingest": "insert",
                }


class TestRunForReal:
    def test_live_shape(self, conn):
        _run_sql_file(conn, UP, NAME)
        snap = _snapshot(conn)
        assert snap["owner"] == {("platform",)}  # ownership unchanged
        assert snap["hold"] == {("care-bus",)}
        lic = {(r[0], r[1], r[3], r[4], r[6]) for r in snap["licence"]}
        assert lic == {
            ("care-bus", f"svc:{T}:care-bus", "care-bus", False, "standard"),
            *{(t, f"svc:{T}:{t}", None, True, "standard") for t in TOOLS},
        }
        assert snap["dep_grant"] == {("care-bus", t, GRANTED_BY) for t in TOOLS}
        # care-bus's issuer grant is 069's row, left alone.
        assert ("care-bus", "migration-069", None) in snap["grant"]
        assert not any(g[1] == GRANTED_BY for g in snap["grant"])
        subs = {r[0]: r[2] for r in snap["subject"] if r[0].startswith(f"svc:{T}:")}
        assert subs == {f"svc:{T}:{a}": T for a in ("care-bus", *TOOLS)}

    def test_rerun_is_a_no_op(self, conn):
        _run_sql_file(conn, UP, NAME)
        snap = _snapshot(conn)
        _run_sql_file(conn, UP, NAME)
        assert _snapshot(conn) == snap

    def test_no_op_when_the_owner_is_not_platform(self, conn):
        conn.execute("UPDATE tenants SET owner_key = 'fr' WHERE id = %s", (T,))
        before = _snapshot(conn)
        _run_sql_file(conn, UP, NAME)
        assert _snapshot(conn) == before

    def test_no_op_when_the_tenant_is_absent(self, conn):
        conn.execute("DELETE FROM tenant_app_grant WHERE tenant_id = %s", (T,))
        conn.execute("DELETE FROM tenants WHERE id = %s", (T,))
        n = conn.execute("SELECT count(*) FROM license").fetchone()
        _run_sql_file(conn, UP, NAME)
        assert conn.execute("SELECT count(*) FROM license").fetchone() == n
        assert conn.execute("SELECT count(*) FROM tenant_bundle").fetchone() == (0,)


class TestDowngrade:
    def test_restores_the_078_state(self, conn):
        before = _snapshot(conn)
        _run_sql_file(conn, UP, NAME)
        _run_sql_file(conn, DOWN, "down")
        assert _snapshot(conn) == before

    def test_refuses_while_a_token_exists(self, conn):
        _run_sql_file(conn, UP, NAME)
        conn.execute(
            "INSERT INTO token (license_id, token_hash, token_type) SELECT id, 'h079', 'internal' FROM license "
            "WHERE tenant_id = %s AND metadata->>'provisioned_by' = 'migration-079' LIMIT 1",
            (T,),
        )
        snap = _snapshot(conn)
        with pytest.raises(Exception, match="REFUSED"):
            _run_sql_file(conn, DOWN, "down")
        conn.execute("ROLLBACK")  # the file's BEGIN left the aborted transaction open
        assert _snapshot(conn) == snap


@contextmanager
def _client(db: DatabaseManager, azp: str, role: str) -> Iterator[TestClient]:
    main.db_manager = db
    user = KeycloakUser(
        sub=f"service-account-{azp}",
        preferred_username=f"service-account-{azp}",
        realm_access={"roles": [role]},
        resource_access={},
    )
    with TestClient(main.app, raise_server_exceptions=False) as client:
        client.headers["Authorization"] = f"Bearer {_make_mock_jwt(user, azp=azp)}"
        yield client


class TestThroughTheApi:
    @pytest.fixture
    def db(self, conn, monkeypatch) -> Iterator[DatabaseManager]:
        _run_sql_file(conn, UP, NAME)
        monkeypatch.setattr(main, "db_manager", main.db_manager)  # restored afterwards
        manager = _db_manager(_dsn_of(conn))
        yield manager
        manager.engine.dispose()

    def test_care_bus_reads_the_bundle_licence(self, db):
        with _client(db, "care-bus-licenses-admin", "license-admin") as c:
            resp = c.get(
                "/licenses/active",
                params={
                    "tenant_id": T,
                    "app_key": "care-bus",
                    "subject_id": f"svc:{T}:care-bus",
                },
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["active"] is True
        assert body["tenant_id"] == T
        # The bundle licence held by svc:<t>:care-bus — subject_match proves the seat
        # selector resolved (else the route falls back to its tenant-level pick).
        with db.engine.connect() as c2:
            lic_id = c2.exec_driver_sql(
                "SELECT l.id::text FROM license l JOIN subject s ON s.id = l.subject_id "
                "JOIN bundle b ON b.id = l.bundle_id WHERE l.tenant_id = %(t)s "
                "AND b.key = 'care-bus' AND s.keycloak_sub = %(s)s",
                {"t": T, "s": f"svc:{T}:care-bus"},
            ).scalar_one()
        assert body["license_id"] == lic_id
        assert body["subject_match"] is True
        assert body["metadata"] == {"bundle": "care-bus", "provisioned_by": BY}
        assert body["app"]["key"] == "care-bus"
        assert body["status"] == "active"

    def test_care_bus_ensure_is_still_owner_only(self, db):
        with _client(db, "care-bus-licenses-admin", "license-admin") as c:
            c.headers["X-Tenant-Id"] = T
            resp = c.post(
                f"/internal/tenants/{T}/bundles/care-bus/ensure", json={"plans": ["standard"]}
            )
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error"] == "tenant_owner_only"

    @pytest.mark.parametrize("tool", TOOLS)
    def test_the_minter_mints_each_tool(self, db, tool):
        with _client(db, "care-bus-bundle-minter", "bundle-token-minter") as c:
            resp = c.post(f"/internal/tenants/{T}/bundle-token?app={tool}")
        assert resp.status_code == 201, resp.text


def _dsn_of(c: psycopg.Connection) -> str:
    """libpq DSN of ``c``'s database (``info.dsn`` hides the password)."""
    parts = urlsplit(_libpq_dsn())
    return urlunsplit(parts._replace(path=f"/{c.info.dbname}"))
