"""``/license/revoke`` must be able to kill any token its tenant owns.

The defect
----------
The route established ownership by calling ``introspect_token`` and refusing
unless the answer was ``active``::

    result = await asyncio.to_thread(db_manager.introspect_token, request.token)
    if not result or not result.get("active"):
        return TokenRevokeResponse(revoked=False, message="Token not found or already invalid")

``active`` is LICENSE-aware — ``_introspect_from_session`` returns inactive for
``license_inactive`` as well as ``token_invalid`` — while the write it guarded
(``revoke_token``) is the only thing in this service that ever sets
``Token.revoked``.

So the reachable failure is a **leaked credential that survives**:

1. a perpetual token (``expires_at IS NULL``) leaks;
2. its license lapses — nothing here transitions the row, so ``status`` may well
   still read ``active`` while ``expires_at`` is in the past;
3. the operator posts the leaked token to ``/license/revoke`` and is told
   *"Token not found or already invalid"*, with **nothing written**. That is a
   reasonable thing to read as "it is already dead";
4. the customer renews. ``Token.is_valid`` consults only ``revoked`` and the
   token's own expiry — so the credential is live again, at full entitlement, on
   the fleet's hottest auth path.

Revocation is an administrative act on a row. It is not, and must not be gated
on, an assertion about whether the credential authenticates at this instant.

Why no existing test caught it
------------------------------
Every revoke case in ``tests/unit/test_endpoints.py::TestRevokeEndpoint`` and in
``tests/integration/test_tokens.py`` uses an **active** license fixture.
``seed_revoked_license`` exists and is used — but only against
``/license/introspect``, never against ``/license/revoke``. And there was no
double-revoke test anywhere, which is why the two arms of one endpoint were free
to disagree about idempotency.

What is deliberately NOT changed
--------------------------------
"Unknown token" and "not your tenant" still answer with one identical message.
That masking is intentional non-disclosure — without it this endpoint becomes an
oracle for whether a token exists in someone else's tenant — and it is pinned
below so a future refactor cannot quietly split the two apart.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from models import License, Token

TENANT_ID = "01000000-0000-7000-8000-000000000001"


def _license_revoked_at(db_manager, license_id: str):
    session = db_manager.get_session()
    try:
        row = session.query(License).filter(License.id == UUID(str(license_id))).first()
        assert row is not None, "the seeded license is not in the database"
        return row.revoked_at
    finally:
        session.close()


def _token_row(db_manager, raw_token: str) -> Token:
    session = db_manager.get_session()
    try:
        row = session.query(Token).filter(Token.token_hash == Token.hash_token(raw_token)).first()
        assert row is not None, "the seeded token is not in the database"
        session.expunge(row)
        return row
    finally:
        session.close()


@pytest.fixture
def lapsed_license(db_manager, seed_subject, seed_app, seed_plan) -> License:
    """A license whose window has CLOSED while its stored status still says active.

    That combination is not contrived — it is the steady state of this service.
    ``License.effective_status`` derives expiry at read time and nothing ever
    writes the transition back, so every naturally-expired license looks exactly
    like this row.
    """
    return db_manager.create_license(
        subject_id=UUID(str(seed_subject.id)),
        app_id=UUID(str(seed_app.id)),
        plan_id=UUID(str(seed_plan.id)),
        tenant_id=TENANT_ID,
        status="active",
        starts_at=datetime.now(UTC) - timedelta(days=400),
        expires_at=datetime.now(UTC) - timedelta(days=30),
    )


@pytest.fixture
def leaked_token_on_a_lapsed_license(db_manager, lapsed_license) -> str:
    """A PERPETUAL token (no expiry of its own) on that lapsed license."""
    _token, raw = db_manager.create_token(
        license_id=UUID(str(lapsed_license.id)),
        token_type="api",
        expires_at=None,
    )
    return raw


class TestARevokeReachesATokenWhoseLicenseHasLapsed:
    def test_the_row_is_actually_revoked(
        self, auth_client_admin: TestClient, db_manager, leaked_token_on_a_lapsed_license
    ):
        """The END STATE is what matters, so this asserts the row, not the reply.

        On the pre-fix tree the response was ``revoked: false`` AND the row was
        untouched. Asserting only the response would leave a fix that reports
        success without writing anything indistinguishable from a real one.
        """
        raw = leaked_token_on_a_lapsed_license

        # Precondition, measured rather than assumed: the token starts unrevoked.
        assert _token_row(db_manager, raw).revoked is False

        response = auth_client_admin.post("/license/revoke", json={"token": raw})

        assert response.status_code == 200
        assert response.json()["revoked"] is True, response.json()

        row = _token_row(db_manager, raw)
        assert row.revoked is True, (
            "the endpoint reported success but the token row is still unrevoked — "
            "a leaked credential on a lapsed license would come back live the "
            "moment the customer renews"
        )
        assert row.revoked_at is not None, "revoked_at was not stamped"

    def test_it_stays_dead_across_a_renewal(
        self,
        auth_client_admin: TestClient,
        db_manager,
        lapsed_license,
        leaked_token_on_a_lapsed_license,
    ):
        """The consequence, driven end to end rather than argued.

        This is the assertion that names the actual harm: revoke, then renew the
        license, then introspect. Before the fix the revoke wrote nothing, so the
        renewal resurrected the credential.
        """
        raw = leaked_token_on_a_lapsed_license
        auth_client_admin.post("/license/revoke", json={"token": raw})

        # Renew: push the window back into the future, exactly as a renewal would.
        session = db_manager.get_session()
        try:
            row = session.query(License).filter(License.id == lapsed_license.id).first()
            row.expires_at = datetime.now(UTC) + timedelta(days=365)
            session.commit()
        finally:
            session.close()

        # POSITIVE CONTROL, and it has to come first: prove the renewal really did
        # reactivate the license, or "the token is inactive" below would hold for
        # the wrong reason (a license still expired).
        control = auth_client_admin.post(
            "/license/introspect", json={"token": _live_sibling_token(db_manager, lapsed_license)}
        )
        assert control.json()["active"] is True, (
            "the renewal did not reactivate the license, so the assertion below "
            "would pass even if the revoke had done nothing"
        )

        after = auth_client_admin.post("/license/introspect", json={"token": raw})
        assert after.json()["active"] is False, (
            "a revoked token authenticated again after the license was renewed"
        )


def _live_sibling_token(db_manager, license_obj) -> str:
    """A second, never-revoked token on the same license — the control credential."""
    _token, raw = db_manager.create_token(
        license_id=UUID(str(license_obj.id)), token_type="api", expires_at=None
    )
    return raw


class TestRevokeIsIdempotent:
    def test_revoking_twice_reports_success_both_times(
        self, auth_client_admin: TestClient, full_seed_data
    ):
        """A retry must not look like a failure.

        The sibling ``license_id`` arm of this same endpoint has always been
        idempotent (``revoke_license`` returns True whenever the row exists). The
        token arm answered ``false`` with the SAME message as "wrong tenant" and
        "no such token", so an at-least-once client could not tell a completed
        revoke from a refused one.
        """
        raw = full_seed_data["raw_token"]

        first = auth_client_admin.post("/license/revoke", json={"token": raw})
        second = auth_client_admin.post("/license/revoke", json={"token": raw})

        assert first.json()["revoked"] is True
        assert second.json()["revoked"] is True, second.json()
        # The two are distinguishable to a human reading the log, but both succeed.
        assert first.json()["message"] != second.json()["message"]

    def test_a_second_revoke_does_not_overwrite_the_original_timestamp(
        self, auth_client_admin: TestClient, db_manager, full_seed_data
    ):
        """``revoked_at`` is the audit record of when the credential was killed.

        ⚠️ HONEST LABEL: this one PASSES on the pre-fix tree, for the opposite
        reason to the one it is named for. There, the second revoke was refused
        by the ``active`` gate and wrote nothing at all — so the timestamp
        survived by accident, not by policy. It is therefore a REGRESSION GUARD
        on the new idempotent arm (which does reach the row), not a RED proof of
        the defect.

        ⚠️ THE RED-PROOF COUNT, corrected twice. A previous version said "four".
        A commit message then CLAIMED to have corrected it and its diff never
        touched this line — while the same commit ADDED a sixth failing test,
        making the stale number wronger. Enumerated against `origin/main`:

          FAIL (real RED)  test_the_row_is_actually_revoked
          FAIL (real RED)  test_it_stays_dead_across_a_renewal
          FAIL (real RED)  test_revoking_twice_reports_success_both_times
          FAIL (real RED)  test_it_does_not_rewrite_an_already_revoked_token
          FAIL (real RED)  test_a_second_license_revoke_preserves_the_licenses_own_timestamp
          ERROR            test_an_unrecognised_outcome_still_answers_the_non_disclosing_message
                           -- `patch.object` cannot find `revoke_token_for_tenant`
                              on main. An AttributeError is a SETUP error, never
                              a RED proof.
          PASS             this test, and the two TestNonDisclosureSurvives guards

        So: **five** genuine RED proofs, one setup error, three green guards.
        """
        raw = full_seed_data["raw_token"]

        auth_client_admin.post("/license/revoke", json={"token": raw})
        first_stamp = _token_row(db_manager, raw).revoked_at
        assert first_stamp is not None

        auth_client_admin.post("/license/revoke", json={"token": raw})
        assert _token_row(db_manager, raw).revoked_at == first_stamp, (
            "the second revoke rewrote revoked_at, destroying the record of when "
            "the leak was actually closed"
        )


class TestLicenseWideRevokePreservesEarlierTimestamps:
    def test_it_does_not_rewrite_an_already_revoked_token(
        self, auth_client_admin: TestClient, db_manager, full_seed_data
    ):
        """``revoke_license`` bulk-updated EVERY token, with no ``revoked = false``
        predicate — unlike both of its siblings. So a license-wide revoke rewrote
        the timestamp of a token individually killed months earlier, and the
        endpoint is callable repeatedly by any ``license-manager``.
        """
        license_id = str(full_seed_data["license"].id)
        already_dead = full_seed_data["raw_token"]

        auth_client_admin.post("/license/revoke", json={"token": already_dead})
        original_stamp = _token_row(db_manager, already_dead).revoked_at
        assert original_stamp is not None

        # A second, still-live token, so the bulk revoke has real work to do —
        # otherwise this test could pass against an UPDATE that matched nothing.
        _t, live_raw = db_manager.create_token(
            license_id=UUID(license_id), token_type="api", expires_at=None
        )
        assert _token_row(db_manager, live_raw).revoked is False

        response = auth_client_admin.post("/license/revoke", json={"license_id": license_id})
        assert response.json()["revoked"] is True

        # VACUITY FLOOR: the bulk revoke must actually have written something.
        assert _token_row(db_manager, live_raw).revoked is True, (
            "the license-wide revoke did not revoke the live token, so the "
            "timestamp assertion below proves nothing"
        )
        assert _token_row(db_manager, already_dead).revoked_at == original_stamp, (
            "the license-wide revoke rewrote revoked_at on a token that was "
            "already revoked, destroying its audit trail"
        )

    def test_a_second_license_revoke_preserves_the_licenses_own_timestamp(
        self, auth_client_admin: TestClient, db_manager, full_seed_data
    ):
        """The half the first version missed, and the class name already promised.

        `License.revoked_at` was rewritten unconditionally three lines above the
        token UPDATE that this change filtered. The retry is automatic, not
        operator-driven: `n8n-nodes-licenses` wraps this call in
        `auth.executeWithRetry(...)`, and the value is externally observable via
        `License.to_dict()` / `LicenseResponse.revoked_at`.
        """
        license_id = str(full_seed_data["license"].id)

        first = auth_client_admin.post("/license/revoke", json={"license_id": license_id})
        assert first.json()["revoked"] is True
        original = _license_revoked_at(db_manager, license_id)
        assert original is not None, "the first revoke did not stamp revoked_at"

        second = auth_client_admin.post("/license/revoke", json={"license_id": license_id})
        # Still idempotent-successful — that contract is unchanged.
        assert second.json()["revoked"] is True

        assert _license_revoked_at(db_manager, license_id) == original, (
            "a second license-wide revoke rewrote License.revoked_at, destroying "
            "the record of when the license was actually killed"
        )


class TestNonDisclosureSurvives:
    """The masking is deliberate and must not be a casualty of the fix."""

    def test_an_unknown_token_and_a_foreign_token_are_indistinguishable(
        self, auth_client_admin: TestClient, db_manager, seed_other_tenant_license
    ):
        _t, foreign_raw = db_manager.create_token(
            license_id=UUID(str(seed_other_tenant_license.id)),
            token_type="api",
            expires_at=None,
        )

        unknown = auth_client_admin.post("/license/revoke", json={"token": "no-such-token"})
        foreign = auth_client_admin.post("/license/revoke", json={"token": foreign_raw})

        assert unknown.json() == foreign.json(), (
            "the two answers differ, so this endpoint now reveals whether a token "
            "exists in another tenant"
        )
        assert foreign.json()["revoked"] is False

    def test_a_foreign_token_is_not_actually_revoked(
        self, auth_client_admin: TestClient, db_manager, seed_other_tenant_license
    ):
        """The other half — refusing in the RESPONSE while writing anyway would be
        strictly worse than the defect this file fixes."""
        _t, foreign_raw = db_manager.create_token(
            license_id=UUID(str(seed_other_tenant_license.id)),
            token_type="api",
            expires_at=None,
        )

        auth_client_admin.post("/license/revoke", json={"token": foreign_raw})

        assert _token_row(db_manager, foreign_raw).revoked is False, (
            "a cross-tenant revoke succeeded — the ownership check is not enforced"
        )

    def test_an_unrecognised_outcome_still_answers_the_non_disclosing_message(
        self, auth_client_admin: TestClient, full_seed_data
    ):
        """The route's CATCH-ALL is what actually enforces non-disclosure.

        Found by mutation, and worth pinning because it changes where the
        property lives. A mutant that made ``revoke_token_for_tenant`` return a
        distinct ``"wrong_tenant"`` SURVIVED the entire battery — the string
        never reaches the wire, because the route maps everything that is not
        ``revoked`` / ``already_revoked`` onto one answer. So that mutant is
        EQUIVALENT, and the model's return value is not the guarantee.

        This asserts the guarantee where it really is: hand the route an outcome
        it has never heard of and require the non-disclosing reply, rather than a
        crash, a 500, or a distinguishable message.
        """
        from models import DatabaseManager

        raw = full_seed_data["raw_token"]
        with patch.object(
            DatabaseManager, "revoke_token_for_tenant", return_value=("some_future_outcome", 0)
        ):
            response = auth_client_admin.post("/license/revoke", json={"token": raw})

        assert response.status_code == 200
        assert response.json() == {
            "revoked": False,
            "message": "Token not found or already invalid",
            "released_dependency_ids": [],
        }, response.json()
