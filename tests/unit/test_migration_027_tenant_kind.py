"""Migration 027 classifies every tenant; these pin the parts that silently rot.

WHY THIS FILE EXISTS. The registry holds 75 tenant rows and "which of these are
customers?" had no answer in the schema. It was re-derived by hand from name
prefixes in at least four sessions, and the derivations disagreed — because name
prefixes are NOT a partition. `svc-image-e2e-customer`, `svc-modality-gw-e2e`
and the four `svc-probe-*` rows match the service filter AND the test filter, so
two people classifying by grep get two different answers.

027 resolves that with an explicit precedence: **`svc-` wins over every other
rule**. The tests below pin that precedence, because it is exactly the kind of
rule a later edit re-breaks by adding a name to the wrong list.

They also pin the ORM mirror. The runner baselines every current migration on a
database that already has the tables, so on a fresh bootstrap 027 NEVER RUNS and
`create_all` is the only thing that builds the column. A constraint written only
in SQL would exist on no database — the drift `ck_tenants_byo_mode` in models.py
documents having suffered, and which 021 had to go back and repair.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import String, Table

from models import Tenant

# ``Tenant.__table__`` is typed as ``FromClause`` on the declarative base, which
# has neither ``constraints`` nor ``indexes``. The runtime object is a ``Table``.
_TENANTS = cast(Table, Tenant.__table__)

REPO = Path(__file__).resolve().parents[2]
UP = REPO / "migrations" / "027_add_tenant_kind_and_cost_centre.sql"
DOWN = REPO / "migrations" / "downgrades" / "027_add_tenant_kind_and_cost_centre.sql"

KINDS = {"customer", "internal", "service", "test", "unclassified"}

#: `UPDATE tenants SET kind = '<kind>' ... IN ( '<name>', '<name>', ... );`
_BLOCK = re.compile(
    r"UPDATE tenants SET kind = '(\w+)'(.*?);",
    re.DOTALL,
)
_NAME = re.compile(r"'([a-z0-9][a-z0-9-]*)'")
#: `ck_tenants_name_format` on prod, transcribed. Every name 027 writes must satisfy it.
_NAME_CHECK = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _orm_kind_check_sql() -> str:
    """The ORM's ck_tenants_kind expression, as text.

    ``getattr`` rather than ``.sqltext`` because ``Table.constraints`` is typed
    as ``set[Constraint]``, and the checker will not narrow to
    ``CheckConstraint`` through the lookup.
    """
    for constraint in _TENANTS.constraints:
        if constraint.name == "ck_tenants_kind":
            return str(getattr(constraint, "sqltext", ""))
    raise AssertionError("ck_tenants_kind is not declared in the ORM")


def _classified() -> dict[str, set[str]]:
    """kind -> the explicit names 027 assigns to it (the LIKE rule excluded)."""
    out: dict[str, set[str]] = {}
    for kind, body in _BLOCK.findall(UP.read_text()):
        if "LIKE" in body:  # the svc- prefix rule names nothing explicitly
            out.setdefault(kind, set())
            continue
        names = {n for n in _NAME.findall(body) if n not in KINDS}
        out.setdefault(kind, set()).update(names)
    return out


class TestTheMigrationIsParseableAtAll:
    """Positive control. Every assertion below is built by `_classified`; if the
    parse silently returned nothing, they would all pass vacuously."""

    def test_it_classifies_a_useful_number_of_names(self):
        found = _classified()
        assert sum(len(v) for v in found.values()) >= 30, found

    def test_all_three_explicit_kinds_are_present(self):
        assert {"test", "internal", "customer"} <= set(_classified())


class TestTheSvcPrecedenceRuleHolds:
    def test_the_prefix_rule_runs_before_every_explicit_list(self):
        """`svc-` must win the overlap, so its UPDATE has to come first."""
        text = UP.read_text()
        like_at = text.index("WHERE name LIKE 'svc-%'")
        for kind in ("test", "internal", "customer"):
            assert like_at < text.index(f"SET kind = '{kind}'"), (
                f"the '{kind}' list runs before the svc- rule; the overlap would go the wrong way"
            )

    def test_no_svc_name_is_also_listed_explicitly(self):
        """Belt and braces: even ordered correctly, a duplicate listing is a bug.

        `svc-image-e2e-customer` and `svc-modality-gw-e2e` are the live traps.
        """
        for kind, names in _classified().items():
            offenders = {n for n in names if n.startswith("svc-")}
            assert not offenders, f"{kind} explicitly lists svc- tenants: {offenders}"

    def test_the_later_lists_are_guarded_on_unclassified(self):
        """Each explicit UPDATE must re-check `kind = 'unclassified'`.

        Without it a later list could overwrite an earlier decision, and the
        precedence would depend on statement order alone.
        """
        for kind, body in _BLOCK.findall(UP.read_text()):
            if "LIKE" in body:
                continue
            assert "kind = 'unclassified'" in body, f"the '{kind}' UPDATE is unguarded"


class TestTheListsAreWellFormed:
    def test_every_name_satisfies_the_registry_name_constraint(self):
        for kind, names in _classified().items():
            for name in names:
                assert _NAME_CHECK.match(name), f"{kind}: {name!r} violates ck_tenants_name_format"

    def test_no_name_is_classified_twice(self):
        seen: dict[str, str] = {}
        for kind, names in _classified().items():
            for name in names:
                assert name not in seen, f"{name!r} is in both {seen[name]} and {kind}"
                seen[name] = kind

    def test_customer_is_an_explicit_list_not_an_else_branch(self):
        """Over-counting revenue is the failure this column exists to prevent."""
        text = UP.read_text()
        customer_stmt = text[text.index("SET kind = 'customer'") :]
        assert "IN (" in customer_stmt.split(";")[0]


class TestTheOrmMirrorsTheSql:
    """The drift class: a SQL-only constraint exists on NO database here."""

    def test_the_check_constraint_is_declared_in_the_orm(self):
        names = {c.name for c in _TENANTS.constraints if c.name}
        assert "ck_tenants_kind" in names

    def test_the_orm_and_the_sql_allow_exactly_the_same_values(self):
        orm_values = set(_NAME.findall(_orm_kind_check_sql()))
        sql_values = set(
            _NAME.findall(UP.read_text().split("ADD CONSTRAINT ck_tenants_kind")[1].split(";")[0])
        )
        assert orm_values == sql_values == KINDS

    def test_both_columns_exist_on_the_orm_with_the_sql_types(self):
        cols = _TENANTS.columns
        assert cast(String, cols["kind"].type).length == 16, "SQL declares VARCHAR(16)"
        assert cast(String, cols["cost_centre"].type).length == 64, "SQL declares VARCHAR(64)"
        assert cols["kind"].nullable is False
        assert cols["cost_centre"].nullable is True

    def test_the_default_fails_closed(self):
        """A new tenant must not be silently counted as a customer."""
        default = _TENANTS.columns["kind"].server_default
        assert default is not None
        assert "unclassified" in str(getattr(default, "arg", default))

    def test_the_index_is_declared(self):
        assert "ix_tenants_kind" in {i.name for i in _TENANTS.indexes}


class TestTheDowngradeUndoesExactlyThis:
    def test_it_drops_the_constraint_before_the_columns(self):
        text = DOWN.read_text()
        assert text.index("ck_tenants_kind") < text.index("DROP COLUMN IF EXISTS kind")

    @pytest.mark.parametrize("col", ["kind", "cost_centre"])
    def test_it_drops_both_columns(self, col):
        assert f"DROP COLUMN IF EXISTS {col}" in DOWN.read_text()


class TestTheMigrationIsRerunSafe:
    def test_it_returns_early_when_the_column_already_exists(self):
        text = UP.read_text()
        assert "column_name = 'kind'" in text
        assert "RETURN;" in text

    def test_it_does_not_use_archived_at(self):
        """Archiving blocks provisioning; 58 gitops lines pin the svc-* subjects."""
        assert "archived_at" not in UP.read_text().split("DO $$")[1]
