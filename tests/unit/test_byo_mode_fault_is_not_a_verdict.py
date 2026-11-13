"""An infra fault must not leave this route answering 200 with a commercial verdict.

⛔ THE DEFECT. ``GET /tenants/{name_or_id}/byo-mode?app=`` resolved the posture
through ``_lookup_byo_mode``, which fail-CLOSES to ``byo_required`` when the tenant
row cannot be read — and returned that as **200**. ``byo_required`` means
COMMERCIAL downstream: billing treats a 200 from this route as a durable answer and
freezes it into an immutable invoice. So one database blip permanently billed an
internal tenant commercially, and there was no 5xx anywhere for the caller to retry
on. The fabricated string and the real answer were byte-identical.

⚠️ THE TENANT-WIDE BRANCH WAS NEVER LIKE THIS, and that asymmetry is why the fix is
a correction rather than a new policy: ``get_tenant`` does not swallow a DB fault, so
a fault on the no-``app`` path has always escaped as a 5xx. Only the ``?app=`` path
fabricated a 200.

⛔ AND THE FAIL-CLOSED DIRECTION IS STILL AVAILABLE, BECAUSE ONE CALLER GENUINELY
NEEDS IT. ``worker_management``'s trusted-network path is a live access gate: it reads
only ``byo_mode``, caches a 200 for 300s, and FAILS OPEN on every non-200. For it a
503 is worse than a fabricated denial. It therefore passes
``on_unresolved=fail_closed`` and gets the old behaviour, now with
``resolution: "fault"`` in the body so even that answer says what it is. Both arms
are exercised below; asserting only the 503 would let the escape hatch rot.

SECOND DEFECT, SAME ROUTE (the 404). ``get_tenant`` silently degrades a non-UUID
selector to a NAME lookup and returns ``None`` for everything unresolved, so a
selector that could never name a tenant 404'd identically to a genuinely absent one
— and billing maps that 404 to ``byo_required`` = commercial too. A typo must not be
able to produce a commercial verdict, so a malformed selector is now a 400 and the
real absence carries a stable literal.

Design note — the ways a test like this normally fails to be a test:

* The fault is injected at the SEAM THE ROUTE ACTUALLY CROSSES
  (``_lookup_tenant_posture``), not by patching the route's own helper, so it cannot
  pass against an implementation that stopped consulting the database.
* ``_lookup_tenant_posture`` is made to raise on BOTH attempts, because it has a
  bounded retry — a single-shot failure resolves on the retry and would prove
  nothing about the fault path.
* Every arm asserts the STATUS **and** the machine-readable literal. A status alone
  is what three different conditions already looked like on this path.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from fastapi.testclient import TestClient

import models
from models import Tenant

SEED_TENANT = "01000000-0000-7000-8000-000000000001"


@pytest.fixture
def byo_tenant(db_manager) -> None:
    """The seed tenant, explicitly ``local_allowed``.

    ``local_allowed`` — NOT the default — on purpose: it is the OPPOSITE of the
    fail-closed fabrication, so a test that reads ``byo_required`` has necessarily
    read the fabricated value and not merely agreed with the stored one.
    """
    session = db_manager.get_session()
    try:
        session.merge(
            Tenant(
                id=UUID(SEED_TENANT),
                name="byo-fault-test",
                byo_mode="local_allowed",
                owner_key="platform",
            )
        )
        session.commit()
    finally:
        session.close()


def _always_faults(*_args, **_kwargs):
    """Stand in for a database that cannot answer, on every attempt.

    ⚠️ Patched at ``_tenant_posture_from_session``, which is the function
    ``_lookup_tenant_posture`` calls TWICE — once directly and once on its bounded
    retry with a fresh session. Patching only one call would leave the retry to
    succeed and the fault path would never be reached.
    """
    raise RuntimeError("simulated database fault")


class TestAFaultIsNotAnAnswer:
    def test_a_db_fault_answers_503_not_200(
        self, client: TestClient, db_manager, byo_tenant, seed_app, monkeypatch
    ) -> None:
        """⛔ THE CORE ASSERTION. Without the fix this is 200 + byo_required."""
        monkeypatch.setattr(
            type(db_manager), "_tenant_posture_from_session", _always_faults, raising=True
        )
        # ⚠️ `on_unresolved=error` is OPT-IN. The default is `fail_closed`
        # (today's behaviour) so that worker_management — the only `?app=`
        # caller, and one that FAILS OPEN on every non-200 — is not moved
        # to allowing requests during a licences DB fault. This test is
        # about the CAPABILITY, so it asks for it.
        response = client.get(
            f"/tenants/{SEED_TENANT}/byo-mode?app={seed_app.key}&on_unresolved=error"
        )
        assert response.status_code == 503, (
            "a posture that could not be DETERMINED was published as a durable answer; "
            f"body={response.text}"
        )
        assert response.json()["detail"]["error"] == "byo_mode_unresolved"

    def test_the_503_body_does_not_carry_a_posture(
        self, client: TestClient, db_manager, byo_tenant, seed_app, monkeypatch
    ) -> None:
        """A refusal that still ships ``byo_required`` is the defect with a new status.

        The whole point is that the caller gets NO verdict. If the fabricated string
        travels in the refusal body, a lenient client parses it out and the invoice is
        wrong again — via a 503 this time.
        """
        monkeypatch.setattr(
            type(db_manager), "_tenant_posture_from_session", _always_faults, raising=True
        )
        # ⚠️ `on_unresolved=error` is OPT-IN. The default is `fail_closed`
        # (today's behaviour) so that worker_management — the only `?app=`
        # caller, and one that FAILS OPEN on every non-200 — is not moved
        # to allowing requests during a licences DB fault. This test is
        # about the CAPABILITY, so it asks for it.
        response = client.get(
            f"/tenants/{SEED_TENANT}/byo-mode?app={seed_app.key}&on_unresolved=error"
        )
        assert "byo_required" not in response.text

    def test_the_access_gate_can_still_opt_into_fail_closed(
        self, client: TestClient, db_manager, byo_tenant, seed_app, monkeypatch
    ) -> None:
        """``worker_management``'s arm: 200 + the fail-closed value, as before."""
        monkeypatch.setattr(
            type(db_manager), "_tenant_posture_from_session", _always_faults, raising=True
        )
        response = client.get(
            f"/tenants/{SEED_TENANT}/byo-mode?app={seed_app.key}&on_unresolved=fail_closed"
        )
        assert response.status_code == 200
        body = response.json()
        assert body["byo_mode"] == "byo_required", "the fail-closed direction was lost"
        assert body["resolution"] == "fault", (
            "the opt-in arm returned the fabrication WITHOUT saying it was one, which is "
            "the original defect with an extra query parameter"
        )

    def test_a_healthy_read_says_it_came_from_a_row(
        self, client: TestClient, byo_tenant, seed_app
    ) -> None:
        """The control. Without it, a route that ALWAYS said ``fault`` would pass above.

        Also pins that the healthy answer is the tenant's real posture — if this read
        ``byo_required`` the fault tests would be agreeing with the stored value rather
        than with a fabrication.
        """
        response = client.get(f"/tenants/{SEED_TENANT}/byo-mode?app={seed_app.key}")
        assert response.status_code == 200
        body = response.json()
        assert body["resolution"] == "row"
        assert body["byo_mode"] == "local_allowed"

    def test_an_invalid_on_unresolved_value_is_refused(
        self, client: TestClient, byo_tenant, seed_app
    ) -> None:
        """A typo must not silently select the dangerous arm.

        ``on_unresolved=fail_closd`` has to be a 422, not a quiet fall-through to
        either behaviour: an escape hatch whose name can be misspelled into the
        default is not a hatch.
        """
        response = client.get(
            f"/tenants/{SEED_TENANT}/byo-mode?app={seed_app.key}&on_unresolved=fail_closd"
        )
        assert response.status_code == 422


