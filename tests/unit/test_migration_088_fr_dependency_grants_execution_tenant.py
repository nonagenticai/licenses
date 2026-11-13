"""Migration 088: fr may mint ``workers`` / ``secrets`` / ``skill-builder`` on the execution tenant.

2026-11-06. 087 revoked the 33 ``tenant_app_grant`` rows migration 050's backfill had
given fr on tenants it never operated. That was right for fr's PROVISIONING reach, but
fr also needs the tenant's own licences at RUNTIME: it presents
``Authorization: Bearer <tenant workers licence>`` to worker_management so WM can PROVE
the tenant before minting an inference-router bundle token
(``src/auth/tenant_proof.py``). Without an entitlement to mint, fr fell back to its
delegation pair and WM refused the router call
(``wm_ir_bundle_token outcome=tenant_not_proven reason=delegated_without_verified_end_user_token``)
— the fr chat page in ``nonagentic-exec`` listed no models.

088 writes the SAME app-scoped ``tenant_dependency_grant`` shape fr already holds on
fr-test-user and the conveyor tenant (065/066), on ONE tenant, for three apps. Run for
REAL against a database built FROM EMPTY (init.sql -> the genesis shim -> 001..087):

* with no bundle held, nothing is granted — the guard is not vacuous;
* the corpus seeds ``non-agentic-ai`` with NO members (memberships are ``ensure``'s),
  so the grant needs the app to be a member of a non-revoked bundle the tenant HOLDS —
  exactly the condition ``main.py`` enforces when the mint runs;
* a re-run is a no-op (PK (tenant_id, owner_key, app_id));
* an app that is not a member of a held bundle is skipped;
* another owner's grant on the same tenant is never touched by up OR down;
* the downgrade removes ONLY ``migration-088`` rows.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from scripts.build_schema_arms import (
    INIT_SQL,
    SHIM_SQL,
    _drop_database,
    _recreate_database,
    _run_sql_file,
)
from tests.conftest import TEST_DATABASE_URL

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "migrations"
NAME = "088_fr_dependency_grants_execution_tenant.sql"
UP = MIGRATIONS / NAME
DOWN = MIGRATIONS / "downgrades" / NAME
DB = "licenses_m088_test"

TENANT = "0195f48b-d000-7000-8000-000000000003"  # execution tenant
APPS = ("workers", "secrets", "skill-builder")
GRANTED_BY = "migration-088"


def _libpq_dsn() -> str:
    return TEST_DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1)


@pytest.fixture(scope="module")
def conn() -> Iterator[psycopg.Connection]:
    """A database built from EMPTY through 087 — 088 is applied by each test."""
    dsn = _libpq_dsn()
    arm = _recreate_database(dsn, DB)
    connection = psycopg.connect(arm, autocommit=True)
    try:
        _run_sql_file(connection, INIT_SQL, "genesis")
        _run_sql_file(connection, SHIM_SQL, "shim")
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name == NAME:
                continue
            _run_sql_file(connection, path, path.name)
        yield connection
    finally:
        connection.close()
        _drop_database(dsn, DB)


def _reset(c: psycopg.Connection) -> None:
    """Known starting point: no fr grant on TENANT, TENANT holds no bundle,
    `non-agentic-ai` has no members. Rows belonging to other owners/tenants are
    left alone — they are what `test_another_owners_grant_is_untouched` reads."""
    c.execute(
        "DELETE FROM tenant_dependency_grant WHERE owner_key = 'fr' AND tenant_id = %s", (TENANT,)
    )
    c.execute("DELETE FROM tenant_bundle WHERE tenant_id = %s", (TENANT,))
    c.execute(
        "DELETE FROM bundle_app WHERE bundle_id = (SELECT id FROM bundle WHERE key = 'non-agentic-ai')"
    )


def _seed_apps(c: psycopg.Connection) -> None:
    """The three apps are NOT in the migration corpus — a from-empty database has
    no `workers` / `secrets` / `skill-builder` row at all (apps and bundle
    memberships are created by `ensure` / live provisioning, not by migrations).
    Seed them, so the positive path is exercised against a real schema."""
    for app in APPS:
        c.execute(
            "INSERT INTO app (key, name, description, audience) "
            "VALUES (%s, %s, '', 'automation') ON CONFLICT DO NOTHING",
            (app, app),
        )


def _seed(c: psycopg.Connection, apps: tuple[str, ...] = APPS) -> None:
    """Make TENANT hold `non-agentic-ai` carrying `apps`."""
    _seed_apps(c)
    c.execute(
        "INSERT INTO tenant_bundle (tenant_id, bundle_id) "
        "SELECT %s, id FROM bundle WHERE key = 'non-agentic-ai' "
        "ON CONFLICT DO NOTHING",
        (TENANT,),
    )
    for app in apps:
        c.execute(
            "INSERT INTO bundle_app (bundle_id, app_id) "
            "SELECT b.id, a.id FROM bundle b, app a "
            "WHERE b.key = 'non-agentic-ai' AND a.key = %s AND NOT a.revoked "
            "ON CONFLICT DO NOTHING",
            (app,),
        )


def _grants(c: psycopg.Connection) -> list[tuple[str, str, str]]:
    """Every dependency grant ON TENANT, whatever the owner."""
    return [
        (r[0], r[1], r[2])
        for r in c.execute(
            "SELECT a.key, d.owner_key, d.granted_by FROM tenant_dependency_grant d "
            "JOIN app a ON a.id = d.app_id WHERE d.tenant_id = %s ORDER BY 1, 2",
            (TENANT,),
        ).fetchall()
    ]


def _apply_up(c: psycopg.Connection) -> None:
    _run_sql_file(c, UP, NAME)


def _apply_down(c: psycopg.Connection) -> None:
    _run_sql_file(c, DOWN, f"downgrades/{NAME}")


def test_no_bundle_held_grants_nothing(conn: psycopg.Connection) -> None:
    """The guard, not a blanket insert: the apps exist, the tenant holds no bundle."""
    _reset(conn)
    _seed_apps(conn)
    _apply_up(conn)
    assert _grants(conn) == []


def test_grants_exactly_the_three_apps_the_tenant_can_mint(conn: psycopg.Connection) -> None:
    _reset(conn)
    _seed(conn)
    _apply_up(conn)
    assert set(_grants(conn)) == {(app, "fr", GRANTED_BY) for app in APPS}


def test_rerun_is_a_noop(conn: psycopg.Connection) -> None:
    _reset(conn)
    _seed(conn)
    _apply_up(conn)
    _apply_up(conn)
    assert set(_grants(conn)) == {(app, "fr", GRANTED_BY) for app in APPS}


def test_app_that_is_not_a_member_of_a_held_bundle_is_skipped(conn: psycopg.Connection) -> None:
    """`skill-builder` outside the bundle is exactly the 403 `no_bundle_held` case."""
    _reset(conn)
    _seed(conn, ("workers", "secrets"))
    _apply_up(conn)
    assert set(_grants(conn)) == {(app, "fr", GRANTED_BY) for app in ("workers", "secrets")}


def test_another_owners_grant_is_untouched_by_up_and_down(conn: psycopg.Connection) -> None:
    _reset(conn)
    _seed(conn)
    conn.execute(
        "INSERT INTO tenant_dependency_grant (tenant_id, owner_key, app_id, granted_by) "
        "SELECT %s, 'platform', id, 'somebody-else' FROM app WHERE key = 'workers'",
        (TENANT,),
    )
    _apply_up(conn)
    assert ("workers", "platform", "somebody-else") in _grants(conn)

    _apply_down(conn)
    assert _grants(conn) == [("workers", "platform", "somebody-else")]


def test_downgrade_removes_only_what_088_wrote(conn: psycopg.Connection) -> None:
    _reset(conn)
    _seed(conn)
    _apply_up(conn)
    assert {g for g in _grants(conn) if g[1] == "fr"} == {(app, "fr", GRANTED_BY) for app in APPS}

    _apply_down(conn)
    assert [g for g in _grants(conn) if g[1] == "fr"] == []
    # Idempotent: a second downgrade deletes nothing and raises nothing.
    _apply_down(conn)
    assert [g for g in _grants(conn) if g[1] == "fr"] == []
