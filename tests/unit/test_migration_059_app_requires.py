"""Migration 059: app_requires, bundle_caller, license.is_dependency, the ``conveyor``
bundle and the fr / w2v bundle-token minter clients.

Run for REAL against a database built FROM EMPTY (init.sql -> the genesis shim ->
001..057), with the apps seeded carrying their live (2026-10-15) issuers, then 059:

* schema: both tables (named constraints) and ``license.is_dependency`` NOT NULL
  DEFAULT false with ``ck_license_dependency_unbundled`` (a bundled dependency
  licence is refused);
* at head api_client / bundle_ensurer equal the boot seed (models.API_CLIENTS /
  BUNDLE_ENSURER_SEED) and app_requires / bundle_caller equal APP_REQUIRES_SEED /
  BUNDLE_CALLER_SEED — a FRESH database never runs this file;
* ``conveyor`` holds EXACTLY its six apps; ``w2v`` is untouched; fr is conveyor's only
  ensurer, and only while it issues every member (052 rule 1);
* a missing app is skipped; operator rows are never overwritten; a re-run is a no-op;
* a CYCLE (an operator edge closing one) aborts the whole file, nothing changed;
* the downgrade restores the 057 state exactly and refuses while a tenant holds
  conveyor; up -> down -> up through the REAL runner re-applies 059.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from models import API_CLIENTS, APP_REQUIRES_SEED, BUNDLE_CALLER_SEED, BUNDLE_ENSURER_SEED
from scripts.apply_migrations import split_statements
from scripts.build_schema_arms import (
    INIT_SQL,
    SHIM_SQL,
    _drop_database,
    _recreate_database,
    _run_sql_file,
)
from tests.conftest import TEST_DATABASE_URL
from tests.unit.test_migration_078_care_bus_early_access import (
    M078_API_CLIENTS,
    M078_APP_REQUIRES,
    M078_BUNDLE_ENSURERS,
    M080_APP_REQUIRES,
)
from tests.unit.test_migration_082_mcp_toolkit_entitlements import (
    M082_API_CLIENTS,
    M082_APP_REQUIRES,
    M082_BUNDLE_ENSURERS,
    M086_APP_REQUIRES,
    M089_APP_REQUIRES,
)
from tests.unit.test_migration_083_secure_apps_s2s_api_clients import M083_API_CLIENTS
from tests.unit.test_migration_085_w2v_cell_m4_api_client import M085_API_CLIENTS
from tests.unit.test_migration_090_conveyor_service_issuer import M090_API_CLIENTS

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "migrations"
NAME = "059_app_requires_and_conveyor_bundle.sql"
UP = MIGRATIONS / NAME
DOWN = MIGRATIONS / "downgrades" / NAME
DB = "licenses_m059_test"
RUNNER_DB = "licenses_m059_runner_test"

CONVEYOR = {"workers", "coding", "laya", "kag", "requirements", "procedures"}
# Live issuers (the primary, read-only, 2026-10-15).
LIVE_ISSUERS: dict[str, tuple[str, ...]] = {
    "workers": ("fr", "platform"),
    "coding": ("fr", "platform"),
    "laya": ("fr", "platform"),
    "kag": ("fr", "kag"),
    "requirements": ("fr", "platform"),
    "procedures": ("fr", "platform"),
    "inference-router": ("commonpractice", "fr", "kag"),
    "mlx-backend": ("fr", "platform"),
    "mlx-ingestion-backend": ("fr", "platform"),
    "w2v": ("w2v",),
    "w2v-image": ("w2v",),
    "marketing": ("merqi",),
    "sales": ("merqi",),
}

# What 059 adds on top of the 050..057 seeds — older migration tests subtract these.
M059_API_CLIENTS: set[tuple[str, str, str, bool]] = {
    ("secure-apps", "fr-bundle-minter", "fr", False),
    ("secure-apps", "w2v-bundle-minter", "w2v", False),
}
M059_BUNDLE_ENSURERS: dict[str, set[str]] = {"conveyor": {"fr"}}


def _libpq_dsn() -> str:
    return TEST_DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1)


def _build_before(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as c:
        _run_sql_file(c, INIT_SQL, "genesis")
        _run_sql_file(c, SHIM_SQL, "shim")
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name < NAME:
                _run_sql_file(c, path, "corpus")


def _seed_apps(c: psycopg.Connection) -> None:
    for key, issuers in LIVE_ISSUERS.items():
        row = c.execute("SELECT id FROM app WHERE key = %s", (key,)).fetchone()
        if row is None:
            row = c.execute(
                "INSERT INTO app (key, name) VALUES (%s, %s) RETURNING id", (key, key)
            ).fetchone()
        assert row is not None
        c.execute("DELETE FROM app_issuer WHERE app_id = %s", (row[0],))
        for owner in issuers:
            c.execute("INSERT INTO app_issuer (app_id, owner_key) VALUES (%s, %s)", (row[0], owner))
    w2v = c.execute(
        "INSERT INTO bundle (key, name) VALUES ('w2v', 'W2V') "
        "ON CONFLICT (key) DO UPDATE SET name = bundle.name RETURNING id"
    ).fetchone()
    assert w2v is not None
    c.execute(
        "INSERT INTO bundle_app (bundle_id, app_id) SELECT %s, id FROM app "
        "WHERE key IN ('w2v', 'w2v-image') ON CONFLICT DO NOTHING",
        (w2v[0],),
    )


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    """A database at the exact 057 state, built from empty, with the apps."""
    dsn = _recreate_database(_libpq_dsn(), DB)
    try:
        _build_before(dsn)
        with psycopg.connect(dsn, autocommit=True) as c:
            _seed_apps(c)
            yield c
    finally:
        _drop_database(_libpq_dsn(), DB)


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


def _members(conn, bundle: str) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT a.key FROM bundle_app ba JOIN bundle b ON b.id = ba.bundle_id "
            "JOIN app a ON a.id = ba.app_id WHERE b.key = %s",
            (bundle,),
        )
    }


def _exists(conn, table: str) -> bool:
    row = conn.execute("SELECT to_regclass(%s)", (f"public.{table}",)).fetchone()
    return bool(row and row[0])


def _edges(conn) -> set[tuple]:
    if not _exists(conn, "app_requires"):
        return set()
    return {
        tuple(r)
        for r in conn.execute(
            "SELECT a.key, r.key, ar.caller_owner_key FROM app_requires ar "
            "JOIN app a ON a.id = ar.app_id JOIN app r ON r.id = ar.required_app_id"
        )
    }


def _callers(conn) -> set[tuple]:
    if not _exists(conn, "bundle_caller"):
        return set()
    return {
        tuple(r)
        for r in conn.execute(
            "SELECT b.key, c.owner_key, a.key FROM bundle_caller c "
            "JOIN bundle b ON b.id = c.bundle_id JOIN app a ON a.id = c.app_id"
        )
    }


def _has_column(conn) -> bool:
    row = conn.execute(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name = 'license' AND column_name = 'is_dependency'"
    ).fetchone()
    return bool(row and row[0])


def _snapshot(conn) -> tuple:
    return (
        _clients(conn),
        _ensurers(conn),
        _members(conn, "conveyor"),
        _members(conn, "w2v"),
        _edges(conn),
        _callers(conn),
        _has_column(conn),
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

    def test_header_records_the_evidence_and_the_unseeded_edge(self):
        header = UP.read_text(encoding="utf-8").split("BEGIN;", 1)[0]
        for needle in (
            "wm_client.py:224",
            "ir_bundle_tokens.py",
            "NOT SEEDED (not evidenced as a DIRECT call): kag -> inference-router",
            "A REAL COLUMN, not metadata JSON",
            "app_issuer: NONE added",
        ):
            assert needle in header, needle


class TestRunForReal:
    def test_head_equals_the_boot_seed(self, conn):
        assert not (_clients(conn) & M059_API_CLIENTS)  # control: absent at 057
        assert not _has_column(conn) and not _exists(conn, "app_requires")
        _run_sql_file(conn, UP, NAME)
        from tests.unit.test_migration_060_kag_frontend_bundle_minter import (
            M060_API_CLIENTS,
        )
        from tests.unit.test_migration_061_kag_bundle_minter import M061_API_CLIENTS
        from tests.unit.test_migration_063_kag_service_bundle import M063_BUNDLE_ENSURERS
        from tests.unit.test_migration_064_karma_bundle_minter import M064_API_CLIENTS
        from tests.unit.test_migration_067_strata_apps import M067_API_CLIENTS
        from tests.unit.test_migration_068_care_bus_licenses_admin_client import (
            M068_API_CLIENTS,
        )
        from tests.unit.test_migration_070_agent_app import M070_API_CLIENTS

        # 060: kag-frontend minter; 061: kag's own minter; 064: karma's minter.
        assert _clients(conn) == (
            set(API_CLIENTS)
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
        assert _ensurers(conn) == {  # 063: kag-service
            k: set(v)
            for k, v in BUNDLE_ENSURER_SEED.items()
            if k not in M063_BUNDLE_ENSURERS
            and k not in M078_BUNDLE_ENSURERS
            and k not in M082_BUNDLE_ENSURERS
        }
        from tests.unit.test_migration_062_evidence_review_app import M062_APP_REQUIRES

        assert (
            _edges(conn)
            == set(APP_REQUIRES_SEED)
            - M062_APP_REQUIRES
            - M078_APP_REQUIRES
            - M080_APP_REQUIRES
            - M082_APP_REQUIRES
            - M086_APP_REQUIRES
            - M089_APP_REQUIRES
        )
        from tests.unit.test_migration_065_secrets_in_conveyor_bundle import (
            M065_BUNDLE_CALLERS,
        )
        from tests.unit.test_migration_066_skill_builder_in_conveyor_bundle import (
            M066_BUNDLE_CALLERS,
        )

        assert (
            _callers(conn)
            == {  # 065: (conveyor, fr, secrets)
                (b, o, a) for b, pairs in BUNDLE_CALLER_SEED.items() for o, a in pairs
            }
            - M065_BUNDLE_CALLERS
            - M066_BUNDLE_CALLERS  # 066: (conveyor, fr, skill-builder)
        )
        assert _exists(conn, "tenant_dependency_grant")
        assert _has_column(conn)

    def test_conveyor_holds_exactly_its_apps_and_w2v_is_untouched(self, conn):
        w2v_before = _members(conn, "w2v")
        _run_sql_file(conn, UP, NAME)
        assert _members(conn, "conveyor") == CONVEYOR
        assert _members(conn, "w2v") == w2v_before == {"w2v", "w2v-image"}

    def test_a_bundled_dependency_licence_is_refused_by_the_check(self, conn):
        _run_sql_file(conn, UP, NAME)
        row = conn.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_license_dependency_unbundled'"
        ).fetchone()
        assert row is not None and "is_dependency" in row[0]
        default = conn.execute(
            "SELECT column_default, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'license' AND column_name = 'is_dependency'"
        ).fetchone()
        assert default == ("false", "NO")

    def test_a_missing_app_is_skipped(self, conn):
        for key in ("laya", "mlx-backend"):
            conn.execute("DELETE FROM app WHERE key = %s", (key,))
        _run_sql_file(conn, UP, NAME)
        assert _members(conn, "conveyor") == CONVEYOR - {"laya"}
        from tests.unit.test_migration_062_evidence_review_app import M062_APP_REQUIRES

        assert (
            _edges(conn)
            == set(APP_REQUIRES_SEED)
            - {("kag", "mlx-backend", "kag")}
            - M062_APP_REQUIRES
            - M078_APP_REQUIRES
            - M082_APP_REQUIRES
            - M086_APP_REQUIRES
            - M089_APP_REQUIRES
            - M080_APP_REQUIRES
        )

    def test_no_ensurer_when_fr_does_not_issue_a_member(self, conn):
        conn.execute(
            "DELETE FROM app_issuer WHERE owner_key = 'fr' "
            "AND app_id = (SELECT id FROM app WHERE key = 'laya')"
        )
        _run_sql_file(conn, UP, NAME)
        assert _ensurers(conn)["conveyor"] == set()

    def test_an_operator_row_is_never_overwritten(self, conn):
        conn.execute(
            "INSERT INTO api_client (realm, client_id, owner_key) "
            "VALUES ('secure-apps', 'fr-bundle-minter', 'platform')"
        )
        _run_sql_file(conn, UP, NAME)
        assert ("secure-apps", "fr-bundle-minter", "platform", False) in _clients(conn)

    def test_rerun_is_a_no_op(self, conn):
        _run_sql_file(conn, UP, NAME)
        snap = _snapshot(conn)
        _run_sql_file(conn, UP, NAME)
        assert _snapshot(conn) == snap

    @pytest.mark.timeout(30)
    def test_a_cycle_aborts_the_whole_file(self, conn):
        conn.execute(
            "CREATE TABLE app_requires (app_id UUID NOT NULL, required_app_id UUID NOT NULL, "
            "caller_owner_key VARCHAR(64) NOT NULL, PRIMARY KEY (app_id, required_app_id))"
        )
        conn.execute(
            "INSERT INTO app_requires SELECT a.id, r.id, 'platform' FROM app a, app r "
            "WHERE a.key = 'inference-router' AND r.key = 'kag'"
        )
        with pytest.raises(Exception, match="059: app_requires holds a cycle"):
            _run_sql_file(conn, UP, NAME)
        conn.execute("ROLLBACK")
        assert not _has_column(conn)
        assert not (_clients(conn) & M059_API_CLIENTS)
        assert "conveyor" not in _ensurers(conn)


class TestDowngrade:
    def test_restores_the_057_state(self, conn):
        before = _snapshot(conn)
        _run_sql_file(conn, UP, NAME)
        _run_sql_file(conn, DOWN, "down")
        assert _snapshot(conn) == before
        assert not _exists(conn, "app_requires") and not _exists(conn, "bundle_caller")
        assert not _exists(conn, "tenant_dependency_grant")

    def test_refuses_while_a_tenant_holds_conveyor(self, conn):
        _run_sql_file(conn, UP, NAME)
        tid = conn.execute(
            "INSERT INTO tenants (id, name, owner_key) VALUES (uuidv7(), 'fr-cust', 'fr') "
            "RETURNING id"
        ).fetchone()
        assert tid is not None
        conn.execute(
            "INSERT INTO tenant_bundle (tenant_id, bundle_id) "
            "SELECT %s, id FROM bundle WHERE key = 'conveyor'",
            (tid[0],),
        )
        snap = _snapshot(conn)
        with pytest.raises(Exception, match="059 downgrade REFUSED"):
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
                _seed_apps(c)
                c.execute(
                    "CREATE TABLE schema_migrations (filename TEXT PRIMARY KEY, "
                    "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
                )
                for path in sorted(MIGRATIONS.glob("*.sql")):
                    if path.name < NAME:
                        c.execute(
                            "INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,)
                        )

            def state() -> tuple[bool, bool, set[str]]:
                with psycopg.connect(dsn, autocommit=True) as c:
                    row = c.execute(
                        "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE filename = %s)",
                        (NAME,),
                    ).fetchone()
                    return bool(row and row[0]), _has_column(c), _members(c, "conveyor")

            assert apply_all(dsn) == 0
            assert state() == (True, True, CONVEYOR)
            with psycopg.connect(dsn, autocommit=True) as c:
                # apply_all ran 079 too: its dependency licences on fr-test-user are
                # rows 059's downgrade refuses on, so 079 goes first.
                _run_sql_file(c, DOWN.parent / "082_mcp_toolkit_entitlements.sql", "down 082")
                _run_sql_file(c, DOWN.parent / "080_care_bus_laya.sql", "down 080")
                _run_sql_file(c, DOWN.parent / "079_fr_test_user_care_bus_bundle.sql", "down 079")
                _run_sql_file(c, DOWN, "down 059")
            assert state() == (False, False, set())
            assert apply_all(dsn) == 0
            assert state() == (True, True, CONVEYOR)
        finally:
            _drop_database(_libpq_dsn(), RUNNER_DB)
