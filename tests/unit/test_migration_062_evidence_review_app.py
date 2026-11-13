"""Migration 062: the ``evidence-review`` app and the kag -> evidence-review dependency.

Run for REAL against a database built FROM EMPTY (init.sql -> the genesis shim ->
001..061), with 059's live catalogue slice seeded, then 062:

* the app (audience platform), its global ``standard`` plan (metered, never unlimited)
  and its issuers equal the DR / boot seed (scripts/seed_apps.py,
  models.issuers_for_app_key) — a FRESH database never runs this file;
* at head app_requires equals models.APP_REQUIRES_SEED;
* a missing ``kag`` app skips the edge; operator rows are never overwritten; a re-run
  is a no-op; a cycle aborts the whole file;
* the downgrade restores the 061 state exactly and refuses while an evidence-review
  licence, dependency grant, or operator row in offering / tenant_app_byo_mode /
  bundle_app / bundle_caller references the app; up -> down -> up through the REAL runner re-applies 062.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from models import APP_REQUIRES_SEED, issuers_for_app_key
from scripts.apply_migrations import split_statements
from scripts.build_schema_arms import (
    INIT_SQL,
    SHIM_SQL,
    _drop_database,
    _recreate_database,
    _run_sql_file,
)
from tests.conftest import TEST_DATABASE_URL
from tests.unit.test_migration_059_app_requires import _edges
from tests.unit.test_migration_059_app_requires import _seed_apps as _seed_059_apps
from tests.unit.test_migration_078_care_bus_early_access import (
    M078_APP_REQUIRES,
    M080_APP_REQUIRES,
)
from tests.unit.test_migration_082_mcp_toolkit_entitlements import (
    M082_APP_REQUIRES,
    M086_APP_REQUIRES,
    M089_APP_REQUIRES,
)

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "migrations"
NAME = "062_evidence_review_app.sql"
UP = MIGRATIONS / NAME
DOWN = MIGRATIONS / "downgrades" / NAME
DB = "licenses_m062_test"
RUNNER_DB = "licenses_m062_runner_test"
ER = "evidence-review"

# What 062 adds on top of 059's seeds — 059's tests subtract it.
M062_APP_REQUIRES: set[tuple[str, str, str]] = {("kag", ER, "kag")}


def _seed_spec() -> dict:
    spec = importlib.util.spec_from_file_location("seed_apps", REPO / "scripts" / "seed_apps.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return next(a for a in mod.APPS if a["key"] == ER)


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
    """A database at the exact 061 state, built from empty, with 059's apps."""
    dsn = _recreate_database(_libpq_dsn(), DB)
    try:
        _build_before(dsn)
        with psycopg.connect(dsn, autocommit=True) as c:
            yield c
    finally:
        _drop_database(_libpq_dsn(), DB)


def _app(conn) -> tuple | None:
    row = conn.execute("SELECT name, audience, revoked FROM app WHERE key = %s", (ER,)).fetchone()
    return tuple(row) if row else None


def _plans(conn) -> set[tuple]:
    return {
        tuple(r)
        for r in conn.execute(
            "SELECT p.code, p.region_id, p.duration_days, p.rate_limit_rpm, p.rate_limit_burst, "
            "p.quota_monthly, p.token_budget_monthly FROM plan p JOIN app a ON a.id = p.app_id "
            "WHERE a.key = %s",
            (ER,),
        )
    }


def _issuers(conn) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT i.owner_key FROM app_issuer i JOIN app a ON a.id = i.app_id WHERE a.key = %s",
            (ER,),
        )
    }


def _snapshot(conn) -> tuple:
    return (_app(conn), _plans(conn), _issuers(conn), _edges(conn))


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

    def test_header_records_the_issuer_choice_and_the_scope_note(self):
        header = UP.read_text(encoding="utf-8").split("BEGIN;", 1)[0]
        for needle in ("evidence-review -> {kag, platform}", "NOT fr", "SCOPE NOTE"):
            assert needle in header, needle


