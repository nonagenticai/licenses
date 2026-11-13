"""Migration 030 fills `cost_centre` where it is NOT a judgement call.

WHY THIS FILE EXISTS. `cost_centre` is a CHARGEBACK JOIN KEY: a value written
here is joined against, summed, and rendered as a per-centre cost figure, at
which point a guess is indistinguishable from a measurement. Migration 027 left
the column entirely NULL for exactly that reason, and its header names the
failure — four sessions hand-deriving a classification from name prefixes and
disagreeing, because prefixes are not a partition.

030 does not fix that with a better heuristic. It fixes it with an admission
rule, and these tests pin the rule rather than the answers:

* **Only rows whose already-classified siblings agree 100% get a value.** 33 of
  43 `svc-*` rows carry 'service' and none carries anything else, so the
  remaining 10 are a transcription, not a guess.
* **Everything ambiguous stays NULL.** `kind='internal'` is split across five
  different cost centres and `kind='test'` across two, so neither predicts an
  answer. A row left NULL is legible as "undecided"; a row in a wrong bucket is
  legible as nothing.
* **It never overwrites.** Every UPDATE is guarded `WHERE cost_centre IS NULL`,
  so an operator's later correction is the authority, not this file — the same
  principle 027 states.
* **It names its rows explicitly.** A tenant created after 2026-09-05 must not be
  swept into a bucket by an else-branch.

The vocabulary is CLOSED and MEASURED, not invented: service / customers / ci /
improvement / merqi / tests, as they stood on prod 2026-09-05. A value outside it
would mint a new chart-of-accounts entry as a side effect of a backfill.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
UP = REPO / "migrations" / "030_backfill_tenant_cost_centre.sql"
DOWN = REPO / "migrations" / "downgrades" / "030_backfill_tenant_cost_centre.sql"

#: The six values that existed on prod before 030. 030 may use these and only these.
MEASURED_VOCABULARY = frozenset({"service", "customers", "ci", "improvement", "merqi", "tests"})

#: The nine rows 030 REFUSES to decide. Named here so that a later edit which
#: quietly assigns one has to delete a line from this list to go green.
LEFT_TO_THE_OPERATOR = frozenset(
    {
        # kind='internal' — classified internal rows split across FIVE values, and
        # there is no 'internal'/'platform' bucket at all, so any answer mints one.
        "default-anon",
        "george-dekker",
        "nonagentic",
        "portal",
        "system",
        # kind='test' — split between 'ci' and 'tests'; these have no deciding sibling.
        "e2e-smoke-qe95guoq",
        "e2e-smoke-xtzz3028",
        "aa-tenant-proof",
        "zz-s62-probe-a1",
    }
)

_SET_BLOCK = re.compile(r"UPDATE tenants SET cost_centre = '(\w+)'(.*?);", re.DOTALL)
_QUOTED = re.compile(r"'([a-z0-9][a-z0-9-]*)'")


def _up() -> str:
    return UP.read_text(encoding="utf-8")


def _down() -> str:
    return DOWN.read_text(encoding="utf-8")


def _assignments() -> dict[str, str]:
    """value -> the raw body of the UPDATE that writes it (executable part only)."""
    body = _up().split("DO $$", 1)[1]
    return {value: block for value, block in _SET_BLOCK.findall(body)}


class TestItUsesOnlyTheMeasuredVocabulary:
    def test_every_value_written_already_exists_on_prod(self):
        written = set(_assignments())
        unknown = written - MEASURED_VOCABULARY
        assert unknown == set(), (
            f"030 writes cost centre(s) {unknown} that did not exist on prod 2026-09-05. "
            "Minting a new chart-of-accounts entry is a decision, not a backfill."
        )

    def test_it_writes_the_three_buckets_it_claims(self):
        assert set(_assignments()) == {"service", "customers", "tests"}

    def test_it_adds_no_check_constraint(self):
        """Pinning the vocabulary while 9 rows are undecided forces the operator's hand."""
        assert "ADD CONSTRAINT" not in _up()


