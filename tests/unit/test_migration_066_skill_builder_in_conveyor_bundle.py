"""Migration 066: ``skill-builder`` joins ``conveyor``; bundle_caller (conveyor, fr, skill-builder).

George 2026-10-16 (fr P1.5 agent skills) reverses 065's "skill-builder ... stay out".

Run for REAL against a database built FROM EMPTY (init.sql -> the genesis shim ->
001..058, the apps seeded with their live issuers (059's slice, plus ``secrets`` and
``skill-builder`` on 050's default issuers {platform, fr}), then 059..065), then 066:

* at head bundle_caller equals the boot seed (models.BUNDLE_CALLER_SEED) — a FRESH
  database never runs this file;
* ``conveyor`` gains EXACTLY ``skill-builder``; no other bundle's members move
  (skill-builder stays in ``non-agentic-ai`` too); fr stays conveyor's only ensurer;
* if fr does not issue skill-builder the file REFUSES (052 rule 1) and changes nothing;
* a missing app is skipped; operator rows are never overwritten; a re-run is a no-op;
* the downgrade restores the 065 state exactly and refuses while a licence holds
  skill-builder in conveyor; up -> down -> up through the REAL runner re-applies 066.
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
from tests.unit.test_migration_059_app_requires import CONVEYOR as CONVEYOR_059
from tests.unit.test_migration_065_secrets_in_conveyor_bundle import (
    M065_CONVEYOR_MEMBERS,
    _all_members,
    _callers,
    _ensurers,
    _snapshot,
)
from tests.unit.test_migration_065_secrets_in_conveyor_bundle import _seed as _seed_065

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "migrations"
NAME = "066_skill_builder_in_conveyor_bundle.sql"
UP = MIGRATIONS / NAME
DOWN = MIGRATIONS / "downgrades" / NAME
DB = "licenses_m066_test"
RUNNER_DB = "licenses_m066_runner_test"
M059 = "059_app_requires_and_conveyor_bundle.sql"

# conveyor at the 065 state (059's six + secrets).
CONVEYOR = CONVEYOR_059 | M065_CONVEYOR_MEMBERS
# What 066 adds on top — older migration tests subtract these.
M066_CONVEYOR_MEMBERS: set[str] = {"skill-builder"}
M066_BUNDLE_CALLERS: set[tuple[str, str, str]] = {("conveyor", "fr", "skill-builder")}


def _libpq_dsn() -> str:
    return TEST_DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1)


def _seed(c: psycopg.Connection) -> None:
    _seed_065(c)  # 059's live apps + secrets (+ non-agentic-ai)
    row = c.execute("SELECT id FROM app WHERE key = 'skill-builder'").fetchone()
    if row is None:
        row = c.execute(
            "INSERT INTO app (key, name) VALUES ('skill-builder', 'Skill Builder') RETURNING id"
        ).fetchone()
    assert row is not None
    c.execute("DELETE FROM app_issuer WHERE app_id = %s", (row[0],))
    for owner in ("platform", "fr"):
        c.execute("INSERT INTO app_issuer (app_id, owner_key) VALUES (%s, %s)", (row[0], owner))
    # 028: skill-builder sits in non-agentic-ai on prod; 066 must leave that row alone.
    c.execute(
        "INSERT INTO bundle_app (bundle_id, app_id) "
        "SELECT b.id, %s FROM bundle b WHERE b.key = 'non-agentic-ai' ON CONFLICT DO NOTHING",
        (row[0],),
    )


def _build_before(dsn: str) -> None:
    """001..058, the live apps, then 059..065 — the exact 065 state."""
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


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    dsn = _recreate_database(_libpq_dsn(), DB)
    try:
        _build_before(dsn)
        with psycopg.connect(dsn, autocommit=True) as c:
            yield c
    finally:
        _drop_database(_libpq_dsn(), DB)


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
            "THIS REVERSES 065's EXCLUSION",
            "George 2026-10-16",
            "REMAINING other_bundle CASE",
            "scripts/provision_tenant_bundle.py --tenant <uuid> --bundle conveyor",
        ):
            assert needle in header, needle

    def test_seed_mirrors_the_caller_row(self):
        assert ("fr", "skill-builder") in BUNDLE_CALLER_SEED["conveyor"]
        assert BUNDLE_ENSURER_SEED["conveyor"] == ("fr",)


class TestRunForReal:
    def test_head_equals_the_boot_seed(self, conn):
        assert not (_callers(conn) & M066_BUNDLE_CALLERS)  # control: absent at 065
        _run_sql_file(conn, UP, NAME)
        assert _callers(conn) == {
            (b, o, a) for b, pairs in BUNDLE_CALLER_SEED.items() for o, a in pairs
        }

    def test_conveyor_gains_exactly_skill_builder_and_nothing_else_moves(self, conn):
        before = _all_members(conn)
        assert before["conveyor"] == CONVEYOR  # control
        _run_sql_file(conn, UP, NAME)
        after = _all_members(conn)
        assert after.pop("conveyor") == CONVEYOR | M066_CONVEYOR_MEMBERS
        before.pop("conveyor")
        assert after == before
        assert "skill-builder" in after["non-agentic-ai"]

    def test_fr_stays_the_only_ensurer(self, conn):
        _run_sql_file(conn, UP, NAME)
        assert _ensurers(conn)["conveyor"] == {"fr"}

    def test_refuses_when_fr_does_not_issue_skill_builder(self, conn):
        conn.execute(
            "DELETE FROM app_issuer WHERE owner_key = 'fr' "
            "AND app_id = (SELECT id FROM app WHERE key = 'skill-builder')"
        )
        snap = _snapshot(conn)
        with pytest.raises(Exception, match="fr ensures conveyor but does not issue skill-builder"):
            _run_sql_file(conn, UP, NAME)
        conn.execute("ROLLBACK")
        assert _snapshot(conn) == snap

    def test_a_missing_app_is_skipped(self, conn):
        conn.execute(
            "DELETE FROM bundle_app WHERE app_id = (SELECT id FROM app WHERE key = 'skill-builder')"
        )
        conn.execute("DELETE FROM app WHERE key = 'skill-builder'")
        snap = _snapshot(conn)
        _run_sql_file(conn, UP, NAME)
        assert _snapshot(conn) == snap

    def test_an_operator_row_is_never_overwritten(self, conn):
        conn.execute(
            "INSERT INTO bundle_app (bundle_id, app_id) SELECT b.id, a.id FROM bundle b, app a "
            "WHERE b.key = 'conveyor' AND a.key = 'skill-builder'"
        )
        _run_sql_file(conn, UP, NAME)
        assert _all_members(conn)["conveyor"] == CONVEYOR | M066_CONVEYOR_MEMBERS

    def test_rerun_is_a_no_op(self, conn):
        _run_sql_file(conn, UP, NAME)
        snap = _snapshot(conn)
        _run_sql_file(conn, UP, NAME)
        assert _snapshot(conn) == snap


class TestDowngrade:
    def test_restores_the_065_state(self, conn):
        before = _snapshot(conn)
        _run_sql_file(conn, UP, NAME)
        _run_sql_file(conn, DOWN, "down")
        assert _snapshot(conn) == before

    def test_refuses_while_a_licence_holds_skill_builder_in_conveyor(self, conn):
        _run_sql_file(conn, UP, NAME)
        tid = conn.execute(
            "INSERT INTO tenants (id, name, owner_key) VALUES (uuidv7(), 'conv', 'fr') RETURNING id"
        ).fetchone()
        assert tid is not None
        sub = conn.execute(
            "INSERT INTO subject (keycloak_sub, keycloak_realm) VALUES (%s, 'service') "
            "RETURNING id",
            (f"svc:{tid[0]}:skill-builder",),
        ).fetchone()
        assert sub is not None
        plan = conn.execute(
            "INSERT INTO plan (app_id, code, name, rate_limit_rpm, rate_limit_burst) "
            "SELECT id, 'standard', 'Standard', 60, 10 FROM app WHERE key = 'skill-builder' "
            "RETURNING id"
        ).fetchone()
        assert plan is not None
        conn.execute(
            "INSERT INTO license (subject_id, app_id, plan_id, tenant_id, bundle_id, status, "
            "starts_at) SELECT %s, a.id, %s, %s, b.id, 'active', now() "
            "FROM app a, bundle b WHERE a.key = 'skill-builder' AND b.key = 'conveyor'",
            (sub[0], plan[0], tid[0]),
        )
        snap = _snapshot(conn)
        with pytest.raises(Exception, match="066 downgrade REFUSED"):
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
                        _callers(c) & M066_BUNDLE_CALLERS,
                    )

            assert apply_all(dsn) == 0
            assert state() == (True, CONVEYOR | M066_CONVEYOR_MEMBERS, M066_BUNDLE_CALLERS)
            with psycopg.connect(dsn, autocommit=True) as c:
                _run_sql_file(c, DOWN, "down 066")
            assert state() == (False, CONVEYOR, set())
            assert apply_all(dsn) == 0
            assert state() == (True, CONVEYOR | M066_CONVEYOR_MEMBERS, M066_BUNDLE_CALLERS)
        finally:
            _drop_database(_libpq_dsn(), RUNNER_DB)
