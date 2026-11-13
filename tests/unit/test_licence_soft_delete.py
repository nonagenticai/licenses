"""A deleted licence must stay derivable, or a closed invoice stops being reproducible.

⛔ THE DEFECT. ``DELETE /licenses/user/{subject_id}`` -> ``delete_licenses_for_subject``
issued a bare ``.delete()``: no confirmation, no audit row of any kind, and the tokens
went with it via ``token.license_id`` ON DELETE CASCADE. Meanwhile
``GET /internal/tenants/{tenant_id}/seat-count?as_of=`` — the DENOMINATOR of billing's
per-seat access charge — states in its own parameter description that it "cannot
reconstruct a licence that has since been deleted", because it can only filter rows
that still exist. Both true at once: after any delete, every CLOSED period's seat count
silently re-derives SMALLER than the invoice was written from, with nothing left to
explain the gap.

⚠️ THE GATE HAS SINCE BEEN OPENED, AND THIS PARAGRAPH USED TO SAY THE OPPOSITE.
``LICENSE_SOFT_DELETE`` defaulted **False** and this file pinned that today's
behaviour was byte-for-byte unchanged. The stated reason was real — a retained row
was still returned by ``GET /internal/licenses`` and ``GET /licenses`` because
nothing filtered ``deleted_at`` on the list surfaces, so arming it would have made
deleted licences REAPPEAR to their consumers, billing included. But the live
deployment sets no value for the variable, so the "safe" default WAS production, and
production was destroying billing evidence on every call.

Both halves have now landed together: ``list_licenses`` takes ``include_deleted``
(default False), both list surfaces expose it, and the flag defaults **True**. The
tests for the flip and for the read filter that legitimises it are in
``tests/unit/test_soft_delete_is_the_default.py``; this file continues to pin the
ARMED behaviour in detail, and the disarmed opt-out that is still supported.

Design notes — the ways a test like this normally fails to be a test:

* The historical claim is measured by actually COUNTING AT A PAST INSTANT, not by
  asserting a column is set. ``deleted_at`` being populated proves nothing about
  whether the count is reproducible.
* The token revocation is asserted, because the FK cascade no longer fires: a surviving
  row keeps its tokens, so without an explicit revoke a "deleted" licence would keep
  authorizing calls — an audit improvement that is an access-control regression.
* Re-deletion is exercised, because ``deleted_at`` must be the FIRST deletion. A
  timestamp that walks forward silently rewrites a closed period.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

import main
from models import License, Tenant, Token

SEED_TENANT = "01000000-0000-7000-8000-000000000001"


def _as_of(instant: datetime) -> dict[str, str]:
    """``as_of`` as a QUERY PARAM, never interpolated into the URL string.

    ⛔ WHY THIS EXISTS. ``datetime.isoformat()`` on an aware instant ends in
    ``+00:00``, and ``+`` in a query string is the percent-encoding of a SPACE.
    Interpolated into the path, the server therefore parsed
    ``2026-09-13T12:41:30.403289 00:00`` and answered **422** — whose body has no
    ``seats`` key, so the assertion died with ``KeyError: 'seats'`` and never
    reached the behaviour it exists to measure. Passing the value through
    ``params=`` lets httpx encode it (``%2B``), which is what every real caller's
    HTTP client does.

    ⚠️ The status assertion at each call site is part of the fix: a non-200 must
    fail as "the endpoint refused", not as a missing key, so the next transport
    bug cannot masquerade as a seat-count defect.
    """
    return {"as_of": instant.isoformat()}


@pytest.fixture
def registered_tenant(db_manager):
    session = db_manager.get_session()
    try:
        session.merge(Tenant(id=UUID(SEED_TENANT), name="soft-delete-test", owner_key="platform"))
        session.commit()
    finally:
        session.close()


@pytest.fixture
def armed(monkeypatch):
    """⚠️ NOW A NO-OP IN EFFECT, AND KEPT ANYWAY.

    ``LICENSE_SOFT_DELETE`` defaults to True since the flip, so every test below
    would exercise the same path without this fixture. It stays, set EXPLICITLY,
    because these tests are about the ARMED BEHAVIOUR and must keep testing it if
    the default ever moves again — a test whose subject is chosen by a module-level
    default is a test that silently changes subject. ``TestTheDefaultIsRetention``
    is where the default itself is pinned.
    """
    monkeypatch.setattr(main, "LICENSE_SOFT_DELETE", True, raising=True)


@pytest.fixture
def disarmed(monkeypatch):
    """``LICENSE_SOFT_DELETE=false`` — the historical destroy, now opt-IN."""
    monkeypatch.setattr(main, "LICENSE_SOFT_DELETE", False, raising=True)


def _licence_rows(db_manager, subject_id) -> list[License]:
    session = db_manager.get_session()
    try:
        return session.query(License).filter(License.subject_id == subject_id).all()
    finally:
        session.close()


class TestTheDefaultIsRetention:
    """⚠️ CORRECTED. This class used to be ``TestTheFlagIsOffByDefault`` and pinned
    the OPPOSITE: that the default destroyed the row, unconfirmed and unrecorded.

    That was an accurate pin of a live defect. The live deployment sets no value for
    ``LICENSE_SOFT_DELETE``, so the default WAS production behaviour, and production
    was destroying billing evidence on every call. The default has been flipped and
    the list-surface filtering it was waiting on has landed with it; the outcome
    tests live in ``tests/unit/test_soft_delete_is_the_default.py``.

    ⛔ The hard path is DEMOTED, NOT DELETED, and the tests below say so — an
    operator who sets ``LICENSE_SOFT_DELETE=false`` still gets the historical
    unconfirmed destroy, now with an ERROR-level audit line in front of it.
    """

    def test_the_default_retains(self) -> None:
        assert main.LICENSE_SOFT_DELETE is True

    def test_the_rows_are_still_destroyed_when_explicitly_disarmed(
        self,
        auth_client_admin: TestClient,
        db_manager,
        disarmed,
        registered_tenant,
        seed_license,
        seed_token,
    ) -> None:
        """The opt-out still works exactly as it always did."""
        subject_id = UUID(str(seed_license.subject_id))
        response = auth_client_admin.delete(f"/licenses/user/{subject_id}")
        assert response.status_code == 200, response.text
        assert response.json() == {"deleted": 1, "mode": "hard", "released_dependency_ids": []}
        assert _licence_rows(db_manager, subject_id) == []

    def test_no_confirmation_is_required_when_explicitly_disarmed(
        self, auth_client_admin: TestClient, disarmed, registered_tenant, seed_license
    ) -> None:
        """⛔ THE COMPATIBILITY PIN, and the reason the guard is on the SAME switch.

        Requiring confirmation while still hard-deleting would break a caller that
        has deliberately opted back in, to buy an operator a warning about a loss
        that still happens. So the confirmation and the retention arm and disarm
        together — what does NOT disarm with them is the audit line, because the
        destroy is exactly the case with no row left to be the record.
        """
        subject_id = UUID(str(seed_license.subject_id))
        response = auth_client_admin.delete(f"/licenses/user/{subject_id}")
        assert response.status_code == 200, (
            "an unconfirmed delete was refused with the flag explicitly disarmed, "
            f"which breaks the opt-out; body={response.text}"
        )


class TestTheArmedSoftDelete:
    def test_an_unconfirmed_delete_is_refused(
        self, auth_client_admin: TestClient, armed, registered_tenant, seed_license
    ) -> None:
        subject_id = UUID(str(seed_license.subject_id))
        response = auth_client_admin.delete(f"/licenses/user/{subject_id}")
        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert detail["error"] == "licence_delete_requires_confirmation"
        assert detail["licenses"] == 1
        assert detail["active_licences"] == 1

    def test_a_wrong_count_is_refused_distinguishably(
        self, auth_client_admin: TestClient, armed, registered_tenant, seed_license
    ) -> None:
        """Two causes, two literals — the caller's next action differs.

        ``requires_confirmation`` means "read the radius and repeat";
        ``count_mismatch`` means "the rows changed under you, re-read".
        """
        subject_id = UUID(str(seed_license.subject_id))
        response = auth_client_admin.delete(f"/licenses/user/{subject_id}?confirm_licenses=99")
        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "licence_delete_count_mismatch"

    def test_a_confirmed_delete_retains_the_row_and_records_who_and_why(
        self, auth_client_admin: TestClient, db_manager, armed, registered_tenant, seed_license
    ) -> None:
        subject_id = UUID(str(seed_license.subject_id))
        response = auth_client_admin.delete(
            f"/licenses/user/{subject_id}?confirm_licenses=1&reason=offboarded"
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"deleted": 1, "mode": "soft", "released_dependency_ids": []}

        rows = _licence_rows(db_manager, subject_id)
        assert len(rows) == 1, "the row was destroyed despite the soft-delete path"
        row = rows[0]
        assert row.deleted_at is not None
        assert row.delete_reason == "offboarded"
        assert row.deleted_by, "no actor was recorded; the row IS the audit trail"
        assert row.deleted_from_status == "active"
        # ⛔ status MUST move. It frees uq_license_active_tenant_app_subject (the
        # partial unique index WHERE status = 'active') and stops the licence
        # authorizing — see soft_delete_licenses_for_subject.
        assert row.status == "revoked"
        assert row.revoked_at is not None

    def test_the_tokens_are_revoked_not_orphaned(
        self,
        auth_client_admin: TestClient,
        db_manager,
        armed,
        registered_tenant,
        seed_license,
        seed_token,
    ) -> None:
        """⛔ THE CASCADE NO LONGER FIRES, so this has to be explicit.

        The row survives, so ``token.license_id`` ON DELETE CASCADE never runs. Without
        an explicit revoke a "deleted" licence keeps authorizing calls — the audit
        improvement would be an access-control regression.
        """
        subject_id = UUID(str(seed_license.subject_id))
        auth_client_admin.delete(f"/licenses/user/{subject_id}?confirm_licenses=1")

        session = db_manager.get_session()
        try:
            tokens = session.query(Token).filter(Token.license_id == seed_license.id).all()
        finally:
            session.close()
        assert tokens, "the tokens were destroyed; the cascade should not have fired"
        assert all(t.revoked for t in tokens), "a soft-deleted licence kept a live token"

    def test_the_licence_no_longer_introspects_as_active(
        self,
        auth_client_admin: TestClient,
        armed,
        registered_tenant,
        seed_license,
        full_seed_data,
    ) -> None:
        """Retention must not mean the licence still works."""
        subject_id = UUID(str(seed_license.subject_id))
        auth_client_admin.delete(f"/licenses/user/{subject_id}?confirm_licenses=1")
        response = auth_client_admin.post(
            "/license/introspect", json={"token": full_seed_data["raw_token"]}
        )
        assert response.json()["active"] is False

    def test_re_deleting_does_not_move_the_timestamp(
        self, auth_client_admin: TestClient, db_manager, armed, registered_tenant, seed_license
    ) -> None:
        """⛔ ``deleted_at`` IS THE FIRST DELETION, like ``archived_at``.

        A timestamp that walks forward on every repeated call drags every historical
        count along with it, silently rewriting a closed period.
        """
        subject_id = UUID(str(seed_license.subject_id))
        auth_client_admin.delete(f"/licenses/user/{subject_id}?confirm_licenses=1")
        first = _licence_rows(db_manager, subject_id)[0].deleted_at

        second = auth_client_admin.delete(f"/licenses/user/{subject_id}?confirm_licenses=1")
        assert second.status_code == 200
        assert second.json()["deleted"] == 0, "an already-deleted licence was deleted again"
        assert _licence_rows(db_manager, subject_id)[0].deleted_at == first


class TestTheHistoricalSeatCountSurvives:
    """⛔ THE WHOLE POINT. Everything above is machinery; this is the outcome."""

    def test_a_past_instant_still_counts_the_deleted_licence(
        self, auth_client_admin: TestClient, armed, registered_tenant, seed_license
    ) -> None:
        subject_id = UUID(str(seed_license.subject_id))
        before = datetime.now(UTC) - timedelta(minutes=5)

        at_the_time = auth_client_admin.get(
            f"/internal/tenants/{SEED_TENANT}/seat-count", params=_as_of(before)
        )
        assert at_the_time.status_code == 200, at_the_time.text
        assert at_the_time.json()["seats"] == 1

        auth_client_admin.delete(f"/licenses/user/{subject_id}?confirm_licenses=1")

        after_deletion = auth_client_admin.get(
            f"/internal/tenants/{SEED_TENANT}/seat-count", params=_as_of(before)
        )
        assert after_deletion.status_code == 200, after_deletion.text
        assert after_deletion.json()["seats"] == 1, (
            "the seat count for a CLOSED period changed because a licence was deleted "
            "afterwards — the invoice for that period is no longer reproducible, which "
            "is the entire defect this path exists to fix"
        )

    def test_the_present_count_does_drop(
        self, auth_client_admin: TestClient, armed, registered_tenant, seed_license
    ) -> None:
        """The control. Without it, a predicate that counted EVERYTHING would pass above.

        Retention must not mean the tenant keeps being billed: a deleted licence is not
        a seat NOW, only THEN.
        """
        subject_id = UUID(str(seed_license.subject_id))
        auth_client_admin.delete(f"/licenses/user/{subject_id}?confirm_licenses=1")
        now_count = auth_client_admin.get(f"/internal/tenants/{SEED_TENANT}/seat-count")
        assert now_count.status_code == 200, now_count.text
        assert now_count.json()["seats"] == 0

    def test_a_licence_revoked_before_deletion_is_not_resurrected(
        self,
        auth_client_admin: TestClient,
        db_manager,
        armed,
        registered_tenant,
        seed_revoked_license,
    ) -> None:
        """⛔ WHY ``deleted_from_status`` IS NOT REDUNDANT.

        The soft delete overwrites ``status`` with ``revoked``, so the CURRENT status of
        a deleted row says nothing about what it was. Reading ``deleted_at`` alone would
        count this licence — revoked long before anyone deleted it — as a seat at every
        earlier instant, over-billing a licence that was never active then.
        """
        subject_id = UUID(str(seed_revoked_license.subject_id))
        before = datetime.now(UTC) - timedelta(minutes=5)
        auth_client_admin.delete(f"/licenses/user/{subject_id}?confirm_licenses=1")

        rows = _licence_rows(db_manager, subject_id)
        assert rows[0].deleted_from_status == "revoked"

        counted = auth_client_admin.get(
            f"/internal/tenants/{SEED_TENANT}/seat-count", params=_as_of(before)
        )
        assert counted.status_code == 200, counted.text
        assert counted.json()["seats"] == 0
