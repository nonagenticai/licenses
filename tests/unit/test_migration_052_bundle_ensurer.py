"""Migration 052: the per-bundle ensurer allowlist (``bundle_ensurer``).

Run for REAL against a database built FROM EMPTY (init.sql -> the genesis shim ->
001..051, so the bundles 028 / 040 / 051 seed exist), then 052 is applied:

* every bundle gets exactly the ensurers ``models.BUNDLE_ENSURER_SEED`` names, and
  the SQL seed and that map are the same table;
* ``commonpractice`` is CommonPractice's alone; no bundle is left without an ensurer;
* a re-run changes nothing; a bundle absent on the database seeds nothing;
* the downgrade drops the table and un-stamps 052; up -> down(052) -> down(051) ->
  up through the REAL runner re-applies 051 AND 052.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from models import BUNDLE_ENSURER_SEED
from scripts.apply_migrations import split_statements
from scripts.build_schema_arms import (
    INIT_SQL,
    SHIM_SQL,
    _drop_database,
    _recreate_database,
    _run_sql_file,
)
from tests.conftest import TEST_DATABASE_URL
from tests.unit.test_migration_057_kag_owner import M057_BUNDLE_ENSURERS
from tests.unit.test_migration_059_app_requires import M059_BUNDLE_ENSURERS
from tests.unit.test_migration_063_kag_service_bundle import M063_BUNDLE_ENSURERS
from tests.unit.test_migration_078_care_bus_early_access import M078_BUNDLE_ENSURERS
from tests.unit.test_migration_082_mcp_toolkit_entitlements import (
    M082_BUNDLE_ENSURERS,
)

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "migrations"
NAME = "052_bundle_ensurer.sql"
UP = MIGRATIONS / NAME
DOWN = MIGRATIONS / "downgrades" / NAME
M051 = "051_n8n_nodes_bundle_and_retire_legacy_netcare.sql"
DB = "licenses_m052_test"
RUNNER_DB = "licenses_m052_runner_test"
M057 = "057_kag_owner_and_kag_research_bundle.sql"
M059 = "059_app_requires_and_conveyor_bundle.sql"
M060 = "060_kag_frontend_bundle_minter_owner.sql"
M061 = "061_kag_bundle_minter_owner.sql"
M062 = "062_evidence_review_app.sql"
M063 = "063_kag_service_bundle.sql"
M064 = "064_karma_bundle_minter_owner.sql"
M065 = "065_secrets_in_conveyor_bundle.sql"
M066 = "066_skill_builder_in_conveyor_bundle.sql"
# The 052 seed: models.BUNDLE_ENSURER_SEED minus the bundles later migrations add.
SEED_052 = {
    k: set(v)
    for k, v in BUNDLE_ENSURER_SEED.items()
    if k not in M057_BUNDLE_ENSURERS
    and k not in M059_BUNDLE_ENSURERS
    and k not in M063_BUNDLE_ENSURERS
    and k not in M078_BUNDLE_ENSURERS
    and k not in M082_BUNDLE_ENSURERS
}


def _libpq_dsn() -> str:
    return TEST_DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1)


def _build_before(dsn: str, name: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as c:
        _run_sql_file(c, INIT_SQL, "genesis")
        _run_sql_file(c, SHIM_SQL, "shim")
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name < name:
                _run_sql_file(c, path, "corpus")


@pytest.fixture
def conn() -> Iterator[psycopg.Connection]:
    """A database at the exact 051 state, built from empty."""
    dsn = _recreate_database(_libpq_dsn(), DB)
    try:
        _build_before(dsn, NAME)
        with psycopg.connect(dsn, autocommit=True) as c:
            yield c
    finally:
        _drop_database(_libpq_dsn(), DB)


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


def _sql_seed() -> dict[str, set[str]]:
    body = UP.read_text(encoding="utf-8").split("BEGIN;", 1)[1]
    block = body.split("FROM (VALUES", 1)[1].split(") AS s(bundle_key, owner_key)", 1)[0]
    out: dict[str, set[str]] = {}
    for bundle, owner in re.findall(r"\('([a-z0-9-]+)', '([a-z0-9-]+)'\)", block):
        out.setdefault(bundle, set()).add(owner)
    return out


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

    def test_sql_seed_is_the_models_map(self):
        assert _sql_seed() == SEED_052

    def test_commonpractice_is_commonpractices_alone(self):
        assert _sql_seed()["commonpractice"] == {"commonpractice"}
        assert _sql_seed()["n8n-nodes"] == {"commonpractice", "platform", "fr"}
        assert "fr" not in _sql_seed()["w2v"]

    def test_header_names_every_exception_and_the_rejection(self):
        header = UP.read_text(encoding="utf-8").split("BEGIN;", 1)[0]
        for needle in (
            "CROSS-APP EXCEPTIONS",
            "commonpractice -> knowledge",
            "merqi -> w2v",
            "platform -> non-agentic-ai",
            "REJECTED: fr -> w2v",
            "LICENCE_BUNDLE_KEYS",
            "tenant-licence-bundles.ts",
        ):
            assert needle in header, needle


class TestRunForReal:
    def test_every_bundle_gets_its_seeded_ensurers(self, conn):
        _run_sql_file(conn, UP, NAME)
        got = _ensurers(conn)
        assert got == SEED_052
        assert all(got.values()), "a bundle was left with no ensurer"

    def test_rerun_is_a_no_op(self, conn):
        _run_sql_file(conn, UP, NAME)
        once = _ensurers(conn)
        _run_sql_file(conn, UP, NAME)
        assert _ensurers(conn) == once

    def test_an_absent_bundle_seeds_nothing(self, conn):
        conn.execute(
            "DELETE FROM bundle_app WHERE bundle_id = (SELECT id FROM bundle WHERE key = 'n8n-nodes')"
        )
        conn.execute("DELETE FROM bundle WHERE key = 'n8n-nodes'")
        _run_sql_file(conn, UP, NAME)
        assert "n8n-nodes" not in _ensurers(conn)

    def test_deleting_a_bundle_cascades_its_ensurers(self, conn):
        _run_sql_file(conn, UP, NAME)
        conn.execute(
            "DELETE FROM bundle_app WHERE bundle_id = (SELECT id FROM bundle WHERE key = 'w2v')"
        )
        conn.execute("DELETE FROM bundle WHERE key = 'w2v'")
        n = conn.execute(
            "SELECT count(*) FROM bundle_ensurer e LEFT JOIN bundle b ON b.id = e.bundle_id "
            "WHERE b.id IS NULL"
        ).fetchone()
        assert n is not None and n[0] == 0


class TestDowngrade:
    def test_drops_the_table(self, conn):
        _run_sql_file(conn, UP, NAME)
        _run_sql_file(conn, DOWN, "down")
        row = conn.execute("SELECT to_regclass('public.bundle_ensurer')").fetchone()
        assert row is not None and row[0] is None


class TestRunnerCycle:
    """up -> down 066 -> down 065 -> down 064 -> down 063 -> down 062 -> down 061 -> down 060 -> down 059 -> down 057
    -> down 052 -> down 051 -> up through the REAL runner: every downgrade deletes its stamp,
    so the runner applies 051, 052, 057, 059, 060, 061, 062, 063, 064, 065 and 066 again, in order."""

    def test_up_down_up_through_051_and_052(self):
        from scripts.apply_migrations import apply_all

        dsn = _recreate_database(_libpq_dsn(), RUNNER_DB)
        try:
            _build_before(dsn, M051)
            with psycopg.connect(dsn, autocommit=True) as c:
                c.execute(
                    "CREATE TABLE schema_migrations (filename TEXT PRIMARY KEY, "
                    "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
                )
                for path in sorted(MIGRATIONS.glob("*.sql")):
                    if path.name < M051:
                        c.execute(
                            "INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,)
                        )

            def state() -> tuple[set[str], dict[str, set[str]] | None]:
                with psycopg.connect(dsn, autocommit=True) as c:
                    stamped = {
                        r[0]
                        for r in c.execute(
                            "SELECT filename FROM schema_migrations WHERE filename = ANY(%s)",
                            ([M051, NAME, M057, M059, M060, M061, M062, M063, M064, M065, M066],),
                        ).fetchall()
                    }
                    has = c.execute("SELECT to_regclass('public.bundle_ensurer')").fetchone()
                    return stamped, (_ensurers(c) if has and has[0] else None)

            # The runner applies everything after 051, so 057's kag-research, 059's
            # conveyor, 060's / 061's kag minters, 062's evidence-review and 063's
            # kag-service are there too; they are downgraded first (newest first).
            seeded = {k: set(v) for k, v in BUNDLE_ENSURER_SEED.items()}
            assert apply_all(dsn) == 0
            assert state() == (
                {M051, NAME, M057, M059, M060, M061, M062, M063, M064, M065, M066},
                seeded,
            )
            with psycopg.connect(dsn, autocommit=True) as c:
                # 080's laya edge + licences, 079's licences on fr-test-user, then
                # 078's care-bus bundle (newest first).
                _run_sql_file(
                    c, MIGRATIONS / "downgrades" / "082_mcp_toolkit_entitlements.sql", "down 082"
                )
                _run_sql_file(c, MIGRATIONS / "downgrades" / "080_care_bus_laya.sql", "down 080")
                _run_sql_file(
                    c,
                    MIGRATIONS / "downgrades" / "079_fr_test_user_care_bus_bundle.sql",
                    "down 079",
                )
                _run_sql_file(
                    c, MIGRATIONS / "downgrades" / "078_care_bus_early_access.sql", "down 078"
                )
                _run_sql_file(c, MIGRATIONS / "downgrades" / M066, "down 066")
                _run_sql_file(c, MIGRATIONS / "downgrades" / M065, "down 065")
                _run_sql_file(c, MIGRATIONS / "downgrades" / M064, "down 064")
                _run_sql_file(c, MIGRATIONS / "downgrades" / M063, "down 063")
                _run_sql_file(c, MIGRATIONS / "downgrades" / M062, "down 062")
                _run_sql_file(c, MIGRATIONS / "downgrades" / M061, "down 061")
                _run_sql_file(c, MIGRATIONS / "downgrades" / M060, "down 060")
                _run_sql_file(c, MIGRATIONS / "downgrades" / M059, "down 059")
                _run_sql_file(c, MIGRATIONS / "downgrades" / M057, "down 057")
                _run_sql_file(c, DOWN, "down 052")
                _run_sql_file(c, MIGRATIONS / "downgrades" / M051, "down 051")
            assert state() == (set(), None)
            assert apply_all(dsn) == 0
            assert state() == (
                {M051, NAME, M057, M059, M060, M061, M062, M063, M064, M065, M066},
                seeded,
            )
        finally:
            _drop_database(_libpq_dsn(), RUNNER_DB)
