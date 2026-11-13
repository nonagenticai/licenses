"""Migration 056: owner merqi for secure-apps/growth-bundle-minter.

Run for REAL against a database built FROM EMPTY (init.sql -> the genesis shim ->
001..055), then 056 is applied:

* the minter gets its api_client row (owner merqi, not cross-owner) and at head
  api_client equals the boot seed (models.API_CLIENTS) — a FRESH database never runs
  this file;
* an operator's row for the same client is never overwritten; a re-run is a no-op;
* the downgrade removes exactly that row; up -> down -> up through the REAL runner
  re-applies 056.
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
from tests.unit.test_migration_078_care_bus_early_access import M078_API_CLIENTS
from tests.unit.test_migration_082_mcp_toolkit_entitlements import (
    M082_API_CLIENTS,
)
from tests.unit.test_migration_083_secure_apps_s2s_api_clients import M083_API_CLIENTS
from tests.unit.test_migration_085_w2v_cell_m4_api_client import M085_API_CLIENTS
from tests.unit.test_migration_090_conveyor_service_issuer import M090_API_CLIENTS

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "migrations"
NAME = "056_growth_bundle_minter_owner.sql"
UP = MIGRATIONS / NAME
DOWN = MIGRATIONS / "downgrades" / NAME
DB = "licenses_m056_test"
RUNNER_DB = "licenses_m056_runner_test"

M056_API_CLIENTS: set[tuple[str, str, str, bool]] = {
    ("secure-apps", "growth-bundle-minter", "merqi", False),
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
    """A database at the exact 055 state, built from empty."""
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
        assert not (_rows(conn) & M056_API_CLIENTS)  # control: absent at 055
        _run_sql_file(conn, UP, NAME)
        from tests.unit.test_migration_057_kag_owner import M057_API_CLIENTS
        from tests.unit.test_migration_059_app_requires import M059_API_CLIENTS
        from tests.unit.test_migration_060_kag_frontend_bundle_minter import M060_API_CLIENTS
        from tests.unit.test_migration_061_kag_bundle_minter import M061_API_CLIENTS
        from tests.unit.test_migration_064_karma_bundle_minter import M064_API_CLIENTS
        from tests.unit.test_migration_067_strata_apps import M067_API_CLIENTS
        from tests.unit.test_migration_068_care_bus_licenses_admin_client import (
            M068_API_CLIENTS,
        )
        from tests.unit.test_migration_070_agent_app import M070_API_CLIENTS

        # 057: kag's client; 059: fr / w2v bundle minters; 060 / 061: kag minters; 064: karma minter.
        assert _rows(conn) == (
            set(API_CLIENTS)
            - M057_API_CLIENTS
            - M059_API_CLIENTS
            - M060_API_CLIENTS
            - M061_API_CLIENTS
            - M064_API_CLIENTS
            - M067_API_CLIENTS
            - M068_API_CLIENTS
            - M070_API_CLIENTS
            - M078_API_CLIENTS
            - M082_API_CLIENTS
            - M083_API_CLIENTS
            - M085_API_CLIENTS
            - M083_API_CLIENTS
            - M085_API_CLIENTS
            - M090_API_CLIENTS
        )
        assert M056_API_CLIENTS <= set(API_CLIENTS)

    def test_owner_is_merqi_and_merqi_is_a_registered_owner(self, conn):
        _run_sql_file(conn, UP, NAME)
        row = conn.execute(
            "SELECT a.owner_key, a.cross_owner, o.key FROM api_client a "
            "LEFT JOIN tenant_owner o ON o.key = a.owner_key "
            "WHERE a.realm = 'secure-apps' AND a.client_id = 'growth-bundle-minter'"
        ).fetchone()
        assert row == ("merqi", False, "merqi")

    def test_an_operator_change_is_never_overwritten(self, conn):
        conn.execute(
            "INSERT INTO api_client (realm, client_id, owner_key) "
            "VALUES ('secure-apps', 'growth-bundle-minter', 'platform')"
        )
        _run_sql_file(conn, UP, NAME)
        owner = conn.execute(
            "SELECT owner_key FROM api_client WHERE client_id = 'growth-bundle-minter'"
        ).fetchone()
        assert owner == ("platform",)

    def test_rerun_is_a_no_op(self, conn):
        _run_sql_file(conn, UP, NAME)
        snap = _rows(conn)
        _run_sql_file(conn, UP, NAME)
        assert _rows(conn) == snap


class TestDowngrade:
    def test_removes_only_the_056_row(self, conn):
        before = _rows(conn)
        _run_sql_file(conn, UP, NAME)
        _run_sql_file(conn, DOWN, "down")
        assert _rows(conn) == before

    def test_keeps_an_operator_changed_owner(self, conn):
        conn.execute(
            "INSERT INTO api_client (realm, client_id, owner_key) "
            "VALUES ('secure-apps', 'growth-bundle-minter', 'platform')"
        )
        _run_sql_file(conn, DOWN, "down")
        assert ("secure-apps", "growth-bundle-minter", "platform", False) in _rows(conn)


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
                    return bool(row and row[0]), bool(_rows(c) & M056_API_CLIENTS)

            assert apply_all(dsn) == 0
            assert state() == (True, True)
            with psycopg.connect(dsn, autocommit=True) as c:
                _run_sql_file(c, DOWN, "down 056")
            assert state() == (False, False)
            assert apply_all(dsn) == 0
            assert state() == (True, True)
        finally:
            _drop_database(_libpq_dsn(), RUNNER_DB)
