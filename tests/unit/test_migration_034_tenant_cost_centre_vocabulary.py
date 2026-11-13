"""Migration 034 — `tenants.cost_centre` gets a vocabulary, and a write path.

TWO DEFECTS, ONE COLUMN, and they compound.

1. NO CONSTRAINT. `cost_centre` arrived in migration 027 and nothing constrained
   it. Measured on the primary 2026-09-08, `pg_constraint` on `tenants` carried
   `ck_tenants_byo_mode`, `ck_tenants_kind`, `ck_tenants_name_format` and
   `ck_tenants_service_not_byo_required` — not one mentions `cost_centre`. Any
   string up to 64 chars was accepted. It is a chargeback JOIN KEY, so a typo
   does not error: it produces a chargeback bucket of exactly one tenant, and the
   spend that should have landed in the real centre silently never appears there.

2. NO WRITE PATH. It was settable at INSERT only. `POST /tenants` and
   `PUT /tenants/{tenant_id}` both accept it and both apply it only when creating
   a NEW row — `register_tenant` returns an existing tenant UNCHANGED by design —
   while `PATCH /tenants/{name_or_id}` renames and nothing else. So a tenant
   created without a cost centre could never be given one through this API.

Together: the only way to fill the four NULLs was direct SQL against the primary,
and the only thing standing between that SQL and a mistyped join key was the
operator's typing.

MEASURED VOCABULARY (the primary 2026-09-08, all 76 tenant rows):
    service 45 · tests 9 · customers 6 · ci 4 · improvement 4 · merqi 4 · NULL 4

⛔ THE FOUR NULLS ARE NOT FILLED HERE. `default-anon`, `george-dekker`,
`nonagentic` and `portal` are all kind='internal' and which centre carries them
is an APPORTIONMENT decision — an operator's, never this service's. Migration 030
set that precedent when it backfilled 12 rows and deliberately left these four.
This commit makes them WRITABLE; it does not decide them.

⚠️ AND ONE INCONSISTENCY IS REPORTED RATHER THAN REPAIRED: `platform-core` and
`system` are kind='internal' but carry cost_centre='service'. That is either a
mis-set kind or a mis-set cost centre — different consequences, operator's call —
so the CHECK deliberately ADMITS the combination. `TestTheCheckDoesNotCorrelateKind`
pins that, because a correlating CHECK would refuse both rows at apply time and
CrashLoop a pod that backs ext_authz at replicas: 2.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import CheckConstraint, Table, text
from sqlalchemy.exc import IntegrityError

from models import COST_CENTRES, ClientSafeError, Tenant

# `DeclarativeBase.__table__` is typed as the narrower `FromClause` in
# SQLAlchemy's stubs, which has no `.constraints` -- a real `Table` attribute
# (confirmed at runtime) -- so a bare access reads as unresolved to a strict
# checker. Cast once here rather than scatter a suppression over every call site.
_TENANT_TABLE = cast(Table, Tenant.__table__)

REPO = Path(__file__).resolve().parents[2]
UP = REPO / "migrations" / "034_tenant_cost_centre_vocabulary.sql"
DOWN = REPO / "migrations" / "downgrades" / "034_tenant_cost_centre_vocabulary.sql"
MIGRATIONS = REPO / "migrations"


def _up() -> str:
    return UP.read_text(encoding="utf-8")


def _down() -> str:
    return DOWN.read_text(encoding="utf-8")


#: The vocabulary measured on prod. A later edit must change THIS line too, which
#: is the point — the tuple is a measurement, not a preference.
MEASURED_VOCABULARY = ("service", "tests", "customers", "ci", "improvement", "merqi")

#: The four rows left NULL on purpose. Named so that a future backfill has to
#: delete a line from this test to go green.
LEFT_TO_THE_OPERATOR = ("default-anon", "george-dekker", "nonagentic", "portal")


def _mk(db_manager, name: str, **kw) -> Tenant:
    """`create_tenant` takes no `kind` — `ensure_tenant` is the primitive that
    does, so a test needing a specific kind goes through that with an explicit
    id (which is also how `PUT /tenants/{id}` reaches it)."""
    kw.setdefault("owner_key", "platform")
    if "kind" in kw:
        return db_manager.ensure_tenant(str(uuid.uuid4()), name=name, **kw)
    return db_manager.create_tenant(name=name, **kw)


class TestBothFilesExist:
    def test_the_upgrade_exists(self):
        assert UP.is_file(), f"missing {UP}"

    def test_it_has_a_matching_downgrade(self):
        assert DOWN.is_file(), f"missing {DOWN}"
        assert DOWN.name == UP.name

    def test_the_runner_cannot_auto_apply_the_downgrade(self):
        assert DOWN.parent.name == "downgrades"
        assert DOWN.resolve() not in {p.resolve() for p in MIGRATIONS.glob("*.sql")}


class TestTheOrmAgrees:
    """A fresh database baselines 034 as applied without running it, so the CHECK
    must be declared in models.py or it exists on no database at all — exactly the
    drift `ck_tenants_byo_mode` is documented as having suffered."""

    def test_the_check_is_declared_on_the_tenant_model(self):
        names = {c.name for c in _TENANT_TABLE.constraints}
        assert "ck_tenants_cost_centre" in names

    def test_the_constant_and_the_migration_agree(self):
        assert COST_CENTRES == MEASURED_VOCABULARY
        for value in MEASURED_VOCABULARY:
            assert f"'{value}'" in _up(), f"{value} missing from the migration's CHECK"

    def test_the_orm_check_and_the_migration_check_admit_the_same_set(self):
        """Two expressions of one rule can disagree; this is what catches it."""
        orm = next(c for c in _TENANT_TABLE.constraints if c.name == "ck_tenants_cost_centre")
        assert isinstance(orm, CheckConstraint)
        orm_sql = str(orm.sqltext)
        for value in MEASURED_VOCABULARY:
            assert f"'{value}'" in orm_sql
        assert "IS NULL" in orm_sql.upper(), "the ORM CHECK must admit NULL"


class TestTheVocabularyIsEnforced:
    """⭐ These FAIL on origin/main: there was no CHECK, so every one of these
    writes succeeded."""

    def test_an_unlisted_cost_centre_is_rejected(self, db_manager):
        session = db_manager.get_session()
        try:
            session.add(
                Tenant(
                    id=uuid.uuid4(),
                    name="typo-centre",
                    kind="internal",
                    cost_centre="servce",
                    owner_key="platform",
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()
        finally:
            session.rollback()
            session.close()

    def test_the_empty_string_is_rejected(self, db_manager):
        """'' is not 'undecided' — NULL is. An empty join key groups silently."""
        session = db_manager.get_session()
        try:
            session.add(
                Tenant(
                    id=uuid.uuid4(),
                    name="empty-centre",
                    kind="internal",
                    cost_centre="",
                    owner_key="platform",
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()
        finally:
            session.rollback()
            session.close()

    @pytest.mark.parametrize("value", MEASURED_VOCABULARY)
    def test_every_measured_value_still_writes(self, db_manager, value):
        """Positive control — the CHECK must not be over-broad. If any of these
        six is rejected, prod rows are now unwritable."""
        t = _mk(db_manager, f"cc-ok-{value}", cost_centre=value)
        assert t.cost_centre == value

    def test_null_is_admitted(self, db_manager):
        """'Undecided' is a state the column is REQUIRED to express — four prod
        rows are in it deliberately."""
        t = _mk(db_manager, "cc-undecided")
        assert t.cost_centre is None

    def test_the_check_uses_is_null_not_a_bare_in(self):
        """`NULL IN (...)` evaluates to NULL, which a CHECK treats as PASSING — so
        a bare IN would admit NULL by ACCIDENT. If someone later 'tidies' the
        expression, NULL handling must remain a statement, not a side effect."""
        normalised = " ".join(_up().split())
        assert "cost_centre IS NULL OR cost_centre IN" in normalised

    def test_the_db_constraint_rejects_a_raw_insert(self, db_manager):
        """Bypasses every Python guard — this is what proves the CONSTRAINT does
        the rejecting, not the Pydantic model or the setter."""
        session = db_manager.get_session()
        try:
            with pytest.raises(IntegrityError):
                session.execute(
                    text(
                        "INSERT INTO tenants (id, name, kind, cost_centre) "
                        "VALUES (:i, 'raw-bad-centre', 'internal', 'marketing')"
                    ),
                    {"i": uuid.uuid4()},
                )
                session.commit()
        finally:
            session.rollback()
            session.close()


class TestTheCheckDoesNotCorrelateKind:
    """`platform-core` and `system` are kind='internal' with cost_centre='service'
    on prod. Reported, not repaired: a CHECK correlating the two would refuse both
    rows at apply time and CrashLoop the pod."""

    def test_internal_with_service_cost_centre_is_admitted(self, db_manager):
        t = _mk(db_manager, "platform-core-shaped", kind="internal", cost_centre="service")
        assert t.kind == "internal" and t.cost_centre == "service"

    def test_the_migration_does_not_mention_kind(self):
        body = _up().split("ALTER TABLE tenants", 1)[1]
        assert "kind" not in body, (
            "034's CHECK must not correlate kind and cost_centre — that would "
            "refuse two live prod rows at apply time"
        )


class TestCostCentreIsWritableOverHttp:
    """⭐ THE COMMIT-2 DEFECT, asserted end to end.

    `PATCH /tenants/{name_or_id}/cost-centre` DID NOT EXIST on origin/main, so
    every test in this class 404s there.
    """

    def test_a_null_cost_centre_can_be_filled(self, db_manager, auth_client_admin):
        _mk(db_manager, "fillable")
        r = auth_client_admin.patch("/tenants/fillable/cost-centre", json={"cost_centre": "ci"})
        assert r.status_code == 200, r.text
        assert r.json()["cost_centre"] == "ci"
        assert db_manager.get_tenant("fillable").cost_centre == "ci", "the 200 did not persist"

    def test_an_existing_cost_centre_can_be_corrected(self, db_manager, auth_client_admin):
        _mk(db_manager, "correctable", cost_centre="tests")
        r = auth_client_admin.patch(
            "/tenants/correctable/cost-centre", json={"cost_centre": "customers"}
        )
        assert r.status_code == 200, r.text
        assert db_manager.get_tenant("correctable").cost_centre == "customers"

    def test_null_clears_it(self, db_manager, auth_client_admin):
        """Returning a row to 'undecided' is a real operation: an operator who set
        the wrong centre must not be forced to pick a second wrong answer."""
        _mk(db_manager, "clearable", cost_centre="merqi")
        r = auth_client_admin.patch("/tenants/clearable/cost-centre", json={"cost_centre": None})
        assert r.status_code == 200, r.text
        assert r.json()["cost_centre"] is None
        assert db_manager.get_tenant("clearable").cost_centre is None

    def test_it_is_addressable_by_uuid_as_well_as_name(self, db_manager, auth_client_admin):
        t = _mk(db_manager, "by-uuid")
        r = auth_client_admin.patch(
            f"/tenants/{t.id}/cost-centre", json={"cost_centre": "improvement"}
        )
        assert r.status_code == 200, r.text

    def test_an_unlisted_value_is_a_422_not_a_500(self, db_manager, auth_client_admin):
        """The vocabulary is closed, so the failure must be a named client error
        at the edge — not an IntegrityError surfacing as a 500."""
        _mk(db_manager, "reject-me")
        r = auth_client_admin.patch(
            "/tenants/reject-me/cost-centre", json={"cost_centre": "marketing"}
        )
        assert r.status_code == 422, r.text
        assert db_manager.get_tenant("reject-me").cost_centre is None, "a 422 still wrote"

    def test_an_unknown_tenant_is_404(self, auth_client_admin):
        r = auth_client_admin.patch(
            "/tenants/no-such-tenant/cost-centre", json={"cost_centre": "ci"}
        )
        assert r.status_code == 404

    def test_it_cannot_create_a_tenant(self, db_manager, auth_client_admin):
        """A typo'd name must 404, never invent a row — this is an UPDATE."""
        before = len(db_manager.list_tenants())
        auth_client_admin.patch("/tenants/typoed-name/cost-centre", json={"cost_centre": "ci"})
        assert len(db_manager.list_tenants()) == before

    def test_it_cannot_rename(self, db_manager, auth_client_admin):
        """It writes ONE column. A body carrying a name must not move the name."""
        _mk(db_manager, "keeps-its-name")
        auth_client_admin.patch(
            "/tenants/keeps-its-name/cost-centre",
            json={"cost_centre": "ci", "name": "renamed-behind-your-back"},
        )
        assert db_manager.get_tenant("keeps-its-name") is not None
        assert db_manager.get_tenant("renamed-behind-your-back") is None

    def test_it_requires_an_owner_write_role(
        self, db_manager, auth_client_manager, auth_client_reader
    ):
        """Same gate as the other tenant mutators: license-admin OR license-manager
        (OWNER_WRITE_ROLES), owner-only. Owners whose client holds only
        license-manager (merqi, w2v, fr-portal) must manage their own tenant's
        cost centre; owner scoping, not the role, keeps it off other owners'
        tenants (tests/unit/test_tenant_ownership.py). A reader is still refused."""
        _mk(db_manager, "gated")
        r = auth_client_reader.patch("/tenants/gated/cost-centre", json={"cost_centre": "ci"})
        assert r.status_code in (401, 403), r.text
        assert db_manager.get_tenant("gated").cost_centre is None
        r = auth_client_manager.patch("/tenants/gated/cost-centre", json={"cost_centre": "ci"})
        assert r.status_code == 200, r.text
        assert db_manager.get_tenant("gated").cost_centre == "ci"


