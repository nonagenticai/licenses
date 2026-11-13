"""Migration 080: care-bus requires ``laya`` — every care-bus tenant gets a laya licence.

Run for REAL against databases built FROM EMPTY (init.sql -> shim -> 001..079, ``mcp``
and ``laya`` seeded as scripts/seed_apps.py does), with TWO holders of the care-bus
bundle: fr-test-user (owner platform, 079) and a care-bus-OWNED tenant ensured by the
real ``services.bundle_ensure`` before the edge existed. Then:

* the edge equals ``models.APP_REQUIRES_SEED``'s 080 slice;
* DIFFERENTIAL: the same world is built twice; one copy gets the edge (as the boot seed
  writes it) + the REAL ``bundle_ensure.plan`` + ``apply`` on each holder (billing push
  failing, as 080 cannot push), the other gets 080. Rows are IDENTICAL (ids and
  timestamps aside) — for the live shape (laya inserted) and for exists / refuse;
* idempotent; a no-op when laya is absent; the downgrade restores the 079 state and
  refuses while a token exists;
* THROUGH THE API afterwards: care-bus-bundle-minter mints ``laya`` on both tenants
  (laya's issuers are platform / fr — the mint does not check issuers), and ``vllm``
  stays outside the bundle (404 ``app_not_in_bundle``).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest

from models import APP_REQUIRES_SEED, issuers_for_app_key
from scripts.apply_migrations import split_statements
from scripts.build_schema_arms import _drop_database, _recreate_database, _run_sql_file
from services import bundle_ensure
from tests.unit.test_migration_078_care_bus_early_access import M080_APP_REQUIRES
from tests.unit.test_migration_079_fr_test_user_care_bus_bundle import (
    NAME as NAME_079,
)
from tests.unit.test_migration_079_fr_test_user_care_bus_bundle import (
    _build_078,
    _client,
    _db_manager,
    _dsn_of,
    _libpq_dsn,
)

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "migrations"
NAME = "080_care_bus_laya.sql"
UP = MIGRATIONS / NAME
DOWN = MIGRATIONS / "downgrades" / NAME
DB_MIG = "licenses_m080_test"
DB_ENSURE = "licenses_m080_ensure_test"
T_FR = "0195f48b-d000-7000-8000-000000000099"  # fr-test-user (owner platform)
T_CB = "0195f48b-d000-7000-8000-0000000c0b05"  # a care-bus-owned tenant
TENANTS = (T_FR, T_CB)
BY = "migration-080"
GRANTED_BY = "bundle:care-bus by migration-080"


def _seed_laya(c: psycopg.Connection) -> None:
    """``laya`` as seed_apps.py creates it (edge-gate block: issuers default, standard)."""
    row = c.execute(
        "INSERT INTO app (key, name, audience) VALUES ('laya', 'Laya System-1 (Jev API)', "
        "'platform') ON CONFLICT (key) DO UPDATE SET name = app.name RETURNING id"
    ).fetchone()
    assert row is not None
    for owner in issuers_for_app_key("laya"):
        c.execute(
            "INSERT INTO app_issuer (app_id, owner_key) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (row[0], owner),
        )
    c.execute(
        "INSERT INTO plan (app_id, code, name) SELECT %s, 'standard', 'Standard' "
        "WHERE NOT EXISTS (SELECT 1 FROM plan WHERE app_id = %s AND code = 'standard')",
        (row[0], row[0]),
    )


def _ensure(dsn: str, tenant: str, by: str, granted_by: str) -> bundle_ensure.EnsurePlan:
    """The route's decision + writes (services.bundle_ensure), billing push failing."""
    db = _db_manager(dsn)
    try:
        ep = bundle_ensure.plan(db, tenant, "care-bus", ["standard"])
        bundle_ensure.apply(
            db,
            ep,
            provisioned_by=by,
            granted_by=granted_by,
            provision=lambda *a, **k: "billing down (test)",
        )
    finally:
        db.engine.dispose()
    return ep


def _build_079(dsn: str) -> None:
    _build_078(dsn)
    with psycopg.connect(dsn, autocommit=True) as c:
        _seed_laya(c)
        _run_sql_file(c, MIGRATIONS / NAME_079, "079")
        c.execute(
            "INSERT INTO tenants (id, name, byo_mode, owner_key, created_at) "
            "VALUES (%s, 'cb-tenant', 'platform_only', 'care-bus', now())",
            (T_CB,),
        )
    # The care-bus tenant's bundle, ensured by its owner before 080's edge existed.
    _ensure(dsn, T_CB, "care-bus-signup", "bundle:care-bus by secure-apps/care-bus")


