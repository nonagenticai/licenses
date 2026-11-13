"""Migration 053: tenant name / external_ref unique per owner among LIVE tenants.

Run for REAL against a database built FROM EMPTY (init.sql -> the genesis shim ->
001..052), then 053 is applied:

* both 050 unique indexes become partial on ``archived_at IS NULL`` and match the
  ORM (models.Tenant) definition;
* an archived tenant no longer blocks its name / external_ref; two LIVE ones still
  collide;
* a re-run changes nothing;
* the downgrade REFUSES while a live and an archived tenant share a name or an
  external_ref, and otherwise restores 050's full indexes and un-stamps 053;
* up -> down(053) -> up through the REAL runner re-applies 053.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

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
NAME = "053_tenant_names_unique_among_live.sql"
UP = MIGRATIONS / NAME
DOWN = MIGRATIONS / "downgrades" / NAME
DB = "licenses_m053_test"
RUNNER_DB = "licenses_m053_runner_test"

LIVE_NAME = (
    "CREATE UNIQUE INDEX uq_tenants_owner_name ON public.tenants USING btree "
    "(owner_key, name) WHERE (archived_at IS NULL)"
)
LIVE_REF = (
    "CREATE UNIQUE INDEX uq_tenants_owner_external_ref ON public.tenants USING btree "
    "(owner_key, external_ref) WHERE ((external_ref IS NOT NULL) AND (archived_at IS NULL))"
)
FULL_NAME = (
    "CREATE UNIQUE INDEX uq_tenants_owner_name ON public.tenants USING btree (owner_key, name)"
)
FULL_REF = (
    "CREATE UNIQUE INDEX uq_tenants_owner_external_ref ON public.tenants USING btree "
    "(owner_key, external_ref) WHERE (external_ref IS NOT NULL)"
)


def _libpq_dsn() -> str:
    return TEST_DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1)


def _build_before(dsn: str, name: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as c:
        _run_sql_file(c, INIT_SQL, "genesis")
        _run_sql_file(c, SHIM_SQL, "shim")
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name < name:
                _run_sql_file(c, path, "corpus")


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    """A database at the exact 052 state, built from empty."""
    dsn = _recreate_database(_libpq_dsn(), DB)
    try:
        _build_before(dsn, NAME)
        with psycopg.connect(dsn, autocommit=True) as c:
            yield c
    finally:
        _drop_database(_libpq_dsn(), DB)


def _defs(conn) -> tuple[str, str]:
    rows = dict(
        conn.execute(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE indexname IN ('uq_tenants_owner_name', 'uq_tenants_owner_external_ref')"
        ).fetchall()
    )
    return rows["uq_tenants_owner_name"], rows["uq_tenants_owner_external_ref"]


def _insert(conn, name: str, *, ref: str | None = None, archived: bool = False) -> None:
    conn.execute(
        "INSERT INTO tenants (id, name, owner_key, external_ref, archived_at) "
        "VALUES (uuidv7(), %s, 'fr', %s, CASE WHEN %s THEN now() END)",
        (name, ref, archived),
    )


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
    def test_indexes_become_partial(self, conn):
        assert _defs(conn) == (FULL_NAME, FULL_REF)  # control: 050's full indexes
        _run_sql_file(conn, UP, NAME)
        assert _defs(conn) == (LIVE_NAME, LIVE_REF)

    def test_an_archived_tenant_no_longer_blocks_its_name_or_ref(self, conn):
        _insert(conn, "churn", ref="churn-ref", archived=True)
        with pytest.raises(psycopg.errors.UniqueViolation):
            _insert(conn, "churn", ref="other")  # control: 050 blocks it
        _run_sql_file(conn, UP, NAME)
        _insert(conn, "churn", ref="churn-ref")
        _insert(conn, "churn", ref="churn-ref", archived=True)
        with pytest.raises(psycopg.errors.UniqueViolation):
            _insert(conn, "churn")  # two LIVE still collide
        with pytest.raises(psycopg.errors.UniqueViolation):
            _insert(conn, "churn-2", ref="churn-ref")

    def test_rerun_is_a_no_op(self, conn):
        _run_sql_file(conn, UP, NAME)
        _run_sql_file(conn, UP, NAME)
        assert _defs(conn) == (LIVE_NAME, LIVE_REF)


class TestDowngrade:
    def test_restores_the_full_indexes(self, conn):
        _run_sql_file(conn, UP, NAME)
        _insert(conn, "solo", ref="solo-ref", archived=True)
        _run_sql_file(conn, DOWN, "down")
        assert _defs(conn) == (FULL_NAME, FULL_REF)

    @pytest.mark.parametrize(
        ("live", "archived"),
        [
            (("dup", "ref-1"), ("dup", "ref-2")),  # name shared
            (("n-1", "dup-ref"), ("n-2", "dup-ref")),  # external_ref shared
        ],
    )
    def test_refuses_while_live_and_archived_share_an_identity(self, conn, live, archived):
        _run_sql_file(conn, UP, NAME)
        _insert(conn, live[0], ref=live[1])
        _insert(conn, archived[0], ref=archived[1], archived=True)
        with pytest.raises(Exception, match="downgrade 053 REFUSED"):
            _run_sql_file(conn, DOWN, "down")
        conn.execute("ROLLBACK")
        assert _defs(conn) == (LIVE_NAME, LIVE_REF)  # nothing changed


class TestRunnerCycle:
    """up -> down 053 -> up through the REAL runner: the downgrade deletes its stamp,
    so the runner applies 053 again."""

    def test_up_down_up(self):
        from scripts.apply_migrations import apply_all

        dsn = _recreate_database(_libpq_dsn(), RUNNER_DB)
        try:
            _build_before(dsn, NAME)
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

            def state() -> tuple[bool, tuple[str, str]]:
                with psycopg.connect(dsn, autocommit=True) as c:
                    row = c.execute(
                        "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE filename = %s)",
                        (NAME,),
                    ).fetchone()
                    return bool(row and row[0]), _defs(c)

            assert apply_all(dsn) == 0
            assert state() == (True, (LIVE_NAME, LIVE_REF))
            with psycopg.connect(dsn, autocommit=True) as c:
                _run_sql_file(c, DOWN, "down 053")
            assert state() == (False, (FULL_NAME, FULL_REF))
            assert apply_all(dsn) == 0
            assert state() == (True, (LIVE_NAME, LIVE_REF))
        finally:
            _drop_database(_libpq_dsn(), RUNNER_DB)
