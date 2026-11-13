"""Migration 042: new tenants default to byo_mode 'local_allowed' (George 2026-10-02).

"For now the only tenants we have will use our local-llm" — so a tenant created
without an explicit byo_mode is 'local_allowed', reversing the 'byo_required' default
of migration 007 / #51. The migration touches NO existing row, is idempotent, and its
downgrade restores 'byo_required'.

⚠️ SUPERSEDED as the live default by migration 058 (licenses#273, 'byo_required'
again): the per-creation-path assertions of the CURRENT default live in
test_migration_058_tenants_byo_mode_default.py. This file pins only 042's own SQL.
"""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from tests.conftest import TEST_DATABASE_URL
from tests.support.legacy_owner_default import legacy_owner_default  # noqa: F401

REPO = Path(__file__).resolve().parents[2]
UP = REPO / "migrations" / "042_tenants_byo_mode_default_local_allowed.sql"
DOWN = REPO / "migrations" / "downgrades" / "042_tenants_byo_mode_default_local_allowed.sql"


def _libpq_dsn() -> str:
    return TEST_DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1)


def _run_file():
    sys.path.insert(0, str(REPO / "scripts"))
    from apply_migrations import _run_file as run  # type: ignore[import-not-found]

    return run


def _column_default(cur) -> str | None:
    row = cur.execute(
        "SELECT column_default FROM information_schema.columns "
        "WHERE table_name = 'tenants' AND column_name = 'byo_mode'"
    ).fetchone()
    assert row is not None
    return row[0]


class TestMigrationFile:
    def test_idempotent_touches_no_row_and_downgrade_restores(self, db_manager):
        run = _run_file()
        existing = db_manager.ensure_tenant(
            uuid4(), name="pre-existing", byo_mode="byo_required", owner_key="platform"
        )
        # NOT autocommit: every DDL here is rolled back, so the shared schema is untouched.
        conn = psycopg.connect(_libpq_dsn())
        try:
            run(conn, DOWN)
            assert _column_default(conn) == "'byo_required'::character varying"
            run(conn, UP)
            assert _column_default(conn) == "'local_allowed'::character varying"
            run(conn, UP)  # idempotent: a re-run is a no-op, never an error
            assert _column_default(conn) == "'local_allowed'::character varying"
            row = conn.execute(
                "SELECT byo_mode FROM tenants WHERE id = %s", (existing.id,)
            ).fetchone()
            assert row is not None and row[0] == "byo_required", "042 must not update rows"
        finally:
            conn.rollback()
            conn.close()

    @pytest.mark.parametrize("path", [UP, DOWN], ids=["up", "down"])
    def test_sql_has_no_dml(self, path: Path):
        body = "\n".join(
            ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("--")
        ).upper()
        for verb in ("UPDATE ", "INSERT ", "DELETE "):
            assert verb not in body, f"{path.name} must be metadata-only, found {verb!r}"
        assert "ALTER COLUMN BYO_MODE SET DEFAULT" in body


@pytest.fixture(autouse=True)
def _pre_050_writer_default(legacy_owner_default):  # noqa: F811
    """This file replays a pre-050 writer; see tests/support/legacy_owner_default.py."""
