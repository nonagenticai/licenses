"""Migration 090: the conveyor backend may mint conveyor licences through provisioning.

Run for REAL against a database built FROM EMPTY (init.sql -> genesis shim -> every
migration before 090), then 090 is applied:

* api_client gains (secure-apps, conveyor-backend, fr, not cross_owner) and at head
  api_client equals the boot seed (models.API_CLIENTS);
* app_issuer (conveyor -> fr) is added ONLY when the `conveyor` app exists — no
  migration creates it (live it comes from POST /apps), so the fresh-DB leg is the
  announced SKIP, and the app-present leg is exercised by seeding it;
* an existing (conveyor, agent) issuer is never touched; an operator's api_client owner
  is never overwritten; a re-run is a no-op;
* the downgrade removes exactly the two 090 rows and an operator-changed owner is kept;
* up -> down -> up through the REAL runner re-applies 090.
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
NAME = "090_conveyor_service_issuer.sql"
UP = MIGRATIONS / NAME
DOWN = MIGRATIONS / "downgrades" / NAME
DB = "licenses_m090_test"
RUNNER_DB = "licenses_m090_runner_test"

CLIENT = ("secure-apps", "conveyor-backend", "fr", False)

# What 090 adds on top of the earlier seeds — older migration tests subtract these.
M090_API_CLIENTS: set[tuple[str, str, str, bool]] = {CLIENT}

ROWS_SQL = "SELECT realm, client_id, owner_key, cross_owner FROM api_client"


def _libpq_dsn() -> str:
    return TEST_DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1)


@pytest.fixture(scope="module")
def conn() -> Iterator[psycopg.Connection]:
    """A database at the exact 089 state, built from empty. 090 is applied per test."""
    dsn = _recreate_database(_libpq_dsn(), DB)
    connection = psycopg.connect(dsn, autocommit=True)
    try:
        _run_sql_file(connection, INIT_SQL, "genesis")
        _run_sql_file(connection, SHIM_SQL, "shim")
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name != NAME:
                _run_sql_file(connection, path, path.name)
        yield connection
    finally:
        connection.close()
        _drop_database(_libpq_dsn(), DB)


def _seed_conveyor(c: psycopg.Connection) -> None:
    """The app no migration creates (live: POST /apps), with its CA issuer row."""
    c.execute(
        "INSERT INTO app (key, name, audience) VALUES ('conveyor', 'Conveyor', 'platform') "
        "ON CONFLICT (key) DO NOTHING"
    )
    c.execute(
        "INSERT INTO app_issuer (app_id, owner_key) "
        "SELECT id, 'agent' FROM app WHERE key = 'conveyor' "
        "ON CONFLICT (app_id, owner_key) DO NOTHING"
    )


def _reset_client(c: psycopg.Connection) -> None:
    c.execute(
        "DELETE FROM api_client WHERE realm = 'secure-apps' AND client_id = 'conveyor-backend'"
    )


def _issuers(c: psycopg.Connection) -> set[tuple[str, str]]:
    return {
        (key, owner)
        for key, owner in c.execute(
            "SELECT a.key, ai.owner_key FROM app_issuer ai JOIN app a ON a.id = ai.app_id "
            "WHERE a.key = 'conveyor'"
        ).fetchall()
    }


def _rows(c: psycopg.Connection) -> set[tuple]:
    return {tuple(r) for r in c.execute(ROWS_SQL).fetchall()}


def _apply_up(c: psycopg.Connection) -> None:
    _run_sql_file(c, UP, NAME)


def _apply_down(c: psycopg.Connection) -> None:
    _run_sql_file(c, DOWN, f"downgrades/{NAME}")


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


class TestApiClient:
    def test_a_fresh_database_gets_the_row_and_skips_the_missing_app(self, conn):
        _reset_client(conn)
        _apply_up(conn)
        assert CLIENT in _rows(conn)
        # The conveyor app is absent on a from-empty corpus: no issuer row, no error.
        assert ("secure-apps", "conveyor-backend", "fr", False) in API_CLIENTS

    def test_the_app_present_leg_adds_fr_and_keeps_agent(self, conn):
        _reset_client(conn)
        _seed_conveyor(conn)
        try:
            _apply_up(conn)
            assert _issuers(conn) >= {("conveyor", "fr"), ("conveyor", "agent")}
        finally:
            conn.execute(
                "DELETE FROM app_issuer WHERE app_id = (SELECT id FROM app WHERE key = 'conveyor')"
            )
            conn.execute("DELETE FROM app WHERE key = 'conveyor'")

    def test_an_operator_changed_owner_is_never_overwritten(self, conn):
        _reset_client(conn)
        conn.execute(
            "INSERT INTO api_client (realm, client_id, owner_key) "
            "VALUES ('secure-apps', 'conveyor-backend', 'platform')"
        )
        try:
            _apply_up(conn)
            owner = conn.execute(
                "SELECT owner_key FROM api_client "
                "WHERE realm = 'secure-apps' AND client_id = 'conveyor-backend'"
            ).fetchone()
            assert owner == ("platform",)
        finally:
            _reset_client(conn)

    def test_head_equals_the_boot_seed(self, conn):
        _reset_client(conn)
        _apply_up(conn)
        assert _rows(conn) == set(API_CLIENTS)


class TestIdempotence:
    def test_rerun_is_a_no_op(self, conn):
        _reset_client(conn)
        _apply_up(conn)
        snap = _rows(conn)
        _apply_up(conn)
        assert _rows(conn) == snap


class TestDowngrade:
    def test_removes_only_the_090_rows(self, conn):
        _reset_client(conn)
        _seed_conveyor(conn)
        before = _rows(conn)
        try:
            _apply_up(conn)
            assert CLIENT in _rows(conn)
            assert ("conveyor", "fr") in _issuers(conn)
            _apply_down(conn)
            assert _rows(conn) == before
            assert ("conveyor", "fr") not in _issuers(conn)
            assert ("conveyor", "agent") in _issuers(conn)
        finally:
            conn.execute(
                "DELETE FROM app_issuer WHERE app_id = (SELECT id FROM app WHERE key = 'conveyor')"
            )
            conn.execute("DELETE FROM app WHERE key = 'conveyor'")

    def test_keeps_an_operator_changed_owner(self, conn):
        _reset_client(conn)
        conn.execute(
            "INSERT INTO api_client (realm, client_id, owner_key) "
            "VALUES ('secure-apps', 'conveyor-backend', 'platform')"
        )
        try:
            _apply_down(conn)
            assert ("secure-apps", "conveyor-backend", "platform", False) in _rows(conn)
        finally:
            _reset_client(conn)


class TestRunnerCycle:
    def test_up_down_up(self):
        from scripts.apply_migrations import apply_all

        dsn = _recreate_database(_libpq_dsn(), RUNNER_DB)
        try:
            with psycopg.connect(dsn, autocommit=True) as c:
                _run_sql_file(c, INIT_SQL, "genesis")
                _run_sql_file(c, SHIM_SQL, "shim")
                for path in sorted(MIGRATIONS.glob("*.sql")):
                    if path.name != NAME:
                        _run_sql_file(c, path, path.name)
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
                    has = c.execute(
                        "SELECT EXISTS (SELECT 1 FROM api_client WHERE realm = 'secure-apps' "
                        "AND client_id = 'conveyor-backend')"
                    ).fetchone()
                    return bool(row and row[0]), bool(has and has[0])

            assert apply_all(dsn) == 0
            assert state() == (True, True)
            with psycopg.connect(dsn, autocommit=True) as c:
                _run_sql_file(c, DOWN, "down 090")
            assert state() == (False, False)
            assert apply_all(dsn) == 0
            assert state() == (True, True)
        finally:
            _drop_database(_libpq_dsn(), RUNNER_DB)
