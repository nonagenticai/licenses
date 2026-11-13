"""Migration 049: re-key the `cdmsfirst` demo tenant (owner ruling 2026-10-07).

Same structure and guarantees as 046 (this file mirrors that test): it moves
EVERYTHING or NOTHING. The difference from 046 is the data shape: no subject's
`keycloak_sub` is (or contains) the old id, so there is no user-sense occurrence
to preserve; every subject of this tenant is name/sub-based and moves by
`tenant_id`.

WHAT IS PINNED.
* Text-level: ONE top-level statement (the runner is autocommit per statement);
  the runner cannot auto-apply the downgrade; no statement names
  `license.metadata`.
* Run for real (Postgres; every test rolls back): the prod shape (tenant +
  4 licences, 3 active + 1 revoked; 3 subjects; 1 bundle grant; 1 token) lands on
  the new id with every tenant column and the name preserved; nothing references
  the old id; a bystander is untouched; `license.metadata` is byte-identical; a
  `svc:<old>:…` subject and a per-app byo row move too; re-run and absent tenant
  are no-ops; both ids present is refused; the closing assertion really aborts;
  the downgrade restores the exact prior state.
* Bootstrap through the REAL runner and seed: a prod-shaped database stamped
  through 048 is moved by exactly 049 and the seed does not re-create the old id.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import text

from models import PLATFORM_TENANTS
from scripts.apply_migrations import _run_file, _stamp, apply_all, split_statements
from tests.conftest import SEED_TENANT_ID, TEST_DATABASE_URL
from tests.support.legacy_owner_default import legacy_owner_default  # noqa: F401

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "migrations"
UP = MIGRATIONS / "049_rekey_cdmsfirst_demo_tenant.sql"
DOWN = MIGRATIONS / "downgrades" / "049_rekey_cdmsfirst_demo_tenant.sql"

# Literals on purpose: these two ids ARE the contract.
OLD = "0195f48b-d000-7000-8000-000000000100"
NEW = "01a1117c-d9b0-72fc-aeda-a9d1187f2a36"
NAME = "cdmsfirst"
NEW_SVC_SUB = f"svc:{NEW}:inference-router"
METADATA = '{"provisioned_by": "portal-bff", "note": "cdmsfirst pilot"}'

TABLES = ("tenants", "subject", "license", "tenant_bundle", "tenant_app_byo_mode", "token")


def _libpq_dsn() -> str:
    return TEST_DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1)


def _one(conn, sql: str, params=None):
    row = conn.execute(sql, params).fetchone()
    assert row is not None, sql
    return row[0]


def _dump(conn) -> dict[str, list]:
    return {
        t: _one(
            conn,
            f"SELECT coalesce(jsonb_agg(to_jsonb(x) ORDER BY to_jsonb(x)::text), '[]'::jsonb) "  # noqa: S608
            f"FROM {t} x",
        )
        for t in TABLES
    }


def _references(conn, tenant_id: str) -> dict[str, int]:
    q = {
        "tenants.id": "SELECT count(*) FROM tenants WHERE id = %s",
        "license.tenant_id": "SELECT count(*) FROM license WHERE tenant_id = %s",
        "subject.tenant_id": "SELECT count(*) FROM subject WHERE tenant_id = %s",
        "tenant_bundle.tenant_id": "SELECT count(*) FROM tenant_bundle WHERE tenant_id = %s",
        "tenant_app_byo_mode.tenant_id": (
            "SELECT count(*) FROM tenant_app_byo_mode WHERE tenant_id = %s"
        ),
    }
    out = {k: _one(conn, sql, (tenant_id,)) for k, sql in q.items()}
    out["subject.keycloak_sub svc:"] = _one(
        conn, "SELECT count(*) FROM subject WHERE keycloak_sub LIKE %s", (f"svc:{tenant_id}:%",)
    )
    return out


def _seed(conn, tenant_id: str = OLD, *, with_svc_subject: bool = False) -> dict[str, str]:
    """The prod shape of `cdmsfirst` (2026-10-07), plus a bystander on SEED_TENANT_ID.

    Every `tenants` column carries a NON-default value, so a column the migration
    forgot to copy comes back as its default and the comparison goes red.
    """
    keys = (
        "app_core",
        "app_router",
        "plan_core",
        "plan_router",
        "bundle",
        "subj_user_a",
        "subj_user_b",
        "subj_workers",
        "subj_svc",
        "subj_bystander",
        "lic_a",
        "lic_b",
        "lic_workers",
        "lic_revoked",
        "lic_bystander",
        "token",
    )
    ids = {k: str(uuid.uuid4()) for k in keys}
    conn.execute(
        "INSERT INTO tenants (id, name, keycloak_realm, byo_mode, archived_at, kind, "
        "cost_centre, budget_currency, created_at) VALUES "
        "(%s, %s, 'portal', 'platform_only', "
        "'2026-03-04T05:06:07+00', 'internal', 'ci', 'EUR', '2026-07-23T08:13:49+00')",
        (tenant_id, NAME),
    )
    for app, key in (("app_core", "core-app"), ("app_router", "inference-router")):
        conn.execute("INSERT INTO app (id, key, name) VALUES (%s, %s, %s)", (ids[app], key, key))
    for plan, app in (("plan_core", "app_core"), ("plan_router", "app_router")):
        conn.execute(
            "INSERT INTO plan (id, app_id, code, name) VALUES (%s, %s, 'full', 'Full')",
            (ids[plan], ids[app]),
        )
    conn.execute(
        "INSERT INTO bundle (id, key, name) VALUES (%s, 'inference-router', 'Inference Router')",
        (ids["bundle"],),
    )
    subjects: list[tuple[str, str, str | None, str]] = [
        ("subj_user_a", str(uuid.uuid4()), tenant_id, "portal"),
        ("subj_user_b", str(uuid.uuid4()), tenant_id, "portal"),
        ("subj_workers", "svc-workers-cdmsfirst", tenant_id, "service"),
        ("subj_bystander", f"svc:{SEED_TENANT_ID}:inference-router", SEED_TENANT_ID, "service"),
    ]
    if with_svc_subject:
        subjects.append(("subj_svc", f"svc:{tenant_id}:inference-router", tenant_id, "service"))
    for subj, sub, tid, realm in subjects:
        conn.execute(
            "INSERT INTO subject (id, keycloak_sub, keycloak_realm, tenant_id) "
            "VALUES (%s, %s, %s, %s)",
            (ids[subj], sub, realm, tid),
        )
    for lic, subj, app, plan, tid, bundle, status_ in (
        ("lic_a", "subj_user_a", "app_core", "plan_core", tenant_id, None, "active"),
        ("lic_b", "subj_user_b", "app_core", "plan_core", tenant_id, None, "active"),
        (
            "lic_workers",
            "subj_workers",
            "app_router",
            "plan_router",
            tenant_id,
            ids["bundle"],
            "active",
        ),
        ("lic_revoked", "subj_user_a", "app_router", "plan_router", tenant_id, None, "revoked"),
        (
            "lic_bystander",
            "subj_bystander",
            "app_router",
            "plan_router",
            SEED_TENANT_ID,
            None,
            "active",
        ),
    ):
        conn.execute(
            "INSERT INTO license (id, subject_id, app_id, plan_id, tenant_id, bundle_id, "
            "status, metadata, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, now())",
            (ids[lic], ids[subj], ids[app], ids[plan], tid, bundle, status_, METADATA),
        )
    conn.execute(
        "INSERT INTO token (id, license_id, token_hash, token_type) "
        "VALUES (%s, %s, 'deadbeef', 'api')",
        (ids["token"], ids["lic_workers"]),
    )
    conn.execute(
        "INSERT INTO tenant_bundle (tenant_id, bundle_id) VALUES (%s, %s)",
        (tenant_id, ids["bundle"]),
    )
    if with_svc_subject:
        conn.execute(
            "INSERT INTO tenant_app_byo_mode (tenant_id, app_id, byo_mode) "
            "VALUES (%s, %s, 'platform_only')",
            (tenant_id, ids["app_core"]),
        )
    return ids


def _tenant_without_id(conn, tenant_id: str):
    return _one(conn, "SELECT to_jsonb(t) - 'id' FROM tenants t WHERE id = %s", (tenant_id,))


@pytest.fixture
def conn(db_manager):
    c = psycopg.connect(_libpq_dsn())
    try:
        yield c
    finally:
        c.rollback()
        c.close()


class TestTheTextKeepsTheContract:
    def test_the_whole_rekey_is_one_statement_and_so_one_transaction(self):
        for path in (UP, DOWN):
            statements = split_statements(path.read_text(encoding="utf-8"))
            assert len(statements) == 1, path.name
            body = "\n".join(
                ln for ln in statements[0].splitlines() if not ln.lstrip().startswith("--")
            )
            assert body.lstrip().startswith("DO $$"), path.name

    def test_the_runner_cannot_auto_apply_the_downgrade(self):
        assert DOWN.exists()
        assert DOWN.resolve() not in {p.resolve() for p in MIGRATIONS.glob("*.sql")}

    def test_it_names_the_ruling(self):
        header = UP.read_text(encoding="utf-8").split("DO $$", 1)[0]
        assert "OWNER RULING 2026-10-07" in header
        assert "THIRD SANCTIONED EXCEPTION" in header

    def test_no_statement_touches_license_metadata(self):
        for path in (UP, DOWN):
            body = "\n".join(
                ln
                for ln in path.read_text(encoding="utf-8").split("DO $$", 1)[1].splitlines()
                if not ln.lstrip().startswith("--")
            )
            assert "metadata" not in body.lower(), path.name


class TestRunForReal:
    def test_everything_moves_to_the_new_id_and_nothing_is_left_on_the_old(self, conn):
        ids = _seed(conn)
        before = _tenant_without_id(conn, OLD)
        shape = {
            "tenants.id": 1,
            "license.tenant_id": 4,
            "subject.tenant_id": 3,
            "tenant_bundle.tenant_id": 1,
            "tenant_app_byo_mode.tenant_id": 0,
            "subject.keycloak_sub svc:": 0,
        }
        assert _references(conn, OLD) == shape, "positive control: the seed is the prod shape"
        token_before = _dump(conn)["token"]
        subs_before = {
            str(r[0]): r[1] for r in conn.execute("SELECT id, keycloak_sub FROM subject").fetchall()
        }

        _run_file(conn, UP)

        assert set(_references(conn, OLD).values()) == {0}
        assert _references(conn, NEW) == shape
        after = _tenant_without_id(conn, NEW)
        assert after == before
        assert after["name"] == NAME
        assert _one(conn, "SELECT count(*) FROM tenants WHERE name LIKE 'rekey-049-%'") == 0
        assert {
            str(r[0])
            for r in conn.execute("SELECT id FROM license WHERE tenant_id = %s", (NEW,)).fetchall()
        } == {ids["lic_a"], ids["lic_b"], ids["lic_workers"], ids["lic_revoked"]}
        assert _one(conn, "SELECT status FROM license WHERE id = %s", (ids["lic_revoked"],)) == (
            "revoked"
        )
        # No subject's sub changed (none embeds the id), only their tenant.
        assert {
            str(r[0]): r[1] for r in conn.execute("SELECT id, keycloak_sub FROM subject").fetchall()
        } == subs_before
        assert _dump(conn)["token"] == token_before
        assert (
            str(
                _one(
                    conn,
                    "SELECT l.tenant_id FROM token k JOIN license l ON l.id = k.license_id "
                    "WHERE k.id = %s",
                    (ids["token"],),
                )
            )
            == NEW
        )

    def test_license_metadata_is_byte_identical(self, conn):
        _seed(conn)
        before = [r[0] for r in conn.execute("SELECT metadata FROM license ORDER BY id")]
        _run_file(conn, UP)
        assert [r[0] for r in conn.execute("SELECT metadata FROM license ORDER BY id")] == before

    def test_a_svc_prefixed_subject_and_a_per_app_byo_row_move_too(self, conn):
        ids = _seed(conn, with_svc_subject=True)
        assert _references(conn, OLD)["subject.keycloak_sub svc:"] == 1
        assert _references(conn, OLD)["tenant_app_byo_mode.tenant_id"] == 1

        _run_file(conn, UP)

        assert set(_references(conn, OLD).values()) == {0}
        assert _references(conn, NEW)["tenant_app_byo_mode.tenant_id"] == 1
        assert _one(conn, "SELECT keycloak_sub FROM subject WHERE id = %s", (ids["subj_svc"],)) == (
            NEW_SVC_SUB
        )

    def test_a_bystander_tenant_is_untouched(self, conn):
        ids = _seed(conn)
        _run_file(conn, UP)
        assert _one(
            conn, "SELECT tenant_id::text FROM license WHERE id = %s", (ids["lic_bystander"],)
        ) == (SEED_TENANT_ID)
        assert _one(
            conn,
            "SELECT tenant_id::text || '|' || keycloak_sub FROM subject WHERE id = %s",
            (ids["subj_bystander"],),
        ) == (f"{SEED_TENANT_ID}|svc:{SEED_TENANT_ID}:inference-router")

    def test_reapplying_is_a_complete_noop(self, conn):
        _seed(conn)
        _run_file(conn, UP)
        once = _dump(conn)
        assert any(r["id"] == NEW for r in once["tenants"]), "control: the first run did the work"
        _run_file(conn, UP)
        assert _dump(conn) == once

    def test_a_database_without_the_tenant_is_a_complete_noop(self, conn):
        _seed(conn, tenant_id="5b0c9d3e-2f41-4a6b-9c7d-8e1f2a3b4c5d")
        before = _dump(conn)
        assert len(before["license"]) == 5, "control: the dump sees the seeded rows"
        _run_file(conn, UP)
        assert _dump(conn) == before
        assert _references(conn, NEW)["tenants.id"] == 0

    def test_both_ids_present_is_refused_not_guessed_at(self, conn):
        _seed(conn)
        conn.execute("INSERT INTO tenants (id, name) VALUES (%s, 'someone-else')", (NEW,))
        with pytest.raises(RuntimeError, match="049 REFUSED"):
            _run_file(conn, UP)

    def test_the_closing_assertion_really_aborts(self, conn, tmp_path):
        """The `svc:` rewrite is the one no FK backstops; drop it and only the
        assertion stands between prod and a stale `svc:<old>:` subject."""
        sql = UP.read_text(encoding="utf-8")
        needle = "SET keycloak_sub = new_svc || substr(keycloak_sub, length(old_svc) + 1)"
        assert sql.count(needle) == 1
        mutant = tmp_path / "049_mutant.sql"
        mutant.write_text(sql.replace(needle, "SET keycloak_sub = keycloak_sub"), encoding="utf-8")
        _seed(conn, with_svc_subject=True)
        with pytest.raises(RuntimeError, match="049 FAILED: 1 row"):
            _run_file(conn, mutant)

    def test_the_downgrade_restores_the_exact_prior_state(self, conn):
        _seed(conn, with_svc_subject=True)
        before = _dump(conn)
        _run_file(conn, UP)
        assert _dump(conn) != before, "control: the upgrade changed something"
        _run_file(conn, DOWN)
        assert _dump(conn) == before


def _cdmsfirst_rows(db_engine) -> list[tuple[str, str]]:
    with db_engine.connect() as c:
        return [
            (str(r[0]), str(r[1]))
            for r in c.execute(
                text("SELECT id, name FROM tenants WHERE id = ANY(:ids) OR name = :n ORDER BY 1"),
                {"ids": [OLD, NEW], "n": NAME},
            ).fetchall()
        ]


class TestBootstrap:
    def test_no_seed_names_either_id(self):
        assert {OLD, NEW}.isdisjoint({tid for tid, _n, _b in PLATFORM_TENANTS})
        assert NAME not in {n for _t, n, _b in PLATFORM_TENANTS}

    def test_a_prod_shaped_database_is_moved_by_the_runner_and_not_reseeded(
        self, db_manager, db_engine
    ):
        """Prod today: every file through 048 stamped, the tenant on the OLD id. One
        boot of the new image: the runner applies exactly 049, then the seed runs."""
        with psycopg.connect(_libpq_dsn(), autocommit=True) as c:
            ids = _seed(c)
            c.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            for path in sorted(MIGRATIONS.glob("*.sql")):
                if path.name != UP.name:
                    _stamp(c, path.name)
        assert _cdmsfirst_rows(db_engine) == [(OLD, NAME)], "control: prod shape"

        assert apply_all(_libpq_dsn()) == 0
        db_manager.seed_platform_tenants()

        assert _cdmsfirst_rows(db_engine) == [(NEW, NAME)]
        with db_engine.connect() as c:
            assert (
                str(
                    c.execute(
                        text(
                            "SELECT l.tenant_id FROM token k JOIN license l "
                            "ON l.id = k.license_id WHERE k.id = :t"
                        ),
                        {"t": ids["token"]},
                    ).scalar_one()
                )
                == NEW
            )
        assert apply_all(_libpq_dsn()) == 0
        assert db_manager.seed_platform_tenants() == 0
        assert _cdmsfirst_rows(db_engine) == [(NEW, NAME)]


@pytest.fixture(autouse=True)
def _pre_050_writer_default(legacy_owner_default):  # noqa: F811
    """This file replays a pre-050 writer; see tests/support/legacy_owner_default.py."""
