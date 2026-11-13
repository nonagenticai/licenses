"""Migration 051: the ``n8n-nodes`` bundle, and the legacy netcare tenant steps aside.

Run for REAL against a database built FROM EMPTY through 050 (init.sql -> the
genesis shim -> 001..050), seeded with the prod shapes measured read-only on
the primary 2026-10-14, then 051 is applied:

* ``n8n-nodes`` holds the six node-module apps; a missing app is skipped;
* each app without a ``tenant-pooled`` plan gets one with the shape of the 33
  existing ones (quota 1,000,000, NO token budget), rate limits from the app's own
  highest plan; an existing (or revoked) ``tenant-pooled`` is never duplicated;
* the legacy netcare tenant (by ID) becomes ``netcare-legacy`` owned by platform,
  stays archived, every grant revoked, licences untouched — so commonpractice can
  register a fresh ``netcare``;
* absent / already-renamed rows are a no-op; a re-run changes nothing;
* the downgrade restores netcare and its grants, and refuses while the bundle is
  held or CP already has a new ``netcare``; up -> down -> up through the runner.
"""

from __future__ import annotations

import uuid
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
NAME = "051_n8n_nodes_bundle_and_retire_legacy_netcare.sql"
UP = MIGRATIONS / NAME
DOWN = MIGRATIONS / "downgrades" / NAME

NETCARE = "01a0ef72-31af-7561-936a-b8383b5fb6c8"
N8N_NODES = (
    "kaizen_core_actions",
    "kaizen_discover_actions",
    "meta_prompt_core_actions",
    "meta_prompt_discover_actions",
    "metering",
    "observability",
)
# prod 2026-10-14: (code, rpm, burst) per app; two apps have no plan at all.
PROD_PLANS: dict[str, list[tuple[str, int, int]]] = {
    "kaizen_core_actions": [],
    "kaizen_discover_actions": [("system-workflow", 9999, 1000)],
    "meta_prompt_core_actions": [],
    "meta_prompt_discover_actions": [("system-prompt", 9999, 1000)],
    "metering": [("portal-n8n-dev", 9999, 1000)],
    "observability": [("perpetual", 1000, 100)],
}
DB = "licenses_m051_test"
RUNNER_DB = "licenses_m051_runner_test"


def _libpq_dsn() -> str:
    return TEST_DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1)


def _build_to_050(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as c:
        _run_sql_file(c, INIT_SQL, "genesis")
        _run_sql_file(c, SHIM_SQL, "shim")
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name < UP.name:
                _run_sql_file(c, path, "corpus")


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    """A database at the exact 050 state, built from empty."""
    dsn = _recreate_database(_libpq_dsn(), DB)
    try:
        _build_to_050(dsn)
        with psycopg.connect(dsn, autocommit=True) as c:
            yield c
    finally:
        _drop_database(_libpq_dsn(), DB)


def _one(conn, sql: str, params=None):
    row = conn.execute(sql, params).fetchone()
    assert row is not None, sql
    return row[0]


def _app(conn, key: str) -> str:
    existing = conn.execute("SELECT id FROM app WHERE key = %s", (key,)).fetchone()
    if existing is not None:
        return str(existing[0])
    return str(_one(conn, "INSERT INTO app (key, name) VALUES (%s, %s) RETURNING id", (key, key)))


def _plan(conn, app_id: str, code: str, rpm: int = 60, burst: int = 10, *, revoked=False) -> str:
    return str(
        _one(
            conn,
            "INSERT INTO plan (app_id, code, name, rate_limit_rpm, rate_limit_burst, revoked) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (app_id, code, code, rpm, burst, revoked),
        )
    )


def _seed_apps(conn, keys=N8N_NODES) -> dict[str, str]:
    ids = {}
    for key in keys:
        ids[key] = _app(conn, key)
        for code, rpm, burst in PROD_PLANS[key]:
            _plan(conn, ids[key], code, rpm, burst)
    return ids


def _seed_netcare(conn) -> None:
    """The prod shape: archived, owned by commonpractice, 4 active grants, licences."""
    conn.execute(
        "INSERT INTO tenants (id, name, owner_key, archived_at) "
        "VALUES (%s, 'netcare', 'commonpractice', '2026-10-12T20:16:13+00')",
        (NETCARE,),
    )
    for grantee in ("platform", "fr", "merqi", "w2v"):
        conn.execute(
            "INSERT INTO tenant_app_grant (tenant_id, grantee_owner_key, granted_by) "
            "VALUES (%s, %s, 'migration-050')",
            (NETCARE, grantee),
        )
    app_id = _app(conn, "kanban")
    sub = _one(
        conn,
        "INSERT INTO subject (keycloak_sub, keycloak_realm) VALUES (%s, 'service') RETURNING id",
        (f"svc:{NETCARE}:kanban",),
    )
    conn.execute(
        "INSERT INTO license (subject_id, app_id, plan_id, tenant_id, status, starts_at) "
        "VALUES (%s, %s, %s, %s, 'active', now())",
        (sub, app_id, _plan(conn, app_id, "tenant-pooled"), NETCARE),
    )


def _netcare(conn) -> tuple:
    return conn.execute(
        "SELECT name, owner_key, archived_at IS NOT NULL FROM tenants WHERE id = %s",
        (NETCARE,),
    ).fetchone()


def _active_grants(conn) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT grantee_owner_key FROM tenant_app_grant "
            "WHERE tenant_id = %s AND revoked_at IS NULL",
            (NETCARE,),
        ).fetchall()
    }


