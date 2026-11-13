"""Tests for scripts/apply_migrations.py — the startup SQL migration runner."""

from __future__ import annotations

import pytest

from scripts.apply_migrations import apply_all, split_statements
from tests.conftest import TEST_DATABASE_URL
from tests.support.legacy_owner_default import legacy_owner_default  # noqa: F401


class TestSplitStatements:
    def test_simple_split(self):
        assert split_statements("SELECT 1; SELECT 2;") == ["SELECT 1", "SELECT 2"]

    def test_trailing_statement_without_semicolon(self):
        assert split_statements("SELECT 1") == ["SELECT 1"]

    def test_semicolon_in_single_quoted_string_is_not_a_split(self):
        stmts = split_statements("INSERT INTO t VALUES ('a;b'); SELECT 1;")
        assert stmts == ["INSERT INTO t VALUES ('a;b')", "SELECT 1"]

    def test_dollar_quoted_block_keeps_inner_semicolons(self):
        sql = (
            "DO $$ BEGIN IF x THEN RAISE EXCEPTION 'no; go'; END IF; END $$;\n"
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS i ON t (c);"
        )
        stmts = split_statements(sql)
        assert len(stmts) == 2
        assert stmts[0].startswith("DO $$")
        assert "END $$" in stmts[0]
        # The CONCURRENTLY statement is isolated so it can run outside a txn.
        assert stmts[1].startswith("CREATE INDEX CONCURRENTLY")

    def test_line_and_block_comments_dropped_and_not_split(self):
        sql = "-- a; comment\n/* b; comment */\nSELECT 1;\n-- trailing only\n"
        assert split_statements(sql) == ["-- a; comment\n/* b; comment */\nSELECT 1"]

    def test_comment_only_fragment_yields_nothing(self):
        assert split_statements("-- just a comment\n") == []

    def test_named_dollar_tag(self):
        sql = "DO $body$ BEGIN PERFORM 1; END $body$;"
        assert split_statements(sql) == ["DO $body$ BEGIN PERFORM 1; END $body$"]


@pytest.mark.usefixtures("legacy_owner_default")  # replays pre-050 writers
class TestApplyAll:
    """Runs the real migrations/*.sql against the test DB (needs Postgres)."""

    def _dsn(self) -> str:
        return TEST_DATABASE_URL.replace("+psycopg", "")

    def _drop_tracking(self, db_engine) -> None:
        from sqlalchemy import text

        with db_engine.begin() as c:
            c.execute(text("DROP TABLE IF EXISTS schema_migrations"))

    def test_baseline_stamps_existing_schema_without_running(self, db_engine):
        """db_engine create_all'd the full schema (offering exists), so the first
        run BASELINES: every file is stamped applied, none is re-run, and the
        migrated columns are present (from create_all). Idempotent on re-run."""
        from sqlalchemy import inspect, text

        self._drop_tracking(db_engine)
        assert apply_all(self._dsn(), sslmode="disable") == 0
        assert apply_all(self._dsn(), sslmode="disable") == 0  # re-run = no-op

        with db_engine.connect() as c:
            stamped = c.execute(text("SELECT count(*) FROM schema_migrations")).scalar_one()
        from scripts.apply_migrations import MIGRATIONS_DIR

        expected = len(list(MIGRATIONS_DIR.glob("*.sql")))
        assert expected >= 12  # guard: all .sql files must be tracked (git add -f)
        assert stamped == expected  # every current file baselined

        cols = {c["name"] for c in inspect(db_engine).get_columns("offering")}
        assert {"access_price_usd", "access_cadence", "included_allowance_usd"} <= cols
        assert "byo_mode" in {c["name"] for c in inspect(db_engine).get_columns("tenants")}

    def test_baseline_does_not_replay_006_against_platform_only_row(self, db_engine):
        """Regression for the prod CrashLoopBackOff: a tenant with byo_mode
        'platform_only' (enabled by 007) violates 006's NARROW ck_tenants_byo_mode
        CHECK. Re-running 006 would crash; the baseline must NOT replay it."""
        import uuid

        from sqlalchemy import text

        self._drop_tracking(db_engine)
        with db_engine.begin() as c:
            c.execute(
                text(
                    "INSERT INTO tenants (id, name, byo_mode, created_at) "
                    "VALUES (:i, :n, 'platform_only', now())"
                ),
                {"i": str(uuid.uuid4()), "n": f"plat-{uuid.uuid4().hex[:8]}"},
            )
        # Must NOT crash on the narrow-CHECK replay — baseline skips 006.
        assert apply_all(self._dsn(), sslmode="disable") == 0

    def test_new_migration_after_baseline_runs_once(self, db_engine, tmp_path):
        """After baseline, a NEW file runs exactly once — the runner's real job."""
        from sqlalchemy import text

        self._drop_tracking(db_engine)
        # First run baselines the initial set (offering exists → stamped, not run).
        (tmp_path / "001_init.sql").write_text("CREATE TABLE IF NOT EXISTS wsg_a (id int);")
        assert apply_all(self._dsn(), migrations_dir=tmp_path, sslmode="disable") == 0
        # A new migration appears → it RUNS now (tracking table is non-empty).
        (tmp_path / "002_add.sql").write_text("CREATE TABLE IF NOT EXISTS wsg_b (id int);")
        assert apply_all(self._dsn(), migrations_dir=tmp_path, sslmode="disable") == 0
        with db_engine.connect() as c:
            a = c.execute(text("SELECT to_regclass('public.wsg_a')")).scalar_one()
            b = c.execute(text("SELECT to_regclass('public.wsg_b')")).scalar_one()
        assert a is None  # baselined initial file never ran
        assert b is not None  # the new migration ran
        with db_engine.begin() as c:
            c.execute(text("DROP TABLE IF EXISTS wsg_b"))


