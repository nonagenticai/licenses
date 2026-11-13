"""Migration 065: ``secrets`` joins ``conveyor``; bundle_caller (conveyor, fr, secrets).

Run for REAL against a database built FROM EMPTY (init.sql -> the genesis shim ->
001..058, the apps seeded with their live issuers (059's slice, plus ``secrets`` on
050's default issuers {platform, fr}), then 059..064), then 065:

* at head bundle_caller equals the boot seed (models.BUNDLE_CALLER_SEED) — a FRESH
  database never runs this file;
* ``conveyor`` gains EXACTLY ``secrets``; no other bundle's members move (secrets stays
  in ``non-agentic-ai`` too); fr stays conveyor's only ensurer;
* if fr does not issue secrets the file REFUSES (052 rule 1) and changes nothing;
* a missing app is skipped; operator rows are never overwritten; a re-run is a no-op;
* the downgrade restores the 064 state exactly and refuses while a licence holds
  secrets in conveyor; up -> down -> up through the REAL runner re-applies 065.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from models import BUNDLE_CALLER_SEED, BUNDLE_ENSURER_SEED
from scripts.apply_migrations import split_statements
from scripts.build_schema_arms import (
    INIT_SQL,
    SHIM_SQL,
    _drop_database,
    _recreate_database,
    _run_sql_file,
)
from tests.conftest import TEST_DATABASE_URL
from tests.unit.test_migration_059_app_requires import CONVEYOR, _seed_apps

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "migrations"
NAME = "065_secrets_in_conveyor_bundle.sql"
UP = MIGRATIONS / NAME
DOWN = MIGRATIONS / "downgrades" / NAME
DB = "licenses_m065_test"
RUNNER_DB = "licenses_m065_runner_test"

# What 065 adds on top of the 059 seeds — older migration tests subtract these.
M065_CONVEYOR_MEMBERS: set[str] = {"secrets"}
M065_BUNDLE_CALLERS: set[tuple[str, str, str]] = {("conveyor", "fr", "secrets")}


def _libpq_dsn() -> str:
    return TEST_DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1)


M059 = "059_app_requires_and_conveyor_bundle.sql"


def _build_before(dsn: str) -> None:
    """001..058, the live apps, then 059..064: 059 links conveyor's members and the
    (fr, workers) caller only for apps that EXIST, as on prod."""
    with psycopg.connect(dsn, autocommit=True) as c:
        _run_sql_file(c, INIT_SQL, "genesis")
        _run_sql_file(c, SHIM_SQL, "shim")
        corpus = sorted(MIGRATIONS.glob("*.sql"))
        for path in corpus:
            if path.name < M059:
                _run_sql_file(c, path, "corpus")
        _seed(c)
        for path in corpus:
            if M059 <= path.name < NAME:
                _run_sql_file(c, path, "corpus")


def _seed(c: psycopg.Connection) -> None:
    _seed_apps(c)
    row = c.execute("SELECT id FROM app WHERE key = 'secrets'").fetchone()
    if row is None:
        row = c.execute(
            "INSERT INTO app (key, name) VALUES ('secrets', 'Secrets') RETURNING id"
        ).fetchone()
    assert row is not None
    c.execute("DELETE FROM app_issuer WHERE app_id = %s", (row[0],))
    for owner in ("platform", "fr"):
        c.execute("INSERT INTO app_issuer (app_id, owner_key) VALUES (%s, %s)", (row[0], owner))
    # 039: secrets sits in non-agentic-ai on prod; 065 must leave that row alone.
    c.execute(
        "INSERT INTO bundle (key, name) VALUES ('non-agentic-ai', 'Non-Agentic AI') "
        "ON CONFLICT (key) DO NOTHING"
    )
    c.execute(
        "INSERT INTO bundle_app (bundle_id, app_id) "
        "SELECT b.id, %s FROM bundle b WHERE b.key = 'non-agentic-ai' ON CONFLICT DO NOTHING",
        (row[0],),
    )


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    """A database at the exact 064 state, built from empty, with the apps."""
    dsn = _recreate_database(_libpq_dsn(), DB)
    try:
        _build_before(dsn)
        with psycopg.connect(dsn, autocommit=True) as c:
            yield c
    finally:
        _drop_database(_libpq_dsn(), DB)


def _all_members(conn) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for bkey, akey in conn.execute(
        "SELECT b.key, a.key FROM bundle_app ba JOIN bundle b ON b.id = ba.bundle_id "
        "JOIN app a ON a.id = ba.app_id"
    ).fetchall():
        out.setdefault(bkey, set()).add(akey)
    return out


def _callers(conn) -> set[tuple]:
    return {
        tuple(r)
        for r in conn.execute(
            "SELECT b.key, c.owner_key, a.key FROM bundle_caller c "
            "JOIN bundle b ON b.id = c.bundle_id JOIN app a ON a.id = c.app_id"
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


def _snapshot(conn) -> tuple:
    return _all_members(conn), _callers(conn), _ensurers(conn)


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

    def test_header_records_the_reversal_and_the_operator_step(self):
        header = UP.read_text(encoding="utf-8").split("BEGIN;", 1)[0]
        for needle in (
            "THIS REVERSES 059",
            "fr#1579",
            "REMAINING other_bundle CASE",
            "scripts/provision_tenant_bundle.py --tenant <uuid> --bundle conveyor",
        ):
            assert needle in header, needle

    def test_seed_mirrors_the_caller_row(self):
        assert ("fr", "secrets") in BUNDLE_CALLER_SEED["conveyor"]
        assert BUNDLE_ENSURER_SEED["conveyor"] == ("fr",)


class TestRunForReal:
    def test_head_equals_the_boot_seed(self, conn):
        assert not (_callers(conn) & M065_BUNDLE_CALLERS)  # control: absent at 064
        _run_sql_file(conn, UP, NAME)
        from tests.unit.test_migration_066_skill_builder_in_conveyor_bundle import (
            M066_BUNDLE_CALLERS,
        )

        assert (
            _callers(conn)
            == {  # 066 adds (conveyor, fr, skill-builder) later
                (b, o, a) for b, pairs in BUNDLE_CALLER_SEED.items() for o, a in pairs
            }
            - M066_BUNDLE_CALLERS
        )

    def test_conveyor_gains_exactly_secrets_and_nothing_else_moves(self, conn):
        before = _all_members(conn)
        assert before["conveyor"] == CONVEYOR  # control
        _run_sql_file(conn, UP, NAME)
        after = _all_members(conn)
        assert after.pop("conveyor") == CONVEYOR | M065_CONVEYOR_MEMBERS
        before.pop("conveyor")
        assert after == before
        assert "secrets" in after["non-agentic-ai"]

    def test_fr_stays_the_only_ensurer(self, conn):
        _run_sql_file(conn, UP, NAME)
        assert _ensurers(conn)["conveyor"] == {"fr"}

    def test_refuses_when_fr_does_not_issue_secrets(self, conn):
        conn.execute(
            "DELETE FROM app_issuer WHERE owner_key = 'fr' "
            "AND app_id = (SELECT id FROM app WHERE key = 'secrets')"
        )
        snap = _snapshot(conn)
        with pytest.raises(Exception, match="fr ensures conveyor but does not issue secrets"):
            _run_sql_file(conn, UP, NAME)
        conn.execute("ROLLBACK")
        assert _snapshot(conn) == snap

    def test_a_missing_app_is_skipped(self, conn):
        conn.execute("DELETE FROM app WHERE key = 'secrets'")
        snap = _snapshot(conn)
        _run_sql_file(conn, UP, NAME)
        assert _snapshot(conn) == snap

    def test_an_operator_row_is_never_overwritten(self, conn):
        conn.execute(
            "INSERT INTO bundle_app (bundle_id, app_id) SELECT b.id, a.id FROM bundle b, app a "
            "WHERE b.key = 'conveyor' AND a.key = 'secrets'"
        )
        _run_sql_file(conn, UP, NAME)
        assert _all_members(conn)["conveyor"] == CONVEYOR | M065_CONVEYOR_MEMBERS

    def test_rerun_is_a_no_op(self, conn):
        _run_sql_file(conn, UP, NAME)
        snap = _snapshot(conn)
        _run_sql_file(conn, UP, NAME)
        assert _snapshot(conn) == snap


class TestDowngrade:
    def test_restores_the_064_state(self, conn):
        before = _snapshot(conn)
        _run_sql_file(conn, UP, NAME)
        _run_sql_file(conn, DOWN, "down")
        assert _snapshot(conn) == before

    def test_refuses_while_a_licence_holds_secrets_in_conveyor(self, conn):
        _run_sql_file(conn, UP, NAME)
        tid = conn.execute(
            "INSERT INTO tenants (id, name, owner_key) VALUES (uuidv7(), 'conv', 'fr') RETURNING id"
        ).fetchone()
        assert tid is not None
        sub = conn.execute(
            "INSERT INTO subject (keycloak_sub, keycloak_realm) VALUES (%s, 'service') "
            "RETURNING id",
            (f"svc:{tid[0]}:secrets",),
        ).fetchone()
        assert sub is not None
        plan = conn.execute(
            "INSERT INTO plan (app_id, code, name, rate_limit_rpm, rate_limit_burst) "
            "SELECT id, 'standard', 'Standard', 60, 10 FROM app WHERE key = 'secrets' "
            "RETURNING id"
        ).fetchone()
        assert plan is not None
        conn.execute(
            "INSERT INTO license (subject_id, app_id, plan_id, tenant_id, bundle_id, status, "
            "starts_at) SELECT %s, a.id, %s, %s, b.id, 'active', now() "
            "FROM app a, bundle b WHERE a.key = 'secrets' AND b.key = 'conveyor'",
            (sub[0], plan[0], tid[0]),
        )
        snap = _snapshot(conn)
        with pytest.raises(Exception, match="065 downgrade REFUSED"):
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

            def state() -> tuple[bool, set[str], set[tuple]]:
                with psycopg.connect(dsn, autocommit=True) as c:
                    row = c.execute(
                        "SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE filename = %s)",
                        (NAME,),
                    ).fetchone()
                    return (
                        bool(row and row[0]),
                        _all_members(c)["conveyor"],
                        _callers(c) & M065_BUNDLE_CALLERS,
                    )

            assert apply_all(dsn) == 0
            assert state() == (True, CONVEYOR | M065_CONVEYOR_MEMBERS, M065_BUNDLE_CALLERS)
            with psycopg.connect(dsn, autocommit=True) as c:
                _run_sql_file(c, DOWN, "down 065")
            assert state() == (False, CONVEYOR, set())
            assert apply_all(dsn) == 0
            assert state() == (True, CONVEYOR | M065_CONVEYOR_MEMBERS, M065_BUNDLE_CALLERS)
        finally:
            _drop_database(_libpq_dsn(), RUNNER_DB)
