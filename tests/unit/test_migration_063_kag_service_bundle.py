"""Migration 063: the ``kag-service`` bundle = {kag}, ensurer {kag} (052 rule 1).

Run for REAL against a database built FROM EMPTY (init.sql -> the genesis shim ->
001..062), with the apps seeded carrying their live (2026-10-15) issuers (059's
slice), then 063:

* at head bundle_ensurer equals the boot seed (models.BUNDLE_ENSURER_SEED) — a FRESH
  database never runs this file;
* ``kag-service`` holds EXACTLY {kag}; no other bundle's members move; kag is its only
  ensurer, and only while kag issues kag (052 rule 1); platform never ensures it;
* a missing app is skipped; operator rows are never overwritten; a re-run is a no-op;
* the downgrade restores the 062 state exactly and refuses while a tenant holds
  kag-service; up -> down -> up through the REAL runner re-applies 063.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from models import BUNDLE_ENSURER_SEED
from scripts.apply_migrations import split_statements
from scripts.build_schema_arms import (
    INIT_SQL,
    SHIM_SQL,
    _drop_database,
    _recreate_database,
    _run_sql_file,
)
from tests.conftest import TEST_DATABASE_URL
from tests.unit.test_migration_059_app_requires import _seed_apps
from tests.unit.test_migration_078_care_bus_early_access import M078_BUNDLE_ENSURERS
from tests.unit.test_migration_082_mcp_toolkit_entitlements import (
    M082_BUNDLE_ENSURERS,
)

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "migrations"
NAME = "063_kag_service_bundle.sql"
UP = MIGRATIONS / NAME
DOWN = MIGRATIONS / "downgrades" / NAME
DB = "licenses_m063_test"
RUNNER_DB = "licenses_m063_runner_test"

KAG_SERVICE = {"kag"}

# What 063 adds on top of the 052..062 seeds — older migration tests subtract these.
M063_BUNDLE_ENSURERS: dict[str, set[str]] = {"kag-service": {"kag"}}


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
    """A database at the exact 062 state, built from empty, with the apps."""
    dsn = _recreate_database(_libpq_dsn(), DB)
    try:
        _build_before(dsn)
        with psycopg.connect(dsn, autocommit=True) as c:
            _seed_apps(c)
            yield c
    finally:
        _drop_database(_libpq_dsn(), DB)


def _ensurers(conn) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for key, owner in conn.execute(
        "SELECT b.key, e.owner_key FROM bundle b "
        "LEFT JOIN bundle_ensurer e ON e.bundle_id = b.id ORDER BY 1, 2"
    ).fetchall():
        out.setdefault(key, set())
        if owner is not None:
            out[key].add(owner)
    return out


def _all_members(conn) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for bkey, akey in conn.execute(
        "SELECT b.key, a.key FROM bundle_app ba JOIN bundle b ON b.id = ba.bundle_id "
        "JOIN app a ON a.id = ba.app_id"
    ).fetchall():
        out.setdefault(bkey, set()).add(akey)
    return out


def _members(conn, bundle: str = "kag-service") -> set[str]:
    return _all_members(conn).get(bundle, set())


def _bundles(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT key FROM bundle")}


def _snapshot(conn) -> tuple:
    return _bundles(conn), _ensurers(conn), _all_members(conn)


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

    def test_header_records_the_closure_and_the_ensurer_choice(self):
        header = UP.read_text(encoding="utf-8").split("BEGIN;", 1)[0]
        for needle in (
            "workers -> inference-router   (caller platform)",
            "NOT platform: platform does not issue kag",
            "REMAINING other_bundle CASE",
        ):
            assert needle in header, needle

    def test_seed_is_kag_only(self):
        assert BUNDLE_ENSURER_SEED["kag-service"] == ("kag",)
        assert M063_BUNDLE_ENSURERS == {"kag-service": set(BUNDLE_ENSURER_SEED["kag-service"])}


class TestRunForReal:
    def test_head_equals_the_boot_seed(self, conn):
        assert "kag-service" not in _bundles(conn)  # control: absent at 062
        _run_sql_file(conn, UP, NAME)
        assert _ensurers(conn) == {
            k: set(v)
            for k, v in BUNDLE_ENSURER_SEED.items()
            if k not in M078_BUNDLE_ENSURERS and k not in M082_BUNDLE_ENSURERS
        }

    def test_kag_service_holds_exactly_kag_and_nothing_else_moves(self, conn):
        before = _all_members(conn)
        _run_sql_file(conn, UP, NAME)
        after = _all_members(conn)
        assert after.pop("kag-service") == KAG_SERVICE
        assert after == before

    def test_platform_never_ensures_it(self, conn):
        _run_sql_file(conn, UP, NAME)
        assert _ensurers(conn)["kag-service"] == {"kag"}

    def test_a_missing_app_is_skipped(self, conn):
        conn.execute("DELETE FROM app WHERE key = 'kag'")
        _run_sql_file(conn, UP, NAME)
        assert "kag-service" in _bundles(conn)
        assert _members(conn) == set()

    def test_no_ensurer_when_kag_does_not_issue_kag(self, conn):
        conn.execute(
            "DELETE FROM app_issuer WHERE owner_key = 'kag' "
            "AND app_id = (SELECT id FROM app WHERE key = 'kag')"
        )
        _run_sql_file(conn, UP, NAME)
        assert _ensurers(conn)["kag-service"] == set()

    def test_an_operator_row_is_never_overwritten(self, conn):
        conn.execute("INSERT INTO bundle (key, name) VALUES ('kag-service', 'Operator')")
        _run_sql_file(conn, UP, NAME)
        row = conn.execute("SELECT name FROM bundle WHERE key = 'kag-service'").fetchone()
        assert row == ("Operator",)

    def test_rerun_is_a_no_op(self, conn):
        _run_sql_file(conn, UP, NAME)
        snap = _snapshot(conn)
        _run_sql_file(conn, UP, NAME)
        assert _snapshot(conn) == snap


class TestDowngrade:
    def test_restores_the_062_state(self, conn):
        before = _snapshot(conn)
        _run_sql_file(conn, UP, NAME)
        _run_sql_file(conn, DOWN, "down")
        assert _snapshot(conn) == before

    def test_refuses_while_a_tenant_holds_kag_service(self, conn):
        _run_sql_file(conn, UP, NAME)
        tid = conn.execute(
            "INSERT INTO tenants (id, name, owner_key) VALUES (uuidv7(), 'svc-kag', 'platform') "
            "RETURNING id"
        ).fetchone()
        assert tid is not None
        conn.execute(
            "INSERT INTO tenant_bundle (tenant_id, bundle_id) "
            "SELECT %s, id FROM bundle WHERE key = 'kag-service'",
            (tid[0],),
        )
        snap = _snapshot(conn)
        with pytest.raises(Exception, match="063 downgrade REFUSED"):
            _run_sql_file(conn, DOWN, "down")
        conn.execute("ROLLBACK")
        assert _snapshot(conn) == snap


class TestRunnerCycle:
    def test_up_down_up(self):
        from scripts.apply_migrations import apply_all

        dsn = _recreate_database(_libpq_dsn(), RUNNER_DB)
        try:
            _build_before(dsn)
            with psycopg.connect(dsn, autocommit=True) as c:
                _seed_apps(c)
                c.execute(
                    "CREATE TABLE schema_migrations (filename TEXT PRIMARY KEY, "
                    "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
                )
                for path in sorted(MIGRATIONS.glob("*.sql")):
                    if path.name < NAME:
                        c.execute(
                            "INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,)
                        )

            def state() -> tuple[bool, set[str]]:
                with psycopg.connect(dsn, autocommit=True) as c:
                    row = c.execute(
                        "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE filename = %s)",
                        (NAME,),
                    ).fetchone()
                    return bool(row and row[0]), _members(c)

            assert apply_all(dsn) == 0
            assert state() == (True, KAG_SERVICE)
            with psycopg.connect(dsn, autocommit=True) as c:
                _run_sql_file(c, DOWN, "down 063")
            assert state() == (False, set())
            assert apply_all(dsn) == 0
            assert state() == (True, KAG_SERVICE)
        finally:
            _drop_database(_libpq_dsn(), RUNNER_DB)
