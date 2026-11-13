"""The byo-mode backfill must not mint a tenant whose id is a Keycloak principal.

⛔ THE DEFECT, AND WHY THIS SCRIPT SPECIFICALLY. ``ensure_tenant`` refuses to register
a ``tenants`` row whose id is already a ``subject.keycloak_sub`` or a ``subject.id`` —
a tenant is an ORGANISATION and a subject is an identity that AUTHENTICATES within
one, and conflating them makes the authenticating identity the thing that PAYS, with
nothing downstream able to notice (``billing_cost_events`` has no FK to ``tenants``).
``scripts/backfill_tenants_byo_mode.py`` did ``session.add(Tenant(id=…))`` directly
and never passed that primitive. It is not an arbitrary second door either: the ids it
inserts are read straight out of ``license.tenant_id``, which is EXACTLY the column
the known violators live in. Read a principal id out, mint it as a tenant, and the
``LicenseTenantIdIsKeycloakPrincipal`` alert is what fires.

────────────────────────────────────────────────────────────────────────────────────
⛔ READ THIS BEFORE ADDING A TEST HERE: THE GUARDED BRANCH IS UNREACHABLE THROUGH THE
DATABASE, AND THAT IS WHY THESE TESTS LOOK THE WAY THEY DO.

``main()`` only reaches the guard when a ``license.tenant_id`` has NO matching
``tenants`` row. **Migration 016** (``016_add_license_tenant_fk.sql``) retyped
``license.tenant_id`` to UUID and added ``fk_license_tenant`` REFERENCES
``tenants(id)`` ``ON DELETE RESTRICT``, added ``NOT VALID`` then ``VALIDATE``d in the
SAME transaction; ``models.License.tenant_id`` declares the same constraint BY NAME
and ``nullable=False``; and ``tests/conftest.py`` builds the test schema with
``Base.metadata.create_all``, so the test database enforces exactly what prod does.
Measured on this tree: no migration or downgrade drops it, nothing declares it
``DEFERRABLE``, and nothing sets ``session_replication_role`` or disables triggers.

So an orphan licence CANNOT be stored, ``RESTRICT`` means the parent cannot be
deleted out from under one, and ``nullable=False`` means it cannot be blanked — the
insert branch cannot fire. The guard is BELT-AND-BRACES, not a live gate, and it must
NOT be deleted as dead code: it is what holds if the schema ever regresses or the
column is re-widened. ``TestThePremiseIsUnconstructible`` makes that unreachability
EXECUTABLE — if someone drops the FK, it fails, and these tests must go back to
driving ``main()`` end-to-end.

⛔ CONSEQUENCE FOR THIS FILE. The refusal is exercised by calling the script's
decision function ``plan_new_tenant`` DIRECTLY. Every test that used to construct an
orphan licence was asserting against a row PostgreSQL refuses to store, so all six
failed with ``ForeignKeyViolation`` on ``fk_license_tenant`` and none of them ever
reached the behaviour they named. ⛔ Do NOT "fix" that by disabling the FK in a
fixture: the premise being impossible is the estate working, and a fixture that
suspends it would be testing a database this service does not have.
────────────────────────────────────────────────────────────────────────────────────

⛔ WHY THE PREDICATE IS IMPORTED RATHER THAN ``ensure_tenant`` CALLED.
``ensure_tenant`` opens its OWN session and COMMITS. Calling it from the backfill loop
would write tenant rows during a DRY RUN — this script's DEFAULT mode — because that
commit is not the one at the bottom of ``main()`` that ``--apply`` gates. A guard that
silently converts the safe mode into the writing mode is not a guard.
``test_a_dry_run_still_writes_nothing`` is the control for exactly that, and it is the
test that fails if someone "simplifies" this by calling ``ensure_tenant``.

So the script imports the SAME ``services.tenant_subject_invariant.subject_for_tenant_id``
predicate ``ensure_tenant`` imports — the precedent ``seed_platform_tenants`` already
set, and which ``models.py`` documents as "Same predicate, different failure mode".
``test_it_uses_the_shared_predicate`` pins that there is still ONE definition of a
violation and not a third copy of the join.

⛔ REFUSE, NEVER RE-KEY. A hit is skipped and reported. Repairing an existing row is a
data migration with its own blast radius and is explicitly out of scope;
``TestNothingIsRepaired`` fails if that ever changes by accident.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from models import Tenant

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backfill_tenants_byo_mode.py"
_spec = importlib.util.spec_from_file_location("backfill_byo_guard", _SCRIPT)
assert _spec is not None and _spec.loader is not None
backfill = importlib.util.module_from_spec(_spec)
# ⛔ REGISTER BEFORE EXEC. The script defines a frozen @dataclass, and
# `dataclasses._is_type` resolves the decorated class's own module via
# `sys.modules.get(cls.__module__).__dict__`. A module built with
# `module_from_spec` is NOT in `sys.modules` until someone puts it there, so
# that lookup returns None and collection dies with
# `AttributeError: 'NoneType' object has no attribute '__dict__'` — at IMPORT
# time, which reads as a broken test file rather than a loader mistake.
sys.modules[_spec.name] = backfill
_spec.loader.exec_module(backfill)

# A tenant id the script's own allowlists classify as `local_allowed` (gerard-dev).
# Used by the dry-run control, which needs a row whose computed mode DIFFERS from its
# stored one so that there is a pending write for the dry run to withhold.
LOCAL_ALLOWED_ID = UUID("019de842-c92b-74cd-a7ce-848302bce3b2")


@pytest.fixture
def run_backfill(db_manager, monkeypatch):
    """Run the script's real ``main()`` through the FIXTURE's connection.

    ⛔ NO SECOND CONNECTION IS OPENED, AND THAT IS THE POINT. ``main()`` does
    ``DatabaseManager(os.getenv("DATABASE_URL"))``, and ``DatabaseManager.__init__``
    runs ``_append_ssl_params``, which defaults ``POSTGRES_SSLMODE`` to **require**
    (``models.py:2287``). The conftest connects with a raw ``create_engine`` and the
    ``db_manager`` fixture bypasses ``__init__`` entirely (``__new__`` + assign
    engine), which is why no other test meets this. CI's per-job ephemeral
    ``postgres:18`` serves no TLS, so the script's own connection dies with "server
    does not support SSL, but SSL was required" — in CI as much as locally.

    Routing the script's ``DatabaseManager`` at the fixture's already-connected
    manager makes the test ENV-INDEPENDENT: same idiom as
    ``test_seed_portal_n8n_dev._route_seeder_through_fixture``. ⚠️ The patch must go
    on ``backfill``'s own namespace, not on ``models`` — this script binds the name
    at module import (``from models import DatabaseManager``), so patching
    ``models.DatabaseManager`` would leave the script's reference untouched.

    ⚠️ ``DATABASE_URL`` is set to a DELIBERATELY UNUSABLE value. It only has to be
    non-empty to clear ``main()``'s presence check; if the patch above ever stops
    taking effect, the test fails loudly on this DSN instead of quietly opening a
    real second connection and passing for the wrong reason.

    ⛔ The alternative — ``monkeypatch.setenv("POSTGRES_SSLMODE", "disable")``, as
    ``test_rotate_revokes_the_old_service_token`` does — also works, but it leaves
    the script opening its own pool against a DSN the fixture is not managing. This
    way the script writes through the exact engine the assertions read from.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://unused/db")
    monkeypatch.setattr(backfill, "DatabaseManager", lambda *a, **k: db_manager)

    def _run(*argv: str) -> int:
        return backfill.main([*argv, "--tenant-owner", "platform"])

    return _run


