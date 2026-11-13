"""Migration 072: realm_owner (realm-per-app P0).

Run for REAL against a database built FROM EMPTY (init.sql -> the genesis shim ->
001..071), then 072:

* the table exists and at head its rows equal the boot seed (models.REALM_OWNERS) —
  a FRESH database never runs this file;
* the seed is ('commonpractice', 'commonpractice') only: never portal / secure-apps;
* owner_key is FK-bound to tenant_owner (an unknown owner is refused);
* an operator row is never overwritten; a re-run is a no-op;
* the downgrade drops the table; up -> down -> up through the REAL runner re-applies 072.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from models import REALM_OWNERS
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

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "migrations"
NAME = "072_realm_owner.sql"
UP = MIGRATIONS / NAME
DOWN = MIGRATIONS / "downgrades" / NAME
DB = "licenses_m072_test"
RUNNER_DB = "licenses_m072_runner_test"
SEED = ("commonpractice", "commonpractice")


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
    """A database at the exact 071 state, built from empty."""
    dsn = _recreate_database(_libpq_dsn(), DB)
    try:
        _build_before(dsn)
        with psycopg.connect(dsn, autocommit=True) as c:
            yield c
    finally:
        _drop_database(_libpq_dsn(), DB)


def _exists(conn) -> bool:
    row = conn.execute("SELECT to_regclass('public.realm_owner') IS NOT NULL").fetchone()
    return bool(row and row[0])


def _rows(conn) -> set[tuple[str, str]]:
    return {tuple(r) for r in conn.execute("SELECT realm, owner_key FROM realm_owner")}


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

    def test_boot_seed_contains_the_072_seed(self):
        # 074 adds ('agent', 'agent'), 076 ('w2v', 'w2v'), 077 ('fr', 'fr'), 078 care-bus; 072
        # itself seeds commonpractice only.
        assert SEED in set(REALM_OWNERS)
        assert set(REALM_OWNERS) - {SEED} == {
            ("agent", "agent"),
            ("w2v", "w2v"),
            ("fr", "fr"),
            ("care-bus", "care-bus"),
        }


class TestRunForReal:
    def test_head_equals_the_boot_seed(self, conn):
        assert not _exists(conn)  # control: absent at 071
        _run_sql_file(conn, UP, NAME)
        assert _rows(conn) == {SEED}  # 074 adds the agent row

    def test_owner_key_is_fk_bound(self, conn):
        _run_sql_file(conn, UP, NAME)
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute("INSERT INTO realm_owner (realm, owner_key) VALUES ('x', 'no-such-owner')")

    def test_an_operator_row_is_never_overwritten(self, conn):
        _run_sql_file(conn, UP, NAME)
        conn.execute("UPDATE realm_owner SET owner_key = 'fr' WHERE realm = 'commonpractice'")
        _run_sql_file(conn, UP, NAME)
        assert _rows(conn) == {("commonpractice", "fr")}

    def test_rerun_is_a_no_op(self, conn):
        _run_sql_file(conn, UP, NAME)
        snap = _rows(conn)
        _run_sql_file(conn, UP, NAME)
        assert _rows(conn) == snap


class TestDowngrade:
    def test_drops_the_table(self, conn):
        _run_sql_file(conn, UP, NAME)
        _run_sql_file(conn, DOWN, "down")
        assert not _exists(conn)


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
                    return (bool(row and row[0]), _exists(c) and SEED in _rows(c))

            assert apply_all(dsn) == 0
            assert state() == (True, True)
            with psycopg.connect(dsn, autocommit=True) as c:
                _run_sql_file(c, DOWN, "down 072")
            assert state() == (False, False)
            assert apply_all(dsn) == 0
            assert state() == (True, True)
        finally:
            _drop_database(_libpq_dsn(), RUNNER_DB)