def _bundle_apps(conn) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT a.key FROM bundle b JOIN bundle_app ba ON ba.bundle_id = b.id "
            "JOIN app a ON a.id = ba.app_id WHERE b.key = 'n8n-nodes'"
        ).fetchall()
    }


def _pooled(conn) -> dict[str, tuple]:
    return {
        r[0]: tuple(r[1:])
        for r in conn.execute(
            "SELECT a.key, p.name, p.rate_limit_rpm, p.rate_limit_burst, p.quota_monthly, "
            "p.token_budget_monthly, p.revoked FROM plan p JOIN app a ON a.id = p.app_id "
            "WHERE p.code = 'tenant-pooled' AND a.key = ANY(%s)",
            (list(N8N_NODES),),
        ).fetchall()
    }


def _snapshot(conn) -> tuple:
    return (
        _netcare(conn),
        _active_grants(conn),
        _one(conn, "SELECT count(*) FROM tenant_app_grant"),
        _bundle_apps(conn),
        _pooled(conn),
        _one(conn, "SELECT count(*) FROM plan"),
        _one(conn, "SELECT count(*) FROM license"),
    )


def _apply(conn, path: Path) -> None:
    _run_sql_file(conn, path, path.name)


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

    def test_header_names_the_apps_and_the_legacy_tenant(self):
        header = UP.read_text(encoding="utf-8").split("BEGIN;", 1)[0]
        for name in (*N8N_NODES, NETCARE, "netcare-legacy", "tenant-pooled"):
            assert name in header, name


class TestRunForReal:
    def test_bundle_and_plans(self, conn):
        ids = _seed_apps(conn)
        _apply(conn, UP)
        assert _bundle_apps(conn) == set(N8N_NODES)
        pooled = _pooled(conn)
        assert set(pooled) == set(N8N_NODES)
        for key, (name, rpm, burst, quota, budget, revoked) in pooled.items():
            assert name == "Tenant pooled (tenant budget)"
            assert (quota, budget, revoked) == (1_000_000, None, False), key
            expected = max(PROD_PLANS[key], key=lambda p: p[1], default=("", 60, 10))
            assert (rpm, burst) == expected[1:], key
        # what bundle ensure's choose_plan reads: one non-revoked tenant-pooled per app
        for key, app_id in ids.items():
            n = _one(
                conn,
                "SELECT count(*) FROM plan WHERE app_id = %s AND code = 'tenant-pooled' "
                "AND NOT revoked",
                (app_id,),
            )
            assert n == 1, key

    def test_an_existing_or_revoked_tenant_pooled_is_not_duplicated(self, conn):
        ids = _seed_apps(conn)
        keep = _plan(conn, ids["metering"], "tenant-pooled", 5, 5)
        _plan(conn, ids["observability"], "tenant-pooled", revoked=True)
        _apply(conn, UP)
        pooled_ids = [
            str(r[0])
            for r in conn.execute(
                "SELECT id FROM plan WHERE app_id = %s AND code = 'tenant-pooled'",
                (ids["metering"],),
            ).fetchall()
        ]
        assert pooled_ids == [keep]
        assert (
            _one(
                conn,
                "SELECT count(*) FROM plan WHERE app_id = %s AND code = 'tenant-pooled'",
                (ids["observability"],),
            )
            == 1
        )
        assert _pooled(conn)["observability"][-1] is True  # left revoked

    def test_a_missing_app_is_skipped(self, conn):
        _seed_apps(conn, N8N_NODES[:4])
        _apply(conn, UP)
        assert _bundle_apps(conn) == set(N8N_NODES[:4])

    def test_legacy_netcare_is_retired_and_the_name_is_free_for_cp(self, conn):
        _seed_netcare(conn)
        licences = _one(conn, "SELECT count(*) FROM license WHERE tenant_id = %s", (NETCARE,))
        _apply(conn, UP)
        assert _netcare(conn) == ("netcare-legacy", "platform", True)
        assert _active_grants(conn) == set()
        assert (
            _one(
                conn,
                "SELECT count(*) FROM tenant_app_grant WHERE tenant_id = %s "
                "AND revoked_at IS NOT NULL",
                (NETCARE,),
            )
            == 4
        )
        assert (
            _one(
                conn,
                "SELECT count(*) FROM license WHERE tenant_id = %s AND status = 'active'",
                (NETCARE,),
            )
            == licences
        )
        conn.execute(
            "INSERT INTO tenants (id, name, owner_key) VALUES (%s, 'netcare', 'commonpractice')",
            (str(uuid.uuid7()),),
        )

    def test_absent_row_is_a_no_op(self, conn):
        _apply(conn, UP)
        assert _netcare(conn) is None

    def test_an_already_renamed_row_is_left_alone(self, conn):
        _seed_netcare(conn)
        conn.execute("UPDATE tenants SET name = 'something-else' WHERE id = %s", (NETCARE,))
        _apply(conn, UP)
        assert _netcare(conn) == ("something-else", "commonpractice", True)
        assert _active_grants(conn) == {"platform", "fr", "merqi", "w2v"}

    def test_a_taken_legacy_name_skips_instead_of_failing(self, conn):
        _seed_netcare(conn)
        conn.execute(
            "INSERT INTO tenants (id, name, owner_key) VALUES (%s, 'netcare-legacy', 'platform')",
            (str(uuid.uuid7()),),
        )
        _apply(conn, UP)
        assert _netcare(conn) == ("netcare", "commonpractice", True)
        assert _active_grants(conn) == {"platform", "fr", "merqi", "w2v"}

    def test_rerun_is_a_no_op(self, conn):
        _seed_apps(conn)
        _seed_netcare(conn)
        _apply(conn, UP)
        once = _snapshot(conn)
        revoked_at = _one(
            conn, "SELECT max(revoked_at) FROM tenant_app_grant WHERE tenant_id = %s", (NETCARE,)
        )
        _apply(conn, UP)
        assert _snapshot(conn) == once
        assert (
            _one(
                conn,
                "SELECT max(revoked_at) FROM tenant_app_grant WHERE tenant_id = %s",
                (NETCARE,),
            )
            == revoked_at
        )


