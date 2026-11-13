"""Migration 028 adds the bundle layer; these pin the claims its header makes.

WHY THIS FILE EXISTS. 028 makes two strong claims that are cheap to state and
expensive to discover a violation of later:

  1. **It is inert.** No existing row of any kind is modified — no licence is
     created, moved, re-pointed or revoked. If a later edit slips an UPDATE or a
     DELETE into this file, the migration stops being safe to apply to a live
     ext_authz database at replicas: 2, and nothing else would notice.
  2. **Bundle↔app is many-to-many, and that is load-bearing.** `fr` belongs to
     `non-agentic-ai`, `knowledge` AND `research` — the operator named all three
     on 2026-09-04. The obvious "simplification" someone will reach for later is
     a `bundle_id` column on `app`, which cannot represent that and would drop
     two of the three memberships silently. `test_fr_is_in_three_bundles` is the
     regression guard: it fails the moment the requirement stops being met.

Both are asserted against the SQL TEXT, so they run in CI with no database —
which is the point. A test that only runs when a Postgres happens to be present
is an unexercised claim.

⛔ …0002 IS STILL NOT RENAMED HERE and a test below keeps it that way. New
evidence surfaced 2026-09-04 (`AA_IMPLEMENTATION/tenant_assets/README.md` maps
the `workflow-improvement` slot to …0002, independently of fr) which STRENGTHENS
fr's claim to a second source — but two well-sourced contradicting claims is
still a decision (billing #86), not a transcription, and 028 is a bundle
migration with no business renaming anything.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "migrations"
UP = MIGRATIONS / "028_add_bundle_and_bundle_app.sql"
DOWN = MIGRATIONS / "downgrades" / "028_add_bundle_and_bundle_app.sql"
MODELS = REPO / "models.py"

#: `('<bundle_key>', '<app_key>')` — the VALUES rows the membership loop drives from.
_MEMBERSHIP = re.compile(r"\(\s*'([a-z0-9-]+)'\s*,\s*'([a-z0-9-]+)'\s*\)")

#: The disputed row. 028 must not touch it.
_DISPUTED_ID = "0195f48b-d000-7000-8000-000000000002"


def _sql(path: Path) -> str:
    return path.read_text()


def _strip_comments(sql: str) -> str:
    """Statements only. The header is prose ABOUT the migration, and it legitimately
    contains the words this file forbids in executable SQL ("re-pointed", "DELETE")."""
    return "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))


def _memberships() -> set[tuple[str, str]]:
    body = _strip_comments(_sql(UP))
    start = body.index("AS t(bundle_key, app_key)")
    # The VALUES block is everything between the loop's SELECT and its alias.
    block = body[body.index("SELECT * FROM (VALUES") : start]
    return set(_MEMBERSHIP.findall(block))


class TestTheSeedIsParseableAtAll:
    """Positive control. Every assertion below reads sets built by `_memberships`;
    if the regex silently matched nothing, they would all pass vacuously."""

    def test_the_membership_block_yields_rows(self):
        assert len(_memberships()) >= 30

    def test_it_names_every_bundle_the_insert_creates(self):
        body = _strip_comments(_sql(UP))
        # Scope to the bundle INSERT's VALUES block. Reading the whole file would
        # also match the three `raise-my-tickets-*` rows of the app INSERT.
        block = body.split("INSERT INTO bundle (key, name, description)")[1]
        block = block[: block.index("ON CONFLICT")]
        declared = set(re.findall(r"\(\s*'([a-z0-9-]+)'\s*,", block))
        used = {b for b, _ in _memberships()}
        # Every bundle that gets a row must also get at least one app, or it is a
        # bundle nobody can buy.
        assert used <= declared, f"membership names unknown bundles: {used - declared}"
        assert declared <= used, f"bundles with no apps: {declared - used}"


class TestManyToManyIsLoadBearing:
    def test_fr_is_in_three_bundles(self):
        """The requirement that forces a join table. If this drops below three,
        either the seed regressed or someone decided a column would do."""
        fr = {b for b, a in _memberships() if a == "fr"}
        assert fr == {"non-agentic-ai", "knowledge", "research"}, fr

    def test_the_join_table_has_a_composite_primary_key(self):
        assert "PRIMARY KEY (bundle_id, app_id)" in _sql(UP)

    def test_no_bundle_column_is_added_to_app(self):
        body = _strip_comments(_sql(UP))
        assert "ALTER TABLE app" not in body
        assert "bundle_id" not in body.split("CREATE TABLE IF NOT EXISTS bundle_app")[0]


class TestTheMigrationCannotBreakTheAuthzPod:
    """A failed migration raises out of `_run_db_bootstrap` and the pod refuses to
    serve — at replicas: 2, backing ext_authz for the platform. Nothing may fail."""

    def test_both_creates_are_if_not_exists(self):
        body = _strip_comments(_sql(UP))
        assert body.count("CREATE TABLE IF NOT EXISTS") == 2
        assert "CREATE INDEX IF NOT EXISTS" in body

    def test_every_insert_is_on_conflict_do_nothing(self):
        body = _strip_comments(_sql(UP))
        assert body.count("INSERT INTO") == body.count("ON CONFLICT")
        assert "ON CONFLICT" in body
        assert "DO UPDATE" not in body

    def test_a_missing_app_is_skipped_not_raised(self):
        body = _strip_comments(_sql(UP))
        assert "RAISE NOTICE" in body
        assert "RAISE EXCEPTION" not in body
        assert "CONTINUE;" in body


class TestItIsInert:
    """The header claims not one existing row is modified. These make that testable."""

    def test_it_never_updates_or_deletes(self):
        """Matched as STATEMENTS, not substrings: `ON DELETE CASCADE` is a foreign-key
        action on a brand-new table and is not a row-removing statement."""
        body = _strip_comments(_sql(UP)).upper()
        assert not re.search(r"\bDELETE\s+FROM\b", body)
        assert not re.search(r"\bUPDATE\s+\w+\s+SET\b", body)
        assert "TRUNCATE" not in body

    def test_it_drops_nothing(self):
        assert not re.search(
            r"\bDROP\s+(TABLE|COLUMN|INDEX|CONSTRAINT)\b", _strip_comments(_sql(UP)).upper()
        )

    def test_it_touches_no_existing_table_but_app(self):
        """`app` gains three rows; `bundle`/`bundle_app` are new. Nothing else is
        named at all — in particular not `license`, which is what would make this
        migration capable of moving an entitlement."""
        body = _strip_comments(_sql(UP))
        for table in ("license", "token", "tenants", "plan", "offering", "subject"):
            assert f" {table} " not in body, f"028 names {table}"

    def test_the_disputed_tenant_is_not_mentioned_in_any_statement(self):
        assert _DISPUTED_ID not in _strip_comments(_sql(UP))


class TestTheDowngradeIsReal:
    def test_it_drops_both_tables(self):
        body = _strip_comments(_sql(DOWN))
        assert "DROP TABLE IF EXISTS bundle_app;" in body
        assert "DROP TABLE IF EXISTS bundle;" in body

    def test_it_drops_the_join_table_first(self):
        """bundle_app FKs bundle; dropping the parent first would raise."""
        body = _strip_comments(_sql(DOWN))
        assert body.index("DROP TABLE IF EXISTS bundle_app") < body.index(
            "DROP TABLE IF EXISTS bundle;"
        )

    def test_it_does_not_drop_the_new_apps(self):
        """Dropping an `app` cascades to `license`. If anyone licensed a
        raise-my-tickets sub-app between the upgrade and this downgrade, dropping
        it here would delete a real entitlement irreversibly."""
        assert "DROP TABLE IF EXISTS app" not in _sql(DOWN)
        assert "DELETE" not in _strip_comments(_sql(DOWN)).upper()

    def test_the_runner_cannot_auto_apply_it(self):
        """`apply_migrations.py` globs `migrations/*.sql` non-recursively, so a
        downgrade must live in the subdirectory or it runs straight after the
        upgrade it reverses."""
        assert DOWN.parent.name == "downgrades"
        # By PATH, not by name — the downgrade deliberately shares its filename with
        # the upgrade it reverses, exactly as 024 and 026 do.
        assert DOWN.resolve() not in {p.resolve() for p in MIGRATIONS.glob("*.sql")}


class TestTheOrmAgrees:
    """On a FRESH database the runner STAMPS this file without running it, because
    create_all has already built the schema from models.py. So the tables must
    exist in BOTH arms or a from-scratch build silently lacks them entirely."""

    def test_both_tables_are_declared_in_models(self):
        src = MODELS.read_text()
        assert "class Bundle(Base):" in src
        assert "class BundleApp(Base):" in src
        assert '__tablename__ = "bundle"' in src
        assert '__tablename__ = "bundle_app"' in src

    def test_the_orm_join_table_is_also_a_composite_key(self):
        src = MODELS.read_text()
        block = src.split("class BundleApp(Base):")[1].split("class ")[0]
        assert block.count("primary_key=True") == 2
