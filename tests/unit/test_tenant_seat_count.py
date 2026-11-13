"""The seat-count predicate, and the negative controls that stop it over-billing.

WHY THIS FILE EXISTS
--------------------
billing-service bills a per-seat access charge ($25/seat, George 2026-09-10) and
needs a seat count. The tempting source is ``GET /internal/licenses``, which
billing already calls — and counting it would FABRICATE REVENUE. Measured on prod
2026-09-10: **164** rows carry ``status='active'``, of which 107 belong to
``internal`` tenants, 34 to ``service``, 15 to ``test``, and only **8** to the
five active ``kind='customer'`` tenants. Neither ``kind`` nor ``archived_at``
appears on that endpoint, so a per-tenant count over it bills ~156 phantom seats
— roughly $3,900/month of invented revenue.

⚠️ ``status='active'`` IS NOT VALIDITY. Nothing in this estate transitions a row
out of ``'active'`` when ``expires_at`` passes (see ``expire_stale_licenses`` and
``License.effective_status``), and a row whose ``starts_at`` is in the future is
``pending``. A count on the stored column alone therefore OVER-bills on both
edges, and each edge has its own test below.

⚠️ EVERY EXCLUSION HERE IS PROVEN NON-VACUOUSLY. Each test seeds the excluded
shape ALONGSIDE a licence that must still be counted, so a predicate that dropped
its clause entirely and a predicate that excluded everything are distinguishable.
A test that asserts only "seats == 0" passes on an empty table and proves nothing.

⭐ A SEAT IS A PERSON (George, 2026-09-11) — AND ``subjects`` DOES NOT SAY THAT.
A ``subject`` row is an authenticated PRINCIPAL, so machine accounts are in it: 58
of the 254 live rows are in realm ``service``, and measured 2026-09-11 both
``ahtapol`` and ``cdmsfirst`` hold subjects spanning ``portal`` AND ``service``
(one being ``svc-workers-ahtapol``). ``human_subjects`` is the basis-conformant
count, ``service_subjects`` discloses what it excluded, and
``unresolved_subjects`` refuses to promote an unknown principal into either —
see ``TestHumansVersusRobots`` below.

⭐ ``seats`` AND ``subjects`` ARE BOTH RETURNED, and the last test here is why:
provisioning mints one licence per onboarded user per app while fr/kanban/n8n are
sold per-tenant, so one person holding three products is THREE seats and ONE
subject. Measured: ahtapol holds 3 active licences across 3 apps with 2 distinct
subjects — $75 against $50. The billed basis is licences; the other number is
published beside it so the divergence cannot hide.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from models import DatabaseManager, License, Tenant
from tests.conftest import SEED_OTHER_TENANT_ID, SEED_TENANT_ID


def _mk_license(
    db: DatabaseManager,
    app,
    plan,
    *,
    subject_key: str,
    tenant_id: str = SEED_TENANT_ID,
    status: str = "active",
    starts_days: float = -1,
    expires_days: float | None = 30,
    realm: str = "secure-apps",
) -> License:
    """Seed ONE licence in an explicit lifecycle shape.

    ``expires_days=None`` is PERPETUAL (``expires_at IS NULL``) — a different
    thing from one that expired long ago, and both must count as a seat or not
    for their own reasons.

    ``realm`` is the SUBJECT's Keycloak realm and it decides personhood, not
    lifecycle: ``'service'`` is a machine account. It defaults to ``secure-apps``
    so every pre-existing test above keeps seeding a human and its numbers are
    unchanged.
    """
    subject = db.create_or_get_subject(keycloak_sub=subject_key, keycloak_realm=realm)
    now = datetime.now(UTC)
    return db.create_license(
        subject_id=UUID(str(subject.id)),
        app_id=UUID(str(app.id)),
        plan_id=UUID(str(plan.id)),
        tenant_id=tenant_id,
        status=status,
        starts_at=now + timedelta(days=starts_days),
        expires_at=None if expires_days is None else now + timedelta(days=expires_days),
    )


def _seats(db: DatabaseManager, tenant_id: str = SEED_TENANT_ID, **kw) -> dict:
    result = db.count_tenant_seats(tenant_id, **kw)
    assert result is not None, f"tenant {tenant_id} not found — the fixture did not seed it"
    return result


class TestSeatPredicate:
    def test_an_active_started_unexpired_licence_is_a_seat(
        self, db_manager, seed_app, seed_plan
    ) -> None:
        """The positive control. Without it every exclusion below is unfalsifiable."""
        _mk_license(db_manager, seed_app, seed_plan, subject_key="seat-positive")
        out = _seats(db_manager)
        assert out["seats"] == 1
        assert out["subjects"] == 1
        assert out["basis"] == "active_licences"

    def test_a_perpetual_licence_is_a_seat(self, db_manager, seed_app, seed_plan) -> None:
        """``expires_at IS NULL`` means no end date, not an expired one."""
        _mk_license(
            db_manager, seed_app, seed_plan, subject_key="seat-perpetual", expires_days=None
        )
        assert _seats(db_manager)["seats"] == 1

    def test_an_expired_row_still_stored_active_is_not_a_seat(
        self, db_manager, seed_app, seed_plan
    ) -> None:
        """⭐ THE EDGE THE STORED COLUMN CANNOT SEE.

        Nothing transitions ``status`` when ``expires_at`` passes, so this row
        reads ``'active'`` in the database for ever. Counting it bills a seat the
        customer no longer holds.

        ⚠️ MUTATION TARGET: drop the ``expires_at`` clause from
        ``count_tenant_seats`` and this reads 2.
        """
        _mk_license(db_manager, seed_app, seed_plan, subject_key="seat-live")
        _mk_license(db_manager, seed_app, seed_plan, subject_key="seat-expired", expires_days=-5)
        # NON-VACUOUS: the live row must survive, so "excluded everything" fails too.
        assert _seats(db_manager)["seats"] == 1

    def test_a_pending_licence_is_not_a_seat(self, db_manager, seed_app, seed_plan) -> None:
        """A licence that has not STARTED is ``pending`` per ``effective_status``
        — a sold seat, not yet an occupied one.

        ⚠️ MUTATION TARGET: drop the ``starts_at`` clause and this reads 2.
        """
        _mk_license(db_manager, seed_app, seed_plan, subject_key="seat-live2")
        _mk_license(db_manager, seed_app, seed_plan, subject_key="seat-pending", starts_days=+5)
        assert _seats(db_manager)["seats"] == 1

    def test_a_revoked_licence_is_not_a_seat(self, db_manager, seed_app, seed_plan) -> None:
        _mk_license(db_manager, seed_app, seed_plan, subject_key="seat-live3")
        _mk_license(db_manager, seed_app, seed_plan, subject_key="seat-revoked", status="revoked")
        assert _seats(db_manager)["seats"] == 1

    def test_another_tenants_licence_is_not_counted(self, db_manager, seed_app, seed_plan) -> None:
        """The whole $3,900/month failure in one assertion: a licence belonging to
        a different tenant must not be billed to this one."""
        _mk_license(db_manager, seed_app, seed_plan, subject_key="seat-mine")
        _mk_license(
            db_manager,
            seed_app,
            seed_plan,
            subject_key="seat-theirs",
            tenant_id=SEED_OTHER_TENANT_ID,
        )
        assert _seats(db_manager)["seats"] == 1
        assert _seats(db_manager, SEED_OTHER_TENANT_ID)["seats"] == 1


class TestArchivedAndUnknown:
    def test_an_unknown_tenant_returns_none_not_zero(self, db_manager) -> None:
        """⛔ None, never 0. The route turns this into a 404, and billing's client
        treats 404 as a durable answer while any 5xx means "ask again". Collapsing
        absence into 0 here would make an outage look like an empty tenant."""
        assert db_manager.count_tenant_seats("01000000-0000-7000-8000-0000000000ff") is None

    def test_an_archived_tenant_reports_zero_with_a_basis(
        self, db_manager, seed_app, seed_plan
    ) -> None:
        """An archived tenant is OFF and accrues no seat charge — but the 0 says
        why, so "turned off" is distinguishable from "has no seats".

        ⚠️ MUTATION TARGET: remove the archived branch and this reads 1 seat with
        basis ``active_licences`` — a seat charge against a tenant the estate has
        turned off.
        """
        _mk_license(db_manager, seed_app, seed_plan, subject_key="seat-archived-t")
        # Archive the tenant AFTER seeding a live licence, so the licence itself
        # is unambiguously countable and only the tenant's state changes.
        db_manager.set_tenant_archived(SEED_TENANT_ID, True)
        out = _seats(db_manager)
        assert out["seats"] == 0
        assert out["basis"] == "tenant_archived"
        assert out["tenant_archived"] is True


class TestSeatsVersusSubjects:
    def test_one_person_with_three_products_is_three_seats_and_one_subject(
        self, db_manager, seed_app, seed_plan
    ) -> None:
        """⭐ THE DIVERGENCE, pinned so nobody rediscovers it on an invoice.

        ``seats`` is the agreed billing basis (active licences) and ``subjects``
        is distinct humans. They are not the same number, and at $25/seat the
        difference is real money: measured on prod, ahtapol holds 3 active
        licences across 3 apps with 2 distinct subjects — $75 against $50.

        This test does NOT assert which is right. It asserts that both are
        reported, so the choice stays visible.
        """
        # Three licences for the SAME subject. (The partial unique is on
        # (tenant, app, subject) for active rows, so three apps are needed.)
        for i in range(3):
            app = db_manager.create_app(
                key=f"seat-multi-app-{i}",
                name=f"Seat Multi {i}",
                audience="platform",
                issuers=("platform",),
            )
            plan = db_manager.create_plan(
                app_id=UUID(str(app.id)), code=f"seat-multi-plan-{i}", name="Plan"
            )
            _mk_license(db_manager, app, plan, subject_key="seat-one-human")

        out = _seats(db_manager)
        assert out["seats"] == 3
        assert out["subjects"] == 1


class TestHumansVersusRobots:
    """⭐ THE OPERATOR'S BASIS: A SEAT IS A PERSON (George, 2026-09-11).

    ``subjects`` does NOT implement that basis — a ``subject`` row is an
    authenticated PRINCIPAL, and 58 of the 254 live rows are in realm ``service``.
    Measured on the primary 2026-09-11, ``ahtapol`` (3 active licences, 2 subjects)
    and ``cdmsfirst`` (2 and 2) each span realms ``portal`` AND ``service``; one of
    ahtapol's two is ``svc-workers-ahtapol``, a robot. So 2 of the 5 customer
    tenants would be billed $25/month for a machine account.

    ``human_subjects`` is the basis-conformant number; ``service_subjects``
    discloses what it dropped; ``unresolved_subjects`` refuses to guess.
    """

    def test_a_service_realm_subject_is_not_a_human_seat(
        self, db_manager, seed_app, seed_plan
    ) -> None:
        """The ahtapol shape, reduced: one person and one robot under one tenant.

        ⚠️ MUTATION TARGET: empty ``models.NON_HUMAN_SUBJECT_REALMS`` (or drop the
        realm exclusion from ``count_tenant_seats``) and ``human_subjects`` reads 2
        while ``service_subjects`` reads 0 — both assertions fail.
        ⚠️ NON-VACUOUS IN THE OTHER DIRECTION TOO: the human licence must SURVIVE,
        so a predicate that excluded every subject (``human_subjects == 0``) fails
        as well.
        """
        _mk_license(db_manager, seed_app, seed_plan, subject_key="seat-person", realm="portal")
        _mk_license(
            db_manager,
            seed_app,
            seed_plan,
            subject_key="svc-workers-probe",
            realm="service",
        )
        out = _seats(db_manager)
        # The licence-based numbers are UNTOUCHED by the person basis — billing
        # reads them today and the basis change has to stay auditable against them.
        assert out["seats"] == 2
        assert out["subjects"] == 2
        # ...and the person basis reports ONE colleague and says why it is one.
        assert out["human_subjects"] == 1
        assert out["service_subjects"] == 1
        assert out["unresolved_subjects"] == 0
        # The three PARTITION `subjects`: nothing is double-counted or dropped.
        assert (
            out["human_subjects"] + out["service_subjects"] + out["unresolved_subjects"]
            == out["subjects"]
        )

    def test_both_human_realms_count_as_people(self, db_manager, seed_app, seed_plan) -> None:
        """⛔ THE EXCLUSION IS A DENYLIST, NOT AN ALLOWLIST OF ONE REALM.

        Humans live in BOTH ``portal`` (28 rows) and ``secure-apps`` (166) —
        ``better-results-group``, ``book-fulfillment`` and ``subhankar-kag`` are
        ``secure-apps`` people. A predicate written as ``realm == 'portal'`` would
        silently reclassify 166 colleagues as robots.

        ⚠️ MUTATION TARGET: change the predicate to an allowlist
        (``realm == 'portal'``) and this reads ``human_subjects == 1``.
        """
        _mk_license(db_manager, seed_app, seed_plan, subject_key="seat-portal", realm="portal")
        _mk_license(
            db_manager, seed_app, seed_plan, subject_key="seat-secure-apps", realm="secure-apps"
        )
        out = _seats(db_manager)
        assert out["human_subjects"] == 2
        assert out["service_subjects"] == 0

    def test_an_archived_tenant_reports_every_count_as_zero_with_a_basis(
        self, db_manager, seed_app, seed_plan
    ) -> None:
        """The archived branch must return the SAME KEYS as the active one.

        A response whose shape depends on the branch forces billing to guess
        whether a missing ``human_subjects`` means 0 or means "this build does not
        have it yet" — and the safe-looking guess (fall back to ``subjects``) bills
        the robot.

        ⚠️ MUTATION TARGET: remove the three new keys from the ``tenant_archived``
        early return and this raises ``KeyError: 'human_subjects'``.
        """
        _mk_license(db_manager, seed_app, seed_plan, subject_key="seat-arch-person")
        _mk_license(db_manager, seed_app, seed_plan, subject_key="svc-seat-arch", realm="service")
        db_manager.set_tenant_archived(SEED_TENANT_ID, True)
        out = _seats(db_manager)
        assert out["basis"] == "tenant_archived"
        assert out["seats"] == 0
        assert out["subjects"] == 0
        assert out["human_subjects"] == 0
        assert out["service_subjects"] == 0
        assert out["unresolved_subjects"] == 0

    def test_an_unresolvable_subject_id_cannot_exist_and_would_never_be_a_human(
        self, db_manager, db_engine, seed_app, seed_plan
    ) -> None:
        """⛔ THE DECISION ON THE NULL / DANGLING ``subject_id`` CASE, PINNED.

        An unknown principal is counted in ``unresolved_subjects`` — NEITHER a
        person nor a robot. An inner join would delete such a licence from every
        subject count (silent under-billing); an outer join plus
        ``realm != 'service'`` would promote it to a PERSON (billing a ghost). Both
        decide the question by accident of join type, so it gets its own field.

        It should always read 0, and this test proves WHY rather than asserting the
        0 on its own (which would pass on an empty table): the schema makes both
        shapes impossible. ``license.subject_id`` is NOT NULL with an FK to
        ``subject.id``, so neither a NULL nor an orphan can be persisted.

        ⚠️ MUTATION TARGET: drop the FK (or the NOT NULL) from ``License.subject_id``
        and the matching ``pytest.raises(IntegrityError)`` fails — which is exactly
        the day ``unresolved_subjects`` stops being decorative.
        """
        import uuid

        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError

        # A live human licence, so the counts below are non-zero and the partition
        # assertion cannot pass vacuously.
        _mk_license(db_manager, seed_app, seed_plan, subject_key="seat-unresolved-control")

        # (1) An orphan subject_id is refused by the FK.
        with pytest.raises(IntegrityError):
            with db_engine.begin() as c:
                c.execute(
                    text("UPDATE license SET subject_id = :s"),
                    {"s": str(uuid.uuid4())},
                )
        # (2) A NULL subject_id is refused by NOT NULL.
        with pytest.raises(IntegrityError):
            with db_engine.begin() as c:
                c.execute(text("UPDATE license SET subject_id = NULL"))

        out = _seats(db_manager)
        assert out["seats"] == 1
        assert out["subjects"] == 1
        assert out["human_subjects"] == 1
        assert out["unresolved_subjects"] == 0
        assert (
            out["human_subjects"] + out["service_subjects"] + out["unresolved_subjects"]
            == out["subjects"]
        )


class TestAsOf:
    def test_as_of_counts_at_that_instant_not_now(self, db_manager, seed_app, seed_plan) -> None:
        """A CLOSED period must be counted at its own instant.

        A licence that expired 5 days ago was a seat 10 days ago. Without
        ``as_of`` every re-run of a historical invoice would silently re-count
        today's estate — the drift that makes an old statement irreproducible.
        """
        _mk_license(
            db_manager,
            seed_app,
            seed_plan,
            subject_key="seat-asof",
            starts_days=-20,
            expires_days=-5,
        )
        now = datetime.now(UTC)
        assert _seats(db_manager)["seats"] == 0  # today: expired
        assert _seats(db_manager, as_of=now - timedelta(days=10))["seats"] == 1  # then: live
        # And before it started, it was not a seat either.
        assert _seats(db_manager, as_of=now - timedelta(days=25))["seats"] == 0


class TestKindIsDisclosedNotFiltered:
    """⛔ THE CONTRACT THE DOCSTRING GOT WRONG, now pinned.

    The PR that shipped this endpoint claimed the predicate included
    `kind='customer'`. It does not, and the claim was caught by a live control
    after deploy: `n8n-dev` (kind='internal', 23 active licences) returned
    `seats: 23`.

    Disclosing `tenant_kind` rather than filtering on it is the RIGHT choice — a
    0 for a non-customer tenant is a fabricated zero, indistinguishable from a
    customer with no seats. But the contract has to be explicit, because the
    protection against billing an internal tenant lives in billing (no terms
    materialised + `effective_billing_mode == internal` skipping the whole
    access-charge block), and a reader who believes THIS filters `kind` could
    remove that gate.
    """

    def test_a_non_customer_tenant_reports_its_real_count(
        self, db_manager, seed_app, seed_plan
    ) -> None:
        """An internal tenant's seats are COUNTED and its kind is DISCLOSED.

        ⚠️ MUTATION TARGET: add `Tenant.kind == "customer"` to the filter in
        count_tenant_seats and this reads 0 — which is the fabricated zero the
        404-not-zero branch exists to avoid, wearing a different hat.
        """
        # `kind` is INSERT-only through register_tenant, so flip it directly —
        # this test is about the COUNT's predicate, not about the setter.
        session = db_manager.get_session()
        try:
            tenant = session.query(Tenant).filter(Tenant.id == UUID(SEED_TENANT_ID)).first()
            assert tenant is not None, "the seed tenant must exist for this test to mean anything"
            setattr(tenant, "kind", "internal")  # ORM types it as Column[str]
            session.commit()
        finally:
            session.close()

        _mk_license(db_manager, seed_app, seed_plan, subject_key="seat-kind-disclosed")
        out = _seats(db_manager)
        # COUNTED, not zeroed...
        assert out["seats"] == 1
        # ...and the caller is TOLD what kind of tenant it is, so it can decide
        # billability itself. That is the whole contract.
        assert out["tenant_kind"] == "internal"
        assert out["tenant_archived"] is False