class TestTheResolutionSplitIsAtTheModelLayer:
    """The route's 503 is only correct if the posture layer can tell the two apart."""

    def test_a_missing_row_and_a_fault_carry_different_resolutions(
        self, db_manager, monkeypatch
    ) -> None:
        missing = db_manager._lookup_tenant_posture("019e0f00-0000-7000-8000-0000000000ff")
        assert missing.resolution == models.RESOLUTION_MISSING_ROW

        monkeypatch.setattr(
            type(db_manager), "_tenant_posture_from_session", _always_faults, raising=True
        )
        faulted = db_manager._lookup_tenant_posture("019e0f00-0000-7000-8000-0000000000ff")
        assert faulted.resolution == models.RESOLUTION_FAULT

    def test_both_still_fail_closed_on_byo_mode(self, db_manager, monkeypatch) -> None:
        """⛔ THE COMPATIBILITY PIN. The VALUES must not have moved.

        Every access gate in the fleet reads ``byo_mode`` and nothing else. If this
        split had changed either fabricated value, it would have changed enforcement
        everywhere while looking like an observability improvement.
        """
        assert models._MISSING_ROW_TENANT_POSTURE.byo_mode == "byo_required"
        assert models._FAULTED_TENANT_POSTURE.byo_mode == "byo_required"
        # archived fail-OPENS in both, for the opposite reason — see the constants.
        assert models._MISSING_ROW_TENANT_POSTURE.archived is False
        assert models._FAULTED_TENANT_POSTURE.archived is False

    def test_the_narrow_accessor_still_returns_a_bare_string(self, db_manager) -> None:
        """``_lookup_byo_mode``'s signature is unchanged, so no caller had to move."""
        assert db_manager._lookup_byo_mode("019e0f00-0000-7000-8000-0000000000ff") == "byo_required"


