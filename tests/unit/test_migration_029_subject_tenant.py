"""Migration 029 gives `subject` a tenant parent; these pin the parts that rot.

WHY THIS FILE EXISTS. 029 is the ADDITIVE half of a two-part change, and the
whole value of splitting it is that this half is a no-op for every running
caller. Three properties carry that promise, and each is one careless edit away
from being false:

* **The column is NULLABLE and 029 backfills nothing.** The moment someone adds
  a backfill "while they are in there", this stops being a no-op: it starts
  asserting parentage that nobody decided, from a licence join that returns a
  SET rather than a parent. `test_it_writes_no_data` is the pin.

* **The ORM mirrors the SQL exactly.** `scripts/apply_migrations.py` BASELINES —
  on a database that already has the tables it stamps every .sql file as applied
  WITHOUT running it — so on a FRESH bootstrap 029 never executes and
  `create_all` from `models.py` is the only thing that builds this column. A
  column, index or FK written only in SQL would exist on NO fresh database. That
  is the drift class `ck_tenants_byo_mode` in models.py documents having
  suffered and that 021 had to go back and repair.

* **The FK is NAMED and the index is explicit.** Unnamed, `create_all` emits
  PostgreSQL's default `subject_tenant_id_fkey` while 029 emits
  `fk_subject_tenant`, so the two arms differ by constraint name AND 029's own
  idempotency guard (`WHERE conname = 'fk_subject_tenant'`) reads FALSE on a
  create_all'd database. And `index=True` on the column instead of the explicit
  `Index(...)` would emit a SECOND identical btree — the exact defect
  `license.tenant_id`'s comment records (two copies on fresh, one on prod).

`tests/unit/test_schema_parity.py` counts these objects; this file asserts they
are the RIGHT ones. A count cannot tell a named FK from a default-named one.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import Table
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from models import Subject

_SUBJECT = cast(Table, Subject.__table__)

REPO = Path(__file__).resolve().parents[2]
UP = REPO / "migrations" / "029_add_subject_tenant.sql"
DOWN = REPO / "migrations" / "downgrades" / "029_add_subject_tenant.sql"

#: Statements that write row data. 029 must contain NONE of them.
_WRITES_DATA = re.compile(r"\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM)\b", re.IGNORECASE)


def _up() -> str:
    return UP.read_text(encoding="utf-8")


def _down() -> str:
    return DOWN.read_text(encoding="utf-8")


def _executable(sql: str) -> str:
    """`sql` with every ``--`` comment stripped.

    Ordering assertions must read the STATEMENTS, not the prose. The 029
    downgrade's header explains why ``DROP COLUMN`` would take the index and the
    constraint with it, and a naive ``.index("DROP COLUMN")`` finds that sentence
    rather than the statement — a test that passes or fails on a comment reword
    is measuring the wrong thing.
    """
    return "\n".join(re.sub(r"--.*$", "", line) for line in sql.splitlines())


def _column():
    for col in _SUBJECT.columns:
        if col.name == "tenant_id":
            return col
    raise AssertionError("Subject.tenant_id is not declared in the ORM")


class TestTheMigrationFileExistsAndIsShaped:
    def test_both_halves_are_present(self):
        assert UP.exists(), "029 upgrade is missing"
        assert DOWN.exists(), "029 downgrade is missing — every migration here has one"

    def test_it_adds_the_column_to_subject(self):
        assert re.search(
            r"ALTER TABLE subject\s+ADD COLUMN tenant_id UUID", _up(), re.IGNORECASE
        ), "029 must add subject.tenant_id as UUID"

    def test_the_column_is_nullable(self):
        """NOT NULL would demand a value for all 254 existing subjects."""
        assert not re.search(r"ADD COLUMN tenant_id UUID[^;]*NOT NULL", _up(), re.IGNORECASE), (
            "029's column must stay nullable — NULL means 'undecided', not 'no tenant'"
        )

    def test_it_declares_no_default(self):
        """A default would make every future INSERT assert a parent nobody chose."""
        assert not re.search(r"ADD COLUMN tenant_id UUID[^;]*DEFAULT", _up(), re.IGNORECASE), (
            "029's column must have no default"
        )


class TestItIsANoOpForEveryRunningCaller:
    """The entire point of shipping this half separately."""

    def test_it_writes_no_data(self):
        """No INSERT/UPDATE/DELETE anywhere in the file.

        A backfill added here would assert parentage nobody decided, derived from
        a licence join that returns a SET rather than a parent.
        """
        offenders = _WRITES_DATA.findall(_up())
        assert offenders == [], (
            f"029 must not write row data, found {offenders!r}. "
            "Backfilling parentage is a separate, decided operation — see "
            "docs/svc-tenant-migration-plan.md."
        )

    def test_it_touches_no_table_but_subject(self):
        """`tenants` is REFERENCED (the FK target) but never ALTERed."""
        altered = {t.lower() for t in re.findall(r"ALTER TABLE (\w+)", _up(), re.IGNORECASE)}
        assert altered == {"subject"}, f"029 must alter only `subject`, altered {altered}"

    def test_it_does_not_move_a_single_svc_row(self):
        """The destructive half is DEFERRED. This file must not start it."""
        assert "svc-" not in _up().split("DO $$")[1], (
            "029's executable body must not name any svc-* row — moving them is "
            "the deferred half (docs/svc-tenant-migration-plan.md)"
        )


class TestTheOrmMirrorsTheSql:
    """On a FRESH database 029 never runs; models.py is the only builder."""

    def test_the_column_is_declared_in_the_orm(self):
        assert isinstance(_column().type, PGUUID)

    def test_the_orm_column_is_nullable(self):
        assert _column().nullable is True

    def test_the_orm_foreign_key_targets_tenants_id(self):
        fks = list(_column().foreign_keys)
        assert len(fks) == 1
        assert fks[0].column.table.name == "tenants"
        assert fks[0].column.name == "id"

    @pytest.mark.parametrize("arm", ["sql", "orm"])
    def test_the_fk_is_named_fk_subject_tenant(self, arm: str):
        """Unnamed, create_all emits `subject_tenant_id_fkey` and the arms differ."""
        if arm == "sql":
            assert "ADD CONSTRAINT fk_subject_tenant" in _up()
        else:
            fk = next(iter(_column().foreign_keys))
            assert fk.constraint is not None
            assert fk.constraint.name == "fk_subject_tenant"

    @pytest.mark.parametrize("arm", ["sql", "orm"])
    def test_the_fk_restricts_deletes(self, arm: str):
        """A tenant with members must not be deletable out from under them."""
        if arm == "sql":
            assert re.search(r"REFERENCES tenants\(id\) ON DELETE RESTRICT", _up(), re.IGNORECASE)
        else:
            fk = next(iter(_column().foreign_keys))
            assert fk.ondelete == "RESTRICT"

    @pytest.mark.parametrize("arm", ["sql", "orm"])
    def test_the_index_is_declared_once_and_named(self, arm: str):
        """`index=True` would emit a SECOND identical btree (ix_subject_tenant_id)."""
        if arm == "sql":
            assert "CREATE INDEX idx_subject_tenant ON subject (tenant_id)" in _up()
        else:
            assert _column().index is not True, (
                "use the explicit Index('idx_subject_tenant') in __table_args__, "
                "not index=True — index=True adds a duplicate btree"
            )
            named = [ix for ix in _SUBJECT.indexes if [c.name for c in ix.columns] == ["tenant_id"]]
            assert [ix.name for ix in named] == ["idx_subject_tenant"]

    def test_to_dict_exposes_the_column(self):
        """Without this the column is unreadable outside raw SQL."""
        assert "tenant_id" in Subject().to_dict()

    def test_to_dict_preserves_none(self):
        """An unstated parent must be distinguishable from a stated one."""
        assert Subject().to_dict()["tenant_id"] is None


class TestItIsRerunSafe:
    """A failed migration raises out of _run_db_bootstrap and the pod refuses to
    serve — at replicas: 2, backing ext_authz for the whole platform."""

    def test_it_returns_early_when_the_column_already_exists(self):
        body = _up()
        guard = body.index("information_schema.columns")
        early_return = body.index("RETURN;")
        assert guard < early_return

    def test_everything_lands_in_one_do_block(self):
        """Column, FK and index together or not at all."""
        assert _up().count("DO $$") == 1

    def test_it_uses_no_statement_that_cannot_run_in_a_transaction(self):
        """The runner REFUSES a pending file containing CONCURRENTLY or VACUUM —
        it would wait forever on the migration lock's own transaction."""
        assert not re.search(r"\bCONCURRENTLY\b|\bVACUUM\b", _up(), re.IGNORECASE)


class TestTheDowngradeUndoesExactlyThis:
    def test_it_drops_the_constraint_before_the_column(self):
        body = _executable(_down())
        assert body.index("DROP CONSTRAINT fk_subject_tenant") < body.index("DROP COLUMN")

    def test_it_drops_the_index(self):
        assert "DROP INDEX IF EXISTS idx_subject_tenant" in _down()

    def test_it_drops_the_column(self):
        assert "ALTER TABLE subject DROP COLUMN IF EXISTS tenant_id" in _down()

    def test_it_touches_no_other_table(self):
        altered = {t.lower() for t in re.findall(r"ALTER TABLE (\w+)", _down(), re.IGNORECASE)}
        assert altered == {"subject"}

    def test_it_warns_that_recorded_parentage_is_unrecoverable(self):
        """Unlike 027's kind and 028's membership, this column's values are not
        re-derivable from the migration itself."""
        assert "RE-DERIVABLE" in _down()
        assert "subject_tenant_backup.csv" in _down(), (
            "the downgrade must tell the operator how to preserve the pairs first"
        )
