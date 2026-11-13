"""What a FRESH licenses database must contain, and why migrations cannot say it.

`scripts/apply_migrations.py` BASELINES: on the first run against a schema that
already has the app tables it stamps every `.sql` file as applied WITHOUT running
it. Its probe is `to_regclass('public.offering')` — and `create_tables()`, which
runs immediately before it in `_run_db_bootstrap`, creates `offering`. So the
probe cannot distinguish "populated prod, hand-applied" from "brand-new empty
database", and on a fresh database everything that exists ONLY in SQL is skipped
forever.

Measured against prod 2026-08-12: all 7 hardcoded-UUID tenants present (57 total);
`ck_tenants_byo_mode` absent (only `ck_tenants_name_format`); FK named
`fk_license_tenant`. A create_all'd database had none of the three.

These tests pin the three things that closes: the ORM now declares the constraint
and the FK name, and the bootstrap seeds the identities.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import text

from models import PLATFORM_TENANTS

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


def _constraints(engine, table: str, contype: str) -> dict[str, str]:
    with engine.connect() as c:
        rows = c.execute(
            # `cast(:t AS regclass)`, not `:t::regclass` — SQLAlchemy's text() reads
            # the `::` as the start of another bindparam and leaves `:t` unbound.
            text(
                "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = cast(:t AS regclass) AND contype = :ct"
            ),
            {"t": table, "ct": contype},
        ).fetchall()
    return {r[0]: r[1] for r in rows}


class TestTheOrmCarriesWhatOnlySqlUsedTo:
    """`create_all` alone must now produce prod's shape."""

    def test_ck_tenants_byo_mode_exists_on_a_create_all_database(self, db_engine):
        checks = _constraints(db_engine, "tenants", "c")
        assert "ck_tenants_byo_mode" in checks, (
            f"migration 006/007's CHECK is still SQL-only, so a fresh database does not "
            f"have it. Present: {sorted(checks)}"
        )
        body = checks["ck_tenants_byo_mode"]
        for value in ("local_allowed", "byo_required", "platform_only"):
            assert value in body, f"{value} missing from {body}"

    def test_the_check_actually_refuses_a_bad_value(self, db_engine):
        """A constraint that exists but does not bite is not defence in depth."""
        import uuid

        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            with db_engine.begin() as c:
                c.execute(
                    text(
                        "INSERT INTO tenants (id, name, byo_mode, owner_key, created_at) "
                        "VALUES (:i, :n, 'not-a-mode', 'platform', now())"
                    ),
                    {"i": str(uuid.uuid4()), "n": f"ck-probe-{uuid.uuid4().hex[:8]}"},
                )

    def test_a_fresh_database_refuses_two_global_plans_with_the_same_app_and_code(self, db_engine):
        """THE SEMANTIC divergence, tested by BEHAVIOUR rather than by index shape.

        A plain UNIQUE(app_id, code, region_id) does not constrain GLOBAL plans:
        region_id is NULL for them and NULLs never collide in a Postgres unique
        index. Prod has always used the COALESCE expression index (init.sql:67)
        and therefore REFUSES the second row; a fresh database ACCEPTED it.

        Asserting the index DEFINITION would pass on any index that merely looks
        right. This drives the actual insert.

        Mutant that turns this RED: put back
        UniqueConstraint("app_id", "code", "region_id").
        """
        import uuid

        from sqlalchemy.exc import IntegrityError

        app_id, code = str(uuid.uuid4()), f"dup-{uuid.uuid4().hex[:8]}"
        with db_engine.begin() as c:
            c.execute(
                text(
                    "INSERT INTO app (id, key, name, revoked, created_at) "
                    "VALUES (:i, :k, :k, false, now())"
                ),
                {"i": app_id, "k": f"app-{code}"},
            )
            c.execute(
                text(
                    "INSERT INTO plan (id, app_id, code, name, region_id, created_at) "
                    "VALUES (:i, :a, :c, :c, NULL, now())"
                ),
                {"i": str(uuid.uuid4()), "a": app_id, "c": code},
            )

        with pytest.raises(IntegrityError):
            with db_engine.begin() as c:
                c.execute(
                    text(
                        "INSERT INTO plan (id, app_id, code, name, region_id, created_at) "
                        "VALUES (:i, :a, :c, :c, NULL, now())"
                    ),
                    {"i": str(uuid.uuid4()), "a": app_id, "c": code},
                )

    def test_license_tenant_id_has_exactly_one_index_as_prod_does(self, db_engine):
        """`index=True` on the column PLUS an explicit Index of the same shape
        emitted TWO identical btrees on a fresh database. Prod has one
        (`idx_license_tenant`, measured on the primary) because
        create_all(checkfirst=True) skips existing tables wholesale and never
        added the second one there.

        Mutant that turns this RED: restore `index=True` on License.tenant_id.

        Asked of the CATALOG, not of the index definition text. The obvious
        `indexdef LIKE '%(tenant_id)%'` matches only the literal single-column
        form, so an `Index("dup", "tenant_id", "id")` — a second index still
        leading on tenant_id — slips straight past it and the test passes green
        while its own name is false. Verified: that exact mutant survived the
        LIKE version.
        """
        rows = db_engine.connect().execute(
            text(
                "SELECT i.relname, array_to_string(array_agg(a.attname ORDER BY k.ord), ',') "
                "FROM pg_index x "
                "JOIN pg_class t ON t.oid = x.indrelid "
                "JOIN pg_class i ON i.oid = x.indexrelid "
                "JOIN pg_namespace n ON n.oid = t.relnamespace "
                "CROSS JOIN LATERAL unnest(x.indkey) WITH ORDINALITY AS k(attnum, ord) "
                "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum "
                "WHERE t.relname = 'license' AND n.nspname = current_schema() "
                "GROUP BY i.relname HAVING bool_or(a.attname = 'tenant_id')"
            )
        )
        by_name = {r[0]: r[1] for r in rows}
        # The partial unique index legitimately leads on tenant_id; prod has it too.
        extra = {k: v for k, v in by_name.items() if k != "uq_license_active_tenant_app_subject"}
        assert sorted(extra) == ["idx_license_tenant"], (
            f"expected exactly prod's single tenant_id index, got {extra}. A second "
            f"btree containing tenant_id on the hottest table is write amplification "
            f"prod does not pay."
        )

    def test_the_license_tenant_fk_is_named_as_prod_names_it(self, db_engine):
        """Unnamed, create_all emits `license_tenant_id_fkey` while prod carries
        `fk_license_tenant` — and migration 016's idempotency guard keys on the
        latter, so it reads FALSE on a fresh schema and a replay would add a
        SECOND FK. The name is the thing that keeps the two schemas identical."""
        fks = _constraints(db_engine, "license", "f")
        assert "fk_license_tenant" in fks, f"expected fk_license_tenant, got {sorted(fks)}"
        assert "license_tenant_id_fkey" not in fks, (
            "both the named and the default FK are present — the model change added one "
            "instead of renaming"
        )
        assert "tenants" in fks["fk_license_tenant"]

    def test_migration_016_guard_now_matches_a_fresh_schema(self, db_engine):
        """016 guards on `conname = 'fk_license_tenant'`. Read the literal out of the
        file rather than restating it, so a rename there fails here."""
        sql = (MIGRATIONS / "016_add_license_tenant_fk.sql").read_text()
        m = re.search(r"conname\s*=\s*'([a-z_]+)'", sql)
        assert m, "could not find 016's conname guard"
        guard_name = m.group(1)
        assert guard_name in _constraints(db_engine, "license", "f"), (
            f"016 guards on {guard_name!r}, which a create_all'd schema does not have — "
            f"replaying migrations on a fresh database would add a duplicate FK"
        )