class TestA404IsSpecificallyTheTenantsAbsence:
    # Non-UUID selectors are no longer auth-exempt (the tokenless path is UUID-only,
    # see auth/middleware.py), so these two drive the handler's 400/404 contract
    # through the AUTHENTICATED path — the semantics under test belong to the
    # handler, not to the exemption.
    def test_a_malformed_selector_is_400_not_404(self, auth_client_admin: TestClient) -> None:
        """⛔ Without the fix this is a 404, which billing reads as commercial.

        ``not a tenant!`` is neither parseable as a UUID nor legal under
        ``ck_tenants_name_format`` (``^[a-z0-9][a-z0-9-]*$``), so no row could ever
        match it. That is a client fault, and it must not be expressible as a
        statement about the registry.
        """
        response = auth_client_admin.get("/tenants/not%20a%20tenant!/byo-mode")
        assert response.status_code == 400, f"body={response.text}"
        assert response.json()["detail"]["error"] == "malformed_tenant_selector"

    def test_an_absent_tenant_is_404_with_a_stable_literal(self, client: TestClient) -> None:
        """A well-formed selector with no row. Still 404 — but now branchable."""
        response = client.get("/tenants/019e0f00-0000-7000-8000-0000000000fe/byo-mode")
        assert response.status_code == 404
        assert response.json()["detail"]["error"] == "tenant_not_found_in_registry"

    def test_a_legal_name_shape_that_is_absent_is_404_not_400(
        self, auth_client_admin: TestClient
    ) -> None:
        """⛔ THE NARROWNESS CONTROL, and it is the one that could regress silently.

        ``1769980520185202330`` is one of the two selectors MEASURED against the live
        pods (8 of 79 byo-mode lookups answered 404 on 2026-08-19). It is a snowflake
        id, not a UUID — but it IS a legal tenant-name shape, so it is genuinely
        ABSENT rather than impossible.

        Widening the malformed test to "does not look like a UUID" would turn this
        into a 400, and with it every name-based lookup ``get_tenant`` exists to
        serve. A 400 here is the regression this test names.
        """
        response = auth_client_admin.get("/tenants/1769980520185202330/byo-mode")
        assert response.status_code == 404
        assert response.json()["detail"]["error"] == "tenant_not_found_in_registry"

    def test_the_resolver_reports_all_three_outcomes(self, db_manager, byo_tenant) -> None:
        """The model-layer split the three statuses are derived from."""
        assert db_manager.resolve_tenant_selector(SEED_TENANT).outcome == models.TENANT_LOOKUP_FOUND
        assert (
            db_manager.resolve_tenant_selector("019e0f00-0000-7000-8000-0000000000fe").outcome
            == models.TENANT_LOOKUP_NOT_FOUND
        )
        assert (
            db_manager.resolve_tenant_selector("not a tenant!").outcome
            == models.TENANT_LOOKUP_MALFORMED
        )

    def test_a_malformed_selector_is_decided_without_a_query(self, db_manager, monkeypatch) -> None:
        """⛔ It must be a statement about the STRING, never about the database.

        Proven by making the database unusable: if the malformed verdict survives a
        ``get_session`` that raises, it cannot have been derived from a query — so it
        can never be confused with a fault, which is the distinction this whole file
        is about.
        """

        def _no_session(*_a, **_k):
            raise RuntimeError("no database available")

        monkeypatch.setattr(type(db_manager), "get_session", _no_session, raising=True)
        assert (
            db_manager.resolve_tenant_selector("not a tenant!").outcome
            == models.TENANT_LOOKUP_MALFORMED
        )

    def test_resolution_by_name_still_works(self, db_manager, byo_tenant) -> None:
        """The name path is the reason a bare 'non-UUID = 400' rule is wrong."""
        found = db_manager.resolve_tenant_selector("byo-fault-test")
        assert found.outcome == models.TENANT_LOOKUP_FOUND
        assert found.tenant is not None and str(found.tenant.id) == SEED_TENANT


class TestTheDefaultPreservesTodaysBehaviour:
    """⛔ THE TEST THAT GUARDS ANOTHER SERVICE.

    The 503 above is a real improvement for a caller that PERSISTS the answer —
    but it is opt-in, and this pins why. `worker_management`
    (`src/api/auth_middleware.py:381`) is the only caller that passes `?app=`,
    and it FAILS OPEN on every non-200, caching only 200s. So defaulting this
    route to 503 would move that auth middleware from "caches a fail-closed
    guess for 300s" to "allows the request" during a licences database fault.

    And it would buy nothing: billing's `services/byo_mode.py` calls this route
    with NO `app` parameter, so it takes the tenant-wide branch, which has always
    answered 5xx on a fault. Billing was never exposed to the defect this file is
    named for.

    ⇒ Flip the default to `error` only after worker_management passes
    `on_unresolved=fail_closed` explicitly. Until then this test is what stops it.
    """

    def test_the_default_still_answers_200_with_a_posture(
        self, client: TestClient, db_manager, byo_tenant, seed_app, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            type(db_manager), "_tenant_posture_from_session", _always_faults, raising=True
        )
        response = client.get(f"/tenants/{SEED_TENANT}/byo-mode?app={seed_app.key}")
        assert response.status_code == 200, (
            "the DEFAULT must remain today's behaviour — see this class's docstring; "
            "a 503 here fails worker_management OPEN during a licences DB fault"
        )
        assert response.json()["byo_mode"] == "byo_required"
