"""Migration 083: secure-apps twins of portal/w2v-backend (w2v) and portal/provisioning-backend (fr).

Run for REAL against a database built FROM EMPTY (init.sql -> the genesis shim ->
every migration before 083), then 083 is applied:

* both clients get their api_client row (same owner as the portal row, not
  cross-owner) and at head api_client equals the boot seed (models.API_CLIENTS) — a
  FRESH database never runs this file;
* the portal rows are untouched;
* an operator's row for the same client is never overwritten; a re-run is a no-op;
* the downgrade removes exactly that row; up -> down -> up through the REAL runner
  re-applies 083.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from models import API_CLIENTS
from scripts.apply_migrations import split_statements
from scripts.build_schema_arms import (
    INIT_SQL,
    SHIM_SQL,
    _drop_database,
    _recreate_database,
    _run_sql_file,
)
from tests.conftest import TEST_DATABASE_URL

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "migrations"
NAME = "083_secure_apps_s2s_api_clients.sql"
UP = MIGRATIONS / NAME
DOWN = MIGRATIONS / "downgrades" / NAME
DB = "licenses_m083_test"
RUNNER_DB = "licenses_m083_runner_test"

# What 083 adds on top of the earlier seeds — older migration tests subtract these.
M083_API_CLIENTS: set[tuple[str, str, str, bool]] = {
    ("secure-apps", "w2v-backend", "w2v", False),
    ("secure-apps", "provisioning-backend", "fr", False),
}
PORTAL_TWINS: set[tuple[str, str, str, bool]] = {
    ("portal", "w2v-backend", "w2v", False),
    ("portal", "provisioning-backend", "fr", False),
}

ROWS_SQL = "SELECT realm, client_id, owner_key, cross_owner FROM api_client"


def _libpq_dsn() -> str:
    return TEST_DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1)


def _build_before(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as c:
        _run_sql_file(c, INIT_SQL, "genesis")
        _run_sql_file(c, SHIM_SQL, "shim")
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name < NAME:
                _run_sql_file(c, path, "corpus")


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    """A database at the exact 082 state, built from empty."""
    dsn = _recreate_database(_libpq_dsn(), DB)
    try:
        _build_before(dsn)
        with psycopg.connect(dsn, autocommit=True) as c:
            yield c
    finally:
        _drop_database(_libpq_dsn(), DB)


def _rows(conn) -> set[tuple]:
    return {tuple(r) for r in conn.execute(ROWS_SQL)}


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


class TestApiClients:
    def test_head_equals_the_boot_seed(self, conn):
        assert not (_rows(conn) & M083_API_CLIENTS)  # control: absent at 082
        assert PORTAL_TWINS <= _rows(conn)  # control: the portal rows exist (050)
        from tests.unit.test_migration_084_retire_strata_configurator import (
            M084_RETIRED_API_CLIENTS,
        )
        from tests.unit.test_migration_085_w2v_cell_m4_api_client import M085_API_CLIENTS
        from tests.unit.test_migration_090_conveyor_service_issuer import M090_API_CLIENTS

        _run_sql_file(conn, UP, NAME)
        # 084 retires a client later; 085 and 090 add one each.
        assert (
            _rows(conn)
            == (set(API_CLIENTS) - M085_API_CLIENTS - M090_API_CLIENTS) | M084_RETIRED_API_CLIENTS
        )
        assert M083_API_CLIENTS <= set(API_CLIENTS)

    def test_portal_rows_are_untouched(self, conn):
        _run_sql_file(conn, UP, NAME)
        assert PORTAL_TWINS <= _rows(conn)

    def test_owners_are_registered_owners(self, conn):
        _run_sql_file(conn, UP, NAME)
        rows = conn.execute(
            "SELECT a.client_id, a.owner_key, a.cross_owner, o.key FROM api_client a "
            "LEFT JOIN tenant_owner o ON o.key = a.owner_key "
            "WHERE a.realm = 'secure-apps' "
            "AND a.client_id IN ('w2v-backend', 'provisioning-backend')"
        ).fetchall()
        assert {tuple(r) for r in rows} == {
            ("w2v-backend", "w2v", False, "w2v"),
            ("provisioning-backend", "fr", False, "fr"),
        }

    def test_an_operator_change_is_never_overwritten(self, conn):
        conn.execute(
            "INSERT INTO api_client (realm, client_id, owner_key) "
            "VALUES ('secure-apps', 'w2v-backend', 'platform')"
        )
        _run_sql_file(conn, UP, NAME)
        owner = conn.execute(
            "SELECT owner_key FROM api_client "
            "WHERE realm = 'secure-apps' AND client_id = 'w2v-backend'"
        ).fetchone()
        assert owner == ("platform",)
        assert ("secure-apps", "provisioning-backend", "fr", False) in _rows(conn)

    def test_rerun_is_a_no_op(self, conn):
        _run_sql_file(conn, UP, NAME)
        snap = _rows(conn)
        _run_sql_file(conn, UP, NAME)
        assert _rows(conn) == snap


class TestDowngrade:
    def test_removes_only_the_083_rows(self, conn):
        before = _rows(conn)
        _run_sql_file(conn, UP, NAME)
        _run_sql_file(conn, DOWN, "down")
        assert _rows(conn) == before

    def test_keeps_an_operator_changed_owner(self, conn):
        conn.execute(
            "INSERT INTO api_client (realm, client_id, owner_key) "
            "VALUES ('secure-apps', 'w2v-backend', 'platform')"
        )
        _run_sql_file(conn, DOWN, "down")
        assert ("secure-apps", "w2v-backend", "platform", False) in _rows(conn)


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
                    return bool(row and row[0]), bool(_rows(c) & M083_API_CLIENTS)

            assert apply_all(dsn) == 0
            assert state() == (True, True)
            with psycopg.connect(dsn, autocommit=True) as c:
                _run_sql_file(c, DOWN, "down 083")
            assert state() == (False, False)
            assert apply_all(dsn) == 0
            assert state() == (True, True)
        finally:
            _drop_database(_libpq_dsn(), RUNNER_DB)