class TestPlatformTenantsAreSeeded:
    def test_seed_creates_every_hardcoded_identity(self, db_manager, db_engine):
        inserted = db_manager.seed_platform_tenants()
        assert inserted == len(PLATFORM_TENANTS)
        with db_engine.connect() as c:
            found = {
                str(r[0]): (r[1], r[2])
                for r in c.execute(
                    text("SELECT id, name, byo_mode FROM tenants WHERE id = ANY(:ids)"),
                    {"ids": [t[0] for t in PLATFORM_TENANTS]},
                ).fetchall()
            }
        for tenant_id, name, byo_mode in PLATFORM_TENANTS:
            assert tenant_id in found, f"{name} ({tenant_id}) was not seeded"
            assert found[tenant_id] == (name, byo_mode)

    def test_seed_is_idempotent(self, db_manager):
        assert db_manager.seed_platform_tenants() == len(PLATFORM_TENANTS)
        assert db_manager.seed_platform_tenants() == 0, "second run must insert nothing"
        assert db_manager.seed_platform_tenants() == 0

    def test_seed_does_not_overwrite_an_operator_change(self, db_manager, db_engine):
        """ON CONFLICT DO NOTHING, not DO UPDATE. Migration 014 re-affirms posture on
        every apply; running that on every boot would silently revert a deliberate
        operator change, so the seed deliberately does not."""
        db_manager.seed_platform_tenants()
        target = PLATFORM_TENANTS[0][0]
        with db_engine.begin() as c:
            c.execute(
                text("UPDATE tenants SET byo_mode = 'byo_required' WHERE id = :i"),
                {"i": target},
            )
        db_manager.seed_platform_tenants()
        with db_engine.connect() as c:
            got = c.execute(
                text("SELECT byo_mode FROM tenants WHERE id = :i"), {"i": target}
            ).scalar_one()
        assert got == "byo_required", "the seed reverted an operator's byo_mode change"

    def test_a_name_collision_does_not_crash_the_boot(self, db_manager, db_engine, caplog):
        """THE failure mode this seed could have introduced.

        `tenants.name` is UNIQUE. If an unrelated row already holds one of these
        names, a targeted `ON CONFLICT (id)` would let the name collision RAISE —
        out of seed_platform_tenants, out of _run_db_bootstrap, out of lifespan —
        and this service backs ext_authz for the whole platform. Prod already
        contains a real instance of exactly this collision (migration 013's row is
        named `portal-render-e2e-e148e8af` because the plain name was taken).

        So: absorb it, WARN by name, and keep serving.
        """
        import logging
        import uuid

        target_id, target_name, _ = PLATFORM_TENANTS[0]
        # An unrelated row squats the name under a different id.
        with db_engine.begin() as c:
            c.execute(
                text(
                    "INSERT INTO tenants (id, name, byo_mode, owner_key, created_at) "
                    "VALUES (:i, :n, 'byo_required', 'platform', now())"
                ),
                {"i": str(uuid.uuid4()), "n": target_name},
            )

        with caplog.at_level(logging.WARNING, logger="models"):
            db_manager.seed_platform_tenants()  # must NOT raise

        with db_engine.connect() as c:
            seeded = c.execute(
                text("SELECT count(*) FROM tenants WHERE id = :i"), {"i": target_id}
            ).scalar_one()
        assert seeded == 0, "the squatted name should have blocked exactly this one row"

        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("ABSENT after seeding" in m for m in warnings), (
            f"a silently-skipped platform identity must be WARNed by name; got {warnings}"
        )
        assert any(target_name in m for m in warnings), (
            f"the warning must name the missing tenant; got {warnings}"
        )

    def test_every_seeded_value_satisfies_the_new_check(self):
        """The seed and the constraint must not be able to disagree — otherwise the
        first fresh boot fails its own CHECK and the pod refuses to serve."""
        allowed = {"local_allowed", "byo_required", "platform_only"}
        for tenant_id, name, byo_mode in PLATFORM_TENANTS:
            assert byo_mode in allowed, f"{name} has byo_mode {byo_mode!r}"

    def test_ids_are_unique_and_well_formed(self):
        import uuid

        ids = [t[0] for t in PLATFORM_TENANTS]
        assert len(ids) == len(set(ids)), "duplicate id in PLATFORM_TENANTS"
        names = [t[1] for t in PLATFORM_TENANTS]
        assert len(names) == len(set(names)), "duplicate name in PLATFORM_TENANTS"
        for tenant_id in ids:
            uuid.UUID(tenant_id)  # raises if malformed
        for name in names:
            assert re.fullmatch(r"[a-z0-9][a-z0-9-]*", name), (
                f"{name!r} violates ck_tenants_name_format and would fail on insert"
            )


class TestTheBootstrapActuallyCallsIt:
    """Without this, every test above passes on a seed nothing invokes.

    The seeding tests all call `seed_platform_tenants()` directly, so deleting the
    call from `_run_db_bootstrap` would leave them green while a real pod seeds
    nothing — a capability satisfied by a reader that never fires.
    """

    def test_run_db_bootstrap_invokes_the_seed(self, monkeypatch):
        import main

        calls: list[str] = []
        monkeypatch.setattr(
            main.db_manager, "database_is_pristine", lambda: calls.append("probe") or False
        )
        monkeypatch.setattr(main.db_manager, "create_tables", lambda: calls.append("create"))
        monkeypatch.setattr(
            main.db_manager, "seed_platform_tenants", lambda: calls.append("seed") or 0
        )
        monkeypatch.setattr(
            "scripts.apply_migrations.run_from_env", lambda: calls.append("migrate") or 0
        )

        main._run_db_bootstrap()

        assert "seed" in calls, "_run_db_bootstrap no longer seeds the platform tenants"
        assert calls == ["probe", "create", "migrate", "seed"], (
            f"bootstrap order changed: {calls}. The seed must run AFTER create_tables "
            f"(the table must exist) and after the migrations (so a migration that "
            f"reshapes tenants has already run). The probe must run BEFORE "
            f"create_tables — asked after it, it always answers True and the "
            f"fresh-database warning can never fire."
        )

    def test_a_seed_failure_is_not_swallowed(self, monkeypatch):
        """Fail-soft lives INSIDE seed_platform_tenants (a name collision warns).
        A genuine database error must still propagate — _run_db_bootstrap's contract
        is that anything which is not a connect fault refuses to serve."""
        import main

        monkeypatch.setattr(main.db_manager, "database_is_pristine", lambda: False)
        monkeypatch.setattr(main.db_manager, "create_tables", lambda: None)
        monkeypatch.setattr("scripts.apply_migrations.run_from_env", lambda: 0)

        def boom():
            raise RuntimeError("seed exploded")

        monkeypatch.setattr(main.db_manager, "seed_platform_tenants", boom)
        with pytest.raises(RuntimeError, match="seed exploded"):
            main._run_db_bootstrap()