def _principal(db_manager, principal_id: UUID):
    """Register ``principal_id`` as a Keycloak ``sub`` — the ``match="keycloak_sub"``
    class, which is 3 of the known rows on prod (2026-09-10)."""
    return db_manager.create_or_get_subject(keycloak_sub=str(principal_id), keycloak_realm="portal")


def _tenant_row(db_manager, tenant_id: UUID, *, name: str, byo_mode: str) -> None:
    session = db_manager.get_session()
    try:
        session.add(Tenant(id=tenant_id, name=name, byo_mode=byo_mode, owner_key="platform"))
        session.commit()
    finally:
        session.close()


def _licence_on(db_manager, seed_app, seed_plan, tenant_id: UUID):
    """An ACTIVE licence pointing at ``tenant_id``.

    ⚠️ The ``tenants`` row must already exist — see the module docstring.
    """
    holder = db_manager.create_or_get_subject(
        keycloak_sub=f"holder-{tenant_id}", keycloak_realm="secure-apps"
    )
    return db_manager.create_license(
        subject_id=UUID(str(holder.id)),
        app_id=UUID(str(seed_app.id)),
        plan_id=UUID(str(seed_plan.id)),
        tenant_id=str(tenant_id),
        status="active",
    )


def _tenant_rows(db_manager) -> list[Tenant]:
    session = db_manager.get_session()
    try:
        return session.query(Tenant).all()
    finally:
        session.close()


