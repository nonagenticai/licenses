"""Migration 057: kag owns tenants; its licences client; the ``kag-research`` bundle.

Run for REAL against a database built FROM EMPTY (init.sql -> the genesis shim ->
001..056), with the four apps seeded carrying their live (pre-057) issuers, then 057
is applied:

* owner ``kag`` and ``secure-apps/kag-licenses-admin`` (owner kag, not cross-owner)
  exist, and at head tenant_owner / api_client equal the boot seed
  (models.TENANT_OWNERS / API_CLIENTS) — a FRESH database never runs this file;
* kag is ADDED to the issuers of kag, kag-graph, kag-orchestrator and
  inference-router; fr / platform / commonpractice stay; the four equal
  models.issuers_for_app_key;
* ``kag-research`` = those four apps, ensurer {kag} (models.BUNDLE_ENSURER_SEED);
  kag ensures nothing else; a missing app is skipped; when kag does not issue a
  member, no ensurer row is written (052 rule 1);
* operator rows are never overwritten; a re-run is a no-op;
* the downgrade restores the 056 state exactly and refuses while kag owns a tenant;
  up -> down -> up through the REAL runner re-applies 057.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from models import API_CLIENTS, BUNDLE_ENSURER_SEED, TENANT_OWNERS, issuers_for_app_key
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
    M078_BUNDLE_ENSURERS,
)
from tests.unit.test_migration_082_mcp_toolkit_entitlements import (
    M082_API_CLIENTS,
    M082_BUNDLE_ENSURERS,
    M082_TENANT_OWNERS,
)
from tests.unit.test_migration_083_secure_apps_s2s_api_clients import M083_API_CLIENTS
from tests.unit.test_migration_085_w2v_cell_m4_api_client import M085_API_CLIENTS
from tests.unit.test_migration_090_conveyor_service_issuer import M090_API_CLIENTS

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "migrations"
NAME = "057_kag_owner_and_kag_research_bundle.sql"
UP = MIGRATIONS / NAME
DOWN = MIGRATIONS / "downgrades" / NAME
DB = "licenses_m057_test"
RUNNER_DB = "licenses_m057_runner_test"

KAG_RESEARCH = ("kag", "kag-graph", "kag-orchestrator", "inference-router")
# Live issuers BEFORE 057 (the primary, read-only, 2026-10-15).
LIVE_ISSUERS: dict[str, tuple[str, ...]] = {
    "kag": ("fr",),
    "kag-graph": ("fr", "platform"),
    "kag-orchestrator": ("fr", "platform"),
    "inference-router": ("commonpractice", "fr"),
}

# What 057 adds on top of the 050..056 seeds — older migration tests subtract these.
M057_TENANT_OWNERS: set[tuple[str, str]] = {("kag", "KAG")}
M057_API_CLIENTS: set[tuple[str, str, str, bool]] = {
    ("secure-apps", "kag-licenses-admin", "kag", False),
}
M057_ISSUERS: dict[str, set[str]] = {key: {"kag"} for key in KAG_RESEARCH}
M057_BUNDLE_ENSURERS: dict[str, set[str]] = {"kag-research": {"kag"}}


def _libpq_dsn() -> str:
    return TEST_DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1)


def _build_before(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as c:
        _run_sql_file(c, INIT_SQL, "genesis")
        _run_sql_file(c, SHIM_SQL, "shim")
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name < NAME:
                _run_sql_file(c, path, "corpus")


def _seed_apps(c: psycopg.Connection, keys: tuple[str, ...] = KAG_RESEARCH) -> None:
    for key in keys:
        row = c.execute("SELECT id FROM app WHERE key = %s", (key,)).fetchone()
        if row is None:
            row = c.execute(
                "INSERT INTO app (key, name) VALUES (%s, %s) RETURNING id", (key, key)
            ).fetchone()
        assert row is not None
        app_id = row[0]
        c.execute("DELETE FROM app_issuer WHERE app_id = %s", (app_id,))
        for owner in LIVE_ISSUERS[key]:
            c.execute("INSERT INTO app_issuer (app_id, owner_key) VALUES (%s, %s)", (app_id, owner))


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    """A database at the exact 056 state, built from empty, with the four apps."""
    dsn = _recreate_database(_libpq_dsn(), DB)
    try:
        _build_before(dsn)
        with psycopg.connect(dsn, autocommit=True) as c:
            _seed_apps(c)
            yield c
    finally:
        _drop_database(_libpq_dsn(), DB)


def _owners(conn) -> set[tuple]:
    return {tuple(r) for r in conn.execute("SELECT key, display_name FROM tenant_owner")}


def _clients(conn) -> set[tuple]:
    return {
        tuple(r)
        for r in conn.execute("SELECT realm, client_id, owner_key, cross_owner FROM api_client")
    }


def _issuers(conn) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for key, owner in conn.execute(
        "SELECT a.key, ai.owner_key FROM app_issuer ai JOIN app a ON a.id = ai.app_id"
    ).fetchall():
        out.setdefault(key, set()).add(owner)
    return out


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


def _members(conn, bundle: str = "kag-research") -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT a.key FROM bundle_app ba JOIN bundle b ON b.id = ba.bundle_id "
            "JOIN app a ON a.id = ba.app_id WHERE b.key = %s",
            (bundle,),
        )
    }


def _snapshot(conn) -> tuple:
    return _owners(conn), _clients(conn), _issuers(conn), _ensurers(conn), _members(conn)


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

    def test_header_records_the_live_plan_facts_and_r1(self):
        header = UP.read_text(encoding="utf-8").split("BEGIN;", 1)[0]
        for needle in (
            "inference-router  standard ONLY",
            '`plans: ["tenant-pooled", "standard"]`',
            "kag does NOT ensure `knowledge` or `research`",
            "052's SEED RULE, rule 1",
        ):
            assert needle in header, needle


class TestRunForReal:
    def test_head_equals_the_boot_seed(self, conn):
        assert not (_owners(conn) & M057_TENANT_OWNERS)  # control: absent at 056
        assert not (_clients(conn) & M057_API_CLIENTS)
        _run_sql_file(conn, UP, NAME)
        from tests.unit.test_migration_067_strata_apps import M067_TENANT_OWNERS
        from tests.unit.test_migration_068_care_bus_licenses_admin_client import (
            M068_API_CLIENTS,
        )
        from tests.unit.test_migration_070_agent_app import M070_API_CLIENTS, M070_TENANT_OWNERS

        assert _owners(conn) == (
            set(TENANT_OWNERS) - M067_TENANT_OWNERS - M070_TENANT_OWNERS - M082_TENANT_OWNERS
        )  # 067: strata owners; 070: agent
        from tests.unit.test_migration_059_app_requires import (
            M059_API_CLIENTS,
            M059_BUNDLE_ENSURERS,
        )
        from tests.unit.test_migration_060_kag_frontend_bundle_minter import M060_API_CLIENTS
        from tests.unit.test_migration_061_kag_bundle_minter import M061_API_CLIENTS
        from tests.unit.test_migration_063_kag_service_bundle import M063_BUNDLE_ENSURERS
        from tests.unit.test_migration_064_karma_bundle_minter import M064_API_CLIENTS
        from tests.unit.test_migration_067_strata_apps import M067_API_CLIENTS

        # 059: fr/w2v minters; 060: kag-frontend minter; 061: kag's own minter;
        # 064: karma's minter.
        assert _clients(conn) == (
            set(API_CLIENTS)
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
        issuers = _issuers(conn)
        for key in KAG_RESEARCH:
            assert issuers[key] == set(issuers_for_app_key(key)), key
            assert issuers[key] == set(LIVE_ISSUERS[key]) | {"kag"}, key  # ADD only
        assert _ensurers(conn) == {
            k: set(v)
            for k, v in BUNDLE_ENSURER_SEED.items()
            if k not in M059_BUNDLE_ENSURERS
            and k not in M063_BUNDLE_ENSURERS  # 063: kag-service
            and k not in M078_BUNDLE_ENSURERS  # 078: care-bus
            and k not in M082_BUNDLE_ENSURERS  # 082: agent
        }

    def test_kag_research_holds_the_four_apps_and_kag_ensures_only_it(self, conn):
        _run_sql_file(conn, UP, NAME)
        assert _members(conn) == set(KAG_RESEARCH)
        assert {k for k, owners in _ensurers(conn).items() if "kag" in owners} == {"kag-research"}
        assert {c for c in _clients(conn) if c[2] == "kag"} == M057_API_CLIENTS

    def test_a_missing_app_is_skipped(self, conn):
        conn.execute(
            "DELETE FROM bundle_app WHERE app_id = (SELECT id FROM app WHERE key = 'inference-router')"
        )
        conn.execute("DELETE FROM app WHERE key = 'inference-router'")
        _run_sql_file(conn, UP, NAME)
        assert _members(conn) == {"kag", "kag-graph", "kag-orchestrator"}
        assert _ensurers(conn)["kag-research"] == {"kag"}

    def test_no_ensurer_when_kag_does_not_issue_a_member(self, conn):
        """052 rule 1, enforced in the file: an app kag does not issue already in the
        bundle (here: an operator put `hirag` in) leaves kag-research unensurable."""
        hirag = conn.execute(
            "INSERT INTO app (key, name) VALUES ('hirag', 'hirag') RETURNING id"
        ).fetchone()
        assert hirag is not None
        conn.execute("INSERT INTO app_issuer (app_id, owner_key) VALUES (%s, 'fr')", (hirag[0],))
        bundle = conn.execute(
            "INSERT INTO bundle (key, name) VALUES ('kag-research', 'x') RETURNING id"
        ).fetchone()
        assert bundle is not None
        conn.execute(
            "INSERT INTO bundle_app (bundle_id, app_id) VALUES (%s, %s)", (bundle[0], hirag[0])
        )
        _run_sql_file(conn, UP, NAME)
        assert _ensurers(conn)["kag-research"] == set()

    def test_an_operator_row_is_never_overwritten(self, conn):
        conn.execute("INSERT INTO tenant_owner (key, display_name) VALUES ('kag', 'Operator KAG')")
        conn.execute(
            "INSERT INTO api_client (realm, client_id, owner_key) "
            "VALUES ('secure-apps', 'kag-licenses-admin', 'platform')"
        )
        _run_sql_file(conn, UP, NAME)
        assert ("kag", "Operator KAG") in _owners(conn)
        assert ("secure-apps", "kag-licenses-admin", "platform", False) in _clients(conn)

    def test_rerun_is_a_no_op(self, conn):
        _run_sql_file(conn, UP, NAME)
        snap = _snapshot(conn)
        _run_sql_file(conn, UP, NAME)
        assert _snapshot(conn) == snap


class TestDowngrade:
    def test_restores_the_056_state(self, conn):
        before = _snapshot(conn)
        _run_sql_file(conn, UP, NAME)
        _run_sql_file(conn, DOWN, "down")
        assert _snapshot(conn) == before

    def test_refuses_while_kag_owns_a_tenant(self, conn):
        _run_sql_file(conn, UP, NAME)
        conn.execute(
            "INSERT INTO tenants (id, name, owner_key) VALUES (uuidv7(), 'kag-cust', 'kag')"
        )
        snap = _snapshot(conn)
        with pytest.raises(Exception, match="057 downgrade REFUSED"):
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
                    return bool(row and row[0]), bool(_clients(c) & M057_API_CLIENTS), _members(c)

            assert apply_all(dsn) == 0
            assert state() == (True, True, set(KAG_RESEARCH))
            with psycopg.connect(dsn, autocommit=True) as c:
                # 060's / 061's / 064's kag-owned minters are kag api_clients 057 did
                # not write: 057's downgrade refuses while they exist, so they go first.
                # 062's evidence-review issuer row and 063's kag-service ensurer are
                # kag rows 057 did not write.
                _run_sql_file(
                    c,
                    MIGRATIONS / "downgrades" / "064_karma_bundle_minter_owner.sql",
                    "down 064",
                )
                _run_sql_file(
                    c,
                    MIGRATIONS / "downgrades" / "063_kag_service_bundle.sql",
                    "down 063",
                )
                _run_sql_file(
                    c,
                    MIGRATIONS / "downgrades" / "062_evidence_review_app.sql",
                    "down 062",
                )
                _run_sql_file(
                    c,
                    MIGRATIONS / "downgrades" / "061_kag_bundle_minter_owner.sql",
                    "down 061",
                )
                _run_sql_file(
                    c,
                    MIGRATIONS / "downgrades" / "060_kag_frontend_bundle_minter_owner.sql",
                    "down 060",
                )
                _run_sql_file(c, DOWN, "down 057")
            assert state() == (False, False, set())
            assert apply_all(dsn) == 0
            assert state() == (True, True, set(KAG_RESEARCH))
        finally:
            _drop_database(_libpq_dsn(), RUNNER_DB)
