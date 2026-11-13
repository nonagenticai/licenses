"""Migration 026 names the last two `platform-cp-N` placeholders; these pin it.

WHY THIS FILE EXISTS. Same reason as `test_migration_024_registry_names.py`: a
fresh database is seeded from `PLATFORM_TENANTS` while an existing one is
migrated by the SQL, so a rename states the same fact in three places (026, 014,
the constant) and all three must move together or prod diverges from a rebuild
silently.

WHAT THE NAMES CLAIM. `0195f48b-…-0004` and `…-0005` are fr's internal
improvement-loop tenants — `prompt-improvement` and `procedure-improvement`.
That is not an inference: fr migrations 062 and 066 INSERT these exact uuids
under exactly these names, fr's `streams_kind_tenant_check` pins kind->uuid, the
live fr `workspaces` table returns them, and the Keycloak realm-patch script
mints them as the `tenant_id` claim for `system-prompt-operator`,
`aa-card-writer-prompt` and `aa-card-writer-procedure`. 024 deliberately left
them alone because none of that had been found yet. The names here are
TRANSCRIBED from the system that owns the identities, not invented — which is
the only thing that distinguishes this rename from the 2026-07-10 seeding that
created the placeholders in the first place.

⛔ …0002 IS NOT RENAMED, AND THERE IS A TEST BELOW THAT KEEPS IT THAT WAY. fr
calls it `workflow-improvement`, this registry calls it `platform-core`, and the
portal realm retired `gerard-dev` at it. Two well-sourced contradicting claims is
a decision (billing #86), not a transcription.
"""

from __future__ import annotations

import re
from pathlib import Path

from models import PLATFORM_TENANTS

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "migrations"
UP = MIGRATIONS / "026_rename_improvement_tenant_placeholders.sql"
DOWN = MIGRATIONS / "downgrades" / "026_rename_improvement_tenant_placeholders.sql"

#: `('<uuid>'::uuid, '<from>', '<to>')` — the VALUES rows both files drive their
#: loop from.
_PAIR = re.compile(r"\(\s*'([0-9a-f-]{36})'::uuid\s*,\s*'([a-z0-9-]+)'\s*,\s*'([a-z0-9-]+)'\s*\)")

#: `ck_tenants_name_format` on prod, transcribed. A name 026 writes must satisfy it.
_NAME_CHECK = re.compile(r"^[a-z0-9][a-z0-9-]*$")

#: The disputed row. 026 must not touch it in any of its three statements.
_DISPUTED_ID = "0195f48b-d000-7000-8000-000000000002"


def _pairs(path: Path) -> set[tuple[str, str, str]]:
    return {(m.group(1), m.group(2), m.group(3)) for m in _PAIR.finditer(path.read_text())}


class TestTheRenameIsParseableAtAll:
    """Positive control. Every assertion below compares sets built by `_PAIR`; if
    the regex stops matching, they all become vacuous empty-set equalities."""

    def test_the_up_migration_declares_exactly_two_renames(self):
        assert len(_pairs(UP)) == 2

    def test_the_downgrade_declares_exactly_two_renames(self):
        """Doubles as the gate that the downgrade is COMMITTED at all.

        `.gitignore` blanket-ignores `*.sql`; `migrations/downgrades/*.sql` has
        its own un-ignore exception (added for 024). If that ever regresses, this
        reads a file CI does not have and fails here rather than passing
        vacuously.
        """
        assert DOWN.exists(), f"{DOWN} is missing — check the .gitignore exception"
        assert len(_pairs(DOWN)) == 2


class TestTheDowngradeIsReal:
    def test_it_is_the_exact_inverse_of_the_up_migration(self):
        inverted = {(tid, new, old) for tid, old, new in _pairs(UP)}
        assert _pairs(DOWN) == inverted, (
            "the downgrade is not the inverse of 026.\n"
            f"  up (inverted): {sorted(inverted)}\n"
            f"  down:          {sorted(_pairs(DOWN))}"
        )

    def test_the_runner_cannot_auto_apply_it(self):
        """`apply_migrations.py` does `migrations_dir.glob("*.sql")` — NON-recursive.

        A sibling downgrade would sort immediately after 026 and undo it on the
        very same boot.
        """
        discovered = {p.name for p in MIGRATIONS.glob("*.sql")}
        assert UP.name in discovered, "026 must be discoverable by the runner"
        assert DOWN.parent.name == "downgrades"
        assert DOWN.resolve() not in {p.resolve() for p in MIGRATIONS.glob("*.sql")}, (
            "the downgrade is in migrations/ and WILL be applied right after 026"
        )