@contextmanager
def _database(name: str) -> Iterator[str]:
    dsn = _recreate_database(_libpq_dsn(), name)
    try:
        _build_079(dsn)
        yield dsn
    finally:
        _drop_database(_libpq_dsn(), name)


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    with _database(DB_MIG) as dsn, psycopg.connect(dsn, autocommit=True) as c:
        yield c


def _edges(c) -> set[tuple]:
    return {
        tuple(r)
        for r in c.execute(
            "SELECT a.key, q.key, ar.caller_owner_key FROM app_requires ar "
            "JOIN app a ON a.id = ar.app_id JOIN app q ON q.id = ar.required_app_id"
        )
    }


def _snapshot(c) -> dict[str, set[tuple]]:
    q = {
        "hold": "SELECT tb.tenant_id::text, b.key FROM tenant_bundle tb "
        "JOIN bundle b ON b.id = tb.bundle_id",
        "licence": "SELECT l.tenant_id::text, a.key, s.keycloak_sub, s.keycloak_realm, b.key, "
        "l.is_dependency, l.status, p.code, l.expires_at, l.revoked_at, l.deleted_at, "
        "l.token_budget_override, l.quota_override, l.metering_provisioned_at, "
        "l.metering_verified_at, l.metadata::text, l.region_id "
        "FROM license l JOIN app a ON a.id = l.app_id JOIN subject s ON s.id = l.subject_id "
        "JOIN plan p ON p.id = l.plan_id LEFT JOIN bundle b ON b.id = l.bundle_id",
        "subject": "SELECT keycloak_sub, keycloak_realm, tenant_id::text, actor_sub FROM subject",
        "grant": "SELECT tenant_id::text, grantee_owner_key, granted_by, revoked_at "
        "FROM tenant_app_grant",
        "dep_grant": "SELECT g.tenant_id::text, g.owner_key, a.key, g.granted_by "
        "FROM tenant_dependency_grant g JOIN app a ON a.id = g.app_id",
        "edge": "SELECT a.key, q.key, ar.caller_owner_key FROM app_requires ar "
        "JOIN app a ON a.id = ar.app_id JOIN app q ON q.id = ar.required_app_id",
    }
    return {k: {tuple(r) for r in c.execute(sql)} for k, sql in q.items()}


def _laya(snap: dict[str, set[tuple]]) -> set[tuple]:
    return {(r[0], r[2], r[4], r[5], r[7], r[15]) for r in snap["licence"] if r[1] == "laya"}


def _add_edge_as_the_boot_seed(c) -> None:
    for app, req, caller in M080_APP_REQUIRES:
        c.execute(
            "INSERT INTO app_requires (app_id, required_app_id, caller_owner_key) "
            "SELECT a.id, q.id, %s FROM app a, app q WHERE a.key = %s AND q.key = %s "
            "ON CONFLICT DO NOTHING",
            (caller, app, req),
        )


