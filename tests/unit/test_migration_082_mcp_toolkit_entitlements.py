"""Migration 082: owner ``mcp`` + mcp-bundle-minter, mcp -> {coding, strata-rag,
strata-ingest, laya}, the ``agent`` bundle + ensurer + agent-bundle-minter, agent -> mcp.

Run for REAL against a database built FROM EMPTY (init.sql -> the genesis shim -> every
migration before 082), with ``mcp`` / ``coding`` / ``laya`` seeded as
scripts/seed_apps.py does on a live database (no migration creates them), then 082:

* at head tenant_owner / api_client / bundle_ensurer equal the boot seed
  (models.TENANT_OWNERS / API_CLIENTS / BUNDLE_ENSURER_SEED) and app_requires holds
  APP_REQUIRES_SEED's 082 slice;
* the ``agent`` bundle holds EXACTLY {agent}; agent is its only ensurer, only while it
  issues agent (052 rule 1); nothing else moves;
* the REAL ``services.bundle_ensure`` on an agent-owned tenant afterwards walks the
  closure TRANSITIVELY: agent -> mcp -> coding gives a coding dependency licence and a
  tenant_dependency_grant (mcp, coding);
* a missing app is skipped; operator rows are never overwritten; a re-run is a no-op;
  an operator edge closing a cycle aborts the whole file;
* the downgrade restores the pre-082 state exactly and refuses while a tenant holds
  the bundle or a dependency grant is keyed on owner mcp; up -> down -> up through the
  REAL runner re-applies 082.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from models import (
    API_CLIENTS,
    APP_REQUIRES_SEED,
    BUNDLE_ENSURER_SEED,
    TENANT_OWNERS,
    issuers_for_app_key,
)
from scripts.apply_migrations import split_statements
from scripts.build_schema_arms import (
    INIT_SQL,
    SHIM_SQL,
    _drop_database,
    _recreate_database,
    _run_sql_file,
)
from services import bundle_ensure
from tests.conftest import TEST_DATABASE_URL
from tests.unit.test_migration_083_secure_apps_s2s_api_clients import M083_API_CLIENTS
from tests.unit.test_migration_085_w2v_cell_m4_api_client import M085_API_CLIENTS
from tests.unit.test_migration_090_conveyor_service_issuer import M090_API_CLIENTS

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "migrations"
NAME = "082_mcp_toolkit_entitlements.sql"
UP = MIGRATIONS / NAME
DOWN = MIGRATIONS / "downgrades" / NAME
DB = "licenses_m082_test"
RUNNER_DB = "licenses_m082_runner_test"
BACKENDS = ("coding", "strata-rag", "strata-ingest", "laya")
T_AGENT = "0195f48b-d000-7000-8000-0000000a9e01"  # an agent-owned tenant

# What 082 adds on top of the earlier seeds — older migration tests subtract these.
M082_TENANT_OWNERS: set[tuple[str, str]] = {("mcp", "MCP")}
M082_API_CLIENTS: set[tuple[str, str, str, bool]] = {
    ("secure-apps", "mcp-bundle-minter", "mcp", False),
    ("secure-apps", "agent-bundle-minter", "agent", False),
}
M082_BUNDLE_ENSURERS: dict[str, set[str]] = {"agent": {"agent"}}
M082_APP_REQUIRES: set[tuple[str, str, str]] = {
    *{("mcp", b, "mcp") for b in BACKENDS},
    ("agent", "mcp", "agent"),
}


# Migration 086 adds a later edge out of agent; 082's slice excludes it.
M086_APP_REQUIRES: set[tuple[str, str, str]] = {("agent", "inference-router", "agent")}

# Migration 089 adds one more later edge out of agent (agent -> conveyor, SPEC D19).
M089_APP_REQUIRES: set[tuple[str, str, str]] = {("agent", "conveyor", "agent")}


def _libpq_dsn() -> str:
    return TEST_DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1)


def _seed_edge_gate_apps(c: psycopg.Connection) -> None:
    """``mcp`` / ``coding`` / ``laya`` as seed_apps.py creates them (edge-gate block:
    default issuers, a ``standard`` plan)."""
    for key, name in (("mcp", "MCP"), ("coding", "Coding"), ("laya", "Laya System-1 (Jev API)")):
        row = c.execute(
            "INSERT INTO app (key, name, audience) VALUES (%s, %s, 'platform') "
            "ON CONFLICT (key) DO UPDATE SET name = app.name RETURNING id",
            (key, name),
        ).fetchone()
        assert row is not None
        for owner in issuers_for_app_key(key):
            c.execute(
                "INSERT INTO app_issuer (app_id, owner_key) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (row[0], owner),
            )
        c.execute(
            "INSERT INTO plan (app_id, code, name) SELECT %s, 'standard', 'Standard' "
            "WHERE NOT EXISTS (SELECT 1 FROM plan WHERE app_id = %s AND code = 'standard')",
            (row[0], row[0]),
        )


def _build_before(dsn: str) -> None:
    from tests.unit.test_migration_059_app_requires import _seed_apps as _seed_059_apps

    with psycopg.connect(dsn, autocommit=True) as c:
        _run_sql_file(c, INIT_SQL, "genesis")
        _run_sql_file(c, SHIM_SQL, "shim")
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name.startswith("059_"):
                _seed_059_apps(c)
            if path.name.startswith("078_"):
                _seed_edge_gate_apps(c)  # live has them before 078 (seed_apps.py)
            if path.name < NAME:
                _run_sql_file(c, path, "corpus")


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    """A database at the exact pre-082 state, built from empty."""
    dsn = _recreate_database(_libpq_dsn(), DB)
    try:
        _build_before(dsn)
        with psycopg.connect(dsn, autocommit=True) as c:
            yield c
    finally:
        _drop_database(_libpq_dsn(), DB)


def _count(conn, table: str) -> int:
    row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()  # noqa: S608 — fixed names
    assert row is not None
    return int(row[0])


def _owners(conn) -> set[tuple]:
    return {tuple(r) for r in conn.execute("SELECT key, display_name FROM tenant_owner")}


def _clients(conn) -> set[tuple]:
    return {
        tuple(r)
        for r in conn.execute("SELECT realm, client_id, owner_key, cross_owner FROM api_client")
    }


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


def _edges(conn) -> set[tuple]:
    return {
        tuple(r)
        for r in conn.execute(
            "SELECT a.key, r.key, ar.caller_owner_key FROM app_requires ar "
            "JOIN app a ON a.id = ar.app_id JOIN app r ON r.id = ar.required_app_id"
        )
    }


def _issuers(conn) -> set[tuple]:
    return {
        tuple(r)
        for r in conn.execute(
            "SELECT a.key, i.owner_key FROM app_issuer i JOIN app a ON a.id = i.app_id"
        )
    }


def _snapshot(conn) -> tuple:
    return (
        _owners(conn),
        _clients(conn),
        _ensurers(conn),
        _all_members(conn),
        _edges(conn),
        _issuers(conn),
        _count(conn, "app"),
        _count(conn, "plan"),
        _count(conn, "bundle"),
        _count(conn, "tenant_app_grant"),
        _count(conn, "tenant_dependency_grant"),
        _count(conn, "license"),
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

    def test_seeds_carry_the_082_rows(self):
        assert M082_TENANT_OWNERS <= set(TENANT_OWNERS)
        assert M082_API_CLIENTS <= set(API_CLIENTS)
        assert M082_APP_REQUIRES <= set(APP_REQUIRES_SEED)
        assert {k: set(v) for k, v in BUNDLE_ENSURER_SEED.items() if k == "agent"} == (
            M082_BUNDLE_ENSURERS
        )
        assert {
            e for e in APP_REQUIRES_SEED if e[2] == "mcp" or e[0] == "agent"
        } - M086_APP_REQUIRES - M089_APP_REQUIRES == (M082_APP_REQUIRES)


class TestRunForReal:
    def test_head_equals_the_boot_seed(self, conn):
        # Control: none of it exists before 082.
        assert not (_owners(conn) & M082_TENANT_OWNERS)
        assert not (_clients(conn) & M082_API_CLIENTS)
        assert not (_edges(conn) & M082_APP_REQUIRES)
        assert "agent" not in _ensurers(conn)
        from tests.unit.test_migration_084_retire_strata_configurator import (
            M084_RETIRED_API_CLIENTS,
        )

        _run_sql_file(conn, UP, NAME)
        assert _owners(conn) == set(TENANT_OWNERS)
        # 083 adds clients and 084 retires the configurator's, both after 082.
        assert (
            _clients(conn)
            == (set(API_CLIENTS) - M083_API_CLIENTS - M085_API_CLIENTS - M090_API_CLIENTS)
            | M084_RETIRED_API_CLIENTS
        )
        assert _ensurers(conn) == {k: set(v) for k, v in BUNDLE_ENSURER_SEED.items()}
        assert {
            e for e in _edges(conn) if e[2] == "mcp" or e[0] == "agent"
        } - M086_APP_REQUIRES == (M082_APP_REQUIRES)

    def test_bundle_holds_exactly_agent_and_nothing_else_moves(self, conn):
        before = _all_members(conn)
        issuers_before = _issuers(conn)
        _run_sql_file(conn, UP, NAME)
        after = _all_members(conn)
        assert after.pop("agent") == {"agent"}
        assert after == before
        assert _issuers(conn) == issuers_before  # mcp issues nothing; no issuer widened

    def test_a_missing_backend_is_skipped(self, conn):
        conn.execute("DELETE FROM app WHERE key = 'coding'")
        _run_sql_file(conn, UP, NAME)
        assert {
            e for e in _edges(conn) if e[2] == "mcp" or e[0] == "agent"
        } - M086_APP_REQUIRES == (M082_APP_REQUIRES - {("mcp", "coding", "mcp")})

    def test_no_ensurer_when_agent_does_not_issue_agent(self, conn):
        conn.execute(
            "DELETE FROM app_issuer WHERE owner_key = 'agent' "
            "AND app_id = (SELECT id FROM app WHERE key = 'agent')"
        )
        _run_sql_file(conn, UP, NAME)
        assert _ensurers(conn)["agent"] == set()

    def test_an_operator_row_is_never_overwritten(self, conn):
        conn.execute("INSERT INTO bundle (key, name) VALUES ('agent', 'Operator')")
        conn.execute(
            "INSERT INTO api_client (realm, client_id, owner_key, cross_owner) "
            "VALUES ('secure-apps', 'agent-bundle-minter', 'platform', false)"
        )
        conn.execute("INSERT INTO tenant_owner (key, display_name) VALUES ('mcp', 'Ops MCP')")
        _run_sql_file(conn, UP, NAME)
        assert conn.execute("SELECT name FROM bundle WHERE key = 'agent'").fetchone() == (
            "Operator",
        )
        assert ("secure-apps", "agent-bundle-minter", "platform", False) in _clients(conn)
        assert ("mcp", "Ops MCP") in _owners(conn)

    def test_rerun_is_a_no_op(self, conn):
        _run_sql_file(conn, UP, NAME)
        snap = _snapshot(conn)
        _run_sql_file(conn, UP, NAME)
        assert _snapshot(conn) == snap

    def test_a_cycle_aborts_the_whole_file(self, conn):
        # An operator edge coding -> agent closes agent -> mcp -> coding -> agent.
        conn.execute(
            "INSERT INTO app_requires (app_id, required_app_id, caller_owner_key) "
            "SELECT c.id, a.id, 'platform' FROM app c, app a "
            "WHERE c.key = 'coding' AND a.key = 'agent'"
        )
        snap = _snapshot(conn)
        with pytest.raises(Exception, match="082: app_requires holds a cycle"):
            _run_sql_file(conn, UP, NAME)
        conn.execute("ROLLBACK")
        assert _snapshot(conn) == snap


def _agent_tenant(conn) -> None:
    conn.execute(
        "INSERT INTO tenants (id, name, byo_mode, owner_key, created_at) "
        "VALUES (%s, 'agent-tenant', 'platform_only', 'agent', now()) ON CONFLICT DO NOTHING",
        (T_AGENT,),
    )


def _ensure_agent(conn) -> bundle_ensure.EnsurePlan:
    """The route's decision + writes (services.bundle_ensure), billing push failing."""
    # Lazy: older migration tests import this module's M082_* sets at top level.
    from tests.unit.test_migration_079_fr_test_user_care_bus_bundle import _db_manager, _dsn_of

    _agent_tenant(conn)
    db = _db_manager(_dsn_of(conn))
    try:
        ep = bundle_ensure.plan(db, T_AGENT, "agent", ["standard"])
        bundle_ensure.apply(
            db,
            ep,
            provisioned_by="test-082",
            granted_by="bundle:agent by test",
            provision=lambda *a, **k: "billing down (test)",
        )
    finally:
        db.engine.dispose()
    return ep


