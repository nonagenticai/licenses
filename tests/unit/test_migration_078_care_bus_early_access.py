"""Migration 078: care-bus early access — realm_owner, minter client, strata apps,
care-bus -> {mcp, strata-rag, strata-ingest} edges, the ``care-bus`` bundle.

Run for REAL against a database built FROM EMPTY (init.sql -> the genesis shim ->
001..077), with ``mcp`` seeded as scripts/seed_apps.py does on a live database (no
migration creates it), then 078:

* at head realm_owner / api_client / bundle_ensurer equal the boot seed
  (models.REALM_OWNERS / API_CLIENTS / BUNDLE_ENSURER_SEED), app_requires equals
  APP_REQUIRES_SEED's 078 slice, and the strata apps + plans + issuers equal the DR
  seed (scripts/seed_apps.py) — a FRESH database never runs this file;
* the ``care-bus`` bundle holds EXACTLY {care-bus}; care-bus is its only ensurer, and
  only while it issues care-bus (052 rule 1); care-bus issues NO tool app;
* a missing ``mcp`` is skipped; operator rows are never overwritten; a re-run is a
  no-op; an operator edge closing a cycle aborts the whole file;
* the downgrade restores the 077 state exactly and refuses while a tenant holds the
  bundle or a licence references a strata app; up -> down -> up through the REAL
  runner re-applies 078.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from models import (
    API_CLIENTS,
    APP_REQUIRES_SEED,
    BUNDLE_ENSURER_SEED,
    REALM_OWNERS,
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
from tests.conftest import TEST_DATABASE_URL
from tests.unit.test_migration_083_secure_apps_s2s_api_clients import M083_API_CLIENTS
from tests.unit.test_migration_085_w2v_cell_m4_api_client import M085_API_CLIENTS

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "migrations"
NAME = "078_care_bus_early_access.sql"
UP = MIGRATIONS / NAME
DOWN = MIGRATIONS / "downgrades" / NAME
DB = "licenses_m078_test"
RUNNER_DB = "licenses_m078_runner_test"
APPS = ("strata-rag", "strata-ingest")

# What 078 adds on top of the earlier seeds — older migration tests subtract these.
M078_REALM_OWNERS: set[tuple[str, str]] = {("care-bus", "care-bus")}
M078_API_CLIENTS: set[tuple[str, str, str, bool]] = {
    ("secure-apps", "care-bus-bundle-minter", "care-bus", False)
}
M078_BUNDLE_ENSURERS: dict[str, set[str]] = {"care-bus": {"care-bus"}}
M078_APP_REQUIRES: set[tuple[str, str, str]] = {
    ("care-bus", "mcp", "care-bus"),
    ("care-bus", "strata-rag", "care-bus"),
    ("care-bus", "strata-ingest", "care-bus"),
}
# Migration 080's edge (test_migration_080_care_bus_laya.M080_APP_REQUIRES; kept here
# too because that module imports this one).
M080_APP_REQUIRES: set[tuple[str, str, str]] = {("care-bus", "laya", "care-bus")}


def _seed_specs() -> dict[str, dict]:
    spec = importlib.util.spec_from_file_location("seed_apps", REPO / "scripts" / "seed_apps.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return {a["key"]: a for a in mod.APPS if a["key"] in APPS}


def _libpq_dsn() -> str:
    return TEST_DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1)


def _seed_mcp(c: psycopg.Connection) -> None:
    """``mcp`` as seed_apps.py creates it (edge-gate block, issuers {platform, fr})."""
    row = c.execute(
        "INSERT INTO app (key, name, audience) VALUES ('mcp', 'MCP', 'platform') "
        "ON CONFLICT (key) DO UPDATE SET name = app.name RETURNING id"
    ).fetchone()
    assert row is not None
    for owner in issuers_for_app_key("mcp"):
        c.execute(
            "INSERT INTO app_issuer (app_id, owner_key) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (row[0], owner),
        )


def _build_before(dsn: str) -> None:
    # Lazy: older migration tests import this module's M078_* sets at top level.
    from tests.unit.test_migration_059_app_requires import _seed_apps as _seed_059_apps

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
    """A database at the exact 077 state, built from empty, with ``mcp``."""
    dsn = _recreate_database(_libpq_dsn(), DB)
    try:
        _build_before(dsn)
        with psycopg.connect(dsn, autocommit=True) as c:
            _seed_mcp(c)
            yield c
    finally:
        _drop_database(_libpq_dsn(), DB)


def _count(conn, table: str) -> int:
    row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()  # noqa: S608 — fixed names
    assert row is not None
    return int(row[0])


def _realm_owners(conn) -> set[tuple]:
    return {tuple(r) for r in conn.execute("SELECT realm, owner_key FROM realm_owner")}


def _clients(conn) -> set[tuple]:
    return {
        tuple(r)
        for r in conn.execute("SELECT realm, client_id, owner_key, cross_owner FROM api_client")
    }


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


def _issuers(conn, keys: tuple[str, ...] = (*APPS, "care-bus", "mcp")) -> set[tuple[str, str]]:
    return {
        tuple(r)
        for r in conn.execute(
            "SELECT a.key, i.owner_key FROM app_issuer i JOIN app a ON a.id = i.app_id "
            "WHERE a.key = ANY(%s)",
            (list(keys),),
        )
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


def _snapshot(conn) -> tuple:
    return (
        _realm_owners(conn),
        _clients(conn),
        _apps(conn),
        _plans(conn),
        _issuers(conn),
        _ensurers(conn),
        _all_members(conn),
        _edges(conn),
        _count(conn, "app"),
        _count(conn, "plan"),
        _count(conn, "app_issuer"),
        _count(conn, "tenant_app_grant"),
        _count(conn, "tenant_dependency_grant"),
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

    def test_seeds_carry_the_078_rows(self):
        assert M078_REALM_OWNERS <= set(REALM_OWNERS)
        assert M078_API_CLIENTS <= set(API_CLIENTS)
        assert M078_APP_REQUIRES <= set(APP_REQUIRES_SEED)
        assert BUNDLE_ENSURER_SEED["care-bus"] == ("care-bus",)
        # care-bus issues care-bus only — never a tool app (dependency licences only).
        assert issuers_for_app_key("care-bus") == ("care-bus",)
        for key in (*APPS, "mcp"):
            assert "care-bus" not in issuers_for_app_key(key), key


class TestRunForReal:
    def test_head_equals_the_boot_seed(self, conn):
        from tests.unit.test_migration_082_mcp_toolkit_entitlements import (
            M082_API_CLIENTS,
            M082_BUNDLE_ENSURERS,
        )

        # Control: none of it exists at 077.
        assert not (_realm_owners(conn) & M078_REALM_OWNERS)
        assert not (_clients(conn) & M078_API_CLIENTS)
        assert _apps(conn) == {} and "care-bus" not in _ensurers(conn)
        _run_sql_file(conn, UP, NAME)
        assert _realm_owners(conn) == set(REALM_OWNERS)
        from tests.unit.test_migration_084_retire_strata_configurator import (
            M084_RETIRED_API_CLIENTS,
        )
        from tests.unit.test_migration_090_conveyor_service_issuer import M090_API_CLIENTS

        # 084 retired the configurator's client from the seed after 078.
        assert (
            _clients(conn)
            == (
                set(API_CLIENTS)
                - M082_API_CLIENTS
                - M083_API_CLIENTS
                - M085_API_CLIENTS
                - M090_API_CLIENTS
            )
            | M084_RETIRED_API_CLIENTS
        )
        assert _ensurers(conn) == {
            k: set(v) for k, v in BUNDLE_ENSURER_SEED.items() if k not in M082_BUNDLE_ENSURERS
        }
        assert {e for e in _edges(conn) if e[0] == "care-bus"} == M078_APP_REQUIRES
        assert {
            e for e in APP_REQUIRES_SEED if e[0] == "care-bus"
        } - M080_APP_REQUIRES == M078_APP_REQUIRES

    def test_strata_apps_equal_the_dr_seed(self, conn):
        _run_sql_file(conn, UP, NAME)
        specs = _seed_specs()
        assert set(specs) == set(APPS)
        assert _apps(conn) == {
            "strata-rag": ("Strata RAG", "platform", False),
            "strata-ingest": ("Strata Ingest", "platform", False),
        }
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
        assert _plans(conn) == {
            (k, "standard", None, None, 600, 60, 1000000, 50000000) for k in APPS
        }
        assert _issuers(conn, APPS) == {(k, "platform") for k in APPS}
        for k, s in specs.items():
            assert set(issuers_for_app_key(k)) == set(s["issuers"]) == {"platform"}

    def test_bundle_holds_exactly_care_bus_and_nothing_else_moves(self, conn):
        before = _all_members(conn)
        issuers_before = _issuers(conn, ("care-bus", "mcp"))
        _run_sql_file(conn, UP, NAME)
        after = _all_members(conn)
        assert after.pop("care-bus") == {"care-bus"}
        assert after == before
        assert _issuers(conn, ("care-bus", "mcp")) == issuers_before  # no issuer widened

    def test_a_missing_mcp_is_skipped(self, conn):
        conn.execute("DELETE FROM app WHERE key = 'mcp'")
        _run_sql_file(conn, UP, NAME)
        assert {e for e in _edges(conn) if e[0] == "care-bus"} == M078_APP_REQUIRES - {
            ("care-bus", "mcp", "care-bus")
        }

    def test_no_ensurer_when_care_bus_does_not_issue_care_bus(self, conn):
        conn.execute(
            "DELETE FROM app_issuer WHERE owner_key = 'care-bus' "
            "AND app_id = (SELECT id FROM app WHERE key = 'care-bus')"
        )
        _run_sql_file(conn, UP, NAME)
        assert _ensurers(conn)["care-bus"] == set()

    def test_an_operator_row_is_never_overwritten(self, conn):
        conn.execute(
            "INSERT INTO app (key, name, audience) VALUES ('strata-rag', 'RAG (ops)', 'human')"
        )
        conn.execute(
            "INSERT INTO plan (app_id, code, name, rate_limit_rpm) "
            "SELECT id, 'standard', 'Ops', 5 FROM app WHERE key = 'strata-rag'"
        )
        conn.execute("INSERT INTO bundle (key, name) VALUES ('care-bus', 'Operator')")
        conn.execute(
            "INSERT INTO api_client (realm, client_id, owner_key, cross_owner) "
            "VALUES ('secure-apps', 'care-bus-bundle-minter', 'platform', false)"
        )
        _run_sql_file(conn, UP, NAME)
        assert _apps(conn)["strata-rag"] == ("RAG (ops)", "human", False)
        assert ("strata-rag", 5) in {(k, rpm) for k, _c, _r, _d, rpm, *_ in _plans(conn)}
        assert conn.execute("SELECT name FROM bundle WHERE key = 'care-bus'").fetchone() == (
            "Operator",
        )
        assert ("secure-apps", "care-bus-bundle-minter", "platform", False) in _clients(conn)

    def test_rerun_is_a_no_op(self, conn):
        _run_sql_file(conn, UP, NAME)
        snap = _snapshot(conn)
        _run_sql_file(conn, UP, NAME)
        assert _snapshot(conn) == snap

    def test_a_cycle_aborts_the_whole_file(self, conn):
        # An operator edge mcp -> care-bus closes a cycle once 078 adds care-bus -> mcp.
        conn.execute(
            "INSERT INTO app_requires (app_id, required_app_id, caller_owner_key) "
            "SELECT m.id, c.id, 'platform' FROM app m, app c "
            "WHERE m.key = 'mcp' AND c.key = 'care-bus'"
        )
        snap = _snapshot(conn)
        with pytest.raises(Exception, match="078: app_requires holds a cycle"):
            _run_sql_file(conn, UP, NAME)
        conn.execute("ROLLBACK")
        assert _snapshot(conn) == snap


def _tenant(conn) -> object:
    row = conn.execute(
        "INSERT INTO tenants (id, name, owner_key) VALUES (uuidv7(), 'cb-cust', 'care-bus') "
        "RETURNING id"
    ).fetchone()
    assert row is not None
    return row[0]


def _hold_bundle(conn) -> None:
    conn.execute(
        "INSERT INTO tenant_bundle (tenant_id, bundle_id) "
        "SELECT %s, id FROM bundle WHERE key = 'care-bus'",
        (_tenant(conn),),
    )


def _strata_licence(conn) -> None:
    tid = _tenant(conn)
    sub = conn.execute(
        "INSERT INTO subject (keycloak_sub, keycloak_realm) VALUES (%s, 'service') RETURNING id",
        (f"svc:{tid}:strata-rag",),
    ).fetchone()
    assert sub is not None
    conn.execute(
        "INSERT INTO license (subject_id, app_id, plan_id, tenant_id, status, starts_at) "
        "SELECT %s, a.id, p.id, %s, 'active', now() FROM app a JOIN plan p ON p.app_id = a.id "
        "WHERE a.key = 'strata-rag' AND p.code = 'standard'",
        (sub[0], tid),
    )


def _other_issuer(conn) -> None:
    conn.execute(
        "INSERT INTO app_issuer (app_id, owner_key) "
        "SELECT id, 'care-bus' FROM app WHERE key = 'strata-ingest'"
    )


class TestDowngrade:
    def test_restores_the_077_state(self, conn):
        before = _snapshot(conn)
        _run_sql_file(conn, UP, NAME)
        _run_sql_file(conn, DOWN, "down")
        assert _snapshot(conn) == before

    @pytest.mark.parametrize(
        "add",
        [_hold_bundle, _strata_licence, _other_issuer],
        ids=["bundle-holder", "strata-licence", "other-issuer"],
    )
    def test_refuses_and_changes_nothing(self, conn, add):
        _run_sql_file(conn, UP, NAME)
        add(conn)
        snap = _snapshot(conn)
        with pytest.raises(Exception, match="078 downgrade REFUSED"):
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
                _seed_mcp(c)
                c.execute(
                    "CREATE TABLE schema_migrations (filename TEXT PRIMARY KEY, "
                    "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
                )
                for path in sorted(MIGRATIONS.glob("*.sql")):
                    if path.name < NAME:
                        c.execute(
                            "INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,)
                        )

            def state() -> tuple[bool, bool, set[tuple]]:
                with psycopg.connect(dsn, autocommit=True) as c:
                    row = c.execute(
                        "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE filename = %s)",
                        (NAME,),
                    ).fetchone()
                    return (
                        bool(row and row[0]),
                        set(_apps(c)) == set(APPS),
                        {e for e in _edges(c) if e[0] == "care-bus"},
                    )

            assert apply_all(dsn) == 0
            assert state() == (True, True, M078_APP_REQUIRES | M080_APP_REQUIRES)  # 080 ran too
            with psycopg.connect(dsn, autocommit=True) as c:
                # apply_all ran 079 and 080 too: their care-bus licences make 078's
                # downgrade refuse, so 080 then 079 go first.
                _run_sql_file(c, DOWN.parent / "082_mcp_toolkit_entitlements.sql", "down 082")
                _run_sql_file(c, DOWN.parent / "080_care_bus_laya.sql", "down 080")
                _run_sql_file(c, DOWN.parent / "079_fr_test_user_care_bus_bundle.sql", "down 079")
                _run_sql_file(c, DOWN, "down 078")
            assert state() == (False, False, set())
            assert apply_all(dsn) == 0
            assert state() == (True, True, M078_APP_REQUIRES | M080_APP_REQUIRES)  # 080 ran too
        finally:
            _drop_database(_libpq_dsn(), RUNNER_DB)