class TestTheSetterValidatesToo:
    """`scripts/` reaches the DatabaseManager directly, with no Pydantic model in
    the way, so the edge check alone would leave that path unconstrained."""

    def test_the_setter_refuses_an_unlisted_value(self, db_manager):
        _mk(db_manager, "setter-guard")
        with pytest.raises(ClientSafeError):
            db_manager.set_tenant_cost_centre("setter-guard", "marketing")

    def test_the_setter_returns_none_for_an_unknown_tenant(self, db_manager):
        assert db_manager.set_tenant_cost_centre("nobody-here", "ci") is None

    def test_the_setter_accepts_none(self, db_manager):
        _mk(db_manager, "setter-clear", cost_centre="ci")
        assert db_manager.set_tenant_cost_centre("setter-clear", None).cost_centre is None


class TestTheSqlIsGuardedAndSafe:
    def test_the_runner_accepts_both_files(self):
        import sys

        sys.path.insert(0, str(REPO / "scripts"))
        from apply_migrations import (  # type: ignore[import-not-found]
            _reject_out_of_transaction_statements,
        )

        assert _reject_out_of_transaction_statements([UP]) is None
        assert _reject_out_of_transaction_statements([DOWN]) is None

    def test_the_add_is_guarded_by_a_pg_constraint_probe(self):
        assert "pg_constraint" in _up(), "a re-run must be a no-op"

    def test_bad_rows_are_measured_before_the_add(self):
        """033's shape: refuse with a NAMED exception rather than letting a bare
        constraint violation CrashLoop the pod with no explanation."""
        body = _up()
        assert "RAISE EXCEPTION" in body
        assert "refusing to add ck_tenants_cost_centre" in body

    def test_the_refusal_names_the_offending_values(self):
        """An operator reading a CrashLoop needs to know WHICH value broke it."""
        assert "string_agg" in _up()

    def test_it_touches_no_rows(self):
        body = _up()
        for verb in ("UPDATE tenants", "DELETE FROM", "INSERT INTO"):
            assert verb not in body, f"034 is schema-only; found {verb!r}"

    def test_the_four_nulls_are_named_as_the_operators(self):
        body = _up()
        for name in LEFT_TO_THE_OPERATOR:
            assert name in body, (
                f"{name} is one of the four deliberately-NULL rows and must be named, "
                "so a later backfill cannot quietly include it"
            )

    def test_the_downgrade_drops_the_constraint(self):
        assert "DROP CONSTRAINT IF EXISTS ck_tenants_cost_centre" in _down()

    def test_the_downgrade_says_what_it_costs(self):
        assert "WHAT DOWNGRADING COSTS" in _down()
