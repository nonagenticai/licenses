"""Migration 067: the strata apps (strata-configurator, care-bus) and the configurator's
licences client.

Run for REAL against a database built FROM EMPTY (init.sql -> the genesis shim ->
001..066), then 067:

* owners ``strata-configurator`` / ``care-bus`` and
  ``secure-apps/strata-configurator-licenses-admin`` (owner strata-configurator, not
  cross-owner) exist, and at head tenant_owner / api_client equal the boot seed
  (models.TENANT_OWNERS / API_CLIENTS) — a FRESH database never runs this file;
* both apps (audience human), their global ``standard`` plans (perpetual, metered,
  never unlimited) and their issuers equal the DR / boot seed (scripts/seed_apps.py,
  models.issuers_for_app_key): each app is issued by its OWN owner only;
* operator rows are never overwritten; a re-run is a no-op;
* the downgrade restores the 066 state exactly and refuses while a tenant is owned by
  a strata owner or a licence / offering references a strata app; up -> down -> up
  through the REAL runner re-applies 067.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from models import API_CLIENTS, TENANT_OWNERS, issuers_for_app_key
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
from tests.unit.test_migration_078_care_bus_early_access import M078_API_CLIENTS
from tests.unit.test_migration_082_mcp_toolkit_entitlements import (
    M082_API_CLIENTS,
    M082_TENANT_OWNERS,
)
from tests.unit.test_migration_083_secure_apps_s2s_api_clients import M083_API_CLIENTS
from tests.unit.test_migration_085_w2v_cell_m4_api_client import M085_API_CLIENTS
from tests.unit.test_migration_090_conveyor_service_issuer import M090_API_CLIENTS

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "migrations"
NAME = "067_strata_configurator_and_care_bus_apps.sql"
UP = MIGRATIONS / NAME
DOWN = MIGRATIONS / "downgrades" / NAME
DB = "licenses_m067_test"
RUNNER_DB = "licenses_m067_runner_test"
APPS = ("strata-configurator", "care-bus")
CLIENT = ("secure-apps", "strata-configurator-licenses-admin", "strata-configurator", False)

# What 067 adds on top of the 050..066 seeds — older migration tests subtract these.
M067_TENANT_OWNERS: set[tuple[str, str]] = {
    ("strata-configurator", "Strata Configurator"),
    ("care-bus", "Care Bus"),
}
M067_API_CLIENTS: set[tuple[str, str, str, bool]] = {CLIENT}


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
    """A database at the exact 066 state, built from empty."""
    dsn = _recreate_database(_libpq_dsn(), DB)
    try:
        _build_before(dsn)
        with psycopg.connect(dsn, autocommit=True) as c:
            yield c
    finally:
        _drop_database(_libpq_dsn(), DB)


def _owners(conn) -> set[tuple[str, str]]:
    return {tuple(r) for r in conn.execute("SELECT key, display_name FROM tenant_owner")}


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


def _issuers(conn) -> set[tuple[str, str]]:
    return {
        tuple(r)
        for r in conn.execute(
            "SELECT a.key, i.owner_key FROM app_issuer i JOIN app a ON a.id = i.app_id "
            "WHERE a.key = ANY(%s)",
            (list(APPS),),
        )
    }


def _snapshot(conn) -> tuple:
    return (_owners(conn), _clients(conn), _apps(conn), _plans(conn), _issuers(conn))


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
    def test_head_equals_the_seeds(self, conn):
        # Control: none of it exists at 066.
        assert not (_owners(conn) & M067_TENANT_OWNERS)
        assert not (_clients(conn) & M067_API_CLIENTS)
        assert _apps(conn) == {}
        _run_sql_file(conn, UP, NAME)
        from tests.unit.test_migration_070_agent_app import M070_TENANT_OWNERS as _M070_OWNERS

        assert _owners(conn) == set(TENANT_OWNERS) - _M070_OWNERS - M082_TENANT_OWNERS  # 070, 082
        from tests.unit.test_migration_068_care_bus_licenses_admin_client import (
            M068_API_CLIENTS,
        )
        from tests.unit.test_migration_070_agent_app import M070_API_CLIENTS
        from tests.unit.test_migration_084_retire_strata_configurator import (
            M084_RETIRED_API_CLIENTS,
            M084_RETIRED_APPS,
        )

        assert (
            _clients(conn)
            == (
                set(API_CLIENTS)
                - M068_API_CLIENTS
                - M070_API_CLIENTS
                - M078_API_CLIENTS
                - M082_API_CLIENTS
                - M083_API_CLIENTS
                - M085_API_CLIENTS
                - M090_API_CLIENTS
            )
            | M084_RETIRED_API_CLIENTS
        )  # 068: care-bus's client; 070: agent's; 078/082/083 later; 084 retired
        specs = _seed_specs()
        # 084 retired strata-configurator: the seed no longer declares it, so only
        # care-bus is pinned against the seed; strata-configurator against 067's text.
        assert set(specs) == set(APPS) - M084_RETIRED_APPS
        assert _apps(conn) == {
            **{k: (s["name"], s["audience"], False) for k, s in specs.items()},
            "strata-configurator": ("Strata Configurator", "human", False),
        }
        assert {p for p in _plans(conn) if p[0] in specs} == {
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
        assert _issuers(conn) == {(k, k) for k in APPS}
        assert set(issuers_for_app_key("strata-configurator")) == {"strata-configurator"}
        for k, s in specs.items():
            assert set(issuers_for_app_key(k)) == set(s["issuers"]) == {k}

    def test_an_operator_row_is_never_overwritten(self, conn):
        conn.execute(
            "INSERT INTO app (key, name, audience) VALUES ('care-bus', 'CB (ops)', 'platform')"
        )
        conn.execute(
            "INSERT INTO plan (app_id, code, name, rate_limit_rpm) "
            "SELECT id, 'standard', 'Ops', 5 FROM app WHERE key = 'care-bus'"
        )
        _run_sql_file(conn, UP, NAME)
        assert _apps(conn)["care-bus"] == ("CB (ops)", "platform", False)
        assert {(k, rpm) for k, c, _, _, rpm, *_ in _plans(conn) if k == "care-bus"} == {
            ("care-bus", 5)
        }

    def test_rerun_is_a_no_op(self, conn):
        _run_sql_file(conn, UP, NAME)
        snap = _snapshot(conn)
        _run_sql_file(conn, UP, NAME)
        assert _snapshot(conn) == snap


def _tenant(conn, owner: str) -> object:
    row = conn.execute(
        "INSERT INTO tenants (id, name, owner_key) VALUES (uuidv7(), 'strata-cust', %s) "
        "RETURNING id",
        (owner,),
    ).fetchone()
    assert row is not None
    return row[0]


def _add_tenant(conn) -> None:
    _tenant(conn, "strata-configurator")


def _add_licence(conn) -> None:
    tid = _tenant(conn, "fr")  # a licence alone must block, whoever owns the tenant
    app = conn.execute("SELECT id FROM app WHERE key = 'care-bus'").fetchone()
    plan = conn.execute(
        "SELECT p.id FROM plan p JOIN app a ON a.id = p.app_id "
        "WHERE a.key = 'care-bus' AND p.code = 'standard'"
    ).fetchone()
    sub = conn.execute(
        "INSERT INTO subject (keycloak_sub, keycloak_realm) VALUES (%s, 'portal') RETURNING id",
        ("2b8f0d7e-1111-4c4c-8888-000000000067",),
    ).fetchone()
    assert app is not None and plan is not None and sub is not None
    conn.execute(
        "INSERT INTO license (subject_id, app_id, plan_id, tenant_id, status, starts_at) "
        "VALUES (%s, %s, %s, %s, 'active', now())",
        (sub[0], app[0], plan[0], tid),
    )


def _add_offering(conn) -> None:
    conn.execute(
        "INSERT INTO offering (app_id, code, name, byo_mode) "
        "SELECT id, 'standard', 'Std', 'byo_required' FROM app WHERE key = 'strata-configurator'"
    )


class TestDowngrade:
    def test_restores_the_066_state(self, conn):
        before = _snapshot(conn)
        _run_sql_file(conn, UP, NAME)
        _run_sql_file(conn, DOWN, "down")
        assert _snapshot(conn) == before

    @pytest.mark.parametrize(
        "add", [_add_tenant, _add_licence, _add_offering], ids=["tenant", "licence", "offering"]
    )
    def test_refuses_and_changes_nothing(self, conn, add):
        _run_sql_file(conn, UP, NAME)
        add(conn)
        snap = _snapshot(conn)
        with pytest.raises(Exception, match="067 downgrade REFUSED"):
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

            def state() -> tuple[bool, bool, bool]:
                with psycopg.connect(dsn, autocommit=True) as c:
                    row = c.execute(
                        "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE filename = %s)",
                        (NAME,),
                    ).fetchone()
                    return (
                        bool(row and row[0]),
                        set(_apps(c)) == set(APPS),
                        CLIENT in _clients(c),
                    )

            assert apply_all(dsn) == 0
            # apply_all ran 084 too: it deletes the configurator's client (the app
            # stays, revoked).
            assert state() == (True, True, False)
            with psycopg.connect(dsn, autocommit=True) as c:
                # apply_all ran 068 + 069 + 078 too: 078's care-bus bundle, 068's care-bus client and 069's
                # fr-test-user grants are rows 067 did not write, so 067's downgrade
                # refuses until both are downgraded first (newest first).
                with pytest.raises(Exception, match="067 downgrade REFUSED"):
                    _run_sql_file(c, DOWN, "down 067")
                c.execute("ROLLBACK")
                _run_sql_file(c, DOWN.parent / "084_retire_strata_configurator.sql", "down 084")
                _run_sql_file(c, DOWN.parent / "082_mcp_toolkit_entitlements.sql", "down 082")
                _run_sql_file(c, DOWN.parent / "080_care_bus_laya.sql", "down 080")
                _run_sql_file(c, DOWN.parent / "079_fr_test_user_care_bus_bundle.sql", "down 079")
                _run_sql_file(c, DOWN.parent / "078_care_bus_early_access.sql", "down 078")
                _run_sql_file(c, DOWN.parent / "069_fr_test_user_strata_grants.sql", "down 069")
                _run_sql_file(c, DOWN.parent / "068_care_bus_licenses_admin_client.sql", "down 068")
                _run_sql_file(c, DOWN, "down 067")
            assert state() == (False, False, False)
            assert apply_all(dsn) == 0
            assert state() == (True, True, False)
        finally:
            _drop_database(_libpq_dsn(), RUNNER_DB)
