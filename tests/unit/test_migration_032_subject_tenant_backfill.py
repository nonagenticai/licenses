"""Migration 032 parents the 51 subjects the database can answer for, and refuses 203.

WHY THIS FILE EXISTS. 029 added `subject.tenant_id` and backfilled nothing, so it
is NULL on all 254 prod rows — membership became expressible and was never
expressed. 032 fills the part that needs no operator input. The whole value of it
is the REFUSALS, and a refusal is exactly the thing a careless later edit
"completes":

* **41 subjects are derivable and deliberately left NULL** because their single
  licence tenant is itself a `kind='service'` machine. Writing that parentage
  would record "a machine is the billable party" a SECOND time, in the very
  column added to express the corrected shape, and would pre-empt step 0 of
  `docs/svc-tenant-migration-plan.md` (an operator-supplied 43-row
  svc-name -> owning-tenant table). `TestItRefusesTheMachineParentedSubjects` and
  the DB-backed `test_a_machine_parented_subject_is_left_null` are the pins.

* **8 subjects hold licences under MORE THAN ONE tenant and must never be
  guessed.** `tenant_id` is a single column; a multi-tenant subject is a real
  state it cannot express, and the honest value for a question the schema cannot
  answer is NULL. `test_an_ambiguous_subject_is_left_null_and_not_reported_as_
  derivable` is the pin, and it is a REAL one: it executes the migration against a
  seeded ambiguous subject and asserts the file's own derivation query does not
  select it.

* **The discriminator is the resolved TENANT's kind, never the SUBJECT's name.**
  17 of the 51 rows 032 DOES set are `svc-*` subjects whose licences already sit
  under a real org tenant (`svc-workers-ahtapol` -> `ahtapol`). A
  `subject LIKE 'svc-%'` filter would have excluded all 17 correct rows and
  included none of the 41 wrong ones — exactly backwards.
  `TestTheDiscriminatorIsTheTenantNotTheSubjectName` is the pin.

* **It names its rows and never re-derives at apply time.** Same "name the rows,
  never an else-branch" rule 027, 030 and 031 all state. A predicate would sweep
  subjects created after 2026-09-05 and make the downgrade set unknowable.

THE DB-BACKED HALF IS NOT DECORATION. The static assertions can only prove the
SQL text has a shape. `TestTheRuleEvaluatedAgainstARealDatabase` executes the
actual file against a seeded registry holding one subject from each of the four
measured buckets plus one derivable-but-unnamed subject, and asserts on what the
file's own drift query selects. Drop the ambiguity guard, drop the machine
exclusion, or widen the UPDATE into a predicate, and it goes red.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.support.legacy_owner_default import legacy_owner_default  # noqa: F401

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "migrations"
UP = MIGRATIONS / "032_backfill_subject_tenant.sql"
DOWN = MIGRATIONS / "downgrades" / "032_backfill_subject_tenant.sql"

#: The partition measured on prod 2026-09-05, by
#: `count(DISTINCT license.tenant_id)` per subject. Pinned so that widening or
#: narrowing the migration has to edit this too.
MEASURED_PARTITION = {
    "set_by_032": 51,  # exactly one licence tenant, and it is not a machine
    "machine_parented": 41,  # exactly one licence tenant, and it IS a machine
    "ambiguous": 8,  # more than one licence tenant
    "no_licence": 154,  # nothing to derive from
}
TOTAL_SUBJECTS = 254

_PAIR = re.compile(
    r"\('([0-9a-f-]{36})'::uuid,\s*'([0-9a-f-]{36})'::uuid\)",
)


def _up() -> str:
    return UP.read_text(encoding="utf-8")


def _down() -> str:
    return DOWN.read_text(encoding="utf-8")


def _up_body() -> str:
    """The executable part only — the header is prose and names many things."""
    return _up().split("DO $$", 1)[1]


def _pairs(sql: str) -> list[tuple[str, str]]:
    """The (subject_id, tenant_id) pairs inside the VALUES block."""
    block = sql.split("FROM (VALUES", 1)[1].split(") AS v(", 1)[0]
    return _PAIR.findall(block)


def _values_comments(sql: str) -> list[str]:
    block = sql.split("FROM (VALUES", 1)[1].split(") AS v(", 1)[0]
    return re.findall(r"--\s+(.+)$", block, re.MULTILINE)


class TestBothFilesExist:
    def test_the_upgrade_exists(self):
        assert UP.is_file(), f"missing {UP}"

    def test_it_has_a_matching_downgrade(self):
        """Repo convention: every reversible migration ships migrations/downgrades/<same name>."""
        assert DOWN.is_file(), f"missing {DOWN}"
        assert DOWN.name == UP.name

    def test_the_downgrade_is_not_picked_up_by_the_runner(self):
        """`apply_migrations.py` globs `migrations/*.sql` NON-recursively; a
        downgrade that landed in the top directory would be applied as a
        migration."""
        assert DOWN.resolve() not in {p.resolve() for p in MIGRATIONS.glob("*.sql")}


class TestItNamesExactlyTheFiftyOneMeasuredRows:
    def test_it_names_fifty_one_pairs(self):
        assert len(_pairs(_up())) == MEASURED_PARTITION["set_by_032"]

    def test_no_subject_is_listed_twice(self):
        subjects = [s for s, _ in _pairs(_up())]
        assert len(set(subjects)) == len(subjects), (
            "a duplicated subject uuid means the list was hand-edited; the second "
            "row would be a silent no-op under the IS NULL guard"
        )

    def test_every_pair_is_annotated(self):
        """An anonymous uuid list is unreviewable — a reader cannot tell what it hits."""
        assert len(_values_comments(_up())) == MEASURED_PARTITION["set_by_032"]

    def test_every_annotation_names_the_target_tenant(self):
        for comment in _values_comments(_up()):
            assert "->" in comment, f"unannotated pair: {comment!r}"

    def test_the_partition_sums_to_the_measured_population(self):
        """51 + 41 + 8 + 154 = 254. If this stops holding, the census in the
        header is describing a different database than the one it claims."""
        assert sum(MEASURED_PARTITION.values()) == TOTAL_SUBJECTS

    def test_the_header_states_the_whole_partition(self):
        """A migration that moves 51 of 254 rows must say what happened to the
        other 203 — otherwise "still NULL" reads as an oversight."""
        for count in MEASURED_PARTITION.values():
            assert str(count) in _up(), f"the header does not account for {count} subjects"


class TestItUsesTheImmutableKeyOnBothSides:
    def test_it_selects_subjects_on_id_not_on_keycloak_sub(self):
        assert "s.id = v.subject_id" in _up_body()
        assert "keycloak_sub =" not in _up_body()

    def test_it_writes_a_tenant_id_never_a_tenant_name(self):
        """024 and 026 both RENAME registry rows, so `tenants.name` is not stable
        across the window between writing this file and running it."""
        update = _up_body().split("UPDATE subject", 1)[1].split(";", 1)[0]
        assert "t.name" not in update and "tenants" not in update

    def test_it_uses_no_pattern_match_at_all(self):
        """A `keycloak_sub LIKE 'svc-%'` would be the exactly-backwards filter:
        it excludes the 17 correct svc-* rows and includes none of the 41 wrong
        ones. The discriminator is the TENANT's kind, not the subject's name."""
        assert "LIKE" not in _up_body()


class TestItNeverOverwritesAnOperator:
    def test_the_update_is_guarded_on_null(self):
        update = _up_body().split("UPDATE subject", 1)[1].split(";", 1)[0]
        assert "s.tenant_id IS NULL" in update, (
            "the UPDATE must be guarded AND s.tenant_id IS NULL — once an operator "
            "has stated a parent it is theirs, not this file's (027, 030, 031)"
        )

    def test_a_rerun_changes_nothing(self):
        """Guarded on the prior value, the statement is idempotent by construction."""
        assert _up_body().count("UPDATE subject") == 1

    def test_it_sets_no_other_column(self):
        assert set(re.findall(r"SET (\w+) =", _up())) == {"tenant_id"}

    def test_it_changes_no_kind_no_name_no_byo_mode(self):
        for forbidden in ("SET kind", "SET name", "SET byo_mode", "SET cost_centre"):
            assert forbidden not in _up()


class TestItRefusesTheMachineParentedSubjects:
    """The most important decision in the file, and the easiest one to undo."""

    def test_it_never_writes_a_service_tenant_as_a_parent(self):
        """No annotation may resolve to a `[service]` tenant. 032's own comments
        carry the resolved tenant kind precisely so this is checkable."""
        for comment in _values_comments(_up()):
            assert "[service]" not in comment, (
                f"032 parents a subject onto a MACHINE tenant: {comment!r} — that "
                "records 'a machine is the billable party' a second time and "
                "pre-empts step 0 of docs/svc-tenant-migration-plan.md"
            )

    def test_it_counts_and_reports_them_instead(self):
        assert "n_svc_parent" in _up_body()
        assert "t.kind = 'service'" in _up_body()

    def test_the_header_names_the_blocking_operator_step(self):
        """A reader who does not know step 0 exists will read 41 NULLs as an
        unfinished job and 'finish' it."""
        assert "svc-tenant-migration-plan.md" in _up()

    def test_the_refusal_is_stated_as_deliberate(self):
        assert "DO NOT" in _up() or "do not" in _up()


class TestItRefusesToGuessAnAmbiguousSubject:
    def test_it_counts_the_ambiguous_subjects(self):
        assert "n_ambiguous" in _up_body()

    def test_the_ambiguity_test_is_over_all_licences_not_just_active_ones(self):
        """Counting only active rows manufactures certainty: a subject with a
        revoked licence under A and an active one under B would read as
        unambiguous while the database plainly holds two answers."""
        assert "status" not in _up_body(), (
            "032 must derive from ALL of a subject's licences; filtering on "
            "`status` narrows the denominator and invents unambiguity"
        )

    def test_it_uses_a_distinct_count_not_an_any(self):
        assert "count(DISTINCT l.tenant_id)" in _up_body()


class TestTheDiscriminatorIsTheTenantNotTheSubjectName:
    def test_seventeen_svc_named_subjects_are_deliberately_included(self):
        """These are `svc-*` SUBJECTS whose licences already sit under a real org
        tenant — the target model already in effect. Excluding them (the obvious
        `LIKE 'svc-%'` reflex) would drop 17 correct rows."""
        svc_named = [c for c in _values_comments(_up()) if c.lstrip().startswith("svc-")]
        assert len(svc_named) == 17, (
            f"expected 17 svc-* subjects parented onto real org tenants, found "
            f"{len(svc_named)} — the discriminator has drifted to the subject name"
        )

    @pytest.mark.parametrize(
        "subject_name,tenant_name",
        [
            ("svc-workers-ahtapol", "ahtapol"),
            ("svc-kag-merqi", "merqi"),
            ("svc-inference-router-prompt-improvement", "prompt-improvement"),
        ],
    )
    def test_a_delegating_service_actor_is_parented_to_its_real_org(
        self, subject_name: str, tenant_name: str
    ):
        assert any(
            c.lstrip().startswith(subject_name + " ") and f"-> {tenant_name} " in c
            for c in _values_comments(_up())
        ), f"{subject_name} is no longer parented to {tenant_name}"


class TestItIsDataOnlyAndCannotTakeAuthzDown:
    @pytest.mark.parametrize(
        "forbidden", ["INSERT INTO", "DELETE FROM", "DROP ", "ALTER TABLE", "ADD CONSTRAINT"]
    )
    def test_it_contains_no_schema_or_row_lifecycle_statement(self, forbidden: str):
        assert forbidden not in _up_body(), (
            f"032 is a data migration; it must not contain {forbidden!r}"
        )

    def test_it_does_not_make_the_column_not_null(self):
        """203 subjects are legitimately undecided. NOT NULL is only reachable
        after step 0 of the svc-tenant plan. (Matched on the DDL forms, not on the
        string "NOT NULL" — the verify query legitimately reads `IS NOT NULL`.)"""
        body = _up_body()
        assert "SET NOT NULL" not in body
        assert "ALTER COLUMN" not in body

    def test_it_creates_deletes_and_repoints_no_licence(self):
        body = _up_body()
        assert "UPDATE license" not in body
        assert "INSERT INTO license" not in body
        assert "DELETE FROM license" not in body

    def test_drift_is_a_warning_not_an_exception(self):
        """A raise out of _run_db_bootstrap makes the pod refuse to serve, and
        this service backs ext_authz for the whole platform at replicas: 2."""
        assert "RAISE WARNING" in _up()
        assert "RAISE EXCEPTION" not in _up()

    def test_it_pins_the_row_count_it_measured(self):
        assert "n_set <> 51" in _up()

    def test_it_pins_the_three_refused_buckets(self):
        assert "n_svc_parent <> 41" in _up()
        assert "n_ambiguous <> 8" in _up()
        assert "n_no_licence <> 154" in _up()


class TestItReportsDriftInsteadOfSweepingIt:
    def test_it_reports_a_derivable_subject_it_does_not_name(self):
        """A subject created after 2026-09-05 that IS derivable must be surfaced,
        not swept — sweeping makes the downgrade set unknowable (027, 030, 031)."""
        assert "n_leaked" in _up_body()

    def test_the_drift_report_is_a_warning(self):
        assert _up_body().count("RAISE WARNING") >= 3

    def test_it_tells_the_operator_not_to_widen_this_file(self):
        assert "do not widen this one" in _up_body()


class TestTheDowngradeIsExactAndScopedTwice:
    def test_it_reverts_exactly_the_pairs_the_upgrade_set(self):
        assert _pairs(_down()) == _pairs(_up()), (
            "the downgrade's pair list must be identical to the upgrade's, in the "
            "same order — anything else means one of the two was edited alone"
        )

    def test_it_writes_only_null(self):
        assert set(re.findall(r"SET (\w+) = (\w+)", _down())) == {("tenant_id", "NULL")}

    def test_it_is_scoped_by_the_subject_and_by_the_value_032_wrote(self):
        """Scoped twice so a downgrade cannot erase somebody else's work: by
        subject id, and by the tenant 032 actually wrote."""
        assert "s.id = v.subject_id" in _down()
        assert "s.tenant_id = v.tenant_id" in _down()

    def test_it_never_selects_a_whole_population(self):
        for forbidden in ("IS NOT NULL", "LIKE", "kind ="):
            assert forbidden not in _down().split("UPDATE subject", 1)[1], (
                f"the downgrade must name its rows, never select on {forbidden!r}"
            )

    def test_it_is_a_single_statement(self):
        assert _down().count("UPDATE subject") == 1


# ===========================================================================
# The DB-backed half: run the actual file and assert on what its own rule does.
# ===========================================================================

APP_ID = "01000000-0000-7000-8000-0000000000a1"
PLAN_ID = "01000000-0000-7000-8000-0000000000b1"
T_REAL = "01000000-0000-7000-8000-0000000000c1"  # a real org, kind='internal'
T_OTHER = "01000000-0000-7000-8000-0000000000c2"  # a second real org
T_MACHINE = "01000000-0000-7000-8000-0000000000c3"  # kind='service'

S_AMBIGUOUS = "01000000-0000-7000-8000-0000000000d1"
S_MACHINE = "01000000-0000-7000-8000-0000000000d2"
S_NO_LICENCE = "01000000-0000-7000-8000-0000000000d3"
S_DERIVABLE_UNNAMED = "01000000-0000-7000-8000-0000000000d4"


@pytest.fixture
def seeded(db_engine):
    """A miniature registry holding one subject from each measured bucket.

    Plus the FIRST pair 032 actually names, read out of the migration file itself
    so this fixture cannot drift from the list under test.
    """
    named_subject, named_tenant = _pairs(_up())[0]
    with db_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(
            text(
                "INSERT INTO tenants (id, name, kind, byo_mode, created_at) VALUES "
                "(:real, 'real-org', 'internal', 'local_allowed', now()), "
                "(:other, 'other-org', 'customer', 'local_allowed', now()), "
                "(:machine, 'svc-a-machine', 'service', 'local_allowed', now()), "
                "(:named, 'the-named-tenant', 'internal', 'local_allowed', now())"
            ),
            {
                "real": T_REAL,
                "other": T_OTHER,
                "machine": T_MACHINE,
                "named": named_tenant,
            },
        )
        conn.execute(
            text("INSERT INTO app (id, key, name) VALUES (:a, 'probe-app', 'Probe App')"),
            {"a": APP_ID},
        )
        conn.execute(
            text("INSERT INTO plan (id, app_id, code, name) VALUES (:p, :a, 'std', 'Std')"),
            {"p": PLAN_ID, "a": APP_ID},
        )
        conn.execute(
            text(
                "INSERT INTO subject (id, keycloak_sub, keycloak_realm) VALUES "
                "(:amb, 'probe-ambiguous', 'portal'), "
                "(:mach, 'svc-a-machine', 'service'), "
                "(:none, 'probe-no-licence', 'portal'), "
                "(:new, 'probe-derivable-unnamed', 'portal'), "
                "(:named, 'probe-named-by-032', 'portal')"
            ),
            {
                "amb": S_AMBIGUOUS,
                "mach": S_MACHINE,
                "none": S_NO_LICENCE,
                "new": S_DERIVABLE_UNNAMED,
                "named": named_subject,
            },
        )
        # The ambiguous subject deliberately holds one ACTIVE and one REVOKED
        # licence under DIFFERENT tenants — the shape that reads as unambiguous
        # to anyone who filters on status.
        conn.execute(
            text(
                "INSERT INTO license (subject_id, app_id, plan_id, tenant_id, status) VALUES "
                "(:amb, :a, :p, :real, 'active'), "
                "(:amb, :a, :p, :other, 'revoked'), "
                "(:mach, :a, :p, :machine, 'active'), "
                "(:new, :a, :p, :real, 'active'), "
                "(:named, :a, :p, :named_t, 'active')"
            ),
            {
                "amb": S_AMBIGUOUS,
                "mach": S_MACHINE,
                "new": S_DERIVABLE_UNNAMED,
                "named": named_subject,
                "a": APP_ID,
                "p": PLAN_ID,
                "real": T_REAL,
                "other": T_OTHER,
                "machine": T_MACHINE,
                "named_t": named_tenant,
            },
        )
    return {"engine": db_engine, "named_subject": named_subject, "named_tenant": named_tenant}


def _run(engine, sql: str) -> list[str]:
    """Execute a migration file verbatim, returning every NOTICE/WARNING it raised.

    The notices are the point: 032's refusals are reported, not written, so the
    only place its rule is observable is the message stream.
    """
    messages: list[str] = []
    raw = engine.raw_connection()
    try:
        dbapi = raw.dbapi_connection
        dbapi.add_notice_handler(lambda diag: messages.append(diag.message_primary or ""))
        with raw.cursor() as cur:
            cur.execute(sql)
        raw.commit()
    finally:
        raw.close()
    return messages


def _tenant_of(engine, subject_id: str) -> str | None:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT tenant_id FROM subject WHERE id = :s"), {"s": subject_id}
        ).first()
    return None if row is None or row[0] is None else str(row[0])


class TestTheRuleEvaluatedAgainstARealDatabase:
    """Executes the actual migration. These are the assertions a text grep cannot make."""

    def test_it_parents_a_subject_it_names(self, seeded):
        _run(seeded["engine"], _up())
        assert _tenant_of(seeded["engine"], seeded["named_subject"]) == seeded["named_tenant"]

    def test_an_ambiguous_subject_is_left_null_rather_than_guessed(self, seeded):
        """THE case. `probe-ambiguous` holds an ACTIVE licence under one tenant and
        a REVOKED one under another. Every guessing rule anyone would reach for —
        take the active one, take the most recent, take the only non-revoked — has
        an answer here. 032 must have none."""
        _run(seeded["engine"], _up())
        assert _tenant_of(seeded["engine"], S_AMBIGUOUS) is None, (
            "032 guessed a parent for a subject whose licences name two tenants"
        )

    def test_an_ambiguous_subject_is_not_reported_as_derivable(self, seeded):
        """The stronger half: not merely unwritten (it is not in the VALUES list,
        so that is nearly free) but not SELECTED by the file's own derivation
        query either. Relax the `= 1` guard and this goes red."""
        messages = _run(seeded["engine"], _up())
        drift = [m for m in messages if "derivable but not named" in m]
        assert drift, "the drift report did not fire at all — the rule was not evaluated"
        assert "probe-ambiguous" not in drift[0], (
            f"032 considers an ambiguous subject derivable: {drift[0]!r}"
        )

    def test_a_machine_parented_subject_is_left_null(self, seeded):
        """`svc-a-machine` has exactly one licence tenant, so the count rule alone
        would admit it. The second half of the admission rule — the tenant is not
        itself a machine — is what keeps it NULL."""
        _run(seeded["engine"], _up())
        assert _tenant_of(seeded["engine"], S_MACHINE) is None

    def test_a_machine_parented_subject_is_not_reported_as_derivable(self, seeded):
        """Remove the `t.kind = 'service'` exclusion and this goes red."""
        messages = _run(seeded["engine"], _up())
        drift = [m for m in messages if "derivable but not named" in m]
        assert drift and "svc-a-machine" not in drift[0], (
            f"032 considers a machine-parented subject derivable: {drift!r}"
        )

    def test_a_subject_with_no_licence_is_left_null(self, seeded):
        _run(seeded["engine"], _up())
        assert _tenant_of(seeded["engine"], S_NO_LICENCE) is None

    def test_a_derivable_subject_it_does_not_name_is_reported_not_swept(self, seeded):
        """The control that makes the two negative assertions above non-vacuous: a
        subject that DOES satisfy the rule and is NOT in the list must appear in
        the drift report — and must still be left NULL, because 032 names its rows
        and never re-derives at apply time."""
        messages = _run(seeded["engine"], _up())
        drift = [m for m in messages if "derivable but not named" in m]
        assert drift, "the drift report did not fire"
        assert "probe-derivable-unnamed" in drift[0], (
            f"a derivable, unnamed subject was not reported: {drift[0]!r}"
        )
        assert _tenant_of(seeded["engine"], S_DERIVABLE_UNNAMED) is None, (
            "032 swept a subject it does not name — it must report, never sweep"
        )

    def test_a_second_run_changes_nothing(self, seeded):
        """Guarded on `tenant_id IS NULL`, so re-running is a no-op — the property
        `apply_migrations.py` relies on when a file is replayed."""
        _run(seeded["engine"], _up())
        before = _tenant_of(seeded["engine"], seeded["named_subject"])
        messages = _run(seeded["engine"], _up())
        assert _tenant_of(seeded["engine"], seeded["named_subject"]) == before
        assert any("updated 0" in m for m in messages), (
            f"the re-run did not report 0 rows updated: {messages!r}"
        )

    def test_it_never_overwrites_a_parent_an_operator_already_set(self, seeded):
        """An operator's value is the authority (027, 030, 031)."""
        engine = seeded["engine"]
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(
                text("UPDATE subject SET tenant_id = :t WHERE id = :s"),
                {"t": T_OTHER, "s": seeded["named_subject"]},
            )
        _run(engine, _up())
        assert _tenant_of(engine, seeded["named_subject"]) == T_OTHER

    def test_the_downgrade_reverts_exactly_what_the_upgrade_wrote(self, seeded):
        engine = seeded["engine"]
        _run(engine, _up())
        assert _tenant_of(engine, seeded["named_subject"]) is not None
        _run(engine, _down())
        assert _tenant_of(engine, seeded["named_subject"]) is None

    def test_the_downgrade_leaves_a_parent_an_operator_moved(self, seeded):
        """Scoped by the value 032 wrote: if someone has since re-parented the
        subject, that is their value and the downgrade must not touch it."""
        engine = seeded["engine"]
        _run(engine, _up())
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(
                text("UPDATE subject SET tenant_id = :t WHERE id = :s"),
                {"t": T_OTHER, "s": seeded["named_subject"]},
            )
        _run(engine, _down())
        assert _tenant_of(engine, seeded["named_subject"]) == T_OTHER


@pytest.fixture(autouse=True)
def _pre_050_writer_default(legacy_owner_default):  # noqa: F811
    """This file replays a pre-050 writer; see tests/support/legacy_owner_default.py."""
