"""``POST /licenses/{license_id}/tokens`` must not mint on a dead licence (#237).

THE GAP. The route checked existence, tenant ownership and the archived tenant, then
called ``create_token`` with NO licence-state check — while its sibling
``regenerate-token`` has refused a non-active licence with a structured 409 since
2026-08-08. Measured on ``origin/main`` (423eb58), every refusal asserted below
returned **201** and wrote a token row.

THE ONE DELIBERATE EXCEPTION. A licence that is ``pending`` only because ``starts_at``
is in the future is ALLOWED. provisioning (``provisioning/src/licensing.py`` step 5,
and ``src/orchestration.py``) mints its one-time raw token through this route right
after ``create_license`` with a caller-supplied ``starts_at``, and chose this route
precisely because ``regenerate-token`` 409s on pending. Refusing it would break every
forward-dated grant. ``TestAForwardDatedLicenceStillMints`` pins that.

Stored ``suspended`` cannot be written to the DB (migration 033 constrains ``status``
to active/revoked/expired/pending), so it is exercised on an in-memory ``License``;
stored ``pending`` is the reachable non-active status and goes through the route.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

import main
from models import License, Token

TENANT = "01000000-0000-7000-8000-000000000001"


@pytest.fixture
def main_db(db_manager):
    original = main.db_manager
    main.db_manager = db_manager
    try:
        yield db_manager
    finally:
        main.db_manager = original


@pytest.fixture
def make_licence(main_db, seed_app, seed_plan, seed_subject):
    def _make(**kwargs) -> License:
        return main_db.create_license(
            subject_id=UUID(str(seed_subject.id)),
            app_id=UUID(str(seed_app.id)),
            plan_id=UUID(str(seed_plan.id)),
            tenant_id=TENANT,
            **kwargs,
        )

    return _make


def _mint(client: TestClient, licence_id):
    return client.post(f"/licenses/{licence_id}/tokens", headers={"X-Tenant-Id": TENANT})


def _tokens_for(db_manager, licence_id) -> list[Token]:
    session = db_manager.get_session()
    try:
        return session.query(Token).filter(Token.license_id == UUID(str(licence_id))).all()
    finally:
        session.close()


def _assert_refused(response, db_manager, licence_id, expected_status: str) -> None:
    assert response.status_code == 409, (
        f"a dead licence was issued a new credential; body={response.text}"
    )
    detail = response.json()["detail"]
    # Same shape as regenerate-token's license_not_active — fr parses it.
    assert set(detail) == {
        "error",
        "license_id",
        "status",
        "stored_status",
        "starts_at",
        "expires_at",
    }, detail
    assert detail["error"] == "license_not_active"
    assert detail["license_id"] == str(licence_id)
    assert detail["status"] == expected_status
    assert _tokens_for(db_manager, licence_id) == [], (
        "the mint was refused but a token row was written anyway"
    )


class TestMintRefusesADeadLicence:
    """The defect. On origin/main each of these measured 201 + a token row."""

    def test_revoked_licence_is_refused(self, auth_client_admin, main_db, make_licence):
        licence = make_licence(status="active")
        assert main_db.revoke_license(UUID(str(licence.id)))

        _assert_refused(_mint(auth_client_admin, licence.id), main_db, licence.id, "revoked")

    def test_time_expired_licence_is_refused(self, auth_client_admin, main_db, make_licence):
        """Stored 'active' but past expires_at — nothing in the estate transitions
        that row, so the stored status alone would have let it through."""
        licence = make_licence(
            status="active",
            starts_at=datetime.now(UTC) - timedelta(days=10),
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )

        _assert_refused(_mint(auth_client_admin, licence.id), main_db, licence.id, "expired")

    def test_stored_expired_licence_is_refused(self, auth_client_admin, main_db, make_licence):
        licence = make_licence(status="expired")

        _assert_refused(_mint(auth_client_admin, licence.id), main_db, licence.id, "expired")

    def test_stored_non_active_status_is_refused(self, auth_client_admin, main_db, make_licence):
        """Stored 'pending' (not the time-derived pending) is a non-active STORED
        status — the suspended-shaped case the DB constraint actually admits."""
        licence = make_licence(status="pending")

        _assert_refused(_mint(auth_client_admin, licence.id), main_db, licence.id, "pending")


class TestAForwardDatedLicenceStillMints:
    """⛔ The provisioning contract. Do not 'tighten' this to is_active()."""

    def test_not_yet_started_licence_mints(self, auth_client_admin, main_db, make_licence):
        # provisioning/src/licensing.py step 5 mints here immediately after
        # create_license with a caller-supplied (possibly future) starts_at, and
        # picked this route because regenerate-token 409s on pending.
        licence = make_licence(
            status="active",
            starts_at=datetime.now(UTC) + timedelta(days=7),
            expires_at=datetime.now(UTC) + timedelta(days=37),
        )
        assert licence.effective_status() == "pending"

        response = _mint(auth_client_admin, licence.id)

        assert response.status_code == 201, response.text
        assert len(_tokens_for(main_db, licence.id)) == 1


class TestAnActiveLicenceStillMints:
    """The control: a guard that refuses everything passes every refusal above."""

    def test_active_licence_mints(self, auth_client_admin, main_db, make_licence):
        licence = make_licence(
            status="active",
            starts_at=datetime.now(UTC) - timedelta(days=1),
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )

        response = _mint(auth_client_admin, licence.id)

        assert response.status_code == 201, response.text
        assert len(_tokens_for(main_db, licence.id)) == 1

    def test_perpetual_active_licence_mints(self, auth_client_admin, main_db, make_licence):
        licence = make_licence(status="active")

        assert _mint(auth_client_admin, licence.id).status_code == 201


class TestAcceptsNewTokenPredicate:
    """The predicate on rows the DB constraint cannot hold (no DB)."""

    @staticmethod
    def _lic(**kw) -> License:
        base = {"status": "active", "starts_at": None, "expires_at": None, "deleted_at": None}
        base.update(kw)
        return License(**base)

    def test_suspended_is_refused(self):
        assert not self._lic(status="suspended").accepts_new_token()

    def test_unknown_status_is_refused(self):
        assert not self._lic(status="frozen").accepts_new_token()

    def test_soft_deleted_is_refused_even_if_status_reads_active(self):
        assert not self._lic(deleted_at=datetime.now(UTC)).accepts_new_token()

    def test_forward_dated_and_already_expired_is_refused(self):
        """effective_status() reports this 'pending' (pending wins the tie), but it
        can never become active — refuse it."""
        lic = self._lic(
            starts_at=datetime.now(UTC) + timedelta(days=2),
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )
        assert lic.effective_status() == "pending"
        assert not lic.accepts_new_token()

    def test_forward_dated_is_accepted_while_is_active_is_not(self):
        lic = self._lic(starts_at=datetime.now(UTC) + timedelta(days=2))
        assert not lic.is_active()
        assert lic.accepts_new_token()

    def test_active_is_accepted(self):
        assert self._lic().accepts_new_token()
