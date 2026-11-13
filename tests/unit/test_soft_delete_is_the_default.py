"""Retention is the DEFAULT, and the destroying path can no longer be silent.

⛔ THE DEFECT, AS MEASURED. ``LICENSE_SOFT_DELETE`` defaulted **False** and the live
deployment does not set the variable — so production ran the destroying arm of
``DELETE /licenses/user/{subject_id}``: a bare ``.delete()``, no confirmation, no
record of any kind, tokens cascading away with the rows. The loss is not recoverable
by anything this estate has. ``GET /internal/tenants/{id}/seat-count?as_of=`` is the
DENOMINATOR of billing's per-seat access charge and can only filter rows that STILL
EXIST, so one hard delete makes every CLOSED period's count silently re-derive
SMALLER than the invoice was written from — the endpoint's own docstring says so,
and said so while the destructive path was the default.

⛔ WHY THE FLIP NEEDED A SECOND HALF, AND WHY THAT HALF IS TESTED HERE TOO. The old
default was not timidity; it was guarding a real defect, stated in the flag's own
comment: *a retained row was still returned by ``GET /internal/licenses`` and ``GET
/licenses``, because nothing filtered ``deleted_at`` on the list surfaces*. Arming
retention alone would therefore have made deleted licences REAPPEAR to their
consumers, billing included — trading an under-count for an over-count. So
``list_licenses`` grew ``include_deleted`` (default False) and the two list surfaces
expose it. ``TestTheListSurfacesDoNotResurrectIt`` is the half that makes the flip
legitimate; without it this file would be pinning a different bug.

Design notes — the ways a test like this normally fails to be a test:

* THE FLAG'S VALUE IS ASSERTED ONCE AND IS NOT THE POINT. Everything else measures
  the OUTCOME through HTTP — the row survives, the count at a past instant holds,
  the list does not show it. A test suite that only asserts ``is True`` passes
  against a flag that is read nowhere.
* THE DISARMED PATH IS EXERCISED, NOT ASSUMED. ``LICENSE_SOFT_DELETE=false`` is a
  supported configuration and an operator who sets it must not additionally be
  broken by a newly required parameter. ``TestTheDisarmedPathIsStillCompatible`` is
  the NEGATIVE CONTROL: it passes identically before and after this change, and it
  is what fails if the flip is implemented by deleting the hard path instead of
  demoting it.
* THE AUDIT LINE IS READ, NOT GREPPED FOR A SUBSTRING OF ITS OWN NAME. It has to
  carry WHO, WHAT and the COUNTS — and the counts are only obtainable BEFORE the
  delete, so a line that reports them proves the radius was read in the right order.
* THE ``effective=true`` ARM HAS ITS OWN CONTROL. A flat ``deleted_at IS NULL``
  stacked onto ``effectively_active_clauses`` would silently shrink a closed
  period's answer — the very defect the soft delete exists to prevent, reintroduced
  through the filter added to hide deleted rows. ``test_the_historical_arm_survives``
  fails if the two are combined.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

import main
from models import License, Tenant

SEED_TENANT = "01000000-0000-7000-8000-000000000001"


def _as_of(instant: datetime) -> dict[str, str]:
    """``as_of`` as a QUERY PARAM, never interpolated into the URL string.

    ⛔ ``datetime.isoformat()`` on an aware instant ends in ``+00:00``, and ``+`` in a
    query string is the percent-encoding of a SPACE. Interpolated into the path the
    server parses ``…T12:41:30 00:00`` and answers **422**, whose body has no
    ``seats``/list payload — so the assertion dies with a ``KeyError`` and never
    reaches the behaviour it exists to measure. ``params=`` lets httpx encode it
    (``%2B``), which is what every real caller's HTTP client does. Each call site
    asserts ``status_code == 200`` FIRST, so a transport fault can never masquerade
    as a seat-count defect.

    ⛔ AND THE TRAP THAT COMES WITH IT: ``params=`` REPLACES the URL's query string,
    it does not merge into it. ``httpx._urls.URL.__init__`` turns the keyword into
    ``kwargs["query"] = str(QueryParams(params))`` and hands it to ``copy_with``, so
    ``get("/x?a=1", params={"b": "2"})`` requests ``/x?b=2`` and ``a`` is gone —
    SILENTLY, with a 200 and a plausible-looking body. Measured on the pinned
    httpx 0.28.1. So every filter a call site needs must go INSIDE this dict; never
    half in the path and half in ``params=``. ``test_the_historical_arm_survives``
    was written that way and consequently asserted against an unfiltered listing.
    """
    return {"as_of": instant.isoformat()}


@pytest.fixture
def registered_tenant(db_manager):
    session = db_manager.get_session()
    try:
        session.merge(
            Tenant(id=UUID(SEED_TENANT), name="soft-delete-default-test", owner_key="platform")
        )
        session.commit()
    finally:
        session.close()


@pytest.fixture
def disarmed(monkeypatch):
    """``LICENSE_SOFT_DELETE=false`` — the supported opt-out, not the default."""
    monkeypatch.setattr(main, "LICENSE_SOFT_DELETE", False, raising=True)


def _rows(db_manager, subject_id) -> list[License]:
    session = db_manager.get_session()
    try:
        return session.query(License).filter(License.subject_id == subject_id).all()
    finally:
        session.close()


class TestRetentionIsTheDefault:
    def test_the_flag_defaults_to_retaining(self) -> None:
        """The one value assertion. Everything below measures the consequence."""
        assert main.LICENSE_SOFT_DELETE is True, (
            "LICENSE_SOFT_DELETE defaults to destroying the row again. The live "
            "deployment sets no value, so this default IS production behaviour."
        )

    def test_an_unconfirmed_delete_no_longer_destroys_anything(
        self, auth_client_admin: TestClient, db_manager, registered_tenant, seed_license
    ) -> None:
        """⛔ THE OUTCOME TEST. No fixture arms anything — this is the shipped default.

        Before this change the same call returned 200 and the row was gone.
        """
        subject_id = UUID(str(seed_license.subject_id))
        response = auth_client_admin.delete(f"/licenses/user/{subject_id}")

        assert response.status_code == 409, (
            "an unconfirmed delete was accepted by default, which means the "
            f"destructive path is live again; body={response.text}"
        )
        assert response.json()["detail"]["error"] == "licence_delete_requires_confirmation"
        assert _rows(db_manager, subject_id), "the licence row was destroyed by default"

    def test_a_confirmed_delete_retains_and_stamps_by_default(
        self, auth_client_admin: TestClient, db_manager, registered_tenant, seed_license
    ) -> None:
        subject_id = UUID(str(seed_license.subject_id))
        response = auth_client_admin.delete(
            f"/licenses/user/{subject_id}?confirm_licenses=1&reason=offboarded"
        )
        assert response.status_code == 200, response.text
        # ⚠️ The response SHAPE is unchanged — same two keys, same names. Only the
        # value of `mode` differs, and it is published precisely so a caller can
        # tell which happened without knowing this service's flag state.
        assert response.json() == {"deleted": 1, "mode": "soft", "released_dependency_ids": []}

        rows = _rows(db_manager, subject_id)
        assert len(rows) == 1, "the row was destroyed despite the default being retention"
        assert rows[0].deleted_at is not None
        assert rows[0].deleted_by, "no actor recorded; the retained row IS the audit trail"
        assert rows[0].delete_reason == "offboarded"

    def test_a_closed_period_stays_reproducible_by_default(
        self, auth_client_admin: TestClient, registered_tenant, seed_license
    ) -> None:
        """⛔ THE WHOLE POINT. Everything else is machinery; this is the money."""
        subject_id = UUID(str(seed_license.subject_id))
        before = datetime.now(UTC) - timedelta(minutes=5)

        at_the_time = auth_client_admin.get(
            f"/internal/tenants/{SEED_TENANT}/seat-count", params=_as_of(before)
        )
        assert at_the_time.status_code == 200, at_the_time.text
        assert at_the_time.json()["seats"] == 1

        auth_client_admin.delete(f"/licenses/user/{subject_id}?confirm_licenses=1")

        after = auth_client_admin.get(
            f"/internal/tenants/{SEED_TENANT}/seat-count", params=_as_of(before)
        )
        assert after.status_code == 200, after.text
        assert after.json()["seats"] == 1, (
            "a CLOSED period's seat count changed because a licence was deleted "
            "afterwards — the invoice for that period is no longer reproducible"
        )

    def test_the_present_count_still_drops(
        self, auth_client_admin: TestClient, registered_tenant, seed_license
    ) -> None:
        """The control for the test above. A predicate that counted EVERYTHING would
        pass there and fail here: retention must not mean the tenant keeps paying."""
        subject_id = UUID(str(seed_license.subject_id))
        auth_client_admin.delete(f"/licenses/user/{subject_id}?confirm_licenses=1")
        now = auth_client_admin.get(f"/internal/tenants/{SEED_TENANT}/seat-count")
        assert now.status_code == 200, now.text
        assert now.json()["seats"] == 0


class TestTheListSurfacesDoNotResurrectIt:
    """⛔ THE HALF THAT MAKES THE FLIP LEGITIMATE.

    The old default existed to prevent exactly this: a retained row appearing on a
    list surface that used to show nothing, turning a silent under-count into a
    silent over-count.
    """

    def test_a_retained_licence_is_absent_from_the_admin_list(
        self, auth_client_admin: TestClient, registered_tenant, seed_license
    ) -> None:
        subject_id = UUID(str(seed_license.subject_id))
        licence_id = str(seed_license.id)

        listed = auth_client_admin.get("/licenses/admin/all")
        assert listed.status_code == 200, listed.text
        assert licence_id in {row["id"] for row in listed.json()}

        auth_client_admin.delete(f"/licenses/user/{subject_id}?confirm_licenses=1")

        after = auth_client_admin.get("/licenses/admin/all")
        assert after.status_code == 200, after.text
        assert licence_id not in {row["id"] for row in after.json()}, (
            "a deleted licence REAPPEARED on the admin list. This is the exact "
            "consequence the flag's old default was guarding against, and it makes "
            "arming retention a net regression rather than a fix."
        )

    def test_it_is_still_reachable_as_an_audit_record(
        self, auth_client_admin: TestClient, registered_tenant, seed_license
    ) -> None:
        """Hiding it by default must not mean destroying access to it.

        The retained row IS this service's record of the deletion — there is no audit
        table — so ``deleted_at``/``deleted_by``/``delete_reason`` are unreadable if
        nothing can select the row.
        """
        subject_id = UUID(str(seed_license.subject_id))
        licence_id = str(seed_license.id)
        auth_client_admin.delete(f"/licenses/user/{subject_id}?confirm_licenses=1&reason=why")

        audit = auth_client_admin.get("/licenses/admin/all", params={"include_deleted": "true"})
        assert audit.status_code == 200, audit.text
        assert licence_id in {row["id"] for row in audit.json()}, (
            "the deletion record is unreachable through any API — the row survives "
            "but nothing can read it, which is the same as having no audit trail"
        )

    def test_the_internal_list_hides_it_too(
        self, auth_client_admin: TestClient, registered_tenant, seed_license
    ) -> None:
        """billing reads this endpoint. An unfiltered listing has no status predicate
        at all, so without the filter a row-counting consumer starts counting deleted
        licences."""
        subject_id = UUID(str(seed_license.subject_id))
        licence_id = str(seed_license.id)
        auth_client_admin.delete(f"/licenses/user/{subject_id}?confirm_licenses=1")

        listed = auth_client_admin.get(f"/internal/licenses?tenant_id={SEED_TENANT}")
        assert listed.status_code == 200, listed.text
        assert licence_id not in {row["id"] for row in listed.json()}

    def test_the_historical_arm_survives(
        self, auth_client_admin: TestClient, registered_tenant, seed_license
    ) -> None:
        """⛔ THE CONTROL ON THE NEW FILTER, and the one that is easy to get wrong.

        ``effectively_active_clauses`` decides the deleted case AS A FUNCTION OF
        ``as_of``: a licence deleted AFTER ``as_of`` was still a seat AT ``as_of`` and
        MUST come back. A flat ``deleted_at IS NULL`` stacked on top would drop it and
        re-derive a CLOSED period smaller than its invoice — the defect the soft
        delete exists to prevent, reintroduced by the filter meant to hide deleted
        rows. This fails if the two predicates are combined.

        ⚠️ EVERY FILTER GOES IN ``params=``, NONE IN THE PATH. This test first
        failed with an EMPTY listing, not a missing id — because ``params=`` REPLACES
        httpx's query string (see ``_as_of``), so ``tenant_id``/``status``/
        ``effective`` written into the path string were dropped and the request that
        actually went out was a bare ``GET /internal/licenses?as_of=…``. That takes
        ``effective``'s default ``True`` with ``status=None``, which makes
        ``effective_active`` False, which correctly applies the flat
        ``deleted_at IS NULL`` filter — i.e. the failure was this test asking the
        wrong question, and the guard in ``list_licenses`` was right all along. The
        assertion on the SENT url below is what stops that recurring silently.
        """
        subject_id = UUID(str(seed_license.subject_id))
        licence_id = str(seed_license.id)
        before = datetime.now(UTC) - timedelta(minutes=5)

        auth_client_admin.delete(f"/licenses/user/{subject_id}?confirm_licenses=1")

        historical = auth_client_admin.get(
            "/internal/licenses",
            params={
                "tenant_id": SEED_TENANT,
                "status": "active",
                "effective": "true",
                **_as_of(before),
            },
        )
        sent = historical.request.url.params
        assert (sent.get("status"), sent.get("effective"), sent.get("tenant_id")) == (
            "active",
            "true",
            SEED_TENANT,
        ), (
            "the filters this test exists to exercise never reached the server, so a "
            f"pass or a fail here would both be meaningless; sent={historical.request.url}"
        )
        assert historical.status_code == 200, historical.text
        assert licence_id in {row["id"] for row in historical.json()}, (
            "a licence deleted AFTER as_of vanished from the historical listing — "
            "include_deleted has been stacked onto the effective predicate"
        )


class TestTheDisarmedPathIsStillCompatible:
    """⛔ THE NEGATIVE CONTROL. Green before this change and green after it.

    ``LICENSE_SOFT_DELETE=false`` is a supported configuration. A caller who has
    explicitly opted back into the destroy must not ALSO be broken by a newly
    required parameter — that would be two changes sold as one. This class fails if
    the flip is implemented by removing the hard path rather than demoting it.
    """

    def test_the_rows_are_still_destroyed_when_disarmed(
        self,
        auth_client_admin: TestClient,
        db_manager,
        disarmed,
        registered_tenant,
        seed_license,
        seed_token,
    ) -> None:
        subject_id = UUID(str(seed_license.subject_id))
        response = auth_client_admin.delete(f"/licenses/user/{subject_id}")
        assert response.status_code == 200, response.text
        assert response.json() == {"deleted": 1, "mode": "hard", "released_dependency_ids": []}
        assert _rows(db_manager, subject_id) == []

    def test_no_confirmation_is_required_when_disarmed(
        self, auth_client_admin: TestClient, disarmed, registered_tenant, seed_license
    ) -> None:
        subject_id = UUID(str(seed_license.subject_id))
        response = auth_client_admin.delete(f"/licenses/user/{subject_id}")
        assert response.status_code == 200, (
            "an unconfirmed delete was refused with the flag explicitly disarmed, "
            f"which breaks the opt-out this branch exists to preserve; {response.text}"
        )


class TestTheHardPathIsNoLongerSilent:
    """⛔ THE ROW CANNOT BE THE AUDIT RECORD WHEN THE ROW IS WHAT IS DESTROYED.

    So the log line is, and it is the SAME mechanism ``delete_app`` has used for the
    same reason — this service has no audit table (see
    ``soft_delete_licenses_for_subject``), and inventing one here would give the
    estate a second, half-populated answer to a question the retained row already
    answers everywhere else.
    """

    def test_it_records_who_what_and_the_counts(
        self,
        auth_client_admin: TestClient,
        db_manager,
        disarmed,
        registered_tenant,
        seed_license,
        seed_token,
        caplog,
    ) -> None:
        subject_id = UUID(str(seed_license.subject_id))
        with caplog.at_level(logging.ERROR, logger=main.logger.name):
            response = auth_client_admin.delete(
                f"/licenses/user/{subject_id}?reason=support-request-4821"
            )
        assert response.status_code == 200, response.text

        lines = [
            r.getMessage()
            for r in caplog.records
            if "licence_delete_confirmed" in r.getMessage() and r.levelno >= logging.ERROR
        ]
        assert lines, (
            "a hard delete destroyed a licence and wrote NOTHING. The rows are gone, "
            "so this line is the only possible record that it happened."
        )
        line = lines[0]

        assert "mode=hard" in line, (
            "the record does not say WHICH path ran; a reader cannot tell a "
            f"recoverable soft delete from an irreversible one. line={line!r}"
        )
        assert f"subject={subject_id}" in line, f"no subject recorded; line={line!r}"
        assert "actor=admin-user-uuid" in line, (
            f"WHO is missing — the point of an audit record; line={line!r}"
        )
        assert "support-request-4821" in line, (
            f"WHY is missing; `reason` is discarded on this path again. line={line!r}"
        )
        assert "deleted=1" in line, f"no count recorded; line={line!r}"

    def test_it_reports_the_radius_that_only_existed_before_the_delete(
        self,
        auth_client_admin: TestClient,
        disarmed,
        registered_tenant,
        seed_license,
        seed_token,
        caplog,
    ) -> None:
        """⛔ THE ORDERING PROOF, and the reason this is a separate test.

        ``active_licences`` and ``tokens`` are UNOBTAINABLE one line after the
        ``.delete()``: the rows are gone and the tokens went with them via
        ``token.license_id`` ON DELETE CASCADE. A line carrying non-zero values for
        both therefore proves the radius was read BEFORE the destroy — which a log
        statement written afterwards could not do, and which is the difference
        between an audit record and a rumour.
        """
        subject_id = UUID(str(seed_license.subject_id))
        with caplog.at_level(logging.ERROR, logger=main.logger.name):
            auth_client_admin.delete(f"/licenses/user/{subject_id}")

        line = next(
            r.getMessage() for r in caplog.records if "licence_delete_confirmed" in r.getMessage()
        )
        assert "active=1" in line, (
            "the ACTIVE count is wrong or missing. It is the number that moves a seat "
            f"count and therefore an invoice — deleting 1 revoked row and 1 active row "
            f"are the same `deleted` figure and completely different events. line={line!r}"
        )
        assert "tokens=1" in line, (
            "the cascaded token count is missing, so the record understates what was "
            f"destroyed; line={line!r}"
        )

    def test_a_delete_that_removes_nothing_writes_nothing(
        self, auth_client_admin: TestClient, disarmed, registered_tenant, seed_subject, caplog
    ) -> None:
        """The control on the logging. A record for a no-op delete would make the
        audit trail unreadable — every 'deletion' in it would need re-checking
        against a count."""
        with caplog.at_level(logging.ERROR, logger=main.logger.name):
            response = auth_client_admin.delete(f"/licenses/user/{seed_subject.id}")
        assert response.status_code == 200, response.text
        assert response.json()["deleted"] == 0
        assert not [r for r in caplog.records if "licence_delete_confirmed" in r.getMessage()], (
            "a delete that destroyed nothing was recorded as a destruction"
        )