class TestTheConstantDoesNotDriftFromTheMigrations:
    """PLATFORM_TENANTS transcribes migrations 013 + 014. If someone edits a
    migration and not the constant, a fresh database and a migrated one diverge —
    which is the whole class of bug this change exists to close."""

    def _seeded_in(self, filename: str) -> set[tuple[str, str, str]]:
        sql = (MIGRATIONS / filename).read_text()
        # ('<uuid>', '<name>', '<byo_mode>', NOW())
        return {
            (m.group(1), m.group(2), m.group(3))
            for m in re.finditer(
                r"\(\s*'([0-9a-f-]{36})'\s*,\s*'([a-z0-9-]+)'\s*,\s*'([a-z_]+)'\s*,\s*NOW\(\)\s*\)",
                sql,
            )
        }

    def _rekeys(self) -> dict[str, str]:
        """old tenant id -> new tenant id, for every `NNN_rekey_*.sql` in the corpus.

        013 / 014 are stamped everywhere and are never edited, so a seeded id that
        a LATER migration re-keys is transcribed into PLATFORM_TENANTS under its
        NEW id. Read out of the re-key files rather than restated here."""
        moved: dict[str, str] = {}
        for path in sorted(MIGRATIONS.glob("*_rekey_*.sql")):
            sql = path.read_text()
            old = re.search(r"old_id\s+constant uuid := '([0-9a-f-]{36})'", sql)
            new = re.search(r"new_id\s+constant uuid := '([0-9a-f-]{36})'", sql)
            assert old and new, f"{path.name}: could not read old_id / new_id"
            moved[old.group(1)] = new.group(1)
        return moved

    def _seeded_then_rekeyed(self, filename: str) -> set[tuple[str, str, str]]:
        moved = self._rekeys()
        return {(moved.get(i, i), n, b) for i, n, b in self._seeded_in(filename)}

    def test_the_migration_rows_are_parseable_at_all(self):
        """Positive control: if the regex stops matching, every comparison below
        becomes a vacuous empty-set equality."""
        assert len(self._seeded_in("014_seed_platform_internal_tenants.sql")) == 7
        assert len(self._seeded_in("013_seed_render_e2e_tenant.sql")) == 1
        assert self._rekeys() == {
            "1d0090ac-adbf-4c07-b704-bc66061b572e": "01a1109c-f895-7766-ba28-6ea56e56a5f7",
            "e148e8af-3751-49bf-a0fa-56ab46d31ddc": "01a1113b-674f-72b0-a84b-dc746d39afca",
            "0195f48b-d000-7000-8000-000000000100": "01a1117c-d9b0-72fc-aeda-a9d1187f2a36",
        }

    def test_constant_matches_013_plus_014_exactly(self):
        """PLATFORM_TENANTS transcribes BOTH seeding migrations, 013 and 014.

        013 was excluded until 2026-08-12 on the belief that its id was contested
        in prod. Token usage disproved that (the row it called dead carries all
        the live traffic), prod was renamed to match 013, and the constant now
        covers both — so a fresh database reproduces every hardcoded identity.

        ⚠️ Since migration 046 the comparison is against 013/014 AS RE-KEYED by the
        later corpus: 013 still INSERTs `e148e8af-…` (it is stamped everywhere and
        never replayed), 046 moves that row, and the constant must name where the
        row ENDS UP. Leave the constant on 013's literal and this goes red — and a
        fresh database would be born with the principal's id as a tenant again.
        """
        from_sql = self._seeded_in(
            "014_seed_platform_internal_tenants.sql"
        ) | self._seeded_then_rekeyed("013_seed_render_e2e_tenant.sql")
        assert self._seeded_in("013_seed_render_e2e_tenant.sql").isdisjoint(from_sql), (
            "control: 013's literal id is NOT what the constant is compared against"
        )
        from_const = set(PLATFORM_TENANTS)
        assert from_const == from_sql, (
            "PLATFORM_TENANTS has drifted from migrations 013/014.\n"
            f"  only in models.py:   {sorted(from_const - from_sql)}\n"
            f"  only in migrations:  {sorted(from_sql - from_const)}"
        )

    def test_013s_render_tenant_is_seeded_under_the_canonical_name(self):
        """The reverse of what this asserted until 2026-08-12.

        It used to assert EXCLUSION, because prod held 013's literal id under the
        suffixed name `portal-render-e2e-e148e8af` while a random uuid7 held the
        plain name. The tie-breaker nobody had looked at was token usage: the
        suffixed row had 5 tokens (3 used that day) and the name-holder had none.
        Prod was renamed so the canonical name follows the live identity, which is
        exactly what 013's own `ON CONFLICT (id) DO UPDATE SET name` does.

        Pinned in BOTH directions so neither half can rot: the id must be seeded,
        AND it must carry the unsuffixed name. A future re-collision that re-adds a
        suffix would satisfy the first assertion alone.
        """
        render = self._seeded_then_rekeyed("013_seed_render_e2e_tenant.sql")
        assert render, "013 no longer parses — the assertions below are unverified"
        assert {i for i, _n, _b in render} == {"01a1113b-674f-72b0-a84b-dc746d39afca"}
        assert render <= set(PLATFORM_TENANTS), (
            "013's render tenant is missing from PLATFORM_TENANTS. A fresh database "
            "would then lack the identity prod resolves by name; read the comment on "
            "the constant."
        )
        names = {name for _id, name, _byo in render}
        assert names == {"portal-render-e2e"}, (
            f"013 must seed the CANONICAL name, got {names}. A suffixed name means "
            "the UNIQUE(name) collision has returned and the identity is split again."
        )


