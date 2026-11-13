"""Migration 058: new tenants default to byo_mode 'byo_required' (licenses#273).

Owner ruling 2026-10-15: platform Mac GPU services are only for the owner's EXISTING
tenants; every new early-access tenant brings its own API tokens. So a tenant created
without an explicit byo_mode is 'byo_required', reversing migration 042's
'local_allowed'. Pinned on every creation path: the SQL column default, the ORM,
``create_tenant`` / ``ensure_tenant``, and both HTTP routes (POST /tenants,
PUT /tenants/{id}). Explicit values are still honoured, and a kind='service' tenant
with no byo_mode is still born 'local_allowed' (migration 033). The migration touches
NO existing row (byo_mode counts identical before/after), is idempotent, and its
downgrade restores 'local_allowed'.
"""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from models import Tenant
from tests.conftest import TEST_DATABASE_URL

REPO = Path(__file__).resolve().parents[2]
UP = REPO / "migrations" / "058_tenants_byo_mode_default_byo_required.sql"
DOWN = REPO / "migrations" / "downgrades" / "058_tenants_byo_mode_default_byo_required.sql"


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


def _counts(conn) -> dict[str, int]:
    return dict(conn.execute("SELECT byo_mode, count(*) FROM tenants GROUP BY byo_mode").fetchall())


class TestNewTenantDefault:
    def test_sql_insert_without_byo_mode_gets_byo_required(self, db_manager):
        conn = psycopg.connect(_libpq_dsn())
        try:
            assert _column_default(conn) == "'byo_required'::character varying"
            got = conn.execute(
                "INSERT INTO tenants (id, name, owner_key) "
                "VALUES (%s, 'sql-default', 'platform') RETURNING byo_mode",
                (uuid4(),),
            ).fetchone()
            assert got is not None and got[0] == "byo_required"
        finally:
            conn.rollback()
            conn.close()

    def test_orm_and_create_tenant_default(self, db_manager):
        assert Tenant.__table__.c.byo_mode.server_default.arg == "byo_required"
        assert (
            db_manager.create_tenant(name="orm-default", owner_key="platform").byo_mode
            == "byo_required"
        )
        assert (
            db_manager.ensure_tenant(uuid4(), name="ensure-default", owner_key="platform").byo_mode
            == "byo_required"
        )

    def test_explicit_values_are_still_honoured(self, db_manager):
        for mode in ("byo_required", "platform_only", "local_allowed"):
            t = db_manager.ensure_tenant(
                uuid4(),
                name=f"explicit-{mode.replace('_', '-')}",
                byo_mode=mode,
                owner_key="platform",
            )
            assert t.byo_mode == mode

    def test_service_tenant_without_byo_mode_stays_local_allowed(self, db_manager):
        """kind='service' + byo_required is forbidden (033); the primitive keeps it coherent."""
        t = db_manager.ensure_tenant(
            uuid4(), name="svc-default", kind="service", owner_key="platform"
        )
        assert t.byo_mode == "local_allowed"

    def test_post_tenants_route_defaults_to_byo_required(self, auth_client_admin: TestClient):
        resp = auth_client_admin.post("/tenants", json={"name": "route-default"})
        assert resp.status_code == 201
        assert resp.json()["byo_mode"] == "byo_required"

    def test_put_tenants_route_defaults_to_byo_required(self, auth_client_admin: TestClient):
        resp = auth_client_admin.put(f"/tenants/{uuid4()}", json={"name": "register-default"})
        assert resp.status_code == 201
        assert resp.json()["byo_mode"] == "byo_required"


class TestMigrationFile:
    def test_idempotent_touches_no_row_and_downgrade_restores(self, db_manager):
        run = _run_file()
        # Existing rows of every posture, created BEFORE 058 runs (default = 042's).
        ids = {
            mode: db_manager.ensure_tenant(
                uuid4(), name=f"pre-{mode.replace('_', '-')}", byo_mode=mode, owner_key="platform"
            ).id
            for mode in ("local_allowed", "byo_required", "platform_only")
        }
        # NOT autocommit: every DDL here is rolled back, so the shared schema is untouched.
        conn = psycopg.connect(_libpq_dsn())
        try:
            run(conn, DOWN)
            assert _column_default(conn) == "'local_allowed'::character varying"
            # A row born under the 042 default, before 058 runs.
            legacy = conn.execute(
                "INSERT INTO tenants (id, name, owner_key) "
                "VALUES (%s, 'pre-058-default', 'platform') RETURNING id, byo_mode",
                (uuid4(),),
            ).fetchone()
            assert legacy is not None and legacy[1] == "local_allowed"
            before = _counts(conn)

            run(conn, UP)
            assert _column_default(conn) == "'byo_required'::character varying"
            run(conn, UP)  # idempotent: a re-run is a no-op, never an error
            assert _column_default(conn) == "'byo_required'::character varying"

            assert _counts(conn) == before, "058 must not rewrite any existing row"
            for mode, tid in ids.items():
                row = conn.execute("SELECT byo_mode FROM tenants WHERE id = %s", (tid,)).fetchone()
                assert row is not None and row[0] == mode
            row = conn.execute(
                "SELECT byo_mode FROM tenants WHERE id = %s", (legacy[0],)
            ).fetchone()
            assert row is not None and row[0] == "local_allowed"
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
