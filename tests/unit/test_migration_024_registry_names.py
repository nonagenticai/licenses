"""Migration 024 renames two registry rows; these pin it to everything else.

WHY THIS FILE EXISTS. `tests/unit/test_bootstrap_seed.py` already pins
`PLATFORM_TENANTS` against migrations 013/014, because a fresh database is seeded
from the constant while an existing one is migrated by the SQL. Migration 024
introduces a THIRD statement of the same fact — the rename an already-existing
database receives — and nothing pinned it. Change the constant without changing
024 and prod diverges from a rebuild silently, which is exactly the bug class the
existing gate was written to close.

The names themselves are a claim about identity, so it is worth writing down what
they mean: `0195f48b-…-0001` and `…-0003` are the canonical uuid7s of n8n-dev and
george-dev in the Keycloak PORTAL realm — the literal `tenant_id` claim in live
user JWTs — not anonymous platform slots. They were registered under invented
`platform-cp-N` labels on 2026-07-10 and kept them. 024 makes the registry say
who they are; it re-points nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

from models import PLATFORM_TENANTS

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "migrations"
UP = MIGRATIONS / "024_rename_platform_cp_placeholder_names.sql"
DOWN = MIGRATIONS / "downgrades" / "024_rename_platform_cp_placeholder_names.sql"

#: `('<uuid>'::uuid, '<from>', '<to>')` — the VALUES rows both files drive their
#: loop from.
_PAIR = re.compile(r"\(\s*'([0-9a-f-]{36})'::uuid\s*,\s*'([a-z0-9-]+)'\s*,\s*'([a-z0-9-]+)'\s*\)")

#: `ck_tenants_name_format` on prod, transcribed. A name 024 writes must satisfy it.
_NAME_CHECK = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _pairs(path: Path) -> set[tuple[str, str, str]]:
    # `m.group(N)` rather than `m.groups()`: the latter is typed
    # `tuple[str | Any, ...]`, which does not narrow to the 3-tuple the callers
    # unpack. Same idiom as `test_bootstrap_seed._seeded_in`.
    return {(m.group(1), m.group(2), m.group(3)) for m in _PAIR.finditer(path.read_text())}


class TestTheRenameIsParseableAtAll:
    """Positive control. Every assertion below compares sets built by `_PAIR`; if
    the regex stops matching, they all become vacuous empty-set equalities."""

    def test_the_up_migration_declares_exactly_two_renames(self):
        assert len(_pairs(UP)) == 2

    def test_the_downgrade_declares_exactly_two_renames(self):
        """Doubles as the gate that the downgrade is COMMITTED at all.

        `.gitignore` blanket-ignores `*.sql` and un-ignores it with
        `!migrations/*.sql`, which does not match a subdirectory — so
        `migrations/downgrades/*.sql` needs its own exception or it is silently
        dropped from the commit that adds it, and you discover that only when you
        need to roll back. If that happens, this reads a file CI does not have and
        fails here rather than passing vacuously.
        """
        assert DOWN.exists(), f"{DOWN} is missing — check the .gitignore exception"
        assert len(_pairs(DOWN)) == 2


class TestTheDowngradeIsReal:
    def test_it_is_the_exact_inverse_of_the_up_migration(self):
        """Not "a downgrade exists" — the same ids, with from/to swapped."""
        inverted = {(tid, new, old) for tid, old, new in _pairs(UP)}
        assert _pairs(DOWN) == inverted, (
            "the downgrade is not the inverse of 024.\n"
            f"  up (inverted): {sorted(inverted)}\n"
            f"  down:          {sorted(_pairs(DOWN))}"
        )

    def test_the_runner_cannot_auto_apply_it(self):
        """`apply_migrations.py` does `migrations_dir.glob("*.sql")` — NON-recursive.

        If the downgrade were a sibling of 024 it would sort immediately after it
        and silently undo the migration on the very same boot. It must stay in the
        subdirectory, so assert on the glob the runner actually uses.
        """
        discovered = {p.name for p in MIGRATIONS.glob("*.sql")}
        assert UP.name in discovered, "024 must be discoverable by the runner"
        assert DOWN.parent.name == "downgrades"
        assert DOWN.resolve() not in {p.resolve() for p in MIGRATIONS.glob("*.sql")}, (
            "the downgrade is in migrations/ and WILL be applied right after 024"
        )


class TestTheRenameAgreesWithTheSeedConstant:
    def test_every_new_name_is_what_a_fresh_database_would_be_seeded_with(self):
        """The migrated path and the fresh path must produce the same names.

        024 never runs on a fresh database (the runner baselines it), so the
        constant is the ONLY thing that gives a rebuilt database these names. If
        the two disagree, prod and a DR rebuild hold different labels for the same
        uuid — and `get_tenant` resolves by NAME as well as id.
        """
        by_id = {tid: name for tid, name, _ in PLATFORM_TENANTS}
        for tenant_id, old_name, new_name in _pairs(UP):
            assert tenant_id in by_id, (
                f"024 renames {tenant_id}, which PLATFORM_TENANTS does not seed"
            )
            assert by_id[tenant_id] == new_name, (
                f"024 renames {tenant_id} to {new_name!r} but the constant seeds it "
                f"as {by_id[tenant_id]!r} — a fresh database and a migrated one "
                f"would disagree"
            )

    def test_the_placeholder_names_are_gone_from_the_constant(self):
        """The lie must not be re-seeded by a future edit to the constant."""
        seeded = {name for _, name, _ in PLATFORM_TENANTS}
        for _, old_name, _ in _pairs(UP):
            assert old_name not in seeded, (
                f"{old_name!r} is back in PLATFORM_TENANTS — a fresh database would "
                f"re-create the placeholder 024 exists to remove"
            )


class TestTheNewNamesAreLegalAndDistinct:
    def test_they_satisfy_ck_tenants_name_format(self):
        for _, _, new_name in _pairs(UP):
            assert _NAME_CHECK.match(new_name), (
                f"{new_name!r} violates ck_tenants_name_format — the UPDATE would "
                f"raise inside _run_db_bootstrap and the pod would refuse to serve"
            )

    def test_they_do_not_collide_with_anything_the_constant_already_seeds(self):
        """`tenants.name` is UNIQUE. 024 guards on this at runtime too, but a
        collision here means the guard would SKIP the rename and the lie would
        survive a migration that reported success."""
        others = [
            name for tid, name, _ in PLATFORM_TENANTS if tid not in {p[0] for p in _pairs(UP)}
        ]
        for _, _, new_name in _pairs(UP):
            assert new_name not in others, f"{new_name!r} is already seeded by another row"

    def test_the_two_renames_do_not_collide_with_each_other(self):
        new_names = [new for _, _, new in _pairs(UP)]
        assert len(new_names) == len(set(new_names))


class TestTheMigrationCannotBreakTheAuthzPod:
    """024 runs inside `_run_db_bootstrap`. Anything that raises there stops the
    pod serving, and this service backs ext_authz for the whole platform."""

    def test_the_rename_is_guarded_on_the_old_name(self):
        """Idempotence, and it means an operator's later rename is not clobbered."""
        assert "AND name = r.old_name" in UP.read_text()

    def test_the_rename_is_guarded_on_the_target_being_free(self):
        """UNIQUE(name) must not be able to raise."""
        sql = UP.read_text()
        assert "EXISTS (SELECT 1 FROM tenants WHERE name = r.new_name)" in sql
        assert "CONTINUE;" in sql

    def test_it_touches_no_table_but_tenants(self):
        """024 renames rows. It must not UPDATE/DELETE/INSERT anything else — the
        whole point is that no licence, token or FK moves."""
        sql = UP.read_text()
        body = sql[sql.index("DO $$") :]
        writes = re.findall(r"\b(?:UPDATE|DELETE\s+FROM|INSERT\s+INTO)\s+(\w+)", body)
        assert writes == ["tenants"], f"024 writes to more than tenants: {writes}"