@pytest.mark.usefixtures("legacy_owner_default")  # replays pre-050 writers
class TestSeedRenderE2ETenant:
    """013_seed_render_e2e_tenant.sql — restores the render/E2E service-account row.

    Regression for the WM 402 (byo_llm_not_configured): migration 004 deleted this
    account's name==uuid row, so the token-path BYO lookup fail-closed to byo_required.
    """

    RENDER_E2E_ID = "e148e8af-3751-49bf-a0fa-56ab46d31ddc"

    def _statements(self):
        from scripts.apply_migrations import MIGRATIONS_DIR

        sql = (MIGRATIONS_DIR / "013_seed_render_e2e_tenant.sql").read_text()
        # split_statements prepends the leading comment block to the first real
        # statement, so filtering by a startswith("INSERT") prefix drops it — just
        # run every statement (the trailing verify SELECT is a harmless read).
        return split_statements(sql)

    def _run(self, db_engine):
        from sqlalchemy import text

        for stmt in self._statements():
            with db_engine.begin() as c:
                c.execute(text(stmt))

    def _row(self, db_engine):
        from sqlalchemy import text

        with db_engine.connect() as c:
            return c.execute(
                text("SELECT name, byo_mode FROM tenants WHERE id = :i"),
                {"i": self.RENDER_E2E_ID},
            ).first()

    def test_seeds_render_e2e_tenant_as_local_allowed(self, db_engine):
        """From an absent row (the prod-broken state), the seed creates it correctly."""
        assert self._row(db_engine) is None  # db_engine starts clean
        self._run(db_engine)
        row = self._row(db_engine)
        assert row is not None
        assert row.name == "portal-render-e2e"  # friendly name → immune to 004's delete
        assert row.byo_mode == "local_allowed"

    def test_is_idempotent_and_self_asserting(self, db_engine):
        """Re-running is a no-op; a stale/wrong row is corrected to the right posture."""
        from sqlalchemy import text

        # Pre-seed a stale row: the exact bad state 004 leaves behind if it lingers —
        # name == uuid, byo_mode == byo_required.
        with db_engine.begin() as c:
            c.execute(
                text(
                    "INSERT INTO tenants (id, name, byo_mode, created_at) "
                    "VALUES (:i, :n, 'byo_required', now())"
                ),
                {"i": self.RENDER_E2E_ID, "n": self.RENDER_E2E_ID},
            )
        self._run(db_engine)  # ON CONFLICT DO UPDATE fixes name + posture
        row = self._row(db_engine)
        assert row.name == "portal-render-e2e"
        assert row.byo_mode == "local_allowed"

        self._run(db_engine)  # second apply is a clean no-op
        with db_engine.connect() as c:
            count = c.execute(
                text("SELECT count(*) FROM tenants WHERE id = :i"), {"i": self.RENDER_E2E_ID}
            ).scalar_one()
        assert count == 1