class TestRunForReal:
    def test_head_equals_the_seeds(self, conn):
        assert _app(conn) is None and not (_edges(conn) & M062_APP_REQUIRES)  # control
        _run_sql_file(conn, UP, NAME)
        spec = _seed_spec()
        assert _app(conn) == (spec["name"], spec["audience"], False)
        p = spec["plan"]
        assert _plans(conn) == {
            (
                p["code"],
                None,
                p["duration_days"],
                p["rate_limit_rpm"],
                p["rate_limit_burst"],
                p["quota_monthly"],
                p["token_budget_monthly"],
            )
        }
        assert _issuers(conn) == set(issuers_for_app_key(ER)) == set(spec["issuers"])
        assert (
            _edges(conn)
            == set(APP_REQUIRES_SEED)
            - M078_APP_REQUIRES
            - M080_APP_REQUIRES
            - M082_APP_REQUIRES
            - M086_APP_REQUIRES
            - M089_APP_REQUIRES
        )  # 078/080: care-bus

    def test_a_missing_kag_app_skips_the_edge(self, conn):
        conn.execute("DELETE FROM app WHERE key = 'kag'")
        _run_sql_file(conn, UP, NAME)
        assert _app(conn) is not None
        assert not (_edges(conn) & M062_APP_REQUIRES)

    def test_an_operator_row_is_never_overwritten(self, conn):
        conn.execute(
            "INSERT INTO app (key, name, audience) VALUES (%s, 'ER (ops)', 'human')", (ER,)
        )
        conn.execute(
            "INSERT INTO plan (app_id, code, name, rate_limit_rpm) "
            "SELECT id, 'standard', 'Ops', 5 FROM app WHERE key = %s",
            (ER,),
        )
        _run_sql_file(conn, UP, NAME)
        assert _app(conn) == ("ER (ops)", "human", False)
        assert {(c, rpm) for c, _, _, rpm, *_ in _plans(conn)} == {("standard", 5)}

    def test_rerun_is_a_no_op(self, conn):
        _run_sql_file(conn, UP, NAME)
        snap = _snapshot(conn)
        _run_sql_file(conn, UP, NAME)
        assert _snapshot(conn) == snap

    @pytest.mark.timeout(30)
    def test_a_cycle_aborts_the_whole_file(self, conn):
        conn.execute("INSERT INTO app (key, name) VALUES (%s, 'ER (ops)')", (ER,))
        conn.execute(
            "INSERT INTO app_requires SELECT a.id, r.id, 'platform' FROM app a, app r "
            "WHERE a.key = %s AND r.key = 'kag'",
            (ER,),
        )
        with pytest.raises(Exception, match="062: app_requires holds a cycle"):
            _run_sql_file(conn, UP, NAME)
        conn.execute("ROLLBACK")
        assert _plans(conn) == set() and _issuers(conn) == set()


class TestDowngrade:
    def test_restores_the_061_state(self, conn):
        before = _snapshot(conn)
        _run_sql_file(conn, UP, NAME)
        _run_sql_file(conn, DOWN, "down")
        assert _snapshot(conn) == before

    def test_refuses_while_an_evidence_review_grant_exists(self, conn):
        _run_sql_file(conn, UP, NAME)
        app = conn.execute("SELECT id FROM app WHERE key = %s", (ER,)).fetchone()
        tid = conn.execute(
            "INSERT INTO tenants (id, name, owner_key) VALUES (uuidv7(), 'fr-cust', 'fr') "
            "RETURNING id"
        ).fetchone()
        assert app is not None and tid is not None
        conn.execute(
            "INSERT INTO tenant_dependency_grant (tenant_id, owner_key, app_id, granted_by) "
            "VALUES (%s, 'kag', %s, 'test')",
            (tid[0], app[0]),
        )
        snap = _snapshot(conn)
        with pytest.raises(Exception, match="062 downgrade REFUSED"):
            _run_sql_file(conn, DOWN, "down")
        conn.execute("ROLLBACK")
        assert _snapshot(conn) == snap


def _er_tenant(conn) -> tuple:
    app = conn.execute("SELECT id FROM app WHERE key = %s", (ER,)).fetchone()
    tid = conn.execute(
        "INSERT INTO tenants (id, name, owner_key) VALUES (uuidv7(), 'kag-cust', 'kag') "
        "RETURNING id"
    ).fetchone()
    assert app is not None and tid is not None
    return app[0], tid[0]


