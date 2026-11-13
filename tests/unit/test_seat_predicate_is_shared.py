"""The two endpoints billing reads for ONE invoice must agree on what a seat is.

⛔ THE DEFECT. ``GET /internal/tenants/{tenant_id}/seat-count`` applied a three-part
rule — stored-active AND started AND unexpired, at ``as_of`` — while
``GET /internal/licenses?status=active`` filtered the STORED ``status`` column and
nothing else. **billing reads both, for the same invoice.** So one licence whose row
still says ``active`` while its ``expires_at`` has passed was a seat on one endpoint
and not on the other, and nothing told the consumer which number it was holding.

``status`` is not validity and cannot become it: NOTHING in the estate transitions the
column when ``expires_at`` passes (see ``expire_stale_licenses``), so the stored value
drifts permanently and only ever in the over-counting direction.

⚠️ THE DISAGREEMENT IS INVISIBLE ON TODAY'S DATA, which is exactly why it needs a
test rather than a query. Measured on the primary 2026-09-10 there were ZERO
stored-active-but-past-expiry rows, so the two endpoints agree on every row that
currently exists — and would keep agreeing right up to the day one expires, which is
the day the invoice is wrong. Every test here therefore CONSTRUCTS that row.

Design notes — the ways a test like this normally fails to be a test:

* It builds the divergent row and asserts the two endpoints return the SAME answer
  about it, rather than asserting each against a hardcoded number. A pair of
  hardcoded expectations passes just as happily when both sides are wrong.
* It pins that the predicate is SHARED at the source (one call site each), because
  two copies of an expression that agree today are the state this defect started in.
* It has a positive control: the admin list routes are asserted to STILL show the
  drifted row, so "they agree" cannot be achieved by hiding the row everywhere.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

import models
from models import Tenant

SEED_TENANT = "01000000-0000-7000-8000-000000000001"


@pytest.fixture
def expired_but_active_licence(db_manager, seed_subject, seed_app, seed_plan):
    """A licence whose STORED status says ``active`` and whose window has closed.

    This is the row the whole defect turns on, and it is legal: nothing transitions
    the column, so a perfectly ordinary licence becomes this row simply by the clock
    moving. Registered under the seed tenant so both endpoints can see it.
    """
    session = db_manager.get_session()
    try:
        session.merge(
            Tenant(id=UUID(SEED_TENANT), name="seat-predicate-test", owner_key="platform")
        )
        session.commit()
    finally:
        session.close()
    return db_manager.create_license(
        subject_id=UUID(str(seed_subject.id)),
        app_id=UUID(str(seed_app.id)),
        plan_id=UUID(str(seed_plan.id)),
        tenant_id=SEED_TENANT,
        status="active",
        starts_at=datetime.now(UTC) - timedelta(days=90),
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )


class TestTheTwoEndpointsAgree:
    def test_an_expired_active_row_is_not_a_seat_on_either_endpoint(
        self, auth_client_admin: TestClient, expired_but_active_licence
    ) -> None:
        """⛔ THE CORE ASSERTION. Without the fix the list says 1 and the count says 0."""
        listed = auth_client_admin.get(f"/internal/licenses?tenant_id={SEED_TENANT}&status=active")
        assert listed.status_code == 200, listed.text
        counted = auth_client_admin.get(f"/internal/tenants/{SEED_TENANT}/seat-count")
        assert counted.status_code == 200, counted.text

        assert len(listed.json()) == counted.json()["seats"], (
            "the two endpoints billing reads for one invoice disagree about how many "
            f"active licences this tenant has: list={len(listed.json())} "
            f"seat-count={counted.json()['seats']}"
        )
        assert counted.json()["seats"] == 0, "an expired licence was counted as a seat"

    def test_a_genuinely_active_row_is_a_seat_on_both(
        self, auth_client_admin: TestClient, seed_license
    ) -> None:
        """The control. Without it, a predicate that excluded EVERYTHING would pass above."""
        listed = auth_client_admin.get(f"/internal/licenses?tenant_id={SEED_TENANT}&status=active")
        counted = auth_client_admin.get(f"/internal/tenants/{SEED_TENANT}/seat-count")
        assert len(listed.json()) == counted.json()["seats"] == 1

    def test_the_stored_column_is_still_visible_on_the_row(
        self, auth_client_admin: TestClient, expired_but_active_licence
    ) -> None:
        """Agreement must not be bought by CONCEALING the drift.

        The row is excluded from the active FILTER, but an unfiltered read must still
        show ``status='active'`` beside ``effective_status='expired'`` — that pair IS
        the drift, and it is the only way an operator can see it.
        """
        response = auth_client_admin.get(f"/internal/licenses?tenant_id={SEED_TENANT}")
        rows = response.json()
        assert len(rows) == 1
        assert rows[0]["status"] == "active"
        assert rows[0]["effective_status"] == "expired"

    def test_the_operator_can_still_ask_for_the_stored_column(
        self, auth_client_admin: TestClient, expired_but_active_licence
    ) -> None:
        """``effective=false`` keeps the old semantics for the audit case."""
        response = auth_client_admin.get(
            f"/internal/licenses?tenant_id={SEED_TENANT}&status=active&effective=false"
        )
        assert len(response.json()) == 1, (
            "the stored-column filter was removed rather than made opt-out; an operator "
            "looking for exactly this drift now has no way to query it"
        )

    def test_the_admin_list_routes_are_unchanged(
        self, auth_client_admin: TestClient, expired_but_active_licence
    ) -> None:
        """⛔ DELIBERATELY NOT FIXED, and this pins that it stays that way.

        ``GET /licenses`` is an operator surface, not a billing one. An operator
        debugging a licence must be able to see a row whose column disagrees with the
        clock; hiding it there is how the drift became invisible in the first place.
        """
        response = auth_client_admin.get("/licenses?status=active")
        assert response.status_code == 200
        assert len(response.json()) == 1


class TestThePredicateIsNotDuplicated:
    """A shared definition that has been copied is not shared."""

    def test_both_call_sites_use_the_helper(self) -> None:
        source = inspect.getsource(models)
        calls = source.count("effectively_active_clauses(")
        # 1 definition + 1 in count_tenant_seats + 1 in list_licenses.
        assert calls >= 3, f"expected the helper to be called from both sites; found {calls}"

    def test_the_expression_appears_exactly_once(self) -> None:
        """⛔ THE ANTI-DRIFT PIN, and the reason it is keyed on the EXPIRY clause.

        The three-part rule is only one predicate while its distinctive clause exists
        in one place. ``License.expires_at > as_of`` is that clause: it is what
        ``/internal/licenses`` was missing, and a second copy of it is precisely how
        the two endpoints drifted apart. ``get_active_license`` deliberately writes a
        DIFFERENT expression (``License.expires_at > now``, plus no NULL arm on
        ``starts_at``) because it is an authorization path, not a billing one — so
        keying on ``as_of`` also keeps this test from dragging that surface in.
        """
        source = inspect.getsource(models)
        assert source.count("License.expires_at > as_of") == 1, (
            "the seat-expiry clause exists in more than one place again — two copies "
            "that agree today are exactly the state this defect started in"
        )

    def test_the_helper_reduces_to_the_stored_status_without_soft_deletes(
        self, db_manager, seed_license
    ) -> None:
        """The soft-delete arm must be INERT while nothing is soft-deleted.

        ``effectively_active_clauses`` reads ``deleted_at``/``deleted_from_status``
        (migration 036) so that arming ``LICENSE_SOFT_DELETE`` is not also a query
        change. While the flag is off those columns are NULL on every row, and this
        asserts the extra arm therefore changes no answer — otherwise the predicate
        would have silently altered billing on the day the columns landed.
        """
        as_of = datetime.now(UTC)
        session = db_manager.get_session()
        try:
            with_arm = (
                session.query(models.License)
                .filter(*models.effectively_active_clauses(as_of))
                .count()
            )
            without_arm = (
                session.query(models.License)
                .filter(
                    models.License.status == "active",
                    models.or_(
                        models.License.starts_at.is_(None), models.License.starts_at <= as_of
                    ),
                    models.or_(
                        models.License.expires_at.is_(None), models.License.expires_at > as_of
                    ),
                )
                .count()
            )
        finally:
            session.close()
        assert with_arm == without_arm == 1