def _byo_mode(db_manager, tenant_id: UUID) -> str | None:
    session = db_manager.get_session()
    try:
        row = session.query(Tenant).filter(Tenant.id == tenant_id).first()
        return None if row is None else str(row.byo_mode)
    finally:
        session.close()


class TestThePremiseIsUnconstructible:
    """⛔ THE UNREACHABILITY, MADE EXECUTABLE INSTEAD OF LEFT IN A COMMENT.

    Every other test in this file is shaped by the fact below. Stating it only in
    prose would let it rot silently: drop ``fk_license_tenant`` and the guarded
    branch becomes LIVE again while these tests carry on passing for the wrong
    reason. This is the test that notices.
    """

    def test_an_orphan_licence_cannot_be_stored_at_all(
        self, db_manager, seed_app, seed_plan
    ) -> None:
        """Migration 016's FK refuses the row these tests used to build.

        If this starts FAILING, the FK is gone, the backfill's insert branch is
        reachable through the database again, and the refusal tests below should be
        rewritten to drive ``main()`` end-to-end rather than ``plan_new_tenant``.
        """
        with pytest.raises(IntegrityError) as exc:
            _licence_on(db_manager, seed_app, seed_plan, uuid4())

        assert "fk_license_tenant" in str(exc.value), (
            "a licence with an unprovisioned tenant_id was refused by something "
            f"OTHER than migration 016's foreign key: {exc.value}"
        )

    def test_the_tenant_cannot_be_deleted_out_from_under_a_licence(
        self, db_manager, seed_app, seed_plan
    ) -> None:
        """``ON DELETE RESTRICT`` — the other way an orphan could have appeared.

        Without this, ``ON DELETE SET NULL`` or ``CASCADE`` on the parent would
        reopen the branch and nothing here would say so.
        """
        tenant_id = uuid4()
        _tenant_row(db_manager, tenant_id, name="restrict-control", byo_mode="byo_required")
        _licence_on(db_manager, seed_app, seed_plan, tenant_id)

        session = db_manager.get_session()
        try:
            with pytest.raises(IntegrityError) as exc:
                session.query(Tenant).filter(Tenant.id == tenant_id).delete()
                session.commit()
        finally:
            session.rollback()
            session.close()

        assert "fk_license_tenant" in str(exc.value), str(exc.value)
        assert _byo_mode(db_manager, tenant_id) is not None, "the tenant row was destroyed"