def _add_licence(conn) -> None:
    app, tid = _er_tenant(conn)
    plan = conn.execute(
        "SELECT id FROM plan WHERE app_id = %s AND code = 'standard'", (app,)
    ).fetchone()
    sub = conn.execute(
        "INSERT INTO subject (keycloak_sub, keycloak_realm) VALUES (%s, 'service') RETURNING id",
        (f"svc:{tid}:{ER}",),
    ).fetchone()
    assert plan is not None and sub is not None
    conn.execute(
        "INSERT INTO license (subject_id, app_id, plan_id, tenant_id, status, starts_at, "
        "is_dependency) VALUES (%s, %s, %s, %s, 'active', now(), true)",
        (sub[0], app, plan[0], tid),
    )


def _add_offering(conn) -> None:
    conn.execute(
        "INSERT INTO offering (app_id, code, name, byo_mode) "
        "SELECT id, 'ops', 'Ops offering', 'platform_only' FROM app WHERE key = %s",
        (ER,),
    )


def _add_byo_mode(conn) -> None:
    app, tid = _er_tenant(conn)
    conn.execute(
        "INSERT INTO tenant_app_byo_mode (tenant_id, app_id, byo_mode) "
        "VALUES (%s, %s, 'platform_only')",
        (tid, app),
    )


def _ops_bundle(conn):
    row = conn.execute(
        "INSERT INTO bundle (key, name) VALUES ('ops-er-bundle', 'Ops') RETURNING id"
    ).fetchone()
    assert row is not None
    return row[0]


def _add_bundle_app(conn) -> None:
    bid = _ops_bundle(conn)
    conn.execute(
        "INSERT INTO bundle_app (bundle_id, app_id) SELECT %s, id FROM app WHERE key = %s",
        (bid, ER),
    )


def _add_bundle_caller(conn) -> None:
    bid = _ops_bundle(conn)
    conn.execute(
        "INSERT INTO bundle_caller (bundle_id, owner_key, app_id) "
        "SELECT %s, 'kag', id FROM app WHERE key = %s",
        (bid, ER),
    )


class TestDowngradeRefusesOperatorState:
    """Deleting the app CASCADEs into every one of these: the downgrade must refuse."""

    @pytest.mark.parametrize(
        ("add", "count_sql"),
        [
            (_add_licence, "SELECT count(*) FROM license l JOIN app a ON a.id = l.app_id"),
            (_add_offering, "SELECT count(*) FROM offering o JOIN app a ON a.id = o.app_id"),
            (
                _add_byo_mode,
                "SELECT count(*) FROM tenant_app_byo_mode b JOIN app a ON a.id = b.app_id",
            ),
            (_add_bundle_app, "SELECT count(*) FROM bundle_app b JOIN app a ON a.id = b.app_id"),
            (
                _add_bundle_caller,
                "SELECT count(*) FROM bundle_caller b JOIN app a ON a.id = b.app_id",
            ),
        ],
        ids=["licence", "offering", "byo_mode", "bundle_app", "bundle_caller"],
    )
    def test_refuses_and_changes_nothing(self, conn, add, count_sql):
        _run_sql_file(conn, UP, NAME)
        add(conn)
        q = count_sql + " WHERE a.key = %s"
        row = conn.execute(q, (ER,)).fetchone()
        assert row is not None and row[0] == 1  # control: the row is really there
        snap = _snapshot(conn)
        with pytest.raises(Exception, match="062 downgrade REFUSED"):
            _run_sql_file(conn, DOWN, "down")
        conn.execute("ROLLBACK")
        assert _snapshot(conn) == snap
        row = conn.execute(q, (ER,)).fetchone()
        assert row is not None and row[0] == 1


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

            def state() -> tuple[bool, bool, bool]:
                with psycopg.connect(dsn, autocommit=True) as c:
                    row = c.execute(
                        "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE filename = %s)",
                        (NAME,),
                    ).fetchone()
                    return (
                        bool(row and row[0]),
                        _app(c) is not None,
                        bool(_edges(c) & M062_APP_REQUIRES),
                    )

            assert apply_all(dsn) == 0
            assert state() == (True, True, True)
            with psycopg.connect(dsn, autocommit=True) as c:
                _run_sql_file(c, DOWN, "down 062")
            assert state() == (False, False, False)
            assert apply_all(dsn) == 0
            assert state() == (True, True, True)
        finally:
            _drop_database(_libpq_dsn(), RUNNER_DB)
