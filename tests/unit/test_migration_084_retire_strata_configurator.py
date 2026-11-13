"""Migration 084: retire the strata-configurator owner / app (agent merge, licences PR B).

Run for REAL against databases built FROM EMPTY (init.sql -> shim -> 001..080, as
test_migration_080 builds them) plus the live shape of the configurator: three tenants
it owns, its licences already revoked by the operator, one redundant (tenant, agent)
grant active and one revoked. Then:

* the tenants move to ``agent``; the (tenant, agent) grants are gone; the app is
  revoked; the client is deleted; 069's grant is soft-revoked; the owner row stays
  while anything references it and goes when nothing does; care-bus rows are untouched;
* the seeds match head: the client is not in ``models.API_CLIENTS`` and the app is not
  declared by ``scripts/seed_apps.py`` / ``seed_offerings.py``;
* it REFUSES (changes nothing) while a configurator licence is active / pending or a
  moved tenant would clash with a live agent tenant; a re-run is a no-op;
* the downgrade restores the reversible part (owner, client, app, 069's grant);
* THROUGH THE API afterwards: agent-licenses-admin re-registers a moved tenant (200,
  owner agent) and strata-configurator-licenses-admin is ``unknown_api_client``.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest

from models import API_CLIENTS, TENANT_OWNERS
from scripts.apply_migrations import split_statements
from scripts.build_schema_arms import _drop_database, _recreate_database, _run_sql_file
from tests.unit.test_migration_079_fr_test_user_care_bus_bundle import (
    _client,
    _db_manager,
    _dsn_of,
    _libpq_dsn,
)
from tests.unit.test_migration_080_care_bus_laya import NAME as NAME_080
from tests.unit.test_migration_080_care_bus_laya import _build_079

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "migrations"
NAME = "084_retire_strata_configurator.sql"
UP = MIGRATIONS / NAME
DOWN = MIGRATIONS / "downgrades" / NAME
DB = "licenses_m084_test"
SC = "strata-configurator"
CLIENT = ("secure-apps", "strata-configurator-licenses-admin", SC, False)
FR_TEST_USER = "0195f48b-d000-7000-8000-000000000099"
# The live shape: three configurator-owned tenants (ids as on prod where known).
T1 = "0195f48b-d000-7000-8000-0000000c0f01"
T2 = "69b0681a-566c-4d4a-862a-80ee1b23aa52"
T3 = "0195f48b-d000-7000-8000-0000000c0f03"
MOVED = (T1, T2, T3)

# What 084 removes from the head seeds — older migration tests add these back.
M084_RETIRED_API_CLIENTS: set[tuple[str, str, str, bool]] = {CLIENT}
M084_RETIRED_APPS: frozenset[str] = frozenset({SC})


def _build_080(dsn: str) -> None:
    _build_079(dsn)
    with psycopg.connect(dsn, autocommit=True) as c:
        _run_sql_file(c, MIGRATIONS / NAME_080, "080")
        for i, t in enumerate(MOVED, 1):
            c.execute(
                "INSERT INTO tenants (id, name, owner_key, external_ref, created_at) "
                "VALUES (%s, %s, %s, %s, now())",
                (t, f"configurator-{i}", SC, f"sc-{i}"),
            )
        # Redundant once agent owns T1 / T2: one active, one revoked.
        c.execute(
            "INSERT INTO tenant_app_grant (tenant_id, grantee_owner_key, granted_by) "
            "VALUES (%s, 'agent', 'test')",
            (T1,),
        )
        c.execute(
            "INSERT INTO tenant_app_grant (tenant_id, grantee_owner_key, granted_by, "
            "revoked_at) VALUES (%s, 'agent', 'test', now())",
            (T2,),
        )
        # The operator's step 9: a configurator licence, already revoked.
        c.execute("INSERT INTO subject (keycloak_sub, keycloak_realm) VALUES ('sc-u1', 'portal')")
        _licence(c, T1, "revoked")


def _licence(c, tenant: str, status: str) -> None:
    c.execute(
        "INSERT INTO license (subject_id, app_id, plan_id, tenant_id, status, starts_at) "
        "SELECT s.id, a.id, p.id, %s, %s, now() FROM subject s, app a "
        "JOIN plan p ON p.app_id = a.id AND p.code = 'standard' "
        "WHERE s.keycloak_sub = 'sc-u1' AND a.key = %s",
        (tenant, status, SC),
    )


@contextmanager
def _database() -> Iterator[str]:
    dsn = _recreate_database(_libpq_dsn(), DB)
    try:
        _build_080(dsn)
        yield dsn
    finally:
        _drop_database(_libpq_dsn(), DB)


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    with _database() as dsn, psycopg.connect(dsn, autocommit=True) as c:
        yield c


def _snapshot(c) -> dict[str, set[tuple]]:
    q = {
        "tenant": "SELECT id::text, owner_key FROM tenants",
        "grant": "SELECT tenant_id::text, grantee_owner_key, granted_by, revoked_at IS NULL "
        "FROM tenant_app_grant",
        "owner": "SELECT key FROM tenant_owner",
        "client": "SELECT realm, client_id, owner_key, cross_owner FROM api_client",
        "app": "SELECT key, revoked FROM app",
        "plan": "SELECT a.key, p.code, p.revoked FROM plan p JOIN app a ON a.id = p.app_id",
        "issuer": "SELECT a.key, i.owner_key FROM app_issuer i JOIN app a ON a.id = i.app_id",
        "licence": "SELECT l.tenant_id::text, a.key, l.status FROM license l "
        "JOIN app a ON a.id = l.app_id",
        "realm": "SELECT realm, owner_key FROM realm_owner",
    }
    return {k: {tuple(r) for r in c.execute(sql)} for k, sql in q.items()}


def _care_bus(snap: dict[str, set[tuple]]) -> dict[str, set[tuple]]:
    return {k: {r for r in rows if "care-bus" in r} for k, rows in snap.items()}


def _seed_module(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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

    def test_never_writes_a_licence(self):
        body = "\n".join(
            ln for ln in UP.read_text(encoding="utf-8").splitlines() if not ln.startswith("--")
        ).lower()
        for verb in ("update license", "delete from license", "insert into license"):
            assert verb not in body


class TestSeeds:
    def test_head_seeds_carry_no_retired_rows(self):
        assert CLIENT not in API_CLIENTS
        assert not any(c[1] == CLIENT[1] for c in API_CLIENTS)
        assert SC not in {a["key"] for a in _seed_module("seed_apps").APPS}
        assert SC not in {o["app_key"] for o in _seed_module("seed_offerings").OFFERINGS}
        # The owner row stays live (step 6 keeps it while referenced) and in the seed.
        assert (SC, "Strata Configurator") in TENANT_OWNERS
        # care-bus is untouched.
        assert "care-bus" in {a["key"] for a in _seed_module("seed_apps").APPS}
        assert ("secure-apps", "care-bus-licenses-admin", "care-bus", False) in API_CLIENTS


class TestRunForReal:
    def test_live_shape(self, conn):
        before = _snapshot(conn)
        assert CLIENT in before["client"] and (SC, False) in before["app"]  # control
        _run_sql_file(conn, UP, NAME)
        after = _snapshot(conn)

        owners = {t: o for t, o in after["tenant"]}
        assert {owners[t] for t in MOVED} == {"agent"}
        assert not any(o == SC for o in owners.values())
        # No (tenant, agent) row on the moved tenants, active or revoked.
        assert not {g for g in after["grant"] if g[0] in MOVED and g[1] == "agent"}
        assert not conn.execute(
            "SELECT EXISTS (SELECT 1 FROM tenant_app_grant g JOIN tenants t "
            "ON t.id = g.tenant_id WHERE t.owner_key = g.grantee_owner_key)"
        ).fetchone()[0]
        assert (SC, True) in after["app"]
        assert CLIENT not in after["client"]
        # 069's grant: soft-revoked, history kept.
        assert (FR_TEST_USER, SC, "migration-069", False) in after["grant"]
        assert not {g for g in after["grant"] if g[1] == SC and g[3]}
        # Owner stays: the revoked app's issuer row and 069's grant still name it.
        assert (SC,) in after["owner"]
        assert (SC, SC) in after["issuer"]
        # Licences are never written.
        assert after["licence"] == before["licence"]
        # Only what 084 names changed; care-bus is untouched.
        assert _care_bus(after) == _care_bus(before)
        for k in ("plan", "issuer", "licence", "realm", "owner"):
            assert after[k] == before[k], k
        assert after["client"] == before["client"] - {CLIENT}
        assert after["app"] == before["app"] - {(SC, False)} | {(SC, True)}

    def test_rerun_is_a_no_op(self, conn):
        _run_sql_file(conn, UP, NAME)
        snap = _snapshot(conn)
        _run_sql_file(conn, UP, NAME)
        assert _snapshot(conn) == snap

    def test_owner_row_goes_when_nothing_references_it(self, conn):
        conn.execute(
            "DELETE FROM app_issuer WHERE owner_key = %s", (SC,)
        )  # the only refs left after 084 are this and 069's grant
        conn.execute("DELETE FROM tenant_app_grant WHERE grantee_owner_key = %s", (SC,))
        _run_sql_file(conn, UP, NAME)
        assert (SC,) not in _snapshot(conn)["owner"]

    @pytest.mark.parametrize("status", ["active", "pending"])
    def test_refuses_while_a_licence_is_live(self, conn, status):
        _licence(conn, T2, status)
        snap = _snapshot(conn)
        with pytest.raises(Exception, match="084 REFUSED: 1 strata-configurator licence"):
            _run_sql_file(conn, UP, NAME)
        conn.execute("ROLLBACK")
        assert _snapshot(conn) == snap

    @pytest.mark.parametrize("clash", ["name", "external_ref"])
    def test_refuses_on_a_clash_with_a_live_agent_tenant(self, conn, clash):
        name, ref = ("configurator-2", None) if clash == "name" else ("other", "sc-2")
        conn.execute(
            "INSERT INTO tenants (id, name, owner_key, external_ref, created_at) "
            "VALUES (uuidv7(), %s, 'agent', %s, now())",
            (name, ref),
        )
        snap = _snapshot(conn)
        with pytest.raises(Exception, match="084 REFUSED: 1 strata-configurator tenant"):
            _run_sql_file(conn, UP, NAME)
        conn.execute("ROLLBACK")
        assert _snapshot(conn) == snap


class TestDowngrade:
    def test_restores_the_reversible_part(self, conn):
        before = _snapshot(conn)
        _run_sql_file(conn, UP, NAME)
        _run_sql_file(conn, DOWN, "down")
        after = _snapshot(conn)
        for k in ("owner", "client", "app", "plan", "issuer", "licence", "realm"):
            assert after[k] == before[k], k
        assert (FR_TEST_USER, SC, "migration-069", True) in after["grant"]
        # Irreversible: the move and the deleted redundant grants.
        assert {o for t, o in after["tenant"] if t in MOVED} == {"agent"}
        assert not {g for g in after["grant"] if g[0] in MOVED and g[1] == "agent"}

    def test_down_up_is_stable(self, conn):
        _run_sql_file(conn, UP, NAME)
        snap = _snapshot(conn)
        _run_sql_file(conn, DOWN, "down")
        _run_sql_file(conn, UP, NAME)
        assert _snapshot(conn) == snap


class TestThroughTheApi:
    @pytest.fixture
    def db(self, conn, monkeypatch):
        import main

        _run_sql_file(conn, UP, NAME)
        monkeypatch.setattr(main, "db_manager", main.db_manager)  # restored afterwards
        manager = _db_manager(_dsn_of(conn))
        yield manager
        manager.engine.dispose()

    def test_agent_now_owns_the_moved_tenant(self, db):
        with _client(db, "agent-licenses-admin", "license-admin") as c:
            resp = c.put(f"/tenants/{T2}", json={"name": "configurator-2"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["owner_key"] == "agent"

    def test_the_configurator_client_is_unknown(self, db):
        with _client(db, CLIENT[1], "license-admin") as c:
            resp = c.put(f"/tenants/{T2}", json={"name": "configurator-2"})
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error"] == "unknown_api_client"