def _held_laya(c, tenant: str, sub: str) -> None:
    """A pre-existing active tenant-level laya licence (an operator / conveyor mint)."""
    c.execute(
        "INSERT INTO subject (keycloak_sub, keycloak_realm) VALUES (%s, 'service') "
        "ON CONFLICT (keycloak_sub) DO NOTHING",
        (sub,),
    )
    c.execute(
        "INSERT INTO license (subject_id, app_id, plan_id, tenant_id, status) "
        "SELECT s.id, a.id, p.id, %s, 'active' "
        "FROM subject s, app a JOIN plan p ON p.app_id = a.id AND p.code = 'standard' "
        "WHERE s.keycloak_sub = %s AND a.key = 'laya'",
        (tenant, sub),
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

    def test_seed_carries_the_080_edge_and_not_vllm(self):
        assert M080_APP_REQUIRES <= set(APP_REQUIRES_SEED)
        assert not any(e[1] == "vllm" for e in APP_REQUIRES_SEED if e[0] == "care-bus")
        # laya keeps its default issuers: care-bus never issues it (dependency only).
        assert issuers_for_app_key("laya") == ("platform", "fr")


class TestIndistinguishableFromEnsure:
    @pytest.mark.parametrize("world", ["live_shape", "mixed"])
    def test_same_rows_as_the_real_ensure(self, world):
        with _database(DB_ENSURE) as dsn_e, _database(DB_MIG) as dsn_m:
            with (
                psycopg.connect(dsn_e, autocommit=True) as ce,
                psycopg.connect(dsn_m, autocommit=True) as cm,
            ):
                if world == "mixed":
                    for c in (ce, cm):
                        # fr-test-user: one tenant-level laya -> exists (shared).
                        _held_laya(c, T_FR, f"svc:{T_FR}:laya")
                        # care-bus tenant: two -> REFUSE several_candidates.
                        _held_laya(c, T_CB, "laya-a")
                        _held_laya(c, T_CB, "laya-b")
                assert _snapshot(ce) == _snapshot(cm)  # control: same start
                before = _snapshot(cm)
                _add_edge_as_the_boot_seed(ce)
                plans = {t: _ensure(dsn_e, t, BY, GRANTED_BY) for t in TENANTS}
                _run_sql_file(cm, UP, NAME)
                after_e, after_m = _snapshot(ce), _snapshot(cm)
            assert after_m == after_e
            assert after_m != before
            laya = {t: next(p for p in ep.apps if p.app_key == "laya") for t, ep in plans.items()}
            if world == "live_shape":
                assert {t: p.action for t, p in laya.items()} == {T_FR: "insert", T_CB: "insert"}
            else:
                assert {t: p.action for t, p in laya.items()} == {T_FR: "exists", T_CB: "REFUSE"}
            # Nothing but laya moved: every other app of the bundle answers exists.
            for ep in plans.values():
                assert {p.app_key: p.action for p in ep.apps if p.app_key != "laya"} == {
                    "care-bus": "exists",
                    "mcp": "exists",
                    "strata-rag": "exists",
                    "strata-ingest": "exists",
                }


class TestRunForReal:
    def test_live_shape(self, conn):
        assert not (_edges(conn) & M080_APP_REQUIRES)  # control
        _run_sql_file(conn, UP, NAME)
        assert {e for e in _edges(conn) if e[0] == "care-bus"} == {
            e for e in APP_REQUIRES_SEED if e[0] == "care-bus"
        }
        snap = _snapshot(conn)
        meta = '{"dependency_of": "care-bus", "provisioned_by": "migration-080"}'
        assert _laya(snap) == {(t, f"svc:{t}:laya", None, True, "standard", meta) for t in TENANTS}
        # The dependency grant: only where care-bus is NOT the owner.
        assert {g for g in snap["dep_grant"] if g[2] == "laya"} == {
            (T_FR, "care-bus", "laya", GRANTED_BY)
        }
        subs = {r[0]: r[2] for r in snap["subject"] if r[0].endswith(":laya")}
        assert subs == {f"svc:{t}:laya": t for t in TENANTS}

    def test_rerun_is_a_no_op(self, conn):
        _run_sql_file(conn, UP, NAME)
        snap = _snapshot(conn)
        _run_sql_file(conn, UP, NAME)
        assert _snapshot(conn) == snap

    def test_no_op_when_laya_is_absent(self, conn):
        conn.execute("DELETE FROM app WHERE key = 'laya'")
        before = _snapshot(conn)
        _run_sql_file(conn, UP, NAME)
        assert _snapshot(conn) == before

    def test_an_archived_holder_is_skipped(self, conn):
        conn.execute("UPDATE tenants SET archived_at = now() WHERE id = %s", (T_CB,))
        _run_sql_file(conn, UP, NAME)
        assert {r[0] for r in _laya(_snapshot(conn))} == {T_FR}


class TestDowngrade:
    def test_restores_the_079_state(self, conn):
        before = _snapshot(conn)
        _run_sql_file(conn, UP, NAME)
        _run_sql_file(conn, DOWN, "down")
        assert _snapshot(conn) == before

    def test_refuses_while_a_token_exists(self, conn):
        _run_sql_file(conn, UP, NAME)
        conn.execute(
            "INSERT INTO token (license_id, token_hash, token_type) SELECT id, 'h080', 'internal' "
            "FROM license WHERE metadata->>'provisioned_by' = 'migration-080' LIMIT 1"
        )
        snap = _snapshot(conn)
        with pytest.raises(Exception, match="REFUSED"):
            _run_sql_file(conn, DOWN, "down")
        conn.execute("ROLLBACK")  # the file's BEGIN left the aborted transaction open
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

    @pytest.mark.parametrize("tenant", TENANTS)
    def test_the_minter_mints_laya(self, db, tenant):
        with _client(db, "care-bus-bundle-minter", "bundle-token-minter") as c:
            resp = c.post(f"/internal/tenants/{tenant}/bundle-token?app=laya")
        assert resp.status_code == 201, resp.text

    def test_vllm_stays_outside_the_bundle(self, conn, db):
        conn.execute(
            "INSERT INTO app (key, name, audience) VALUES ('vllm', 'vLLM Inference', "
            "'platform') ON CONFLICT DO NOTHING"
        )
        with _client(db, "care-bus-bundle-minter", "bundle-token-minter") as c:
            resp = c.post(f"/internal/tenants/{T_CB}/bundle-token?app=vllm")
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"]["error"] == "app_not_in_bundle"
