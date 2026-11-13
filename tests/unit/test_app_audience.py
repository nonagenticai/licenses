"""`app.audience` — the classification, its backfill, and its reachability.

WHAT THIS FILE DELIBERATELY DOES NOT TEST: whether the migration and the ORM agree
about the column. ``tests/unit/test_schema_parity.py`` already does that for the WHOLE
schema, and does it better — it builds the corpus arm FROM EMPTY (not on top of
``create_all``, which makes guarded migrations no-op and the comparison vacuous),
compares every column/constraint FIELD BY FIELD, proves its own discriminating power
against synthetic snapshots, and pins a vacuity floor. An earlier draft of this file
carried its own ORM-vs-migration comparison; it was removed as redundant once measured.
It is worth knowing why the redundancy mattered: two gates for one property drift apart,
and the weaker one makes the property look covered where it is not.

That gate is why ``docs/schema/orm.json`` and ``docs/schema/migrations.json`` must be
refreshed in the same commit as any schema change:

    uv run python scripts/build_schema_arms.py --dsn <dsn> --mode both --refresh-snapshots

What IS here is everything the parity gate says nothing about: the DATA the backfill
writes, the fact that the runner reaches the file at all on a prod-shaped database, and
whether the column is reachable through ``to_dict`` and guarded on the way in.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from models import App, ClientSafeError
from scripts.apply_migrations import split_statements

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "022_add_app_audience.sql"

#: The classification as prod holds it. 47 apps: 7 human, 4 automation, 36 platform.
HUMAN = ("fr", "fr-frontend", "commonpractice", "w2v", "signal", "raise-my-tickets", "ingestion")
AUTOMATION = (
    "kaizen_core_actions",
    "kaizen_discover_actions",
    "meta_prompt_core_actions",
    "meta_prompt_discover_actions",
)
PLATFORM_SAMPLE = ("coding", "n8n", "observability", "kanban", "licenses")


def _audience_facts(conn) -> dict[str, object]:
    """The column facts both paths must agree on, read from the live catalogue."""
    col = conn.execute(
        text(
            "SELECT data_type, character_maximum_length, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_name = 'app' AND column_name = 'audience'"
        )
    ).fetchone()
    chk = conn.execute(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'app'::regclass AND conname = 'ck_app_audience'"
        )
    ).scalar_one_or_none()
    assert col is not None, "app.audience is absent"
    return {
        "data_type": col[0],
        "max_length": col[1],
        "is_nullable": col[2],
        # Normalised: create_all writes ``'platform'::character varying`` and the
        # migration writes the same, but the cast text is not the point of comparison.
        "default_value": (col[3] or "").split("::")[0].strip(),
        "check": chk,
    }


def _rebuild_via_migration(conn) -> None:
    """Drop the ORM-built column and let migration 022 rebuild it, in-transaction.

    Postgres DDL is transactional, so the caller rolls this back and the session's
    shared schema is left exactly as it was.
    """
    conn.execute(text("ALTER TABLE app DROP CONSTRAINT IF EXISTS ck_app_audience"))
    conn.execute(text("ALTER TABLE app DROP COLUMN IF EXISTS audience"))
    for stmt in split_statements(MIGRATION.read_text(encoding="utf-8")):
        conn.execute(text(stmt))


class TestTheBackfillClassifies:
    def _seed(self, conn, keys) -> None:
        for k in keys:
            conn.execute(
                text("INSERT INTO app (key, name) VALUES (:k, :k) ON CONFLICT (key) DO NOTHING"),
                {"k": k},
            )

    def test_it_assigns_human_automation_and_defaults_the_rest(self, db_engine):
        with db_engine.connect() as conn:
            trans = conn.begin()
            try:
                self._seed(conn, HUMAN + AUTOMATION + PLATFORM_SAMPLE)
                _rebuild_via_migration(conn)
                got = dict(
                    conn.execute(text("SELECT key, audience FROM app")).fetchall()  # type: ignore[arg-type]
                )
            finally:
                trans.rollback()

        assert [got[k] for k in HUMAN] == ["human"] * len(HUMAN)
        assert [got[k] for k in AUTOMATION] == ["automation"] * len(AUTOMATION)
        # Everything not named is platform — the fail-closed default. An app is not
        # offered to people until someone declares that it should be.
        assert [got[k] for k in PLATFORM_SAMPLE] == ["platform"] * len(PLATFORM_SAMPLE)

    def test_a_re_run_is_a_no_op_and_keeps_an_operator_correction(self, db_engine):
        """Once the column exists the file must not re-assert the backfill: a later
        operator reclassification is the authority, not this file."""
        with db_engine.connect() as conn:
            trans = conn.begin()
            try:
                self._seed(conn, ("karma",) + HUMAN)
                _rebuild_via_migration(conn)
                conn.execute(text("UPDATE app SET audience = 'human' WHERE key = 'karma'"))
                for stmt in split_statements(MIGRATION.read_text(encoding="utf-8")):
                    conn.execute(text(stmt))
                after = conn.execute(
                    text("SELECT audience FROM app WHERE key = 'karma'")
                ).scalar_one()
            finally:
                trans.rollback()
        assert after == "human"


class TestTheColumnIsReachableAndGuarded:
    def test_to_dict_emits_audience(self, db_engine):
        """A field absent from ``to_dict`` is a field no consumer can see —
        ``AppResponse`` is built from exactly this dict."""
        assert "audience" in App(key="k", name="n", audience="human").to_dict()

    def test_a_new_app_defaults_to_platform(self, db_manager):
        app = db_manager.create_app(key="brand-new", name="Brand New", issuers=("platform",))
        assert app.audience == "platform"

    def test_set_app_audience_rejects_an_unknown_value(self, db_manager):
        db_manager.create_app(key="reclass-me", name="Reclass Me", issuers=("platform",))
        with pytest.raises(ClientSafeError):
            db_manager.set_app_audience("reclass-me", "everyone")

    def test_set_app_audience_reclassifies_and_reports_unknown_keys(self, db_manager):
        db_manager.create_app(key="reclass-me", name="Reclass Me", issuers=("platform",))
        assert db_manager.set_app_audience("reclass-me", "human").audience == "human"
        assert db_manager.set_app_audience("no-such-app", "human") is None


class TestTheRunnerActuallyPicksItUp:
    """The SQL working by hand is not the same as the runner running it.

    021's own tests make this point: a fresh database BASELINES every current file
    without executing it, so the only path on which 022 ever runs is a database
    shaped like prod — the app tables present, and the twenty pre-022 files already
    stamped. If 022 were somehow excluded from the runner's pending set, every test
    above would still pass and prod would still never get the column.
    """

    def test_022_runs_on_a_prod_shaped_database(self, db_engine):
        from scripts.apply_migrations import MIGRATIONS_DIR, apply_all
        from tests.conftest import TEST_DATABASE_URL

        with db_engine.begin() as c:
            c.execute(text("ALTER TABLE app DROP CONSTRAINT IF EXISTS ck_app_audience"))
            c.execute(text("ALTER TABLE app DROP COLUMN IF EXISTS audience"))
            c.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS schema_migrations ("
                    "filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
                )
            )
            c.execute(text("DELETE FROM schema_migrations"))
            for p in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if p.name.startswith("022_"):
                    continue
                c.execute(
                    text("INSERT INTO schema_migrations (filename) VALUES (:f)"), {"f": p.name}
                )
            c.execute(
                text("INSERT INTO app (key, name) VALUES ('fr', 'FR') ON CONFLICT DO NOTHING")
            )

        assert "audience" not in {c["name"] for c in inspect(db_engine).get_columns("app")}

        dsn = TEST_DATABASE_URL.replace("+psycopg", "")
        assert apply_all(dsn, sslmode="disable") == 0

        with db_engine.connect() as conn:
            facts = _audience_facts(conn)
            assert facts["check"] is not None
            assert (
                conn.execute(text("SELECT audience FROM app WHERE key = 'fr'")).scalar_one()
                == "human"
            )
