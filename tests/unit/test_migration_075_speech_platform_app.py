"""Migration 075: the `speech` app (TTS / ASR as shared platform services).

Run for REAL against a database built FROM EMPTY (init.sql -> the genesis shim ->
001..074), then 075:

* the app (audience platform), its global ``standard`` plan (perpetual, metered, never
  unlimited) and its issuer equal the DR seed (scripts/seed_apps.py,
  models.issuers_for_app_key): issued by ``platform`` only — a FRESH database never
  runs this file;
* no owner, api_client, bundle, requires edge or grant is added;
* operator rows are never overwritten; a re-run is a no-op;
* the downgrade restores the 074 state exactly and refuses while a licence / offering
  references the app; up -> down -> up through the REAL runner re-applies 075.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from models import issuers_for_app_key
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
NAME = "075_speech_platform_app.sql"
UP = MIGRATIONS / NAME
DOWN = MIGRATIONS / "downgrades" / NAME
DB = "licenses_m075_test"
RUNNER_DB = "licenses_m075_runner_test"
APPS = ("speech",)


def _seed_specs() -> dict[str, dict]:
    spec = importlib.util.spec_from_file_location("seed_apps", REPO / "scripts" / "seed_apps.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return {a["key"]: a for a in mod.APPS if a["key"] in APPS}


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
    """A database at the exact 074 state, built from empty."""
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


def _apps(conn) -> dict[str, tuple]:
    return {
        r[0]: tuple(r[1:])
        for r in conn.execute(
            "SELECT key, name, audience, revoked FROM app WHERE key = ANY(%s)", (list(APPS),)
        )
    }


def _plans(conn) -> set[tuple]:
    return {
        tuple(r)
        for r in conn.execute(
            "SELECT a.key, p.code, p.region_id, p.duration_days, p.rate_limit_rpm, "
            "p.rate_limit_burst, p.quota_monthly, p.token_budget_monthly "
            "FROM plan p JOIN app a ON a.id = p.app_id WHERE a.key = ANY(%s)",
            (list(APPS),),
        )
    }


def _issuers(conn) -> set[tuple[str, str]]:
    return {
        tuple(r)
        for r in conn.execute(
            "SELECT a.key, i.owner_key FROM app_issuer i JOIN app a ON a.id = i.app_id "
            "WHERE a.key = ANY(%s)",
            (list(APPS),),
        )
    }


_OTHER_TABLES = (
    "tenant_owner",
    "api_client",
    "bundle_app",
    "app_requires",
    "tenant_app_grant",
    "tenant_dependency_grant",
)


def _snapshot(conn) -> tuple:
    return (
        _apps(conn),
        _plans(conn),
        _issuers(conn),
        _count(conn, "app"),
        _count(conn, "plan"),
        _count(conn, "app_issuer"),
        tuple(_count(conn, t) for t in _OTHER_TABLES),
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
        # Control: none of it exists at 074.
        assert _apps(conn) == {}
        others = tuple(_count(conn, t) for t in _OTHER_TABLES)
        _run_sql_file(conn, UP, NAME)
        specs = _seed_specs()
        assert set(specs) == set(APPS)
        assert _apps(conn) == {"speech": ("Speech (TTS / ASR)", "platform", False)}
        assert _apps(conn) == {k: (s["name"], s["audience"], False) for k, s in specs.items()}
        assert _plans(conn) == {
            (
                k,
                s["plan"]["code"],
                None,
                s["plan"]["duration_days"],
                s["plan"]["rate_limit_rpm"],
                s["plan"]["rate_limit_burst"],
                s["plan"]["quota_monthly"],
                s["plan"]["token_budget_monthly"],
            )
            for k, s in specs.items()
        }
        assert _plans(conn) == {("speech", "standard", None, None, 600, 60, 1000000, 50000000)}
        assert _issuers(conn) == {("speech", "platform")}
        assert set(issuers_for_app_key("speech")) == set(specs["speech"]["issuers"]) == {"platform"}
        # Nothing but app / plan / app_issuer moves.
        assert tuple(_count(conn, t) for t in _OTHER_TABLES) == others

    def test_an_operator_row_is_never_overwritten(self, conn):
        conn.execute(
            "INSERT INTO app (key, name, audience) VALUES ('speech', 'Speech (ops)', 'human')"
        )
        conn.execute(
            "INSERT INTO plan (app_id, code, name, rate_limit_rpm) "
            "SELECT id, 'standard', 'Ops', 5 FROM app WHERE key = 'speech'"
        )
        _run_sql_file(conn, UP, NAME)
        assert _apps(conn)["speech"] == ("Speech (ops)", "human", False)
        assert {(k, rpm) for k, _c, _r, _d, rpm, *_ in _plans(conn)} == {("speech", 5)}
        assert _issuers(conn) == {("speech", "platform")}

    def test_rerun_is_a_no_op(self, conn):
        _run_sql_file(conn, UP, NAME)
        snap = _snapshot(conn)
        _run_sql_file(conn, UP, NAME)
        assert _snapshot(conn) == snap


def _add_licence(conn) -> None:
    tid = conn.execute(
        "INSERT INTO tenants (id, name, owner_key) VALUES (uuidv7(), 'speech-cust', 'platform') "
        "RETURNING id"
    ).fetchone()
    app = conn.execute("SELECT id FROM app WHERE key = 'speech'").fetchone()
    plan = conn.execute(
        "SELECT p.id FROM plan p JOIN app a ON a.id = p.app_id "
        "WHERE a.key = 'speech' AND p.code = 'standard'"
    ).fetchone()
    sub = conn.execute(
        "INSERT INTO subject (keycloak_sub, keycloak_realm) VALUES (%s, 'portal') RETURNING id",
        ("2b8f0d7e-1111-4c4c-8888-000000000075",),
    ).fetchone()
    assert tid is not None and app is not None and plan is not None and sub is not None
    conn.execute(
        "INSERT INTO license (subject_id, app_id, plan_id, tenant_id, status, starts_at) "
        "VALUES (%s, %s, %s, %s, 'active', now())",
        (sub[0], app[0], plan[0], tid[0]),
    )


def _add_offering(conn) -> None:
    conn.execute(
        "INSERT INTO offering (app_id, code, name, byo_mode) "
        "SELECT id, 'standard', 'Std', 'byo_required' FROM app WHERE key = 'speech'"
    )


def _add_other_issuer(conn) -> None:
    conn.execute(
        "INSERT INTO app_issuer (app_id, owner_key) SELECT id, 'fr' FROM app WHERE key = 'speech'"
    )


class TestDowngrade:
    def test_restores_the_074_state(self, conn):
        before = _snapshot(conn)
        _run_sql_file(conn, UP, NAME)
        _run_sql_file(conn, DOWN, "down")
        assert _snapshot(conn) == before

    @pytest.mark.parametrize(
        "add",
        [_add_licence, _add_offering, _add_other_issuer],
        ids=["licence", "offering", "other-issuer"],
    )
    def test_refuses_and_changes_nothing(self, conn, add):
        _run_sql_file(conn, UP, NAME)
        add(conn)
        snap = _snapshot(conn)
        with pytest.raises(Exception, match="075 downgrade REFUSED"):
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

            def state() -> tuple[bool, bool]:
                with psycopg.connect(dsn, autocommit=True) as c:
                    row = c.execute(
                        "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE filename = %s)",
                        (NAME,),
                    ).fetchone()
                    return (bool(row and row[0]), set(_apps(c)) == set(APPS))

            assert apply_all(dsn) == 0
            assert state() == (True, True)
            with psycopg.connect(dsn, autocommit=True) as c:
                _run_sql_file(c, DOWN, "down 075")
            assert state() == (False, False)
            assert apply_all(dsn) == 0
            assert state() == (True, True)
        finally:
            _drop_database(_libpq_dsn(), RUNNER_DB)
