"""Migration 071: the agent app's ``byo`` plan (agent issues on byo, like CommonPractice).

Run for REAL against a database built FROM EMPTY (init.sql -> the genesis shim ->
001..070), then 071:

* at head the agent plans equal the DR seed (scripts/seed_apps.py ``all_plans``):
  ``standard`` (070, untouched) + ``byo`` (global, perpetual, standard's limits);
* a licence already issued on agent/standard is untouched (same plan, still active);
* an operator's ``byo`` row is never overwritten; a re-run is a no-op;
* the downgrade restores the 070 state exactly and refuses while a licence is on
  agent/byo; up -> down -> up through the REAL runner re-applies 071.
"""

from __future__ import annotations

import importlib.util
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
from tests.unit.test_migration_059_app_requires import _seed_apps as _seed_059_apps

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "migrations"
NAME = "071_agent_byo_plan.sql"
UP = MIGRATIONS / NAME
DOWN = MIGRATIONS / "downgrades" / NAME
DB = "licenses_m071_test"
RUNNER_DB = "licenses_m071_runner_test"


def _seed_module():
    spec = importlib.util.spec_from_file_location("seed_apps", REPO / "scripts" / "seed_apps.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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
    """A database at the exact 070 state, built from empty."""
    dsn = _recreate_database(_libpq_dsn(), DB)
    try:
        _build_before(dsn)
        with psycopg.connect(dsn, autocommit=True) as c:
            yield c
    finally:
        _drop_database(_libpq_dsn(), DB)


def _plans(conn) -> set[tuple]:
    return {
        tuple(r)
        for r in conn.execute(
            "SELECT p.code, p.name, p.region_id, p.duration_days, p.rate_limit_rpm, "
            "p.rate_limit_burst, p.quota_monthly, p.token_budget_monthly, p.revoked "
            "FROM plan p JOIN app a ON a.id = p.app_id WHERE a.key = 'agent'"
        )
    }


def _licences(conn) -> set[tuple]:
    return {
        tuple(r)
        for r in conn.execute(
            "SELECT l.id, p.code, l.status FROM license l JOIN plan p ON p.id = l.plan_id "
            "JOIN app a ON a.id = l.app_id WHERE a.key = 'agent'"
        )
    }


def _licence_on(conn, code: str) -> None:
    tid = conn.execute(
        "INSERT INTO tenants (id, name, owner_key) VALUES (uuidv7(), 'agent-cust', 'agent') "
        "RETURNING id"
    ).fetchone()
    app = conn.execute("SELECT id FROM app WHERE key = 'agent'").fetchone()
    plan = conn.execute(
        "SELECT p.id FROM plan p JOIN app a ON a.id = p.app_id "
        "WHERE a.key = 'agent' AND p.code = %s",
        (code,),
    ).fetchone()
    sub = conn.execute(
        "INSERT INTO subject (keycloak_sub, keycloak_realm) VALUES (uuidv7()::text, 'portal') "
        "RETURNING id"
    ).fetchone()
    assert tid is not None and app is not None and plan is not None and sub is not None
    conn.execute(
        "INSERT INTO license (subject_id, app_id, plan_id, tenant_id, status, starts_at) "
        "VALUES (%s, %s, %s, %s, 'active', now())",
        (sub[0], app[0], plan[0], tid[0]),
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
    def test_head_equals_the_seed(self, conn):
        # Control: at 070 agent has standard only.
        assert {p[0] for p in _plans(conn)} == {"standard"}
        _run_sql_file(conn, UP, NAME)
        mod = _seed_module()
        spec = {a["key"]: a for a in mod.APPS}["agent"]
        assert _plans(conn) == {
            (
                p["code"],
                p["name"],
                None,
                p["duration_days"],
                p["rate_limit_rpm"],
                p["rate_limit_burst"],
                p["quota_monthly"],
                p["token_budget_monthly"],
                False,
            )
            for p in mod.all_plans(spec)
        }
        assert {p[0] for p in _plans(conn)} == {"standard", "byo"}

    def test_existing_standard_licences_are_untouched(self, conn):
        _licence_on(conn, "standard")
        before = _licences(conn)
        std_before = {p for p in _plans(conn) if p[0] == "standard"}
        _run_sql_file(conn, UP, NAME)
        assert _licences(conn) == before
        assert {p for p in _plans(conn) if p[0] == "standard"} == std_before

    def test_an_operator_row_is_never_overwritten(self, conn):
        conn.execute(
            "INSERT INTO plan (app_id, code, name, rate_limit_rpm, quota_monthly, "
            "token_budget_monthly) SELECT id, 'byo', 'Ops', 5, 1, 1 FROM app WHERE key = 'agent'"
        )
        _run_sql_file(conn, UP, NAME)
        assert {(p[1], p[4]) for p in _plans(conn) if p[0] == "byo"} == {("Ops", 5)}

    def test_rerun_is_a_no_op(self, conn):
        _run_sql_file(conn, UP, NAME)
        snap = _plans(conn)
        _run_sql_file(conn, UP, NAME)
        assert _plans(conn) == snap


class TestDowngrade:
    def test_restores_the_070_state(self, conn):
        before = _plans(conn)
        _run_sql_file(conn, UP, NAME)
        _run_sql_file(conn, DOWN, "down")
        assert _plans(conn) == before

    def test_keeps_standard_licences(self, conn):
        _licence_on(conn, "standard")
        before = _licences(conn)
        _run_sql_file(conn, UP, NAME)
        _run_sql_file(conn, DOWN, "down")
        assert _licences(conn) == before

    def test_refuses_and_changes_nothing_while_a_byo_licence_exists(self, conn):
        _run_sql_file(conn, UP, NAME)
        _licence_on(conn, "byo")
        snap = (_plans(conn), _licences(conn))
        with pytest.raises(Exception, match="071 downgrade REFUSED"):
            _run_sql_file(conn, DOWN, "down")
        conn.execute("ROLLBACK")
        assert (_plans(conn), _licences(conn)) == snap


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
                    return (bool(row and row[0]), "byo" in {p[0] for p in _plans(c)})

            assert state() == (False, False)
            assert apply_all(dsn) == 0
            assert state() == (True, True)
            with psycopg.connect(dsn, autocommit=True) as c:
                _run_sql_file(c, DOWN, "down 071")
            assert state() == (False, False)
            assert apply_all(dsn) == 0
            assert state() == (True, True)
        finally:
            _drop_database(_libpq_dsn(), RUNNER_DB)