class TestEnsureWalksTheClosureTransitively:
    def test_agent_ensure_reaches_the_backends_through_mcp(self, conn):
        _run_sql_file(conn, UP, NAME)
        ep = _ensure_agent(conn)
        assert not ep.refused
        lic = {
            tuple(r)
            for r in conn.execute(
                "SELECT a.key, l.is_dependency FROM license l JOIN app a ON a.id = l.app_id "
                "WHERE l.tenant_id = %s AND l.status = 'active'",
                (T_AGENT,),
            )
        }
        assert lic == {("agent", False), ("mcp", True), *{(b, True) for b in BACKENDS}}
        grants = {
            tuple(r)
            for r in conn.execute(
                "SELECT g.owner_key, a.key FROM tenant_dependency_grant g "
                "JOIN app a ON a.id = g.app_id WHERE g.tenant_id = %s",
                (T_AGENT,),
            )
        }
        assert grants == {("mcp", b) for b in BACKENDS}  # none for agent (its own owner)


class TestDowngrade:
    def test_restores_the_pre_082_state(self, conn):
        before = _snapshot(conn)
        _run_sql_file(conn, UP, NAME)
        _run_sql_file(conn, DOWN, "down")
        assert _snapshot(conn) == before

    def test_refuses_while_a_tenant_holds_the_bundle_and_mcp_has_grants(self, conn):
        _run_sql_file(conn, UP, NAME)
        _ensure_agent(conn)
        snap = _snapshot(conn)
        with pytest.raises(Exception, match="082 downgrade REFUSED"):
            _run_sql_file(conn, DOWN, "down")
        conn.execute("ROLLBACK")
        assert _snapshot(conn) == snap

    def test_refuses_on_an_operator_mcp_grant_alone(self, conn):
        _run_sql_file(conn, UP, NAME)
        _agent_tenant(conn)
        conn.execute(
            "INSERT INTO tenant_dependency_grant (tenant_id, owner_key, app_id, granted_by) "
            "SELECT %s, 'mcp', id, 'operator' FROM app WHERE key = 'coding'",
            (T_AGENT,),
        )
        snap = _snapshot(conn)
        with pytest.raises(Exception, match="082 downgrade REFUSED"):
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
                c.execute(
                    "CREATE TABLE schema_migrations (filename TEXT PRIMARY KEY, "
                    "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
                )
                for path in sorted(MIGRATIONS.glob("*.sql")):
                    if path.name < NAME:
                        c.execute(
                            "INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,)
                        )

            def state() -> tuple[bool, set[tuple]]:
                with psycopg.connect(dsn, autocommit=True) as c:
                    row = c.execute(
                        "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE filename = %s)",
                        (NAME,),
                    ).fetchone()
                    return (
                        bool(row and row[0]),
                        {e for e in _edges(c) if e[2] == "mcp" or e[0] == "agent"}
                        - M086_APP_REQUIRES,
                    )

            assert apply_all(dsn) == 0
            assert state() == (True, M082_APP_REQUIRES)
            with psycopg.connect(dsn, autocommit=True) as c:
                _run_sql_file(c, DOWN, "down 082")
            assert state() == (False, set())
            assert apply_all(dsn) == 0
            assert state() == (True, M082_APP_REQUIRES)
        finally:
            _drop_database(_libpq_dsn(), RUNNER_DB)