class TestTheBackfillRefusesAPrincipalId:
    """The defect. On ``origin/main`` the tenant row was created.

    Driven through ``plan_new_tenant`` — the script's decision for a tenant_id with
    no ``tenants`` row — because the database will not let the branch be reached any
    other way. ``main()`` dispatches on exactly this function's result, so there is
    still ONE definition of the decision and no second copy to drift.
    """

    def test_no_tenant_is_minted_for_a_principal_id(self, db_manager) -> None:
        principal_id = uuid4()
        _principal(db_manager, principal_id)

        session = db_manager.get_session()
        try:
            plan = backfill.plan_new_tenant(
                session, str(principal_id), principal_id, "byo_required", "platform"
            )

            assert plan.action == "refuse_principal", (
                "the backfill would mint a tenant whose id is a Keycloak principal. "
                "That makes the identity that AUTHENTICATES the thing that PAYS, and "
                "nothing downstream can detect it — billing_cost_events has no FK to "
                f"tenants. plan={plan!r}"
            )
            assert plan.tenant is None, (
                "a Tenant row was built for a refused id; main() adds whatever is in "
                "`tenant`, so a non-None value here IS the mint"
            )
            assert plan.match_kind == "keycloak_sub", plan
            assert plan.subject_id, "the colliding principal is not identified"

            # ⛔ THE DECISION MUST NOT WRITE. `main()` computes the whole plan before
            # deciding whether to commit, so a planner that stages rows would make a
            # dry run write — the same defect `test_a_dry_run_still_writes_nothing`
            # guards from the other side.
            assert not session.new, f"plan_new_tenant staged rows on the session: {session.new}"
        finally:
            session.close()

        assert [t for t in _tenant_rows(db_manager) if str(t.id) == str(principal_id)] == []

    def test_a_subject_id_collision_is_refused_too(self, db_manager) -> None:
        """The second match class. ``subject_id`` sits at 0 rows on prod, so only a
        test can prove the predicate still sees it — an unexercised class is exactly
        where a guard silently stops matching."""
        subject = _principal(db_manager, uuid4())
        subject_pk = UUID(str(subject.id))

        session = db_manager.get_session()
        try:
            plan = backfill.plan_new_tenant(
                session, str(subject_pk), subject_pk, "byo_required", "platform"
            )
        finally:
            session.close()

        assert plan.action == "refuse_principal", plan
        assert plan.match_kind == "subject_id", plan

    def test_the_refusal_is_reported_loudly_and_not_as_a_downgrade(
        self, db_manager, capsys
    ) -> None:
        """⛔ TWO REFUSAL CLASSES, TWO REMEDIES. A BYO-mode downgrade is fixed by
        editing the allowlists in the script; this one is fixed UPSTREAM, by pointing
        ``license.tenant_id`` at the organisation. Reporting them under one heading
        would put the wrong remedy next to a money defect."""
        principal_id = uuid4()
        subject = _principal(db_manager, principal_id)

        session = db_manager.get_session()
        try:
            plan = backfill.plan_new_tenant(
                session, str(principal_id), principal_id, "byo_required", "platform"
            )
        finally:
            session.close()

        backfill.report_refused_principals([plan])
        out = capsys.readouterr().out

        assert "KEYCLOAK PRINCIPAL" in out, (
            f"the refusal was silent; a skipped row that prints nothing reads as a "
            f"clean run. output={out!r}"
        )
        assert str(principal_id) in out, "the refused id is not named, so nobody can fix it"
        assert str(subject.id) in out, "the colliding principal is not named"
        assert "keycloak_sub" in out, (
            "the MATCH class is missing, and it is what decides the remedy — a "
            f"keycloak_sub hit and a subject_id hit are fixed differently. out={out!r}"
        )
        assert "DOWNGRADE" not in out, (
            "the principal refusal was printed under the byo-mode DOWNGRADE heading, "
            "whose remedy is to edit the allowlists — the wrong remedy for a money "
            f"defect. out={out!r}"
        )

    def test_nothing_is_printed_when_there_is_nothing_to_refuse(self, capsys) -> None:
        """The control on the reporter. A block that printed its heading for an empty
        list would make every clean run look like an incident."""
        backfill.report_refused_principals([])
        assert capsys.readouterr().out == ""

    def test_the_summary_counts_refusals_separately_from_downgrades(
        self, run_backfill, capsys
    ) -> None:
        """End-to-end, and reachable: the two counters are distinct FIELDS on the
        summary line. If they were ever summed, an operator reading ``refused=`` would
        think the remedy was an allowlist edit."""
        assert run_backfill() == 0

        out = capsys.readouterr().out
        assert "refused_principal=" in out, f"the counter is not reported at all; {out!r}"
        assert "refused=" in out, out

    def test_it_uses_the_shared_predicate(self) -> None:
        """⛔ ONE DEFINITION OF A VIOLATION, THREE CALLERS.

        ``ensure_tenant`` and ``seed_platform_tenants`` both import
        ``subject_for_tenant_id`` rather than restating the join. A third copy here
        would let the guard and the ``nonagentic_license_tenant_is_subject_rows``
        gauge drift into disagreeing about what a violation IS — and a guard that
        disagrees with its own detector is worse than no guard, because the detector
        reads clean.
        """
        source = _SCRIPT.read_text(encoding="utf-8")
        assert "subject_for_tenant_id" in source, "the backfill no longer uses the shared predicate"
        assert "from services.tenant_subject_invariant import" in source


