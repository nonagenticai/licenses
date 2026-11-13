"""Migration 073: w2v grant on the svc-agent-api service tenant.

Proofs, all driven by the REAL 073 file, against a database built FROM EMPTY
(init.sql -> the genesis shim -> 001..072):

* a fresh database does NOT hold svc-agent-api (it is registered at runtime by
  provision_service_token.py), and 073 is a no-op there;
* with the tenant registered as prod has it (owner ``platform``, kind ``service``) it
  gains exactly the one w2v grant, the owner is unchanged, a re-run is a no-op, an
  operator's grant is never duplicated, and the downgrade restores 072 exactly;
  up -> down -> up through the real runner re-applies it;
* through the API: before 073 w2v-backend's ``GET /licenses/active`` on the tenant is
  403 ``tenant_not_accessible``; after it is 200 with the w2v licence.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import psycopg
import pytest

from scripts.apply_migrations import split_statements
from scripts.build_schema_arms import _drop_database, _recreate_database, _run_sql_file
from tests.conftest import TEST_DATABASE_URL
from tests.unit.test_migration_072_realm_owner import _build_before as _build_072_before
from tests.unit.test_tenant_ownership import PORTAL_ISSUER, W2V, _app, _as, _err

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "migrations"
NAME = "073_svc_agent_api_w2v_grant.sql"
UP = MIGRATIONS / NAME
DOWN = MIGRATIONS / "downgrades" / NAME
DB = "licenses_m073_test"
RUNNER_DB = "licenses_m073_runner_test"
SVC_AGENT_API = "9a4366bf-5684-59dd-b36c-2cfffef9607e"
#: tts-backend reads its entitlement as a reader retargeting via X-Tenant-Id (the
#: middleware is where the live 403 `tenant_not_accessible` came from).
W2V_ROLES = ["license-reader"]


def _libpq_dsn() -> str:
    return TEST_DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1)


def _build_before(dsn: str) -> None:
    """The exact 072 state: every file up to and including 072."""
    _build_072_before(dsn)
    with psycopg.connect(dsn, autocommit=True) as c:
        _run_sql_file(c, MIGRATIONS / "072_realm_owner.sql", "072")


def _register_tenant(c: psycopg.Connection) -> None:
    """svc-agent-api as prod holds it (provision_service_token.py, --tenant-owner platform)."""
    c.execute(
        "INSERT INTO tenants (id, name, kind, byo_mode, owner_key) "
        "VALUES (%s, 'svc-agent-api', 'service', 'local_allowed', 'platform')",
        (SVC_AGENT_API,),
    )


@pytest.fixture
def fresh() -> Iterator[psycopg.Connection]:
    dsn = _recreate_database(_libpq_dsn(), DB)
    try:
        _build_before(dsn)
        with psycopg.connect(dsn, autocommit=True) as c:
            yield c
    finally:
        _drop_database(_libpq_dsn(), DB)


@pytest.fixture
def conn(fresh) -> psycopg.Connection:
    _register_tenant(fresh)
    return fresh


def _active_grants(conn) -> set[tuple[str, str]]:
    return {
        tuple(r)
        for r in conn.execute(
            "SELECT grantee_owner_key, granted_by FROM tenant_app_grant "
            "WHERE tenant_id = %s AND revoked_at IS NULL",
            (SVC_AGENT_API,),
        )
    }


def _all_grants(conn) -> set[tuple]:
    return {
        tuple(r)
        for r in conn.execute(
            "SELECT tenant_id, grantee_owner_key, granted_by, revoked_at FROM tenant_app_grant"
        )
    }


def _owner(conn) -> str:
    row = conn.execute("SELECT owner_key FROM tenants WHERE id = %s", (SVC_AGENT_API,)).fetchone()
    assert row is not None
    return row[0]


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


class TestRunForReal:
    def test_is_a_no_op_on_a_fresh_database(self, fresh):
        row = fresh.execute("SELECT 1 FROM tenants WHERE id = %s", (SVC_AGENT_API,)).fetchone()
        assert row is None  # control: a fresh/CI database never holds svc-agent-api
        before = _all_grants(fresh)
        _run_sql_file(fresh, UP, NAME)
        assert _all_grants(fresh) == before

    def test_adds_exactly_the_w2v_grant_and_keeps_the_owner(self, conn):
        before = _all_grants(conn)
        assert _active_grants(conn) == set()  # control: no grant at 072
        _run_sql_file(conn, UP, NAME)
        assert _owner(conn) == "platform"
        added = _all_grants(conn) - before
        assert {(str(t), g, b, r) for t, g, b, r in added} == {
            (SVC_AGENT_API, "w2v", "migration-073", None),
        }
        assert before <= _all_grants(conn)

    def test_rerun_is_a_no_op(self, conn):
        _run_sql_file(conn, UP, NAME)
        snap = _all_grants(conn)
        _run_sql_file(conn, UP, NAME)
        assert _all_grants(conn) == snap

    def test_an_existing_active_grant_is_left_alone(self, conn):
        conn.execute(
            "INSERT INTO tenant_app_grant (tenant_id, grantee_owner_key, granted_by) "
            "VALUES (%s, 'w2v', 'operator')",
            (SVC_AGENT_API,),
        )
        _run_sql_file(conn, UP, NAME)
        assert _active_grants(conn) == {("w2v", "operator")}

    def test_a_w2v_owned_tenant_gets_no_self_grant(self, conn):
        conn.execute("UPDATE tenants SET owner_key = 'w2v' WHERE id = %s", (SVC_AGENT_API,))
        _run_sql_file(conn, UP, NAME)
        assert _active_grants(conn) == set()


class TestDowngrade:
    def test_restores_the_072_state(self, conn):
        before = _all_grants(conn)
        _run_sql_file(conn, UP, NAME)
        _run_sql_file(conn, DOWN, "down")
        assert _all_grants(conn) == before

    def test_leaves_an_operator_grant_alone(self, conn):
        conn.execute(
            "INSERT INTO tenant_app_grant (tenant_id, grantee_owner_key, granted_by) "
            "VALUES (%s, 'w2v', 'operator')",
            (SVC_AGENT_API,),
        )
        _run_sql_file(conn, UP, NAME)
        _run_sql_file(conn, DOWN, "down")
        assert _active_grants(conn) == {("w2v", "operator")}


class TestRunnerCycle:
    def test_up_down_up(self):
        from scripts.apply_migrations import apply_all

        dsn = _recreate_database(_libpq_dsn(), RUNNER_DB)
        try:
            _build_before(dsn)
            with psycopg.connect(dsn, autocommit=True) as c:
                _register_tenant(c)
                c.execute(
                    "CREATE TABLE schema_migrations (filename TEXT PRIMARY KEY, "
                    "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
                )
                for path in sorted(MIGRATIONS.glob("*.sql")):
                    if path.name < NAME:
                        c.execute(
                            "INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,)
                        )

            def state() -> tuple[bool, bool]:
                with psycopg.connect(dsn, autocommit=True) as c:
                    row = c.execute(
                        "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE filename = %s)",
                        (NAME,),
                    ).fetchone()
                    return (bool(row and row[0]), ("w2v", "migration-073") in _active_grants(c))

            assert apply_all(dsn) == 0
            assert state() == (True, True)
            with psycopg.connect(dsn, autocommit=True) as c:
                _run_sql_file(c, DOWN, "down 073")
            assert state() == (False, False)
            assert apply_all(dsn) == 0
            assert state() == (True, True)
        finally:
            _drop_database(_libpq_dsn(), RUNNER_DB)


class TestThroughTheApi:
    """w2v-backend's entitlement lookup on svc-agent-api, before and after the REAL 073."""

    @pytest.fixture(autouse=True)
    def _trust_portal(self, monkeypatch):
        from tests.support.jwt_keys import TEST_ISSUER

        monkeypatch.setenv("KEYCLOAK_TRUSTED_ISSUERS", f"{TEST_ISSUER},{PORTAL_ISSUER}")

    def test_w2v_is_refused_before_and_resolves_after(self, db_manager):
        db_manager.ensure_tenant(
            SVC_AGENT_API, name="svc-agent-api", kind="service", owner_key="platform"
        )
        app_id, plan_id = _app(db_manager, "w2v", ("platform", "w2v"))
        subject = db_manager.create_or_get_subject(
            keycloak_sub="svc-agent-api", keycloak_realm="service"
        )
        lic = db_manager.create_license(
            subject_id=UUID(str(subject.id)),
            app_id=app_id,
            plan_id=plan_id,
            tenant_id=SVC_AGENT_API,
            status="active",
        )
        url = f"/licenses/active?tenant_id={SVC_AGENT_API}&app_key=w2v"

        with _as(db_manager, W2V, iss=PORTAL_ISSUER, roles=W2V_ROLES, tenant=SVC_AGENT_API) as w2v:
            before = w2v.get(url)
        assert before.status_code == 403, before.text
        assert _err(before) == "tenant_not_accessible"

        with psycopg.connect(_libpq_dsn(), autocommit=True) as c:
            _run_sql_file(c, UP, NAME)

        with _as(db_manager, W2V, iss=PORTAL_ISSUER, roles=W2V_ROLES, tenant=SVC_AGENT_API) as w2v:
            after = w2v.get(url)
        assert after.status_code == 200, after.text
        assert str(lic.id) in after.text
        assert db_manager.get_tenant(SVC_AGENT_API).owner_key == "platform"
