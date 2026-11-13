"""Migration 077: realm_owner row for the fr app's realm `fr`.

Run for REAL against a database built FROM EMPTY (init.sql -> the genesis shim ->
001..076), then 077:

* at head the rows equal the boot seed (models.REALM_OWNERS) — a FRESH database never
  runs this file;
* 077 adds exactly ('fr', 'fr'); never portal / secure-apps;
* owner `fr` already exists, so the FK holds;
* an operator row is never overwritten; a re-run is a no-op;
* the downgrade removes only the fr row; up -> down -> up through the REAL runner
  re-applies 077.
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
NAME = "077_realm_owner_fr.sql"
UP = MIGRATIONS / NAME
DOWN = MIGRATIONS / "downgrades" / NAME
DB = "licenses_m077_test"
RUNNER_DB = "licenses_m077_runner_test"
SEED = ("fr", "fr")
PRIOR = {
    ("commonpractice", "commonpractice"),
    ("agent", "agent"),
    ("w2v", "w2v"),
}  # 072 + 074 + 076


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
    """A database at the exact 076 state, built from empty."""
    dsn = _recreate_database(_libpq_dsn(), DB)
    try:
        _build_before(dsn)
        with psycopg.connect(dsn, autocommit=True) as c:
            yield c
    finally:
        _drop_database(_libpq_dsn(), DB)


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

    def test_boot_seed_is_prior_plus_fr(self):
        assert set(REALM_OWNERS) == PRIOR | {SEED, ("care-bus", "care-bus")}  # 078: care-bus
        realms = {r for r, _ in REALM_OWNERS}
        assert "portal" not in realms and "secure-apps" not in realms


class TestRunForReal:
    def test_head_equals_the_boot_seed(self, conn):
        assert _rows(conn) == PRIOR  # control: 076 state has no fr row
        _run_sql_file(conn, UP, NAME)
        assert _rows(conn) == set(REALM_OWNERS) - {("care-bus", "care-bus")}  # 078: care-bus

    def test_fr_owner_exists_so_the_fk_holds(self, conn):
        row = conn.execute("SELECT 1 FROM tenant_owner WHERE key = 'fr'").fetchone()
        assert row is not None

    def test_an_operator_row_is_never_overwritten(self, conn):
        _run_sql_file(conn, UP, NAME)
        conn.execute("UPDATE realm_owner SET owner_key = 'w2v' WHERE realm = 'fr'")
        _run_sql_file(conn, UP, NAME)
        assert _rows(conn) == PRIOR | {("fr", "w2v")}

    def test_rerun_is_a_no_op(self, conn):
        _run_sql_file(conn, UP, NAME)
        snap = _rows(conn)
        _run_sql_file(conn, UP, NAME)
        assert _rows(conn) == snap


class TestDowngrade:
    def test_removes_only_the_fr_row(self, conn):
        _run_sql_file(conn, UP, NAME)
        _run_sql_file(conn, DOWN, "down")
        assert _rows(conn) == PRIOR


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
                    return (bool(row and row[0]), SEED in _rows(c))

            assert state() == (False, False)
            assert apply_all(dsn) == 0
            assert state() == (True, True)
            with psycopg.connect(dsn, autocommit=True) as c:
                _run_sql_file(c, DOWN, "down 077")
            assert state() == (False, False)
            assert apply_all(dsn) == 0
            assert state() == (True, True)
        finally:
            _drop_database(_libpq_dsn(), RUNNER_DB)
