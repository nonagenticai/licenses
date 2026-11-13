"""Migration 089: the ``agent`` bundle reaches the conveyor — agent -> conveyor.

2026-11-09, SPEC D19: the Rust conveyor serves its own ``/mcp``; the agent calls it
with the tenant's OWN conveyor licence (bundle-token as ``agent-bundle-minter``),
exactly as it calls mcp (082) and inference-router (086). Before this file the
``agent`` bundle's app_requires closure had no conveyor, so
``bundle-token?app=conveyor&bundle=agent`` was 404 ``app_not_in_bundle`` and every
conveyor call from the agent was ``licence_unavailable``.

Run for REAL against a database built FROM EMPTY (init.sql -> the genesis shim ->
every migration before 089), with ``agent`` / ``conveyor`` seeded as
scripts/seed_apps.py does on a live database (no migration creates them):

* before 089 the edge is absent — the gate is not vacuous;
* 089 writes EXACTLY agent -> conveyor (caller agent), one transaction, data only;
* a re-run is a no-op; a missing app is announced and skipped, never raised;
* models.APP_REQUIRES_SEED carries the same edge (the boot-seed mirror);
* the closure of ``{agent}`` walks to conveyor, so ensure provisions its dependency
  licence on every agent-bundle holder;
* the downgrade removes ONLY that edge (agent -> mcp stays) and un-stamps 089, so the
  real runner re-applies it (up -> down -> up).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from models import APP_REQUIRES_SEED
from scripts.build_schema_arms import (
    INIT_SQL,
    SHIM_SQL,
    _drop_database,
    _recreate_database,
    _run_sql_file,
)
from services.app_requires import Edge, closure
from tests.conftest import TEST_DATABASE_URL
from tests.unit.test_migration_082_mcp_toolkit_entitlements import (
    M089_APP_REQUIRES,
    _seed_edge_gate_apps,
)

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "migrations"
NAME = "089_agent_bundle_conveyor.sql"
UP = MIGRATIONS / NAME
DOWN = MIGRATIONS / "downgrades" / NAME
DB = "licenses_m089_test"

EDGE = next(iter(M089_APP_REQUIRES))


def _libpq_dsn() -> str:
    return TEST_DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1)


@pytest.fixture(scope="module")
def conn() -> Iterator[psycopg.Connection]:
    """A database built from EMPTY through 088 — 089 is applied by each test."""
    dsn = _libpq_dsn()
    arm = _recreate_database(dsn, DB)
    connection = psycopg.connect(arm, autocommit=True)
    try:
        _run_sql_file(connection, INIT_SQL, "genesis")
        _run_sql_file(connection, SHIM_SQL, "shim")
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name == NAME:
                continue
            # Live has mcp / coding / laya before 082 (scripts/seed_apps.py); seed them
            # so 082's agent -> mcp edge exists and the downgrade test can prove it stays.
            if path.name.startswith("082_"):
                _seed_edge_gate_apps(connection)
            _run_sql_file(connection, path, path.name)
        yield connection
    finally:
        connection.close()
        _drop_database(dsn, DB)


def _seed(c: psycopg.Connection) -> None:
    """The apps a from-empty corpus does not create (live they come from
    scripts/seed_apps.py), and the owner whose minter mints the edge."""
    c.execute(
        "INSERT INTO tenant_owner (key, display_name) VALUES ('agent', 'Agent') "
        "ON CONFLICT (key) DO NOTHING"
    )
    for key, name in (("agent", "Agent"), ("conveyor", "Conveyor")):
        c.execute(
            "INSERT INTO app (key, name, audience) VALUES (%s, %s, 'automation') "
            "ON CONFLICT (key) DO UPDATE SET name = app.name",
            (key, name),
        )


def _reset(c: psycopg.Connection) -> None:
    """Known starting point: no agent -> conveyor edge, both apps present."""
    c.execute(
        "DELETE FROM app_requires r USING app a, app q "
        "WHERE a.id = r.app_id AND q.id = r.required_app_id "
        "  AND a.key = 'agent' AND q.key = 'conveyor'"
    )
    _seed(c)


def _edges(c: psycopg.Connection) -> set[tuple[str, str, str]]:
    return {
        (a, q, r)
        for a, q, r in c.execute(
            "SELECT a.key, q.key, r.caller_owner_key FROM app_requires r "
            "JOIN app a ON a.id = r.app_id JOIN app q ON q.id = r.required_app_id "
            "WHERE a.key = 'agent'"
        ).fetchall()
    }


def _apply_up(c: psycopg.Connection) -> None:
    _run_sql_file(c, UP, NAME)


def _apply_down(c: psycopg.Connection) -> None:
    _run_sql_file(c, DOWN, f"downgrades/{NAME}")


def test_edge_is_absent_before_089(conn: psycopg.Connection) -> None:
    _reset(conn)
    assert EDGE not in _edges(conn)


def test_089_writes_exactly_the_agent_to_conveyor_edge(conn: psycopg.Connection) -> None:
    _reset(conn)
    _apply_up(conn)
    assert EDGE in _edges(conn)


def test_rerun_is_a_noop(conn: psycopg.Connection) -> None:
    _reset(conn)
    _apply_up(conn)
    _apply_up(conn)
    assert EDGE in _edges(conn)


def test_a_missing_app_is_skipped_not_raised(conn: psycopg.Connection) -> None:
    _reset(conn)
    conn.execute("DELETE FROM app WHERE key = 'conveyor'")
    _apply_up(conn)
    assert EDGE not in _edges(conn)
    _seed(conn)


def test_boot_seed_carries_the_same_edge() -> None:
    assert EDGE in APP_REQUIRES_SEED


def test_closure_of_the_agent_bundle_reaches_conveyor() -> None:
    deps, _ = closure({"agent"}, [Edge(*e) for e in APP_REQUIRES_SEED])
    assert "conveyor" in deps


def test_downgrade_removes_only_this_edge(conn: psycopg.Connection) -> None:
    _reset(conn)
    _apply_up(conn)
    assert EDGE in _edges(conn)
    assert ("agent", "mcp", "agent") in _edges(conn)

    _apply_down(conn)
    assert EDGE not in _edges(conn)
    assert ("agent", "mcp", "agent") in _edges(conn)
    # Idempotent: a second downgrade deletes nothing and raises nothing.
    _apply_down(conn)
    assert EDGE not in _edges(conn)
