"""Migration 068: care-bus's licences client.

Run for REAL against a database built FROM EMPTY (init.sql -> the genesis shim ->
001..067), then 068:

* ``secure-apps/care-bus-licenses-admin`` (owner care-bus, not cross-owner) exists, and
  at head api_client equals the boot seed (models.API_CLIENTS) — a FRESH database never
  runs this file;
* its owner ``care-bus`` is a registered owner (067) and the only issuer of the
  ``care-bus`` app, so the client can issue care-bus licences and nothing else;
* an operator row is never overwritten; a re-run is a no-op;
* the downgrade restores the 067 state exactly and leaves an operator-edited row alone;
  up -> down -> up through the REAL runner re-applies 068.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from models import API_CLIENTS, TENANT_OWNERS, issuers_for_app_key
from scripts.apply_migrations import split_statements
from scripts.build_schema_arms import (
    INIT_SQL,
    SHIM_SQL,
    _drop_database,
    _recreate_database,
    _run_sql_file,
)
from tests.conftest import TEST_DATABASE_URL
from tests.unit.test_migration_059_app_requires import _seed_apps as _seed_059_apps
from tests.unit.test_migration_078_care_bus_early_access import M078_API_CLIENTS
from tests.unit.test_migration_082_mcp_toolkit_entitlements import (
    M082_API_CLIENTS,
    M082_TENANT_OWNERS,
)
from tests.unit.test_migration_083_secure_apps_s2s_api_clients import M083_API_CLIENTS
from tests.unit.test_migration_085_w2v_cell_m4_api_client import M085_API_CLIENTS

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "migrations"
NAME = "068_care_bus_licenses_admin_client.sql"
UP = MIGRATIONS / NAME
DOWN = MIGRATIONS / "downgrades" / NAME
DB = "licenses_m068_test"
RUNNER_DB = "licenses_m068_runner_test"
CLIENT = ("secure-apps", "care-bus-licenses-admin", "care-bus", False)

# What 068 adds on top of the 050..067 seeds — older migration tests subtract these.
M068_API_CLIENTS: set[tuple[str, str, str, bool]] = {CLIENT}


def _libpq_dsn() -> str:
    return TEST_DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1)


def _build_before(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as c:
        _run_sql_file(c, INIT_SQL, "genesis")
        _run_sql_file(c, SHIM_SQL, "shim")
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name.startswith("059_"):
                _seed_059_apps(c)  # as 059's own test: the apps exist when 059 runs
            if path.name < NAME:
                _run_sql_file(c, path, "corpus")


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    """A database at the exact 067 state, built from empty."""
    dsn = _recreate_database(_libpq_dsn(), DB)
    try:
        _build_before(dsn)
        with psycopg.connect(dsn, autocommit=True) as c:
            yield c
    finally:
        _drop_database(_libpq_dsn(), DB)


def _owners(conn) -> set[tuple[str, str]]:
    return {tuple(r) for r in conn.execute("SELECT key, display_name FROM tenant_owner")}


def _clients(conn) -> set[tuple]:
    return {
        tuple(r)
        for r in conn.execute("SELECT realm, client_id, owner_key, cross_owner FROM api_client")
    }


def _issuers(conn, app_key: str) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT i.owner_key FROM app_issuer i JOIN app a ON a.id = i.app_id WHERE a.key = %s",
            (app_key,),
        )
    }


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

    def test_boot_seed_mirrors_the_client(self):
        assert CLIENT in API_CLIENTS
        assert ("care-bus", "Care Bus") in TENANT_OWNERS
        assert set(issuers_for_app_key("care-bus")) == {"care-bus"}


class TestRunForReal:
    def test_head_equals_the_boot_seed(self, conn):
        # Control: absent at 067, while its owner + app already exist.
        assert not (_clients(conn) & M068_API_CLIENTS)
        assert ("care-bus", "Care Bus") in _owners(conn)
        assert _issuers(conn, "care-bus") == {"care-bus"}
        _run_sql_file(conn, UP, NAME)
        from tests.unit.test_migration_070_agent_app import M070_API_CLIENTS, M070_TENANT_OWNERS
        from tests.unit.test_migration_084_retire_strata_configurator import (
            M084_RETIRED_API_CLIENTS,
        )
        from tests.unit.test_migration_090_conveyor_service_issuer import M090_API_CLIENTS

        assert (
            _clients(conn)
            == (
                set(API_CLIENTS)
                - M070_API_CLIENTS
                - M078_API_CLIENTS
                - M082_API_CLIENTS
                - M083_API_CLIENTS
                - M085_API_CLIENTS
                - M090_API_CLIENTS
            )
            | M084_RETIRED_API_CLIENTS
        )  # 070 agent, 078 minter, 082/083 later, 084 retired configurator
        assert (
            _owners(conn) == set(TENANT_OWNERS) - M070_TENANT_OWNERS - M082_TENANT_OWNERS
        )  # 070, 082
        # Not cross-owner and the care-bus app's ONLY issuer is its owner: the client
        # issues care-bus licences on care-bus tenants and nothing else.
        assert _issuers(conn, "care-bus") == {"care-bus"}

    def test_an_operator_row_is_never_overwritten(self, conn):
        conn.execute(
            "INSERT INTO api_client (realm, client_id, owner_key, cross_owner) "
            "VALUES ('secure-apps', 'care-bus-licenses-admin', 'platform', true)"
        )
        _run_sql_file(conn, UP, NAME)
        rows = {c for c in _clients(conn) if c[1] == "care-bus-licenses-admin"}
        assert rows == {("secure-apps", "care-bus-licenses-admin", "platform", True)}

    def test_rerun_is_a_no_op(self, conn):
        _run_sql_file(conn, UP, NAME)
        snap = (_owners(conn), _clients(conn))
        _run_sql_file(conn, UP, NAME)
        assert (_owners(conn), _clients(conn)) == snap


class TestDowngrade:
    def test_restores_the_067_state(self, conn):
        before = (_owners(conn), _clients(conn))
        _run_sql_file(conn, UP, NAME)
        _run_sql_file(conn, DOWN, "down")
        assert (_owners(conn), _clients(conn)) == before

    def test_leaves_an_operator_edited_row_alone(self, conn):
        conn.execute(
            "INSERT INTO api_client (realm, client_id, owner_key, cross_owner) "
            "VALUES ('secure-apps', 'care-bus-licenses-admin', 'care-bus', true)"
        )
        _run_sql_file(conn, DOWN, "down")
        assert ("secure-apps", "care-bus-licenses-admin", "care-bus", True) in _clients(conn)


class TestRunnerCycle:
    def test_up_down_up(self):
        from scripts.apply_migrations import apply_all

        dsn = _recreate_database(_libpq_dsn(), RUNNER_DB)
        try:
            _build_before(dsn)
            with psycopg.connect(dsn, autocommit=True) as c:
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
                    return (bool(row and row[0]), CLIENT in _clients(c))

            assert apply_all(dsn) == 0
            assert state() == (True, True)
            with psycopg.connect(dsn, autocommit=True) as c:
                _run_sql_file(c, DOWN, "down 068")
            assert state() == (False, False)
            assert apply_all(dsn) == 0
            assert state() == (True, True)
        finally:
            _drop_database(_libpq_dsn(), RUNNER_DB)
