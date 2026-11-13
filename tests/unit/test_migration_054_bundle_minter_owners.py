"""Migration 054: owners for the bundle-token minters; merqi / w2v service tenants.

Run for REAL against a database built FROM EMPTY (init.sql -> the genesis shim ->
001..053), then 054 is applied:

* the four minter clients get their api_client rows, and at head api_client equals
  the boot seed (models.API_CLIENTS) — a FRESH database never runs this file;
* the named service tenants move from 'platform' to w2v / merqi; tenant-dev and
  svc-core-app-mlx stay; a row NOT owned by platform, an archived row, or a name the
  target owner already uses live is left alone;
* after the move every non-platform tenant holds an active 'platform' grant and no
  owner holds a grant on its own tenant; w2v KEEPS its grant on merqi's
  svc-control-service-w2v-partner (it issues that tenant's w2v licence);
* a re-run changes nothing; the downgrade moves the tenants back with 050's
  sole-issuer grants; up -> down -> up through the REAL runner re-applies 054.
"""

from __future__ import annotations

import uuid
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
NAME = "054_bundle_minter_owners_and_service_tenant_owners.sql"
UP = MIGRATIONS / NAME
DOWN = MIGRATIONS / "downgrades" / NAME
DB = "licenses_m054_test"
RUNNER_DB = "licenses_m054_runner_test"

M054_API_CLIENTS: set[tuple[str, str, str, bool]] = {
    ("secure-apps", "marketing-bundle-minter", "merqi", False),
    ("secure-apps", "sales-bundle-minter", "merqi", False),
    ("secure-apps", "worker-management-bundle-minter", "platform", False),
    ("secure-apps", "n8n-bundle-resolver", "platform", False),
}

MOVES = {
    "svc-w2v-cell-m4-gate-tenant": "w2v",
    "svc-w2v-cell-m4-orchestrator": "w2v",
    "svc-w2v-cell-m4-partner": "w2v",
    "svc-control-service-w2v-partner": "merqi",
    "svc-marketing": "merqi",
    "svc-marketing-t-c46c9378": "merqi",
    "svc-sales": "merqi",
    "svc-growth": "merqi",
    "svc-control-relay": "merqi",
    "svc-platform-console": "merqi",
}
STAY = ("tenant-dev", "svc-core-app-mlx")


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
    """A database at the exact 053 state, built from empty."""
    dsn = _recreate_database(_libpq_dsn(), DB)
    try:
        _build_before(dsn)
        with psycopg.connect(dsn, autocommit=True) as c:
            yield c
    finally:
        _drop_database(_libpq_dsn(), DB)


def _one(conn, sql: str, params=None):
    row = conn.execute(sql, params).fetchone()
    assert row is not None, sql
    return row[0]


def _tenant(conn, name: str, owner: str = "platform", *, archived: bool = False) -> str:
    return str(
        _one(
            conn,
            "INSERT INTO tenants (id, name, owner_key, kind, archived_at) "
            "VALUES (%s, %s, %s, 'service', CASE WHEN %s THEN now() END) RETURNING id",
            (str(uuid.uuid7()), name, owner, archived),
        )
    )


def _grant(conn, tenant: str, owner: str) -> None:
    conn.execute(
        "INSERT INTO tenant_app_grant (tenant_id, grantee_owner_key, granted_by) "
        "VALUES (%s, %s, 'migration-050')",
        (tenant, owner),
    )


