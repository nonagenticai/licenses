"""Migration 040: the `inference-router` bundle (licenses #239).

WHAT IS PINNED.
* Text-level (no database): it creates no `app` row, every INSERT is ON CONFLICT DO
  NOTHING, a missing app is a NOTICE + CONTINUE (never an exception), it writes no
  licence / tenant_bundle / token / subject row, and the downgrade refuses rather than
  orphan a grant.
* Run for real (Postgres; rolled back): with the app ABSENT the bundle is created and
  the membership skipped; with the app PRESENT it is linked; a re-run is a no-op; the
  downgrade removes an unheld bundle and RAISES on a held one, changing nothing.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import psycopg
import pytest

from tests.conftest import SEED_TENANT_ID, TEST_DATABASE_URL

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "migrations"
UP = MIGRATIONS / "040_inference_router_bundle.sql"
DOWN = MIGRATIONS / "downgrades" / "040_inference_router_bundle.sql"


def _strip_comments(sql: str) -> str:
    return "\n".join(ln for ln in sql.splitlines() if not ln.lstrip().startswith("--"))


def _libpq_dsn() -> str:
    return TEST_DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1)


def _count(conn, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    assert row is not None, sql
    return row[0]


class TestTheTextKeepsTheContract:
    def test_it_creates_no_app_rows(self):
        body = _strip_comments(UP.read_text())
        assert "INSERT INTO app " not in body and "INSERT INTO app(" not in body

    def test_every_insert_is_on_conflict_do_nothing(self):
        body = _strip_comments(UP.read_text())
        assert body.count("INSERT INTO") == body.count("ON CONFLICT") == 2
        assert "DO UPDATE" not in body

    def test_a_missing_app_is_skipped_not_raised(self):
        body = _strip_comments(UP.read_text())
        assert "RAISE NOTICE" in body and "CONTINUE;" in body
        assert "RAISE EXCEPTION" not in body

    def test_it_grants_nothing_and_modifies_nothing(self):
        body = _strip_comments(UP.read_text()).upper()
        assert not re.search(r"\bDELETE\s+FROM\b", body)
        assert not re.search(r"\bUPDATE\s+\w+\s+SET\b", body)
        assert not re.search(r"\bDROP\b", body)
        for table in ("LICENSE", "TENANT_BUNDLE", "TOKEN", "SUBJECT", "TENANTS"):
            assert not re.search(rf"\bINSERT\s+INTO\s+{table}\b", body), table

    def test_it_links_exactly_the_inference_router_app(self):
        body = _strip_comments(UP.read_text())
        block = body[body.index("SELECT * FROM (VALUES") : body.index("AS t(bundle_key, app_key)")]
        rows = re.findall(r"\(\s*'([a-z0-9-]+)'\s*,\s*'([a-z0-9-]+)'\s*\)", block)
        assert rows == [("inference-router", "inference-router")]

    def test_the_downgrade_refuses_a_held_bundle(self):
        body = _strip_comments(DOWN.read_text())
        assert "RAISE EXCEPTION" in body
        assert "FROM license WHERE bundle_id" in body
        assert "FROM tenant_bundle WHERE bundle_id" in body
        # The check precedes the delete.
        assert body.index("RAISE EXCEPTION") < body.index("DELETE FROM bundle_app")
        assert "DELETE FROM app" not in body

    def test_the_runner_cannot_auto_apply_the_downgrade(self):
        assert DOWN.resolve() not in {p.resolve() for p in MIGRATIONS.glob("*.sql")}


def _run(conn, path: Path) -> None:
    sys.path.insert(0, str(REPO / "scripts"))
    from apply_migrations import _run_file  # type: ignore[import-not-found]

    _run_file(conn, path)


def _membership(conn) -> tuple[bool, int]:
    bundle = conn.execute("SELECT id FROM bundle WHERE key = 'inference-router'").fetchone()
    if bundle is None:
        return False, 0
    n = conn.execute(
        "SELECT count(*) FROM bundle_app ba JOIN app a ON a.id = ba.app_id "
        "WHERE ba.bundle_id = %s AND a.key = 'inference-router'",
        (bundle[0],),
    ).fetchone()[0]
    return True, n


class TestRunForReal:
    """Every statement runs inside ONE transaction that is rolled back."""

    def test_app_absent_bundle_created_membership_skipped(self, db_manager):
        conn = psycopg.connect(_libpq_dsn())
        try:
            assert _membership(conn) == (False, 0), "positive control: no bundle yet"
            _run(conn, UP)
            assert _membership(conn) == (True, 0)
            assert _count(conn, "SELECT count(*) FROM app WHERE key = 'inference-router'") == 0
        finally:
            conn.rollback()
            conn.close()

    def test_app_present_is_linked_and_rerun_is_a_noop(self, db_manager):
        db_manager.create_app(
            key="inference-router", name="Inference Router", issuers=("platform",)
        )
        conn = psycopg.connect(_libpq_dsn())
        try:
            _run(conn, UP)
            assert _membership(conn) == (True, 1)
            _run(conn, UP)
            assert _membership(conn) == (True, 1)
            assert _count(conn, "SELECT count(*) FROM bundle WHERE key = 'inference-router'") == 1
        finally:
            conn.rollback()
            conn.close()

    def test_the_downgrade_removes_an_unheld_bundle(self, db_manager):
        db_manager.create_app(
            key="inference-router", name="Inference Router", issuers=("platform",)
        )
        conn = psycopg.connect(_libpq_dsn())
        try:
            _run(conn, UP)
            _run(conn, DOWN)
            assert _membership(conn) == (False, 0)
            assert _count(conn, "SELECT count(*) FROM app WHERE key = 'inference-router'") == 1
        finally:
            conn.rollback()
            conn.close()

    def test_the_downgrade_refuses_a_held_bundle_and_changes_nothing(self, db_manager):
        db_manager.create_app(
            key="inference-router", name="Inference Router", issuers=("platform",)
        )
        conn = psycopg.connect(_libpq_dsn())
        try:
            _run(conn, UP)
            conn.execute(
                "INSERT INTO tenant_bundle (tenant_id, bundle_id) "
                "SELECT %s, id FROM bundle WHERE key = 'inference-router'",
                (SEED_TENANT_ID,),
            )
            with pytest.raises(RuntimeError, match="040 downgrade REFUSED"):
                _run(conn, DOWN)
        finally:
            conn.rollback()
            conn.close()
