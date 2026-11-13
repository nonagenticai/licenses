"""A NEW subject's ``keycloak_sub`` must not be an id that is already a tenant.

⛔ THE DEFECT: THE SAME INVARIANT HAD A GUARDED DOOR AND AN UNGUARDED ONE.
``services/tenant_subject_invariant.py`` states it once — no ``tenants.id`` may
equal any ``subject.keycloak_sub`` nor any ``subject.id``. A tenant is an
ORGANISATION; a subject is an identity that AUTHENTICATES within one. Registering
one id as both makes the authenticating identity the thing that PAYS, and nothing
downstream can detect it: ``billing_cost_events`` has no FK to ``tenants`` and
UUID version does not separate the two populations.

Every guard built for #191/035 asks ONE of the two questions that implies:

  DOOR 1  "is this NEW TENANT id already a principal?"
          ``ensure_tenant`` / ``seed_platform_tenants`` /
          ``scripts/backfill_tenants_byo_mode`` — all guarded.
  DOOR 2  "is this NEW SUBJECT's keycloak_sub already a registered tenant id?"
          ``DatabaseManager.create_or_get_subject`` — UNGUARDED until 2026-09-23.

A collision made through door 2 is INDISTINGUISHABLE from one made through door 1:
same two tables, same join, same ``…_tenant_is_subject_rows{class="violation"}``
row. But no tenant is inserted, so nothing on the tenant side can fire — and
nothing refused it, logged it or counted it.

⚠️ THE UNGUARDED DOOR WAS THE LESS PRIVILEGED ONE, WHICH IS THE WRONG WAY ROUND.
``ensure_tenant`` is reached from ``PUT /tenants/{id}`` (``license-admin``).
``create_or_get_subject`` is reached from ``POST /licenses/user/{subject_id}`` —
where the sub is a caller-supplied PATH PARAMETER — under ``license-admin`` OR
``license-manager``, and from five ``scripts/provision_*`` entry points.

⭐ AND IT IS THE DOOR THE KNOWN FOURTH OFFENDER WOULD ARRIVE THROUGH. Measured on
prod 2026-09-23: 85 tenants, 82 with no ``subject`` row naming them — each one
call from this method away from a new violation. Among the 82 is
``e4f70a83-81ee-429a-abca-6999e3a0d24f`` ``nonagentic-gtm``, a LIVE ``secure-apps``
service-account principal already registered as a tenant, which 035 and the gauge
are blind to for exactly one reason: it was never written to ``subject``. So the
gauge's blind spot and this unguarded door are ONE HOLE SEEN FROM TWO SIDES — the
single event that would make that row visible was the event nothing was refusing.

⚠️ WHAT THESE TESTS ARE NOT. None of them asserts "a log line was emitted" as the
proof of refusal: a log line can be written by a call that inserted anyway. The
refusal is proven by MEASURING THE ROW — the subject must not exist afterwards.

Each test pins a decision a future edit could reverse with nothing else going red:

  1. EXISTING SUBJECTS ARE RETURNED, NEVER REFUSED. Three rows violate on prod and
     have served for months. A guard that refused them would break licence lookups
     for live identities — the same fail-soft rule ``seed_platform_tenants`` and
     ``report_at_boot`` state for themselves. The guard sits AFTER the idempotent
     early return precisely so this holds.
  2. A NON-UUID SUB IS NOT AN ERROR. ``subject.keycloak_sub`` is free ``TEXT`` and
     most real subs are not UUIDs (``service-account-book-fulfillment-svc``). The
     comparison is text-to-text against ``tenants.id::text``; an UNCAST version
     would not mis-answer, it would not compile.
  3. THE ORDINARY CASE STILL WORKS. A guard that refused everything would pass
     tests 1 and 2 by accident.
  4. THE COUNTER IS KEYED PER IDENTITY, not incremented per call — otherwise a
     retrying caller turns the gauge into a function of retry volume.
  5. THE HTTP CONTRACT IS 409, NOT 500. ``main.create_user_license`` carried a
     comment asserting this DAL path "contains no ``raise``" and had no
     ``ClientSafeError`` clause. Adding the guard without the call-site catch
     would have reported a deliberate caller-error refusal as a server fault,
     swallowing the caller-safe message and polluting 5xx alerting.
  6. ⭐ THE REFUSAL IS NARROW — only a GENUINELY NEW registration raises. The
     first version refused every collision and was refuted twice over: CI went
     `assert 409 == 201` on
     ``test_endpoints.py::TestCreateUserLicenseMintGuard::test_guard_does_not_fire_for_real_self_tenant``,
     and prod would have refused `e4f70a83-…` nonagentic-gtm's next mint (5 live
     licences, ZERO subject rows) along with the first mint of every identity in
     that shape after a DR rebuild. Two exemptions, each derived from live state
     rather than from any list of ids, each pinned BOTH ways in
     ``TestTheNarrowingLetsPreExistingIdentitiesKeepMinting``: the tenant already
     holds licences, or the caller is minting that tenant its own licence.
     ⚠️ A narrowing that only proves the admissions is the failure mode here, so
     the refusing half is pinned with a measured zero-licence precondition.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from models import ClientSafeError, Subject, Tenant
from services import tenant_subject_invariant as tsi


@pytest.fixture(autouse=True)
def _clean_refusal_register():
    """The register is PROCESS-lifetime, so it leaks between tests without this."""
    tsi.reset_subject_refusals()
    yield
    tsi.reset_subject_refusals()


def _make_tenant(db_manager, name: str) -> str:
    """Register an ordinary organisation and return its id as text."""
    tenant = db_manager.create_tenant(name=name, owner_key="platform")
    return str(tenant.id)


def _subject_exists(db_manager, sub: str) -> bool:
    session = db_manager.get_session()
    try:
        return session.query(Subject).filter(Subject.keycloak_sub == sub).first() is not None
    finally:
        session.close()


class TestTheSubjectDoorRefusesATenantId:
    def test_the_subject_row_is_not_created(self, db_manager) -> None:
        """⛔ THE CORE ASSERTION, measured on the ROW and not on a log line."""
        tenant_id = _make_tenant(db_manager, "door-two-org")

        with pytest.raises(ClientSafeError) as exc:
            db_manager.create_or_get_subject(keycloak_sub=tenant_id, keycloak_realm="portal")

        assert not _subject_exists(db_manager, tenant_id), (
            f"a subject was created for {tenant_id} even though that id is already a "
            "registered tenant — door 2 onto the tenant/subject invariant is still open"
        )
        # The message must name BOTH halves, because the operator reading it has to
        # know which id to change and what it collided with.
        assert tenant_id in str(exc.value)
        assert "door-two-org" in str(exc.value)

    def test_the_refusal_is_counted_and_named(self, db_manager) -> None:
        """A refusal nobody can see is a refusal nobody will fix upstream."""
        tenant_id = _make_tenant(db_manager, "counted-org")

        with pytest.raises(ClientSafeError):
            db_manager.create_or_get_subject(keycloak_sub=tenant_id, keycloak_realm="portal")

        refusals = tsi.subject_refusals()
        assert refusals == {(tenant_id, "portal"): "counted-org"}

    def test_the_counter_is_keyed_not_tallied(self, db_manager) -> None:
        """⛔ Retrying must not inflate the gauge.

        A caller that retries a refused mint would otherwise turn this number into a
        measure of retry volume rather than of how many distinct identities are
        confused — the same rule the seed register states for boot count.
        """
        tenant_id = _make_tenant(db_manager, "retried-org")

        for _ in range(3):
            with pytest.raises(ClientSafeError):
                db_manager.create_or_get_subject(keycloak_sub=tenant_id, keycloak_realm="portal")

        assert len(tsi.subject_refusals()) == 1

    def test_a_clean_registration_records_nothing(self, db_manager) -> None:
        """The control. Without it a register that ALWAYS held an entry would pass."""
        db_manager.create_or_get_subject(keycloak_sub="an-ordinary-sub", keycloak_realm="portal")
        assert tsi.subject_refusals() == {}


class TestTheGuardDoesNotOverreach:
    def test_an_ordinary_subject_is_still_created(self, db_manager) -> None:
        """⛔ THE NEGATIVE CONTROL. A guard that refused everything passes the rest."""
        subject = db_manager.create_or_get_subject(
            keycloak_sub="service-account-ordinary-svc", keycloak_realm="secure-apps"
        )
        assert subject.id is not None
        assert _subject_exists(db_manager, "service-account-ordinary-svc")

    def test_a_uuid_that_is_not_a_tenant_is_allowed(self, db_manager) -> None:
        """⛔ UUID SHAPE IS NOT THE PREDICATE.

        Most real subs ARE UUIDs. If the guard keyed on "looks like a UUID" instead
        of "IS a row in tenants", it would refuse nearly every genuine principal —
        the instrument would condemn the estate it exists to protect.
        """
        stranger = str(uuid.uuid4())
        subject = db_manager.create_or_get_subject(keycloak_sub=stranger, keycloak_realm="portal")
        assert subject.id is not None

    def test_a_non_uuid_sub_does_not_raise_a_cast_error(self, db_manager) -> None:
        """⚠️ ``keycloak_sub`` is free TEXT; ``tenants.id`` is ``uuid``.

        An uncast comparison does not return the wrong answer — it does not compile
        (``operator does not exist: text = uuid``). This is the test that fails if
        someone "simplifies" ``lower(t.id::text) = lower(:candidate)``.
        """
        subject = db_manager.create_or_get_subject(
            keycloak_sub="service-account-book-fulfillment-svc", keycloak_realm="secure-apps"
        )
        assert subject.id is not None

    def test_an_existing_violating_subject_is_returned_not_refused(self, db_manager) -> None:
        """⛔ THE FAIL-SOFT RULE. Existing violations are REPORTED, never refused.

        This is the ``e148e8af-…`` / ``1d0090ac-…`` shape: the pair already exists and
        has served for months. Refusing it here would break licence lookups for live
        identities over a billing-hygiene defect — the exact trade
        ``report_at_boot`` and ``seed_platform_tenants`` refuse to make.

        The pre-guard pair is fabricated by direct writes, deliberately: going
        through the DAL could only ever build states the guards permit, which is
        precisely the set of states that are NOT the defect.
        """
        tenant_id = str(uuid.uuid4())
        session = db_manager.get_session()
        try:
            session.add(
                Tenant(id=uuid.UUID(tenant_id), name="legacy-violator", owner_key="platform")
            )
            session.add(Subject(keycloak_sub=tenant_id, keycloak_realm="portal"))
            session.commit()
        finally:
            session.close()

        returned = db_manager.create_or_get_subject(keycloak_sub=tenant_id, keycloak_realm="portal")

        assert str(returned.keycloak_sub) == tenant_id
        assert tsi.subject_refusals() == {}, (
            "returning an EXISTING violating subject recorded a refusal — the counter "
            "must measure violations PREVENTED, not violations that already exist "
            "(…_tenant_is_subject_rows is what counts those)"
        )


class TestThePredicateIsShared:
    def test_the_mirror_predicate_agrees_with_the_gauge(self, db_manager) -> None:
        """⛔ ONE DEFINITION PER DIRECTION, NEVER A COPY OF THE COMPARISON.

        If ``tenant_for_subject_sub`` drifted from the join the gauge runs, the guard
        and the alert would disagree about what a violation is — and the guard would
        be the one nobody notices is wrong.
        """
        tenant_id = _make_tenant(db_manager, "shared-predicate-org")
        session = db_manager.get_session()
        try:
            hit = tsi.tenant_for_subject_sub(session, tenant_id)
            miss = tsi.tenant_for_subject_sub(session, "definitely-not-a-tenant")
        finally:
            session.close()

        assert hit == (tenant_id, "shared-predicate-org")
        assert miss is None


class TestTheHttpContract:
    """⛔ 409, NOT 500 — see point 5 in the module docstring."""

    def test_minting_a_licence_for_a_tenant_id_is_a_409(
        self, auth_client_admin, db_manager, seed_app, seed_plan
    ) -> None:
        tenant_id = _make_tenant(db_manager, "http-contract-org")

        response = auth_client_admin.post(
            f"/licenses/user/{tenant_id}",
            json={"app_id": str(seed_app.id), "plan_id": str(seed_plan.id)},
        )

        assert response.status_code == 409, (
            "the principal-id refusal did not surface as a 409. If this is a 500 the "
            "call-site catch in main.create_user_license is missing and the broad "
            "`except Exception` swallowed a deliberate caller-error refusal — "
            "reporting the caller's mistake as a server fault and hiding the "
            "caller-safe message the DAL authored."
        )
        assert "already registered tenant" in response.json()["detail"]
        assert not _subject_exists(db_manager, tenant_id)


def _give_the_tenant_a_licence(db_manager, tenant_id, seed_app, seed_plan, status="active"):
    """Make `tenant_id` an identity that is already TRADING.

    The licence is held by a DIFFERENT, ordinary principal — which is the real
    shape: `e4f70a83-…` nonagentic-gtm holds 5 licences and has no `subject` row
    of its own. A licence held by the colliding id itself could not exist before
    the subject row does, so it could never be the evidence this predicate needs.
    """
    holder = db_manager.create_or_get_subject(
        keycloak_sub=f"service-account-holder-{uuid.uuid4()}", keycloak_realm="secure-apps"
    )
    return db_manager.create_license(
        subject_id=holder.id,
        app_id=seed_app.id,
        plan_id=seed_plan.id,
        tenant_id=tenant_id,
        status=status,
    )


class TestTheNarrowingLetsPreExistingIdentitiesKeepMinting:
    """⛔ THE GUARD REFUSES ONLY A *GENUINELY NEW* PRINCIPAL-AS-SUBJECT ROW.

    The first version of this guard refused every collision, and both CI and prod
    refuted it: `test_endpoints.py::TestCreateUserLicenseMintGuard::
    test_guard_does_not_fire_for_real_self_tenant` went `assert 409 == 201`, and
    `e4f70a83-…` nonagentic-gtm — 5 live licences, ZERO subject rows, measured
    2026-09-23 — would have had its next mint refused, as would the first mint of
    every identity in that shape after a DR rebuild.

    ⚠️ THE IDEMPOTENT EARLY RETURN IS NOT THE NARROWING. It spares only violators
    that happen to have been WRITTEN to `subject` already; the dangerous ones have
    not been. These tests therefore all assert `_subject_exists(...) is False`
    FIRST — without that precondition each of them would pass by taking the early
    return, i.e. by never reaching the guard at all.
    """

    def test_a_tenant_with_licences_and_no_subject_row_is_admitted(
        self, db_manager, seed_app, seed_plan
    ) -> None:
        """⭐ THE `nonagentic-gtm` SHAPE — the exact case CI caught, pinned forever."""
        tenant_id = _make_tenant(db_manager, "gtm-shaped-org")
        _give_the_tenant_a_licence(db_manager, tenant_id, seed_app, seed_plan)
        assert not _subject_exists(db_manager, tenant_id), (
            "precondition failed: this shape is only meaningful with NO subject row — "
            "otherwise the idempotent early return answers and the guard never runs"
        )

        subject = db_manager.create_or_get_subject(
            keycloak_sub=tenant_id, keycloak_realm="secure-apps"
        )

        assert subject.id is not None
        assert _subject_exists(db_manager, tenant_id)
        assert tsi.subject_refusals() == {}, (
            "an ADMITTED collision was counted as a refusal — that gauge must measure "
            "what the guard PREVENTED, or it can no longer say what it refused"
        )

    def test_a_revoked_licence_still_counts_as_pre_existing(
        self, db_manager, seed_app, seed_plan
    ) -> None:
        """⚠️ ANY status, not just `active`.

        Keying the exemption on `active` would make it blink in and out with
        ordinary licence lifecycle: an identity whose licences are all currently
        revoked would be refused today and admitted tomorrow, turning a hygiene
        guard into an intermittent outage.
        """
        tenant_id = _make_tenant(db_manager, "lapsed-org")
        _give_the_tenant_a_licence(db_manager, tenant_id, seed_app, seed_plan, status="revoked")

        subject = db_manager.create_or_get_subject(keycloak_sub=tenant_id, keycloak_realm="portal")

        assert subject.id is not None

    def test_a_self_tenant_mint_is_admitted_even_with_no_licences(self, db_manager) -> None:
        """The `portal-render-e2e` shape, at the primitive.

        `TestCreateUserLicenseMintGuard::test_guard_does_not_fire_for_real_self_tenant`
        pins the same thing through HTTP and is deliberately left UNMODIFIED; this
        states it where the decision is actually taken, so a future edit to the DAL
        cannot quietly move it without a red test here too.
        """
        tenant_id = _make_tenant(db_manager, "self-tenant-org")

        subject = db_manager.create_or_get_subject(
            keycloak_sub=tenant_id, keycloak_realm="portal", self_tenant_mint=True
        )

        assert subject.id is not None
        assert tsi.subject_refusals() == {}

    def test_the_door_is_still_shut_for_a_tenant_with_no_licences(self, db_manager) -> None:
        """⛔ THE OTHER HALF, and the one a narrowing usually loses.

        No licences, no subject row, not a self-tenant mint — a brand-new
        principal-as-subject registration, which is exactly what this guard exists
        to refuse. The zero-licence precondition is MEASURED with the same
        predicate the guard uses, so the test cannot pass because the exemption
        silently fired.
        """
        tenant_id = _make_tenant(db_manager, "brand-new-org")
        session = db_manager.get_session()
        try:
            assert tsi.tenant_has_licences(session, tenant_id) is False
        finally:
            session.close()
        assert not _subject_exists(db_manager, tenant_id)

        with pytest.raises(ClientSafeError) as exc:
            db_manager.create_or_get_subject(keycloak_sub=tenant_id, keycloak_realm="portal")

        assert "already registered tenant" in str(exc.value)
        assert not _subject_exists(db_manager, tenant_id)
        assert tsi.subject_refusals() == {(tenant_id, "portal"): "brand-new-org"}

    def test_the_predicate_sees_a_licence_the_moment_it_exists(
        self, db_manager, seed_app, seed_plan
    ) -> None:
        """The control for the predicate itself: the SAME tenant, before and after.

        Without this, `tenant_has_licences` returning a constant would satisfy every
        other test in this class — one of them by admitting, the other by refusing.
        """
        tenant_id = _make_tenant(db_manager, "before-and-after-org")
        session = db_manager.get_session()
        try:
            before = tsi.tenant_has_licences(session, tenant_id)
        finally:
            session.close()
        _give_the_tenant_a_licence(db_manager, tenant_id, seed_app, seed_plan)
        session = db_manager.get_session()
        try:
            after = tsi.tenant_has_licences(session, tenant_id)
        finally:
            session.close()

        assert (before, after) == (False, True)


class TestTheNarrowingOverHttp:
    """The same two halves through `POST /licenses/user/{subject_id}`."""

    def test_minting_for_a_trading_tenant_id_is_201(
        self, auth_client_admin, db_manager, seed_app, seed_plan
    ) -> None:
        """⭐ THE REGRESSION TEST FOR THE CI FAILURE THIS NARROWING FIXES.

        ⚠️ The header tenant is a DIFFERENT registered organisation, deliberately:
        that rules out the self-tenant exemption, so a 201 here can ONLY come from
        the licence clause. If someone later deletes that clause and keeps only
        `self_tenant_mint`, this test goes red while an all-self-tenant test would
        stay green and hide it.
        """
        trading_id = _make_tenant(db_manager, "trading-org")
        _give_the_tenant_a_licence(db_manager, trading_id, seed_app, seed_plan)
        billing_tenant = _make_tenant(db_manager, "billing-org")
        assert not _subject_exists(db_manager, trading_id)

        with patch("main._provision_budget", new_callable=AsyncMock):
            response = auth_client_admin.post(
                f"/licenses/user/{trading_id}",
                json={"app_id": str(seed_app.id), "plan_id": str(seed_plan.id)},
                headers={"X-Tenant-Id": billing_tenant},
            )

        assert response.status_code == 201, response.text
        assert tsi.subject_refusals() == {}

    def test_minting_for_a_tenant_id_with_no_licences_is_still_a_409(
        self, auth_client_admin, db_manager, seed_app, seed_plan
    ) -> None:
        """The negative half over HTTP, with a REGISTERED billing tenant in the
        header — so the refusal is the subject-side guard's and not the mint
        guard's `tenant_unknown` 400 wearing a different number."""
        fresh_id = _make_tenant(db_manager, "fresh-org")
        billing_tenant = _make_tenant(db_manager, "billing-org-2")

        with patch("main._provision_budget", new_callable=AsyncMock):
            response = auth_client_admin.post(
                f"/licenses/user/{fresh_id}",
                json={"app_id": str(seed_app.id), "plan_id": str(seed_plan.id)},
                headers={"X-Tenant-Id": billing_tenant},
            )

        assert response.status_code == 409, response.text
        assert "already registered tenant" in response.json()["detail"]
        assert not _subject_exists(db_manager, fresh_id)