def _w2v_licence(conn, tenant: str) -> None:
    """An active `w2v` licence (sole issuer w2v) on ``tenant``."""
    app = conn.execute("SELECT id FROM app WHERE key = 'w2v'").fetchone()
    if app is None:
        app_id = _one(conn, "INSERT INTO app (key, name) VALUES ('w2v', 'w2v') RETURNING id")
        conn.execute("INSERT INTO app_issuer (app_id, owner_key) VALUES (%s, 'w2v')", (app_id,))
    else:
        app_id = app[0]
    plan = _one(
        conn,
        "INSERT INTO plan (app_id, code, name) VALUES (%s, %s, 'Std') RETURNING id",
        (app_id, f"std-{uuid.uuid4().hex[:6]}"),
    )
    sub = _one(
        conn,
        "INSERT INTO subject (keycloak_sub, keycloak_realm) VALUES (%s, 'service') RETURNING id",
        (f"svc:{tenant}:w2v",),
    )
    conn.execute(
        "INSERT INTO license (subject_id, app_id, plan_id, tenant_id, status, starts_at) "
        "VALUES (%s, %s, %s, %s, 'active', now() - interval '1 day')",
        (sub, app_id, plan, tenant),
    )


def _owner(conn, tid: str) -> str:
    return _one(conn, "SELECT owner_key FROM tenants WHERE id = %s", (tid,))


def _grants(conn, tid: str) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT grantee_owner_key FROM tenant_app_grant "
            "WHERE tenant_id = %s AND revoked_at IS NULL",
            (tid,),
        ).fetchall()
    }


def _seed_prod_shape(conn) -> dict[str, str]:
    """The 2026-10-14 prod shape (the primary, read-only): all on platform; w2v holds a
    grant on the three cells and the partner tenant, merqi on svc-marketing-t-*."""
    ids = {name: _tenant(conn, name) for name in (*MOVES, *STAY)}
    for name in (
        "svc-w2v-cell-m4-gate-tenant",
        "svc-w2v-cell-m4-orchestrator",
        "svc-w2v-cell-m4-partner",
        "svc-control-service-w2v-partner",
    ):
        _grant(conn, ids[name], "w2v")
    _w2v_licence(conn, ids["svc-control-service-w2v-partner"])
    _grant(conn, ids["svc-marketing-t-c46c9378"], "merqi")
    for owner in ("fr", "merqi", "w2v"):
        _grant(conn, ids["tenant-dev"], owner)
    return ids