class TestMigration021:
    def test_it_exists_and_is_guarded(self):
        sql = (MIGRATIONS / "021_add_tenants_byo_mode_check.sql").read_text()
        assert "ck_tenants_byo_mode" in sql
        assert "pg_constraint" in sql, "021 must be guarded on pg_constraint to be re-runnable"
        for value in ("local_allowed", "byo_required", "platform_only"):
            assert value in sql, f"021 omits {value} — it would diverge from the ORM"

    def test_it_refuses_rather_than_crashes_on_bad_data(self):
        """A migration failure raises MigrationError out of _run_db_bootstrap and the
        pod refuses to serve — and this service backs ext_authz for the platform. So
        021 must diagnose bad data with a named error, not a bare constraint
        violation."""
        sql = (MIGRATIONS / "021_add_tenants_byo_mode_check.sql").read_text()
        assert "RAISE EXCEPTION" in sql
        assert "bad_rows" in sql

    def _make_prod_shaped(self, db_engine) -> None:
        """A database shaped like prod: the CHECK absent, and every file EXCEPT 021
        already stamped — which is exactly what apply_all() left behind when it
        baselined prod's 20 files. This is the only path on which 021 actually runs;
        a fresh database baselines it away and gets the constraint from the ORM.
        """
        with db_engine.begin() as c:
            c.execute(text("ALTER TABLE tenants DROP CONSTRAINT IF EXISTS ck_tenants_byo_mode"))
            c.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS schema_migrations ("
                    "filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
                )
            )
            c.execute(text("DELETE FROM schema_migrations"))
            for p in sorted(MIGRATIONS.glob("*.sql")):
                if p.name.startswith("021_"):
                    continue
                c.execute(
                    text("INSERT INTO schema_migrations (filename) VALUES (:f)"), {"f": p.name}
                )

    def test_it_applies_to_a_prod_shaped_database(self, db_engine):
        from scripts.apply_migrations import apply_all
        from tests.conftest import TEST_DATABASE_URL

        self._make_prod_shaped(db_engine)
        assert "ck_tenants_byo_mode" not in _constraints(db_engine, "tenants", "c")

        dsn = TEST_DATABASE_URL.replace("+psycopg", "")
        assert apply_all(dsn, sslmode="disable") == 0
        assert "ck_tenants_byo_mode" in _constraints(db_engine, "tenants", "c"), (
            "021 did not run on a prod-shaped database — the CHECK is still missing"
        )
        # Re-running the whole set must stay a no-op (021 is now stamped, and its DO
        # block is guarded anyway).
        assert apply_all(dsn, sslmode="disable") == 0
        assert "ck_tenants_byo_mode" in _constraints(db_engine, "tenants", "c")

    def test_it_refuses_loudly_when_the_data_would_violate_it(self, db_engine):
        """The guard must diagnose bad data instead of dying on a bare constraint
        violation — a migration failure CrashLoops the pod, so the log is the only
        thing the operator gets."""
        import uuid

        from scripts.apply_migrations import apply_all
        from tests.conftest import TEST_DATABASE_URL

        self._make_prod_shaped(db_engine)
        with db_engine.begin() as c:
            c.execute(
                text(
                    "INSERT INTO tenants (id, name, byo_mode, owner_key, created_at) "
                    "VALUES (:i, :n, 'bogus-mode', 'platform', now())"
                ),
                {"i": str(uuid.uuid4()), "n": f"bad-{uuid.uuid4().hex[:8]}"},
            )
        dsn = TEST_DATABASE_URL.replace("+psycopg", "")
        assert apply_all(dsn, sslmode="disable") == 1, "021 must FAIL on violating data"
        assert "ck_tenants_byo_mode" not in _constraints(db_engine, "tenants", "c")

    def test_the_pending_set_relative_to_prod_is_known(self):
        """A ratchet on what the next rollout will actually execute.

        Prod carries 20 stamped files. Everything after them runs on the next pod
        start, in filename order, and a failure there means the pod refuses to serve
        while backing ext_authz for the platform — so the pending set must be a
        deliberate list somebody wrote down, not whatever happens to be in the
        directory. Adding a migration is meant to break this test: update the list in
        the same commit, having satisfied yourself the new file is safe to run
        against prod's data.

        Pending as of 2026-09-01:
          021 — ck_tenants_byo_mode on databases that already exist.
          022 — app.audience + backfill (7 human / 4 automation / 36 platform,
                verified against a copy of prod's 47-row app table).
          023 — tenants.archived_at. Nullable, no default, no backfill: every
                existing row stays NULL, which reads as "never archived".
                Metadata-only on PG11+, so no rewrite of the ~75-row table.
          024 — renames two tenants: `platform-cp-1` -> `n8n-dev-portal-realm`
                and `platform-cp-3` -> `george-dev-portal-realm`, because those
                uuids are real Keycloak portal-realm identities, not anonymous
                platform slots. Safe against prod's data: two UPDATEs by primary
                key on a ~100-row table, each guarded on the row still holding
                the exact old name (so a re-run is a no-op) AND on the target
                name being free (so UNIQUE(name) cannot raise and CrashLoop the
                pod). Verified against prod 2026-08-28: both target names were
                unused, no column in the database stores a tenant name as text,
                and `license`/`tenant_app_byo_mode` reference `tenants.id` only —
                so the 5 and 6 licences on those rows do not move. Nothing is
                re-pointed, merged or deleted; see the file's header for why
                …0002/…0004/…0005 are deliberately left alone.
          025 — adds `tenant_app_byo_mode.offering_code`, recording WHICH
                offering produced each per-product posture. Safe against prod's
                data: ADD COLUMN, nullable, no DEFAULT — metadata-only on PG11+,
                no rewrite, no backfill — and guarded on the column so a re-run
                is a no-op. Measured against prod 2026-08-28: the table holds
                ZERO rows, so there is not even data to preserve. Verified on a
                prod-shaped copy (seeded with rows anyway): applies, is
                idempotent across two runs, and leaves every row NULL.
          026 — renames the last two placeholders: `platform-cp-4` ->
                `prompt-improvement` and `platform-cp-5` ->
                `procedure-improvement`. 024 left these alone for want of
                evidence; the evidence is fr migrations 062/066 (which INSERT
                these uuids under these names and CHECK-pin `streams.kind` to
                them), the live fr `workspaces` table, and the Keycloak
                realm-patch script's OPERATORS + CARD_WRITERS maps. Guarded
                exactly like 024 — old name must still be held, target name must
                be free — so it cannot raise and CrashLoop the ext_authz pod.
                Measured against prod 2026-09-01: both target names unused, the
                3 and 2 licences on those rows reference `tenants.id` and do not
                move, and 0 by-name tenant lookups were served in the current
                pods' lifetime. …0002 is STILL left alone — see the file header.
          027 — adds `tenants.kind` (NOT NULL DEFAULT 'unclassified') and
                `tenants.cost_centre` (nullable), backfills the 75 known names,
                then adds ck_tenants_kind and ix_tenants_kind. Safe against
                prod's data: both ADD COLUMNs are metadata-only on PG11+ (the
                NOT NULL one carries a constant DEFAULT, so no table rewrite),
                the CHECK is added only AFTER every row has been assigned a
                value, and the whole thing is one DO block guarded on the column
                so a re-run is a no-op. No statement can fail on data.
                VERIFIED 2026-09-02 on a prod-shaped copy: schema built from the
                ORM, 027's own columns dropped to simulate a pre-027 database,
                then seeded with the exact 75 tenant names read from prod. Result
                42 service / 15 internal / 10 test / 8 customer / **0
                unclassified**; a second run printed "already present — nothing
                to do"; the downgrade dropped all three objects and a third run
                reproduced the identical counts.
                ⚠️ The first draft declared ix_tenants_kind in models.py but NOT
                in the SQL. test_schema_parity caught it as a new ORM-only
                divergence. That is the failure mode this ratchet and that gate
                both exist for, and it is why the index is in both arms now.

          028 — adds `bundle` and `bundle_app`: the grouping layer between a
                tenant and its app licences, which has never existed. Also adds
                three `raise-my-tickets-*` apps and seeds 8 bundles with their
                memberships. MANY-TO-MANY is the load-bearing choice: `fr`
                belongs to `non-agentic-ai`, `knowledge` AND `research`, so a
                `bundle_id` column on `app` could not represent it. Safe against
                prod's data: two CREATE TABLE IF NOT EXISTS on NEW tables, and
                every INSERT is ON CONFLICT DO NOTHING, so a re-run is a no-op.
                NOT ONE EXISTING ROW IS MODIFIED — no licence is created, moved
                or re-pointed, and nothing reads these tables yet, so the
                migration is inert by construction. A membership naming an app
                key absent from this environment's catalogue is announced via
                RAISE NOTICE and skipped, never fatal — which is what keeps it
                from raising out of `_run_db_bootstrap` and CrashLooping the
                ext_authz pod. The 9 licences on the flat `raise-my-tickets` app
                stay exactly where they are: re-pointing them needs someone to
                say which of the three sub-apps each customer bought, and that
                is business knowledge this database does not hold.

          029 — adds `subject.tenant_id`: a nullable UUID FK to `tenants(id)`
                (ON DELETE RESTRICT, named `fk_subject_tenant`) plus
                `idx_subject_tenant`. It makes "this subject BELONGS TO this
                tenant" expressible; before it, membership could only be
                inferred from the licence rows, which inverts the relationship.
                Safe against prod's data: ADD COLUMN, nullable, NO DEFAULT and
                NO backfill — metadata-only on PG11+, so no rewrite of the
                254-row table — and the FK is added to a column in which every
                row is NULL by construction (it is created in the same DO
                block), so validation cannot find a violating row and no
                NOT VALID/VALIDATE dance is needed. One DO block guarded on the
                column, so a re-run is a no-op. The ACCESS EXCLUSIVE window is
                sub-millisecond (254 subjects, 76 tenants) and the runner's
                MIGRATION_LOCK_TIMEOUT_MS bounds the wait to acquire it.
                VERIFIED 2026-09-05 on a throwaway PG18 container shaped like
                prod (76 tenants, 254 subjects): applies with
                "254 subject rows, all NULL"; a second run prints "already
                present — nothing to do"; the FK rejects a tenant_id absent from
                `tenants`; ON DELETE RESTRICT refuses to delete a tenant that has
                members; the downgrade returns `subject` to its 4 columns.
                ⛔ It moves NO `svc-*` row and re-points NO licence — that half is
                deliberately deferred, see docs/svc-tenant-migration-plan.md.

          030 — DATA ONLY: sets `tenants.cost_centre` on 12 of the 21 rows that
                still hold NULL, and deliberately leaves the other 9 for the
                operator. It adds no schema object of any kind, so it moves none
                of test_schema_parity's structural counts. The admission rule is
                written in the file header: assign only where 100% of the
                already-classified rows sharing the discriminating feature agree.
                10 x `svc-%` -> 'service' (33 of 43 svc rows are classified and
                ALL 33 carry 'service'; 027 states the same `svc-` precedence),
                `subhankar-kag-2` -> 'customers' (all 5 classified kind='customer'
                rows carry it, and its name sibling `subhankar-kag` is one of
                them), `fr-test` -> 'tests' (siblings `fr-test-user` and
                `n8n-test`). The 9 refused rows are named in the header with the
                reason each was refused — 5 kind='internal' rows whose classified
                peers split across FIVE different centres, and 4 test/probe
                fixtures with no agreeing sibling. Safe against prod's data:
                every UPDATE is guarded `WHERE cost_centre IS NULL`, so a re-run
                is a no-op AND an operator's later correction is never
                overwritten; every value written already exists in prod's
                vocabulary so nothing can fail on length or a constraint; and a
                count that does not match the measurement is a RAISE WARNING,
                never an EXCEPTION, because a raise here CrashLoops the ext_authz
                pod.
                VERIFIED 2026-09-05 against the REAL 76 tenant rows exported from
                prod into a throwaway container: applied as
                "10 service, 1 customers, 1 tests (9 left NULL)" with no drift
                warning; a second run changed 0 rows (and fired the intended
                warning, which is the drift detector working); the downgrade
                returned the table BYTE-IDENTICAL to the prod snapshot it started
                from.

        031 — DATA ONLY: sets `tenants.byo_mode = 'local_allowed'` on the 34 rows
                measured on prod 2026-09-05 as kind='service' AND
                byo_mode='byo_required'. It adds no schema object of any kind, so
                it moves none of test_schema_parity's structural counts. WHY: a
                `kind='service'` row is a machine identity (`svc-*`, minted by
                scripts/provision_service_token.py), and `byo_required` means
                "commercial: must bring their own provider; worker_management
                hard-fails a run with no configured one" (models.py:890) — which
                a machine with no account and nowhere to put an API key cannot
                ever satisfy, so the only reachable outcome is a 402. svc-kag
                took exactly that 402 on POST /workers/kag_synthesize/message at
                2026-09-03T04:16:53Z. The other 9 service tenants already carry
                'local_allowed' with zero counter-examples, which makes this a
                transcription rather than a judgement call; 7 of those 9 were
                corrected by hand on 2026-07-23 for the same reason (see the note
                in scripts/backfill_tenants_byo_mode.py). Safe against prod's
                data: the UPDATE names its 34 rows BY ID — the immutable key,
                because 024 and 026 both RENAME registry rows — and is guarded
                `WHERE byo_mode = 'byo_required'`, so a re-run changes 0 rows AND
                an operator's later correction is never overwritten;
                'local_allowed' is a member of ck_tenants_byo_mode (021) so the
                write cannot fail on a constraint; and drift from the measurement
                is a RAISE WARNING, never an EXCEPTION, because a raise here
                CrashLoops the ext_authz pod. It deliberately does NOT select on
                `WHERE kind='service'`: that would sweep rows created after
                2026-09-05 and make the downgrade set unknowable. Instead it
                REPORTS any such row in a second warning, because the root cause
                is upstream and unfixed — models.py:896 server-defaults byo_mode
                to 'byo_required' and ensure_tenant() cannot override it, so
                provision_service_token.py mints every new service principal into
                this exact state.
                VERIFIED 2026-09-05 against the REAL 76 tenant rows exported from
                prod into a throwaway postgres:18 container built from prod's own
                pg_dump'd DDL (real CHECK constraints included): the upgrade moved
                exactly 34 rows (service became 43 local_allowed / 0 byo_required,
                every other kind unchanged); a second run changed 0 rows and fired
                the intended drift warning; a newly-inserted kind='service' row
                was REPORTED by the generator warning and NOT swept; and the
                downgrade returned the table BYTE-IDENTICAL to the prod snapshot
                it started from (md5 92c1aa35f5e8a55ffaa533a0c65499c1 both sides).

        ⚠️ 021-025 are all APPLIED on prod as of 2026-09-01 (025 at 2026-08-28
        16:18:51Z, measured from `schema_migrations`). They stay in this list
        because the assertion pins the last N FILENAMES — the window is what
        gives the gate its strength, and narrowing it to only the
        genuinely-unapplied file would weaken the ratchet to match whatever was
        added last.
        ⚠️ UPDATED 2026-09-05: 026, 027 and 028 are now APPLIED TOO — measured
        from prod's `schema_migrations` (028 at 2026-09-04 09:44:59Z), so the
        "026, 027 and 028 are the files prod has not run" that stood here is
        stale. **029 and 030 are the only files prod has not run.** They stay
        listed alongside the applied ones for the window reason above.
        ⚠️ UPDATED 2026-09-05: 031 is added by this PR and is therefore also
        unapplied on prod — the unapplied set is now 029, 030 and 031.

        032 — DATA ONLY: sets `subject.tenant_id` on the 51 subjects whose owning
                tenant is derivable from data already in the database, and leaves
                the other 203 NULL. It adds no schema object, so it moves none of
                test_schema_parity's structural counts. WHY: 029 added the column
                and backfilled nothing, so it is NULL on all 254 prod rows —
                membership became expressible and was never expressed. The
                admission rule is `count(DISTINCT license.tenant_id) = 1` over ALL
                of a subject's licences AND that tenant is not itself a machine.
                Measured partition on prod 2026-09-05: 51 derivable, 41 whose only
                tenant is a `kind='service'` machine tenant, 8 ambiguous (>1
                tenant), 154 with no licence at all. The 41 are refused
                DELIBERATELY — writing `subject.tenant_id = <the svc-* tenant>`
                would record "a machine is the billable party" a second time, in
                the very column added to express the corrected shape, and would
                pre-empt step 0 of docs/svc-tenant-migration-plan.md (an
                operator-supplied 43-row svc-name -> owning-tenant table). Note
                the discriminator is the resolved TENANT's kind, not the
                SUBJECT's name: 17 of the 51 rows it DOES set are `svc-*`
                subjects whose licences already sit under a real org tenant, so a
                `subject LIKE 'svc-%'` filter would have been exactly backwards.
                Safe against prod's data: the UPDATE names its 51 (subject,
                tenant) pairs BY ID on both sides — the immutable keys, because
                024 and 026 both RENAME registry rows — and is guarded
                `AND s.tenant_id IS NULL`, so a re-run changes 0 rows AND an
                operator's later correction is never overwritten; every tenant id
                is an existing row so fk_subject_tenant (029) cannot be violated;
                the column stays nullable, because 203 rows are legitimately
                undecided; and drift from the measurement is a RAISE WARNING,
                never an EXCEPTION, because a raise here CrashLoops the ext_authz
                pod. It deliberately does NOT re-run the derivation as a predicate
                at apply time: that would sweep subjects created after 2026-09-05
                and make the downgrade set unknowable. Instead it REPORTS any such
                row in a drift warning.
                VERIFIED 2026-09-05 against the REAL 254 subject / 258 licence /
                76 tenant rows exported from prod into a throwaway postgres:18
                container: the upgrade set exactly 51 and reported exactly
                41 machine-parented / 8 ambiguous / 154 without a licence, with no
                drift warning; a second run changed 0 rows and fired the intended
                already-applied warning; a newly-inserted derivable subject was
                REPORTED by the drift warning and NOT swept; and the downgrade
                returned `subject` BYTE-IDENTICAL to the prod snapshot it started
                from (md5 e213011587447f1986e76b575e3b38fd both sides).

        033 — SCHEMA ONLY: adds three CHECK constraints in one file —
                ck_license_status, ck_token_token_type and
                ck_tenants_service_not_byo_required (the constraint 031's own
                header proposed and declined to add). Safe against prod's data:
                each ADD CONSTRAINT is preceded by a row count against its own
                predicate and a violation is a named RAISE EXCEPTION, never a
                bare constraint failure; each is guarded on pg_constraint so a
                re-run is a no-op; no row is inserted, updated or deleted.
                Measured live 2026-09-07: license.status is active(164)/
                revoked(92)/expired(2) — 'pending' is included in the CHECK
                anyway (0 rows, but a documented intended stored value, see the
                file header); token.token_type is api(176)/internal(74) — 'node'
                is included anyway (0 rows, but a live reachable enum value via
                POST /licenses/{id}/tokens?token_type=node); and 0 tenants rows
                hold kind='service' AND byo_mode='byo_required' (031 already
                fixed the 34 that had accumulated). The tenants CHECK is only
                safe because `DatabaseManager.ensure_tenant` is fixed in the same
                commit: `kind='service'` with no explicit `byo_mode` now defaults
                to 'local_allowed' instead of falling through to the column's
                own server_default('byo_required') — closing the gap for every
                caller of the shared primitive, not just
                provision_service_token.py (which #183 already fixed at its one
                call site).

        034 — SCHEMA ONLY: adds `ck_tenants_cost_centre`, pinned to the six
                values measured in production (`service, tests, customers, ci,
                improvement, merqi`) plus NULL — NULL stays legal for the 4
                tenants with no cost centre (`default-anon`, `george-dekker`,
                `nonagentic`, `portal`), an apportionment decision this service
                must never guess. Guarded/idempotent; refuses with a named
                exception listing offenders if any row falls outside the
                vocabulary. No row is inserted, updated or deleted.

        035 — REPORT ONLY: no schema object at all. A DO block walks
                `tenants` x `subject` (both directions: `keycloak_sub` and
                `subject.id`) and RAISEs NOTICE/WARNING per offending identity.
                It CANNOT be made fatal: 3 rows match on prod today
                (`portal-render-e2e`, `book-fulfillment` kind='customer', and
                the deliberate `default-anon` sentinel) and
                `seed_platform_tenants()` re-INSERTs two of them on EVERY boot,
                so a RAISE EXCEPTION here would CrashLoop both replicas of the
                platform auth path. A CHECK cannot express a cross-table
                predicate and a trigger was rejected (BEFORE INSERT fires before
                ON CONFLICT arbitration, and this repo has no triggers at all) —
                the enforcement lives in `DatabaseManager.ensure_tenant`
                (write-time refusal), `report_at_boot` and the
                `nonagentic_license_tenant_is_subject_rows{class,match,source}`
                gauge. ⚠️ Single-source: blind to `nonagentic-gtm`, a live
                Keycloak principal registered as a tenant that was never written
                to `subject`. No row is inserted, updated or deleted.
        """
        pending = [
            "021_add_tenants_byo_mode_check.sql",
            "022_add_app_audience.sql",
            "023_add_tenant_archived_at.sql",
            "024_rename_platform_cp_placeholder_names.sql",
            "025_add_tenant_app_byo_mode_offering_code.sql",
            "026_rename_improvement_tenant_placeholders.sql",
            "027_add_tenant_kind_and_cost_centre.sql",
            "028_add_bundle_and_bundle_app.sql",
            "029_add_subject_tenant.sql",
            "030_backfill_tenant_cost_centre.sql",
            "031_service_tenants_are_not_byo.sql",
            "032_backfill_subject_tenant.sql",
            "033_enum_checks_and_service_byo_guard.sql",
            "034_tenant_cost_centre_vocabulary.sql",
            "035_tenant_id_must_not_be_a_subject.sql",
            # Round 8: the audited licence soft delete. It is PENDING relative to
            # prod like every entry above it — this list is the set that has not
            # run there yet, so a new migration belongs here on the day it lands,
            # not when it is deployed.
            "036_license_soft_delete.sql",
            # Round 9 (#217): the SECOND door onto the tenant/subject invariant.
            # 035 reports a tenant id that is a principal; this reports the mirror
            # — a `subject.keycloak_sub` that is a registered `tenants.id` — and
            # records the guard now applied in `create_or_get_subject`. Like 035 it
            # creates no schema object and inserts, updates or deletes NO ROW: the
            # offending ids are live serving identities whose billing history lives
            # in another service's database with no FK back to `tenants`, so
            # re-keying them is a commercial decision (#217), not a migration.
            "037_subject_sub_must_not_be_a_tenant_id.sql",
            # w53 admin purge: `token.granted_roles` + two CHECKs — the only place a
            # licence token can carry a role (a short-lived operator token).
            "038_token_granted_roles.sql",
            # Bundle licences: `tenant_bundle` + `license.bundle_id`, and the conveyor
            # apps that exist are linked into `non-agentic-ai`.
            "039_tenant_bundle_and_license_bundle.sql",
            # #239: the `inference-router` bundle (data only). Links the prod-only
            # `inference-router` app when it exists; mints no licence.
            "040_inference_router_bundle.sql",
            # #239: per-licence token_budget_override / quota_override, read by
            # provisioning AND the reconcile sweep (schema-only, two CHECKs).
            "041_license_metering_overrides.sql",
            # George 2026-10-02: tenants.byo_mode DEFAULT -> 'local_allowed' (metadata
            # only; no existing row is updated).
            "042_tenants_byo_mode_default_local_allowed.sql",
            # PLAN §2.2: tenants.budget_currency (NULL | 'EUR'); metadata only, every
            # existing row stays NULL.
            "043_tenants_budget_currency.sql",
            # licenses#244: token.long_lived_owner / long_lived_reason + one CHECK
            # (the long-lived register); no backfill, every existing row stays NULL.
            "044_token_long_lived_register.sql",
            # George 2026-10-06: re-key book-fulfillment off its Keycloak principal
            # id. One atomic DO block, no-op when the old tenant is absent. Checked
            # against prod 2026-10-06: tenants has exactly the 9 columns copied, the
            # 4 inbound FKs are the 4 tables repointed, no triggers; 2 licences,
            # 2 subjects, 1 tenant_bundle move. Raises only if both ids exist.
            "045_rekey_book_fulfillment_tenant.sql",
            # George 2026-10-06: the same re-key for portal-render-e2e (its id is a
            # deleted portal user's sub, still subject 8382f0b3's keycloak_sub).
            # Same DO block as 045 with the ids swapped. Read-only prod census
            # 2026-10-06: 11 licences (6 active), 1 subject, 1 tenant_bundle,
            # 0 tenant_app_byo_mode, 0 `svc:<old>:` subjects move; the bare
            # keycloak_sub and license.metadata are not touched.
            "046_rekey_portal_render_e2e_tenant.sql",
            # 2026-10-07: subject.actor_sub (+1 NULLable uuid column) and the stated
            # service-account sub for 31 service subjects, matched by exact
            # keycloak_sub; fills NULLs only. keycloak_sub is not touched.
            "047_subject_actor_sub.sql",
            # 2026-10-07: subject.actor_sub for the 12 new service SAs (gitops #4527) and
            # 11 tenant-scoped licences whose single presenter was verified live. Data
            # only; fills NULLs only; keycloak_sub is not touched.
            "048_subject_actor_sub_presenters.sql",
            # George 2026-10-07 (unit B2): the same re-key for the cdmsfirst demo
            # tenant, off the reserved slot …-100. Same DO block as 046 with the
            # ids swapped. Read-only prod census 2026-10-07: 4 licences (3 active),
            # 3 subjects, 1 tenant_bundle, 0 tenant_app_byo_mode, 0 `svc:<old>:`
            # subjects move; no subject's keycloak_sub is or contains the old id.
            "049_rekey_cdmsfirst_demo_tenant.sql",
            # 2026-10-13: tenants owned per app. 4 new tables + seeds, tenants
            # gains owner_key (backfilled: all 'platform' except netcare /
            # cp-e2e-2026-10-13 -> commonpractice, ola-armatys -> w2v) and
            # external_ref; UNIQUE(name) swapped for UNIQUE(owner_key, name); one
            # grant per non-owner issuer with an active licence. One transaction;
            # pinned by test_migration_050_tenant_ownership.py.
            "050_tenant_ownership_per_app.sql",
            # George 2026-10-14: the n8n-nodes bundle (+ tenant-pooled plans for its
            # six apps) and the legacy netcare tenant retired as netcare-legacy
            # (platform, grants revoked) so CP can register a fresh netcare. DATA
            # ONLY, one transaction; pinned by test_migration_051_n8n_nodes_bundle.py.
            "051_n8n_nodes_bundle_and_retire_legacy_netcare.sql",
            # The per-bundle ensurer allowlist (bundle_ensurer) closing the hole
            # where any owner could ensure any bundle (fr -> commonpractice). One
            # transaction; pinned by test_migration_052_bundle_ensurer.py.
            "052_bundle_ensurer.sql",
            # Tenant names / external_refs unique per owner among LIVE tenants only
            # (both 050 unique indexes made partial on archived_at IS NULL). One
            # transaction; pinned by test_migration_053_tenant_names_live.py.
            "053_tenant_names_unique_among_live.sql",
            # Owners for the four bundle-token-minter clients + merqi / w2v service
            # tenants moved off platform. Data only, one transaction; pinned by
            # test_migration_054_bundle_minter_owners.py.
            "054_bundle_minter_owners_and_service_tenant_owners.sql",
            # Owner fr for provisioning's bundle-token minter client. Data only, one
            # transaction; pinned by test_migration_055_provisioning_bundle_minter.py.
            "055_provisioning_bundle_minter_owner.sql",
            # Owner merqi for growth's bundle-token minter client. Data only, one
            # transaction; pinned by test_migration_056_growth_bundle_minter.py.
            "056_growth_bundle_minter_owner.sql",
            # Owner kag, its licences client, kag added to four apps' issuers and
            # the kag-research bundle (ensurer kag). Data only, one transaction;
            # pinned by test_migration_057_kag_owner.py.
            "057_kag_owner_and_kag_research_bundle.sql",
            # New tenants default to byo_mode 'byo_required' (licenses#273). Column
            # DEFAULT only, no row touched; pinned by
            # test_migration_058_tenants_byo_mode_default.py.
            "058_tenants_byo_mode_default_byo_required.sql",
            # app_requires + bundle_caller tables, license.is_dependency (NOT NULL
            # DEFAULT false: metadata-only ADD COLUMN on PG11+, plus a CHECK that
            # every existing row satisfies since all are false), the conveyor
            # bundle, fr/w2v bundle-minter clients. One transaction, guarded DDL;
            # a cycle in app_requires aborts it. Pinned by
            # test_migration_059_app_requires.py.
            "059_app_requires_and_conveyor_bundle.sql",
            # Owner kag for kag-frontend's bundle-token minter client. Data only,
            # one transaction; pinned by test_migration_060_kag_frontend_bundle_minter.py.
            "060_kag_frontend_bundle_minter_owner.sql",
            # Owner kag for kag's own outbound bundle-token minter client. Data only,
            # one transaction; pinned by test_migration_061_kag_bundle_minter.py.
            "061_kag_bundle_minter_owner.sql",
            # The evidence-review app + kag -> evidence-review dependency edge. Data
            # only, one transaction; pinned by test_migration_062_evidence_review_app.py.
            "062_evidence_review_app.sql",
            # The kag-service bundle = {kag} + its ensurer {kag}. Data only, one
            # transaction; pinned by test_migration_063_kag_service_bundle.py.
            "063_kag_service_bundle.sql",
            # Owner kag for karma's outbound bundle-token minter client. Data only,
            # one transaction; pinned by test_migration_064_karma_bundle_minter.py.
            "064_karma_bundle_minter_owner.sql",
            # secrets joins the conveyor bundle + bundle_caller (conveyor, fr, secrets).
            # Data only, one transaction; pinned by
            # test_migration_065_secrets_in_conveyor_bundle.py.
            "065_secrets_in_conveyor_bundle.sql",
            # skill-builder joins the conveyor bundle + bundle_caller
            # (conveyor, fr, skill-builder). Data only, one transaction; pinned by
            # test_migration_066_skill_builder_in_conveyor_bundle.py.
            "066_skill_builder_in_conveyor_bundle.sql",
            # The strata apps (strata-configurator, care-bus): owners, the
            # configurator's licences api_client, apps + plans + issuers. Data only,
            # one transaction; pinned by test_migration_067_strata_apps.py.
            "067_strata_configurator_and_care_bus_apps.sql",
            # care-bus's licences api_client (owner care-bus, not cross-owner). Data
            # only, one transaction; pinned by
            # test_migration_068_care_bus_licenses_admin_client.py.
            "068_care_bus_licenses_admin_client.sql",
            # Grants strata-configurator + care-bus on fr-test-user (owner stays
            # platform). Data only, one transaction; pinned by
            # test_migration_069_fr_test_user_strata_grants.py.
            "069_fr_test_user_strata_grants.sql",
            # The agent app: owner, its licences api_client, app + plan + issuer.
            # Data only, one transaction; pinned by test_migration_070_agent_app.py.
            "070_agent_app.sql",
            # The agent app's `byo` plan (agent issues on byo, like CommonPractice).
            # Data only, one transaction; pinned by test_migration_071_agent_byo_plan.py.
            "071_agent_byo_plan.sql",
            # realm_owner (realm-per-app P0): +1 table + the commonpractice seed row;
            # one transaction, pinned by test_migration_072_realm_owner.py.
            "072_realm_owner.sql",
            # Grants w2v on the svc-agent-api service tenant (owner stays platform;
            # no-op where the tenant is absent). Data only, one transaction; pinned
            # by test_migration_073_svc_agent_api_w2v_grant.py.
            "073_svc_agent_api_w2v_grant.sql",
            # realm_owner row ('agent', 'agent') for the agent app's own realm. Data
            # only, one transaction; pinned by test_migration_074_realm_owner_agent.py.
            "074_realm_owner_agent.sql",
            # The `speech` app (TTS / ASR shared platform services): one app + plan +
            # issuer (platform). Data only, one transaction; pinned by
            # test_migration_075_speech_platform_app.py.
            "075_speech_platform_app.sql",
            # realm_owner row ('w2v', 'w2v') for Studio's own realm. Data only, one
            # transaction; pinned by test_migration_076_realm_owner_w2v.py.
            "076_realm_owner_w2v.sql",
            # realm_owner row ('fr', 'fr') for the fr app's own realm. Data only, one
            # transaction; pinned by test_migration_077_realm_owner_fr.py.
            "077_realm_owner_fr.sql",
            # care-bus early access: realm_owner care-bus, care-bus-bundle-minter, the
            # strata apps, care-bus -> {mcp, strata-*} edges, the care-bus bundle. Data
            # only, one transaction; pinned by test_migration_078_care_bus_early_access.py.
            "078_care_bus_early_access.sql",
            # The owner (platform) ensures the care-bus bundle on fr-test-user:
            # services/bundle_ensure's rules in SQL, guarded (no-op where the tenant is
            # absent or not platform's), one transaction; pinned by
            # test_migration_079_fr_test_user_care_bus_bundle.py.
            "079_fr_test_user_care_bus_bundle.sql",
            # care-bus -> laya edge + the laya dependency licence on every tenant
            # holding the care-bus bundle (bundle_ensure's dependency rules in SQL),
            # one transaction; pinned by test_migration_080_care_bus_laya.py.
            "080_care_bus_laya.sql",
            # owner mcp + mcp-bundle-minter, mcp -> {coding, strata-rag, strata-ingest,
            # laya}, the agent bundle + ensurer + agent-bundle-minter, agent -> mcp. Data
            # only, one transaction; pinned by test_migration_082_mcp_toolkit_entitlements.py.
            "082_mcp_toolkit_entitlements.sql",
            # secure-apps twins of portal/w2v-backend (w2v) and portal/provisioning-backend
            # (fr) in api_client (realm-per-app P7 chain C). Data only, one transaction;
            # pinned by test_migration_083_secure_apps_s2s_api_clients.py.
            "083_secure_apps_s2s_api_clients.sql",
            # Retires strata-configurator: its tenants move to agent, app revoked,
            # client deleted, grants soft-revoked (refuses while a licence is live).
            # Data only, one transaction; pinned by
            # test_migration_084_retire_strata_configurator.py.
            "084_retire_strata_configurator.sql",
            # api_client row secure-apps/w2v-cell-m4 -> w2v (the m4 cell's own S2S
            # client, so license-reader suffices). Data only, one transaction; pinned
            # by test_migration_085_w2v_cell_m4_api_client.py.
            "085_w2v_cell_m4_api_client.sql",
            # app_requires edge agent -> inference-router (caller agent). Data only, one
            # transaction; pinned by test_mcp_toolkit_entitlements.py.
            "086_agent_bundle_inference_router.sql",
            # 087 — DATA ONLY: one UPDATE of tenant_app_grant.revoked_at for
            #       grantee 'fr', excluding svc-n8n-local-conveyor. No schema
            #       object, no insert, no delete; touches at most 32 rows on a
            #       table that holds a few dozen. Safe to run against prod's data:
            #       a re-run matches nothing (revoked_at IS NULL is the predicate),
            #       and it cannot raise — no unique index is violated by setting
            #       revoked_at, and the partial index uq_tenant_app_grant_active
            #       is RELEASED by it, never contended.
            "087_revoke_stale_fr_tenant_grants.sql",
            # 088 — DATA ONLY: three INSERTs into tenant_dependency_grant for
            #       (execution tenant …0003, owner 'fr', app workers | secrets |
            #       skill-builder), each guarded on the app being a member of a
            #       non-revoked bundle the tenant already holds. No schema object,
            #       no UPDATE, no DELETE. Safe to run against prod's data: the PK
            #       (tenant_id, owner_key, app_id) makes a re-run a no-op, every
            #       referenced row is already present, and the guard is exactly
            #       the app-in-bundle condition the bundle-token route enforces at
            #       mint time — so it cannot grant something unusable, and it
            #       writes nothing at all where that condition does not hold
            #       (verified on a database built from empty: 0 rows).
            "088_fr_dependency_grants_execution_tenant.sql",
            # app_requires edge agent -> conveyor (caller agent). Data only, one
            # transaction; pinned by test_migration_089_agent_bundle_conveyor.py.
            "089_agent_bundle_conveyor.sql",
            # 090 — DATA ONLY: app_issuer (conveyor -> fr) when the app exists (it is
            #       created at runtime, so the leg announces-and-skips on a database
            #       built from empty) and api_client (secure-apps, conveyor-backend,
            #       fr, not cross_owner) so the conveyor's Platform wizard can mint
            #       conveyor licences through provisioning, whose own SA is fr-owned.
            #       No schema object, no UPDATE, no DELETE; ON CONFLICT DO NOTHING
            #       makes a re-run a no-op and never overwrites an operator's row.
            #       Pinned by test_migration_090_conveyor_service_issuer.py.
            "090_conveyor_service_issuer.sql",
        ]
        files = sorted(p.name for p in MIGRATIONS.glob("*.sql"))
        assert files[-len(pending) :] == pending, (
            f"pending set changed: expected {pending}, directory ends with {files[-len(pending) :]}"
        )
        assert len(files) == 20 + len(pending), (
            f"expected {20 + len(pending)} migration files, found {len(files)}: {files}"
        )