@pytest.mark.usefixtures("legacy_owner_default")  # replays pre-050 writers
class TestSeedPlatformInternalTenants:
    """014_seed_platform_internal_tenants.sql — restores the rest of the real tenants.

    Migration 004 wiped every name==uuid row (~39 of 52) on 2026-07-09; 014 seeds all
    known platform/internal/commercial tenants with friendly (004-immune) names.
    """

    # A representative missing tenant of each posture (all wiped by 004).
    FR_TEST_USER = "0195f48b-d000-7000-8000-000000000099"  # local_allowed
    SYSTEM_TENANT = "00000000-0000-0000-0000-000000000001"  # platform_only

    def _run(self, db_engine):
        from sqlalchemy import text

        from scripts.apply_migrations import MIGRATIONS_DIR

        sql = (MIGRATIONS_DIR / "014_seed_platform_internal_tenants.sql").read_text()
        for stmt in split_statements(sql):
            with db_engine.begin() as c:
                c.execute(text(stmt))

    def _row(self, db_engine, tid):
        from sqlalchemy import text

        with db_engine.connect() as c:
            return c.execute(
                text("SELECT name, byo_mode FROM tenants WHERE id = :i"), {"i": tid}
            ).first()

    def test_seeds_missing_tenants_with_correct_posture_and_safe_names(self, db_engine):
        self._run(db_engine)

        fr = self._row(db_engine, self.FR_TEST_USER)
        assert fr is not None and fr.byo_mode == "local_allowed"
        assert fr.name == "fr-test-user" and fr.name != self.FR_TEST_USER  # 004-immune

        sys_row = self._row(db_engine, self.SYSTEM_TENANT)
        assert sys_row is not None and sys_row.byo_mode == "platform_only"
        assert sys_row.name != self.SYSTEM_TENANT

        # No seeded row may have a name equal to its own id (what 004 deletes).
        from sqlalchemy import text

        with db_engine.connect() as c:
            bad = c.execute(text("SELECT count(*) FROM tenants WHERE name = id::text")).scalar_one()
        assert bad == 0

    def test_reaffirms_posture_without_renaming_existing_row(self, db_engine):
        """An existing row with a DIFFERENT (already-good) name keeps its name; only
        byo_mode is re-affirmed — the seed must never rename an established tenant."""
        from sqlalchemy import text

        # Simulate an already-present row for a seeded id, but under a different name
        # and a drifted posture; confirm 014 corrects posture yet keeps the name.
        with db_engine.begin() as c:
            c.execute(
                text(
                    "INSERT INTO tenants (id, name, byo_mode, created_at) "
                    "VALUES (:i, 'fr-test-user-legacy', 'byo_required', now())"
                ),
                {"i": self.FR_TEST_USER},
            )
        self._run(db_engine)
        row = self._row(db_engine, self.FR_TEST_USER)
        assert row.name == "fr-test-user-legacy"  # unchanged
        assert row.byo_mode == "local_allowed"  # re-affirmed to the classifier value

    def test_does_not_collide_with_003_004_seed_names(self, db_engine):
        """004 seeds n8n-dev/gerard-dev/marketing/sales with RANDOM uuidv7 ids. 014
        must not re-insert those names (would violate the name UNIQUE constraint on a
        fresh DB). Applying those seeds first, then 014, must not raise."""
        from sqlalchemy import text

        for name in ("n8n-dev", "gerard-dev", "marketing", "sales"):
            with db_engine.begin() as c:
                c.execute(
                    text(
                        "INSERT INTO tenants (id, name, byo_mode, created_at) "
                        "VALUES (uuidv7(), :n, 'local_allowed', now())"
                    ),
                    {"n": name},
                )
        self._run(db_engine)  # must not raise a unique-violation
        assert self._row(db_engine, self.FR_TEST_USER) is not None

    def test_is_idempotent(self, db_engine):
        from sqlalchemy import text

        self._run(db_engine)
        self._run(db_engine)  # second apply must not error or duplicate
        with db_engine.connect() as c:
            count = c.execute(
                text("SELECT count(*) FROM tenants WHERE id = :i"), {"i": self.FR_TEST_USER}
            ).scalar_one()
        assert count == 1
