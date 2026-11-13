"""Migration 069: strata-configurator + care-bus grants on fr-test-user.

Two proofs, both driven by the REAL 069 file:

* run for real against a database built FROM EMPTY (init.sql -> the genesis shim ->
  001..068): fr-test-user stays owned by ``platform``, gains exactly the two grants
  (control: absent at 068), keeps every grant it already had, a re-run is a no-op,
  an operator's grant is never duplicated, and the downgrade restores 068 exactly;
  up -> down -> up through the real runner re-applies it.
* through the API: before 069 the strata-configurator SA's ``PUT /tenants/{id}`` is
  409 ``tenant_owned_by_other_app`` and its mint is 403 ``tenant_not_accessible``;
  after 069 the PUT is 200 (row unchanged, owner platform) and its
  ``strata-configurator`` licence is issued; an owner 069 did NOT grant
  (book-fulfillment) is still 409 / 403.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from scripts.apply_migrations import split_statements
from scripts.build_schema_arms import (
    _drop_database,
    _recreate_database,
    _run_sql_file,
)
from tests.conftest import TEST_DATABASE_URL
from tests.unit.test_migration_068_care_bus_licenses_admin_client import (
    _build_before as _build_068_before,
)
from tests.unit.test_tenant_ownership import _app, _as, _err, _mint

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "migrations"
NAME = "069_fr_test_user_strata_grants.sql"
UP = MIGRATIONS / NAME
DOWN = MIGRATIONS / "downgrades" / NAME
DB = "licenses_m069_test"
RUNNER_DB = "licenses_m069_runner_test"
FR_TEST_USER = "0195f48b-d000-7000-8000-000000000099"
GRANTEES = {"strata-configurator", "care-bus"}

STRATA = "strata-configurator-licenses-admin"
BOOK = "book-fulfillment-svc"


def _libpq_dsn() -> str:
    return TEST_DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1)


def _build_before(dsn: str) -> None:
    """The exact 068 state: every file up to and including 068 (068's helper runs
    everything < 068, so 068 itself is applied here)."""
    _build_068_before(dsn)
    with psycopg.connect(dsn, autocommit=True) as c:
        _run_sql_file(c, MIGRATIONS / "068_care_bus_licenses_admin_client.sql", "068")


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    dsn = _recreate_database(_libpq_dsn(), DB)
    try:
        _build_before(dsn)
        with psycopg.connect(dsn, autocommit=True) as c:
            yield c
    finally:
        _drop_database(_libpq_dsn(), DB)


def _active_grants(conn, tenant: str = FR_TEST_USER) -> set[tuple[str, str]]:
    return {
        tuple(r)
        for r in conn.execute(
            "SELECT grantee_owner_key, granted_by FROM tenant_app_grant "
            "WHERE tenant_id = %s AND revoked_at IS NULL",
            (tenant,),
        )
    }


def _all_grants(conn) -> set[tuple]:
    return {
        tuple(r)
        for r in conn.execute(
            "SELECT tenant_id, grantee_owner_key, granted_by, revoked_at FROM tenant_app_grant"
        )
    }


def _owner(conn) -> str:
    row = conn.execute("SELECT owner_key FROM tenants WHERE id = %s", (FR_TEST_USER,)).fetchone()
    assert row is not None, "fr-test-user must exist at 068 (seeded by 014)"
    return row[0]


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
    def test_adds_exactly_the_two_grants_and_keeps_the_owner(self, conn):
        before = _all_grants(conn)
        # Control: at 068 neither strata owner holds a grant on fr-test-user.
        assert not {g for g, _ in _active_grants(conn)} & GRANTEES
        assert _owner(conn) == "platform"
        _run_sql_file(conn, UP, NAME)
        assert _owner(conn) == "platform"
        added = _all_grants(conn) - before
        assert {(str(t), g, b, r) for t, g, b, r in added} == {
            (FR_TEST_USER, "strata-configurator", "migration-069", None),
            (FR_TEST_USER, "care-bus", "migration-069", None),
        }
        assert before <= _all_grants(conn)  # nothing removed or changed

    def test_rerun_is_a_no_op(self, conn):
        _run_sql_file(conn, UP, NAME)
        snap = _all_grants(conn)
        _run_sql_file(conn, UP, NAME)
        assert _all_grants(conn) == snap

    def test_an_existing_active_grant_is_left_alone(self, conn):
        conn.execute(
            "INSERT INTO tenant_app_grant (tenant_id, grantee_owner_key, granted_by) "
            "VALUES (%s, 'care-bus', 'operator')",
            (FR_TEST_USER,),
        )
        _run_sql_file(conn, UP, NAME)
        assert {r for r in _active_grants(conn) if r[0] in GRANTEES} == {
            ("care-bus", "operator"),
            ("strata-configurator", "migration-069"),
        }


class TestDowngrade:
    def test_restores_the_068_state(self, conn):
        before = _all_grants(conn)
        _run_sql_file(conn, UP, NAME)
        _run_sql_file(conn, DOWN, "down")
        assert _all_grants(conn) == before

    def test_leaves_an_operator_grant_alone(self, conn):
        conn.execute(
            "INSERT INTO tenant_app_grant (tenant_id, grantee_owner_key, granted_by) "
            "VALUES (%s, 'care-bus', 'operator')",
            (FR_TEST_USER,),
        )
        _run_sql_file(conn, UP, NAME)
        _run_sql_file(conn, DOWN, "down")
        assert {r for r in _active_grants(conn) if r[0] in GRANTEES} == {("care-bus", "operator")}


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
                    return (bool(row and row[0]), GRANTEES <= {g for g, _ in _active_grants(c)})

            def revoked_by_084() -> bool:
                # apply_all runs 084 too: it soft-revokes 069's strata-configurator
                # grant (care-bus's stays active).
                with psycopg.connect(dsn, autocommit=True) as c:
                    active = {g for g, _ in _active_grants(c)}
                    return "care-bus" in active and "strata-configurator" not in active

            assert apply_all(dsn) == 0
            assert state() == (True, False) and revoked_by_084()
            with psycopg.connect(dsn, autocommit=True) as c:
                _run_sql_file(c, DOWN.parent / "084_retire_strata_configurator.sql", "down 084")
                assert GRANTEES <= {g for g, _ in _active_grants(c)}  # 084 down re-grants
                _run_sql_file(c, DOWN, "down 069")
            assert state() == (False, False)
            assert apply_all(dsn) == 0
            assert state() == (True, False) and revoked_by_084()
        finally:
            _drop_database(_libpq_dsn(), RUNNER_DB)


class TestThroughTheApi:
    """The strata-configurator SA against fr-test-user, before and after the REAL 069."""

    @pytest.fixture(autouse=True)
    def _strata_client(self, db_manager):
        """The configurator's client as it was at 069: 084 retired it from the boot seed
        (models.API_CLIENTS), so the 069-era world registers it here."""
        with db_manager.engine.begin() as c:
            c.exec_driver_sql(
                "INSERT INTO api_client (realm, client_id, owner_key, cross_owner) "
                "VALUES ('secure-apps', %(c)s, 'strata-configurator', false) "
                "ON CONFLICT (realm, client_id) DO NOTHING",
                {"c": STRATA},
            )

    @staticmethod
    def _apply_069() -> None:
        with psycopg.connect(_libpq_dsn(), autocommit=True) as c:
            _run_sql_file(c, UP, NAME)

    def test_strata_configurator_is_refused_before_and_admitted_after(self, db_manager):
        db_manager.ensure_tenant(FR_TEST_USER, name="fr-test-user", owner_key="platform")
        app_id, plan_id = _app(db_manager, "strata-configurator", ("strata-configurator",))

        with _as(db_manager, STRATA) as sc:
            put_before = sc.put(f"/tenants/{FR_TEST_USER}", json={"name": "fr-test-user"})
        with _as(db_manager, STRATA, tenant=FR_TEST_USER) as sc:
            mint_before = _mint(sc, app_id, plan_id)
        assert put_before.status_code == 409, put_before.text
        assert _err(put_before) == "tenant_owned_by_other_app"
        assert mint_before.status_code == 403, mint_before.text
        assert _err(mint_before) == "tenant_not_accessible"

        self._apply_069()

        with _as(db_manager, STRATA) as sc:
            put_after = sc.put(f"/tenants/{FR_TEST_USER}", json={"name": "fr-test-user"})
        with _as(db_manager, STRATA, tenant=FR_TEST_USER) as sc:
            mint_after = _mint(sc, app_id, plan_id, sub=f"user-{uuid.uuid4().hex[:8]}")
        assert put_after.status_code == 200, put_after.text
        assert put_after.json()["owner_key"] == "platform"
        assert db_manager.get_tenant(FR_TEST_USER).owner_key == "platform"
        assert mint_after.status_code == 201, mint_after.text
        assert mint_after.json()["tenant_id"] == FR_TEST_USER

    def test_an_owner_069_did_not_grant_is_still_refused(self, db_manager):
        db_manager.ensure_tenant(FR_TEST_USER, name="fr-test-user", owner_key="platform")
        app_id, plan_id = _app(db_manager, "book-fulfillment", ("book-fulfillment",))
        self._apply_069()

        with _as(db_manager, BOOK) as bf:
            put = bf.put(f"/tenants/{FR_TEST_USER}", json={"name": "fr-test-user"})
        with _as(db_manager, BOOK, tenant=FR_TEST_USER) as bf:
            mint = _mint(bf, app_id, plan_id)
        assert put.status_code == 409, put.text
        assert _err(put) == "tenant_owned_by_other_app"
        assert mint.status_code == 403, mint.text
        assert _err(mint) == "tenant_not_accessible"

    def test_the_grant_does_not_let_strata_issue_another_owners_app(self, db_manager):
        """Tenant access is not issuance: care-bus's app is still not strata's to mint."""
        db_manager.ensure_tenant(FR_TEST_USER, name="fr-test-user", owner_key="platform")
        app_id, plan_id = _app(db_manager, "care-bus", ("care-bus",))
        self._apply_069()
        with _as(db_manager, STRATA, tenant=FR_TEST_USER) as sc:
            resp = _mint(sc, app_id, plan_id)
        assert resp.status_code == 403, resp.text
        assert _err(resp) == "app_not_issuable_by_caller"