class TestDowngrade:
    def test_restores_the_050_state(self, conn):
        _seed_apps(conn)
        _seed_netcare(conn)
        plans_before = _one(conn, "SELECT count(*) FROM plan")
        _apply(conn, UP)
        _apply(conn, DOWN)
        assert _netcare(conn) == ("netcare", "commonpractice", True)
        assert _active_grants(conn) == {"platform", "fr", "merqi", "w2v"}
        assert _bundle_apps(conn) == set()
        assert _one(conn, "SELECT count(*) FROM bundle WHERE key = 'n8n-nodes'") == 0
        assert _one(conn, "SELECT count(*) FROM plan") == plans_before

    def test_refuses_while_the_bundle_is_held(self, conn):
        _seed_apps(conn)
        _apply(conn, UP)
        tid = str(uuid.uuid7())
        conn.execute(
            "INSERT INTO tenants (id, name, owner_key) VALUES (%s, 'holder', 'commonpractice')",
            (tid,),
        )
        conn.execute(
            "INSERT INTO tenant_bundle (tenant_id, bundle_id) "
            "SELECT %s, id FROM bundle WHERE key = 'n8n-nodes'",
            (tid,),
        )
        with pytest.raises(Exception, match="051 downgrade REFUSED"):
            _apply(conn, DOWN)
        conn.execute("ROLLBACK")
        assert _bundle_apps(conn) == set(N8N_NODES)

    def test_refuses_once_cp_has_a_new_netcare(self, conn):
        _seed_netcare(conn)
        _apply(conn, UP)
        conn.execute(
            "INSERT INTO tenants (id, name, owner_key) VALUES (%s, 'netcare', 'commonpractice')",
            (str(uuid.uuid7()),),
        )
        with pytest.raises(Exception, match="051 downgrade REFUSED"):
            _apply(conn, DOWN)
        conn.execute("ROLLBACK")
        assert _netcare(conn) == ("netcare-legacy", "platform", True)


class TestRunnerCycle:
    """up -> down -> up through the REAL runner (scripts/apply_migrations.py): the
    downgrade deletes 051's stamp, so the runner applies 051 again."""

    def test_up_down_up(self):
        from scripts.apply_migrations import apply_all

        dsn = _recreate_database(_libpq_dsn(), RUNNER_DB)
        try:
            _build_to_050(dsn)
            with psycopg.connect(dsn, autocommit=True) as c:
                c.execute(
                    "CREATE TABLE schema_migrations (filename TEXT PRIMARY KEY, "
                    "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
                )
                for path in sorted(MIGRATIONS.glob("*.sql")):
                    if path.name < UP.name:
                        c.execute(
                            "INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,)
                        )
                _seed_apps(c)
                _seed_netcare(c)

            def state() -> tuple[bool, tuple, set[str]]:
                with psycopg.connect(dsn, autocommit=True) as c:
                    stamped = bool(
                        _one(
                            c,
                            "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE filename = %s)",
                            (UP.name,),
                        )
                    )
                    return stamped, _netcare(c), _bundle_apps(c)

            assert apply_all(dsn) == 0
            assert state() == (True, ("netcare-legacy", "platform", True), set(N8N_NODES))
            with psycopg.connect(dsn, autocommit=True) as c:
                _run_sql_file(c, DOWN, "down")
            assert state() == (False, ("netcare", "commonpractice", True), set())
            assert apply_all(dsn) == 0
            assert state() == (True, ("netcare-legacy", "platform", True), set(N8N_NODES))
        finally:
            _drop_database(_libpq_dsn(), RUNNER_DB)