class TestTheRenameAgreesWithTheSeedConstant:
    def test_every_new_name_is_what_a_fresh_database_would_be_seeded_with(self):
        """026 never runs on a fresh database (the runner baselines it), so the
        constant is the ONLY thing that gives a rebuilt database these names."""
        by_id = {tid: name for tid, name, _ in PLATFORM_TENANTS}
        for tenant_id, _old_name, new_name in _pairs(UP):
            assert tenant_id in by_id, (
                f"026 renames {tenant_id}, which PLATFORM_TENANTS does not seed"
            )
            assert by_id[tenant_id] == new_name, (
                f"026 renames {tenant_id} to {new_name!r} but the constant seeds it "
                f"as {by_id[tenant_id]!r} — a fresh database and a migrated one "
                f"would disagree"
            )

    def test_the_placeholder_names_are_gone_from_the_constant(self):
        """The placeholder must not be re-seeded by a future edit to the constant."""
        seeded = {name for _, name, _ in PLATFORM_TENANTS}
        for _, old_name, _ in _pairs(UP):
            assert old_name not in seeded, (
                f"{old_name!r} is back in PLATFORM_TENANTS — a fresh database would "
                f"re-create the placeholder 026 exists to remove"
            )


class TestTheNewNamesAreLegalAndDistinct:
    def test_they_satisfy_ck_tenants_name_format(self):
        for _, _, new_name in _pairs(UP):
            assert _NAME_CHECK.match(new_name), (
                f"{new_name!r} violates ck_tenants_name_format — the UPDATE would "
                f"raise inside _run_db_bootstrap and the pod would refuse to serve"
            )

    def test_they_do_not_collide_with_anything_the_constant_already_seeds(self):
        """`tenants.name` is UNIQUE. 026 guards on this at runtime too, but a
        collision here means the guard would SKIP the rename and the placeholder
        would survive a migration that reported success."""
        renamed_ids = {p[0] for p in _pairs(UP)}
        others = [name for tid, name, _ in PLATFORM_TENANTS if tid not in renamed_ids]
        for _, _, new_name in _pairs(UP):
            assert new_name not in others, f"{new_name!r} is already seeded by another row"

    def test_the_two_renames_do_not_collide_with_each_other(self):
        new_names = [new for _, _, new in _pairs(UP)]
        assert len(new_names) == len(set(new_names))


class TestTheDisputedRowIsLeftAlone:
    """…0002 is claimed as `workflow-improvement` by fr and `platform-core` here.

    The evidence that resolved …0004/…0005 does NOT resolve …0002 — it sharpens
    the collision. A future edit that quietly folds …0002 into this migration
    would be taking a side in billing #86 under cover of a transcription.
    """

    def test_the_migration_does_not_rename_it(self):
        assert _DISPUTED_ID not in {tid for tid, _, _ in _pairs(UP)}

    def test_the_seed_constant_does_not_name_it_at_all(self):
        """…0002 is deliberately ABSENT from `PLATFORM_TENANTS` and from 014.

        Measured 2026-09-01: the constant seeds 8 rows and …0002 is not one of
        them — the `platform-core` row exists only in prod, created outside this
        corpus. So a fresh database does not currently assert ANY name for that
        uuid, which is the honest state while the dispute is open. Adding it here
        under either `platform-core` or fr's `workflow-improvement` would settle
        billing #86 by seeding, without a decision and without a migration.
        """
        assert _DISPUTED_ID not in {tid for tid, _, _ in PLATFORM_TENANTS}, (
            "…0002 is now seeded by PLATFORM_TENANTS. Whichever name it was given, "
            "that takes a side in billing #86 — fr calls it `workflow-improvement`, "
            "this registry calls it `platform-core`, and the portal realm retired "
            "`gerard-dev` at it. It needs its own change, with a decision attached."
        )


class TestTheMigrationCannotBreakTheAuthzPod:
    """026 runs inside `_run_db_bootstrap`. Anything that raises there stops the
    pod serving, and this service backs ext_authz for the whole platform."""

    def test_the_rename_is_guarded_on_the_old_name(self):
        assert "AND name = r.old_name" in UP.read_text()

    def test_the_rename_is_guarded_on_the_target_being_free(self):
        sql = UP.read_text()
        assert "EXISTS (SELECT 1 FROM tenants WHERE name = r.new_name)" in sql
        assert "CONTINUE;" in sql

    def test_it_touches_no_table_but_tenants(self):
        """026 renames rows. No licence, token or FK may move."""
        sql = UP.read_text()
        body = sql[sql.index("DO $$") :]
        writes = re.findall(r"\b(?:UPDATE|DELETE\s+FROM|INSERT\s+INTO)\s+(\w+)", body)
        assert writes == ["tenants"], f"026 writes to more than tenants: {writes}"