class TestTheBackfillStillDoesItsJob:
    """⛔ THE NEGATIVE CONTROLS. Green before this change and green after it.

    Without these, a guard that refused EVERYTHING — or one wired to the wrong
    column — would pass every assertion above while breaking the script's actual
    purpose, which is to give every active-licence tenant an explicit, classified
    ``byo_mode`` row.
    """

    def test_an_ordinary_tenant_is_still_created(self, db_manager) -> None:
        """The guard must skip a row, not refuse ids that are not principals at all,
        and not change how the surviving ones are classified."""
        ordinary = uuid4()  # registered as no subject anywhere

        session = db_manager.get_session()
        try:
            plan = backfill.plan_new_tenant(
                session, str(ordinary), ordinary, "local_allowed", "platform"
            )

            assert plan.action == "create", (
                "the backfill stopped creating legitimate tenant rows — the guard is "
                f"refusing ids that are not principals at all. plan={plan!r}"
            )
            assert plan.tenant is not None
            assert UUID(str(plan.tenant.id)) == ordinary, "the row was minted under another id"
            assert str(plan.tenant.byo_mode) == "local_allowed", (
                "the classification was lost; the guard must skip a row, not change "
                "how the surviving ones are classified"
            )
            assert not session.new, "the planner staged the row itself instead of returning it"
        finally:
            session.close()

    def test_a_legacy_non_uuid_tenant_id_is_still_created_unguarded(self, db_manager) -> None:
        """⚠️ DELIBERATELY UNGUARDED, and the reason is worth pinning. The predicate
        matches a candidate UUID against ``subject.keycloak_sub``/``subject.id``;
        there is no caller-supplied id here to collide with, because the row is born
        with a fresh uuid7. Same reasoning that leaves ``create_tenant`` unguarded.

        ⛔ THE ``tenant-`` PREFIX IS LOAD-BEARING, NOT COSMETIC — this assertion
        first read ``== "n8n-dev"`` and the SCRIPT was right, not the test.
        ``_pick_name`` derives ``tenant-<id>`` for any id with no ``FRIENDLY_NAMES``
        entry specifically so a backfilled name can NEVER equal ``id::text``,
        because ``migrations/004_backfill_license_tenant_uuid.sql:33`` runs
        ``DELETE FROM tenants WHERE name = id::text`` — a self-conflict that
        silently removed ~39 backfilled rows when 004 applied on 2026-07-09,
        including the render/E2E service account. Asserting the bare id here would
        have pinned the deleted shape as the contract.
        """
        session = db_manager.get_session()
        try:
            plan = backfill.plan_new_tenant(session, "n8n-dev", None, "local_allowed", "platform")
        finally:
            session.close()

        assert plan.action == "create", plan
        assert plan.tenant is not None
        assert plan.tenant.id is None, "a legacy name was minted under a caller-supplied id"
        assert plan.name == "tenant-n8n-dev", (
            "the `tenant-` prefix is gone, so a backfilled name can equal id::text "
            "again and migration 004's DELETE can wipe the row — see the docstring"
        )
        assert str(plan.tenant.name) == plan.name

    def test_a_principal_id_does_not_block_its_neighbours(self, db_manager) -> None:
        """⛔ A REFUSAL IS A `continue`, NOT AN ABORT.

        One bad id must not stop the other tenants being backfilled — otherwise the
        guard converts a single upstream defect into a total outage of this script,
        and an operator's only way out is to remove the guard. Each id is decided
        independently and the refusal carries no state into the next decision, which
        is what makes ``main()``'s ``continue`` safe.
        """
        principal_id = uuid4()
        _principal(db_manager, principal_id)
        ordinary = uuid4()

        session = db_manager.get_session()
        try:
            refused = backfill.plan_new_tenant(
                session, str(principal_id), principal_id, "byo_required", "platform"
            )
            following = backfill.plan_new_tenant(
                session, str(ordinary), ordinary, "local_allowed", "platform"
            )
        finally:
            session.close()

        assert refused.action == "refuse_principal", refused
        assert following.action == "create", (
            f"a refused id poisoned the decision for the NEXT tenant_id; {following!r}"
        )
        assert following.tenant is not None
        assert str(following.tenant.byo_mode) == "local_allowed"

    def test_a_dry_run_still_writes_nothing(
        self, db_manager, run_backfill, seed_app, seed_plan, capsys
    ) -> None:
        """⛔ THE CONTROL THAT FORBIDS THE OBVIOUS WRONG FIX.

        "Route it through ``ensure_tenant``" reads as the clean answer and is not:
        ``ensure_tenant`` opens its own session and COMMITS, so calling it from the
        loop would write during the script's DEFAULT mode — a dry run that writes.

        ⚠️ It drives the UPDATE path, not the CREATE path, because CREATE is
        unreachable (module docstring). The row's stored ``byo_mode`` is deliberately
        set to something ``classify()`` disagrees with, so there IS a pending write
        for the dry run to withhold — the ``"UPDATE"`` assertion is what stops this
        passing vacuously against a run that found nothing to do at all.
        """
        _tenant_row(db_manager, LOCAL_ALLOWED_ID, name="dry-run-subject", byo_mode="byo_required")
        _licence_on(db_manager, seed_app, seed_plan, LOCAL_ALLOWED_ID)

        assert run_backfill() == 0  # no --apply
        out = capsys.readouterr().out

        assert "UPDATE" in out, (
            "the dry run planned no change at all, so it cannot show that a dry run "
            f"withholds one; output={out!r}"
        )
        assert "DRY-RUN" in out, out
        assert _byo_mode(db_manager, LOCAL_ALLOWED_ID) == "byo_required", (
            "a DRY RUN committed an UPDATE. Something in the loop commits on its own "
            "session instead of deferring to main()'s single --apply-gated commit."
        )

        assert run_backfill("--apply") == 0
        assert _byo_mode(db_manager, LOCAL_ALLOWED_ID) == "local_allowed", (
            "the control failed: --apply did not commit either, so the assertion "
            "above proves nothing about the dry run"
        )