class TestItNeverOverwritesAnOperator:
    @pytest.mark.parametrize("value", ["service", "customers", "tests"])
    def test_every_update_is_guarded_on_null(self, value: str):
        assert "cost_centre IS NULL" in _assignments()[value], (
            f"the {value} UPDATE must be guarded WHERE cost_centre IS NULL — "
            "once a value exists the operator is the authority, not this file"
        )

    def test_a_rerun_is_a_no_op(self):
        """Guarded on NULL, every statement is idempotent by construction."""
        assert len(_assignments()) == _up().split("DO $$", 1)[1].count("SET cost_centre = '")


class TestItRefusesTheAmbiguousRows:
    @pytest.mark.parametrize("name", sorted(LEFT_TO_THE_OPERATOR))
    def test_the_undecided_row_is_never_assigned(self, name: str):
        """Assigning one of these means a later edit guessed. Deleting it from
        LEFT_TO_THE_OPERATOR is the deliberate act that should require a review."""
        for value, block in _assignments().items():
            assert f"'{name}'" not in block, (
                f"030 assigns {name!r} to {value!r}, but it is a judgement call: "
                "its kind does not predict a centre and it has no agreeing sibling"
            )

    def test_it_names_its_rows_and_never_uses_an_else_branch(self):
        """ "Whatever is left" would sweep tenants created after 2026-09-05."""
        for value, block in _assignments().items():
            has_explicit_target = "name =" in block or "name IN" in block or "name LIKE" in block
            assert has_explicit_target, f"the {value} UPDATE selects no explicit rows"

    def test_the_only_pattern_match_is_the_svc_prefix(self):
        """LIKE is admissible for `svc-%` because 33/33 classified svc rows agree.
        A LIKE on anything else is a heuristic wearing a rule's clothes."""
        patterns = re.findall(r"name LIKE '([^']+)'", _up())
        assert patterns == ["svc-%"], f"unexpected LIKE pattern(s): {patterns}"

    def test_svc_rows_go_to_service_not_to_customers(self):
        """`svc-image-e2e-customer` matches the service, test AND customer greps —
        027's own worked example. The svc- precedence rule settles it as service."""
        assert "svc-%" in _assignments()["service"]
        assert "svc-" not in _assignments()["customers"]


class TestItChangesNothingButCostCentre:
    def test_it_sets_no_other_column(self):
        assigned = set(re.findall(r"SET (\w+) =", _up()))
        assert assigned == {"cost_centre"}

    def test_it_moves_no_row_and_touches_no_licence(self):
        for forbidden in ("INSERT INTO", "DELETE FROM", "ALTER TABLE", "license"):
            assert forbidden not in _up().split("DO $$", 1)[1], (
                f"030 is a cost-centre backfill; it must not contain {forbidden!r}"
            )

    def test_it_changes_no_name_and_no_uuid(self):
        assert "SET name" not in _up()
        assert "SET id" not in _up()


class TestItFailsLoudlyButNotFatally:
    def test_drift_from_the_measurement_is_a_warning_not_an_exception(self):
        """A NOTICE cannot crashloop ext_authz for the whole platform; a raise can."""
        assert "RAISE WARNING" in _up()
        assert "RAISE EXCEPTION" not in _up()

    def test_it_pins_the_row_counts_it_measured(self):
        assert "n_service <> 10" in _up()
        assert "n_remaining <> 9" in _up()


class TestTheDowngradeIsScopedTwice:
    def test_it_names_every_row_it_reverts(self):
        """Never 'everything that is not NULL' — that would erase the 43 rows
        classified by hand before 030 existed."""
        assert "IS NOT NULL" not in _down()

    def test_it_reverts_only_the_value_030_wrote(self):
        """If an operator has since changed a row, that value is theirs."""
        for value in ("service", "customers", "tests"):
            assert f"WHERE cost_centre = '{value}'" in _down()

    def test_it_reverts_exactly_the_ten_svc_rows(self):
        block = _down().split("cost_centre = 'service'", 1)[1].split(";", 1)[0]
        names = [n for n in _QUOTED.findall(block) if n.startswith("svc-")]
        assert len(names) == 10, f"expected 10 svc-* rows in the downgrade, found {len(names)}"

    def test_it_sets_nothing_but_null(self):
        assert set(re.findall(r"SET cost_centre = (\w+)", _down())) == {"NULL"}