def _invariants(conn) -> None:
    assert not conn.execute(
        "SELECT name FROM tenants t WHERE owner_key <> 'platform' AND NOT EXISTS ("
        "SELECT 1 FROM tenant_app_grant g WHERE g.tenant_id = t.id "
        "AND g.grantee_owner_key = 'platform' AND g.revoked_at IS NULL)"
    ).fetchall()
    assert not conn.execute(
        "SELECT 1 FROM tenant_app_grant g JOIN tenants t ON t.id = g.tenant_id "
        "WHERE t.owner_key = g.grantee_owner_key AND g.revoked_at IS NULL"
    ).fetchall()


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
        before = {
            tuple(r)
            for r in conn.execute("SELECT realm, client_id, owner_key, cross_owner FROM api_client")
        }
        assert not (before & M054_API_CLIENTS)  # control: absent at 053
        _run_sql_file(conn, UP, NAME)
        after = {
            tuple(r)
            for r in conn.execute("SELECT realm, client_id, owner_key, cross_owner FROM api_client")
        }
        # 055/056 seed provisioning's / growth's minters, 057 kag's client; 057's test
        # pins head == the boot seed.
        from tests.unit.test_migration_055_provisioning_bundle_minter import M055_API_CLIENTS
        from tests.unit.test_migration_056_growth_bundle_minter import M056_API_CLIENTS
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

        assert after == (
            set(API_CLIENTS)
            - M055_API_CLIENTS
            - M056_API_CLIENTS
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
        assert M054_API_CLIENTS <= set(API_CLIENTS)

    def test_an_operator_change_is_never_overwritten(self, conn):
        conn.execute(
            "INSERT INTO api_client (realm, client_id, owner_key) "
            "VALUES ('secure-apps', 'sales-bundle-minter', 'platform')"
        )
        _run_sql_file(conn, UP, NAME)
        assert (
            _one(
                conn,
                "SELECT owner_key FROM api_client WHERE client_id = 'sales-bundle-minter'",
            )
            == "platform"
        )


class TestTenantMoves:
    def test_moves_and_stays(self, conn):
        ids = _seed_prod_shape(conn)
        _run_sql_file(conn, UP, NAME)
        for name, owner in MOVES.items():
            assert _owner(conn, ids[name]) == owner, name
        for name in STAY:
            assert _owner(conn, ids[name]) == "platform", name
        _invariants(conn)

    def test_grants_after_the_move(self, conn):
        ids = _seed_prod_shape(conn)
        _run_sql_file(conn, UP, NAME)
        # w2v keeps issuing the partner licence on merqi's tenant.
        assert _grants(conn, ids["svc-control-service-w2v-partner"]) == {"platform", "w2v"}
        # the w2v cells are w2v's own now: no self-grant, platform infra grant.
        assert _grants(conn, ids["svc-w2v-cell-m4-partner"]) == {"platform"}
        assert _grants(conn, ids["svc-marketing-t-c46c9378"]) == {"platform"}
        assert _grants(conn, ids["svc-sales"]) == {"platform"}
        assert _grants(conn, ids["tenant-dev"]) == {"fr", "merqi", "w2v"}

    def test_only_live_platform_rows_move(self, conn):
        fr_row = _tenant(conn, "svc-marketing", owner="fr")
        archived = _tenant(conn, "svc-sales", archived=True)
        _run_sql_file(conn, UP, NAME)
        assert _owner(conn, fr_row) == "fr"
        assert _owner(conn, archived) == "platform"

    def test_a_name_the_target_already_uses_is_skipped(self, conn):
        theirs = _tenant(conn, "svc-growth", owner="merqi")
        ours = _tenant(conn, "svc-growth")
        _run_sql_file(conn, UP, NAME)
        assert (_owner(conn, theirs), _owner(conn, ours)) == ("merqi", "platform")

    def test_rerun_is_a_no_op(self, conn):
        ids = _seed_prod_shape(conn)
        _run_sql_file(conn, UP, NAME)
        snap = conn.execute(
            "SELECT tenant_id, grantee_owner_key, revoked_at FROM tenant_app_grant ORDER BY 1, 2, 3"
        ).fetchall()
        owners = {n: _owner(conn, t) for n, t in ids.items()}
        _run_sql_file(conn, UP, NAME)
        assert (
            conn.execute(
                "SELECT tenant_id, grantee_owner_key, revoked_at FROM tenant_app_grant "
                "ORDER BY 1, 2, 3"
            ).fetchall()
            == snap
        )
        assert {n: _owner(conn, t) for n, t in ids.items()} == owners


class TestDowngrade:
    def test_moves_back_with_sole_issuer_grants(self, conn):
        ids = _seed_prod_shape(conn)
        _run_sql_file(conn, UP, NAME)
        _run_sql_file(conn, DOWN, "down")
        for name in (*MOVES, *STAY):
            assert _owner(conn, ids[name]) == "platform", name
        # the w2v licence on the partner tenant re-derives w2v's grant (050 rule 2)
        assert _grants(conn, ids["svc-control-service-w2v-partner"]) == {"w2v"}
        assert _grants(conn, ids["svc-sales"]) == set()
        assert not (
            {
                tuple(r)
                for r in conn.execute(
                    "SELECT realm, client_id, owner_key, cross_owner FROM api_client"
                )
            }
            & M054_API_CLIENTS
        )
        _invariants(conn)


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
                tid = _tenant(c, "svc-sales")

            def state() -> tuple[bool, str]:
                with psycopg.connect(dsn, autocommit=True) as c:
                    row = c.execute(
                        "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE filename = %s)",
                        (NAME,),
                    ).fetchone()
                    return bool(row and row[0]), _owner(c, tid)

            assert apply_all(dsn) == 0
            assert state() == (True, "merqi")
            with psycopg.connect(dsn, autocommit=True) as c:
                _run_sql_file(c, DOWN, "down 054")
            assert state() == (False, "platform")
            assert apply_all(dsn) == 0
            assert state() == (True, "merqi")
        finally:
            _drop_database(_libpq_dsn(), RUNNER_DB)