class TestNothingIsRepaired:
    """⛔ REFUSE, NEVER RE-KEY — and never adopt, either.

    Repairing an existing violating row is a data migration with its own blast
    radius (1,399 billing rows / $86.02 in 24h hang off ``e148e8af-…``) and is
    explicitly out of scope. This script has never UPDATEd an id and must not start.
    """

    def test_an_existing_violating_row_is_left_exactly_as_it_is(
        self, db_manager, run_backfill, seed_app, seed_plan
    ) -> None:
        """⭐ THE LIVE PROD SHAPE, AND THE ONE CASE THAT IS STILL END-TO-END.

        ``e148e8af-…`` sits in ``tenants`` as ``portal-render-e2e`` AND is the
        ``keycloak_sub`` of a real subject. Because the ``tenants`` row EXISTS, this
        state is legal under ``fk_license_tenant`` — which is precisely why it is the
        shape that actually occurs, and why it is reachable here when the orphan is
        not. The guard sits on the INSERT branch only, so this row is found by the
        existing-row lookup and never reaches it — which is correct. What must not
        happen is the script deleting it, re-keying it, or renaming it.
        """
        principal_id = uuid4()
        _principal(db_manager, principal_id)
        _tenant_row(db_manager, principal_id, name="pre-existing-violator", byo_mode="byo_required")
        _licence_on(db_manager, seed_app, seed_plan, principal_id)

        assert run_backfill("--apply") == 0

        rows = [t for t in _tenant_rows(db_manager) if str(t.id) == str(principal_id)]
        assert len(rows) == 1, "the pre-existing row was destroyed or duplicated"
        assert rows[0].name == "pre-existing-violator", "the row was re-keyed or renamed"
        assert str(rows[0].id) == str(principal_id), "the id itself was rewritten"
