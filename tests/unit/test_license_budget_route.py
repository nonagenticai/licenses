"""``PATCH /licenses/{license_id}/budget`` — the licence-level budget lever.

WHY IT EXISTS (2026-10-31). ``token_budget_override`` was INSERT-ONLY: operator
provisioning scripts set it, every read published it, and nothing updated it. A
budget edit made in billing alone is reverted by ``sync_from_licenses``, which
upserts the budget FROM this column — so the licence is the durable lever and
this route is its writer.

Access is the LICENCE-WRITE rule (``_authorize_issuance``), not owner-only:
the caller's owner must ISSUE the licence's app AND own the tenant or hold an
active grant on it. George's assignment is that fr's super-admin console
controls budgets for fr/conveyor. The tenant arm is ALREADY satisfied: the
``tenant_app_grant`` on conveyor is live with ``granted_by='migration-050'``
(rule 2), which is why ``owner_can_access_tenant('fr', conveyor)`` is already
True. The app arm comes from conveyor's licence being ``inference-router``,
whose ``app_issuer`` set is {fr, commonpractice}. A stricter
``_resolve_owned_tenant`` guard would answer fr 403 ``tenant_owner_only`` and
ship a control that cannot work.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

import main
from tests.unit.test_tenant_ownership import CP, FR, _app, _as, _err

CONVEYOR = "5f868d42-03f8-51f0-a93c-3e83070dc196"
STRANGER = "w2v-backend"
#: The issuer that does NOT hold a grant on conveyor.
ISSUER_WITHOUT_GRANT = CP


@pytest.fixture
def licence(db_manager):
    """A live licence on the conveyor tenant, owned by `platform` (as prod has it).

    The app is ``inference-router`` — conveyor's live licence's app, whose
    ``app_issuer`` seed is {fr, commonpractice} (migration 050) — so the
    issuance arm is exercised with the real relationship, not a fictional app
    fr happens to issue.
    """
    db_manager.ensure_tenant(
        CONVEYOR, name="svc-n8n-local-conveyor", kind="service", owner_key="platform"
    )
    app_id, plan_id = _app(db_manager, "inference-router", ("fr", "commonpractice"))
    subject = db_manager.create_or_get_subject(
        keycloak_sub="conveyor-budget-sub", keycloak_realm="service"
    )
    return db_manager.create_license(
        subject_id=UUID(str(subject.id)),
        app_id=app_id,
        plan_id=plan_id,
        tenant_id=CONVEYOR,
        status="active",
        starts_at=datetime.now(UTC) - timedelta(days=1),
        expires_at=datetime.now(UTC) + timedelta(days=30),
    )


@pytest.fixture
def pushes(monkeypatch):
    """Capture ``push_effective_token_budgets`` calls AND the column value it
    would have pushed, so the test proves the write was committed FIRST."""
    calls: list[dict[str, Any]] = []

    async def _record(*, license_ids=None, plan_id=None, plan_driven_only=False, reason):
        session = main.db_manager.get_session()
        try:
            seen = session.execute(
                text("SELECT token_budget_override FROM license WHERE id = :i"),
                {"i": (license_ids or [None])[0]},
            ).scalar()
        finally:
            session.close()
        calls.append({"license_ids": license_ids, "reason": reason, "seen": seen})
        return None

    monkeypatch.setattr(main, "push_effective_token_budgets", _record)
    return calls


def _column(db_manager, licence) -> int | None:
    session = db_manager.get_session()
    try:
        return session.execute(
            text("SELECT token_budget_override FROM license WHERE id = :i"),
            {"i": str(licence.id)},
        ).scalar()
    finally:
        session.close()


class TestWritesTheOverride:
    def test_a_granted_owner_sets_the_override_and_pushes_after_the_write(
        self, db_manager, licence, pushes
    ):
        db_manager.put_tenant_grant(CONVEYOR, "fr", "test")
        with _as(db_manager, FR) as fr:
            resp = fr.patch(
                f"/licenses/{licence.id}/budget",
                json={"token_budget_override": 1234, "reason": "raised for launch"},
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["id"] == str(licence.id)
        assert body["token_budget_override"] == 1234
        assert body["plan"]["code"] == "standard"
        assert _column(db_manager, licence) == 1234
        # The push names this licence, carries the operator's reason, and ran
        # after the commit (it observed the new value).
        assert pushes == [
            {
                "license_ids": [str(licence.id)],
                "reason": (
                    f"license {licence.id} token_budget_override None -> 1234 (raised for launch)"
                ),
                "seen": 1234,
            }
        ]

    def test_null_clears_the_override_and_pushes_the_plan_value(self, db_manager, licence, pushes):
        db_manager.put_tenant_grant(CONVEYOR, "fr", "test")
        db_manager.set_license_token_budget_override(UUID(str(licence.id)), 9000)
        with _as(db_manager, FR) as fr:
            resp = fr.patch(f"/licenses/{licence.id}/budget", json={"token_budget_override": None})
        assert resp.status_code == 200, resp.text
        assert resp.json()["token_budget_override"] is None
        assert _column(db_manager, licence) is None
        assert pushes[0]["seen"] is None
        assert pushes[0]["reason"].endswith("9000 -> None")

    def test_zero_is_a_value_not_a_clear(self, db_manager, licence, pushes):
        """0 = block all. It must round-trip and be pushed — not collapse to None."""
        db_manager.put_tenant_grant(CONVEYOR, "fr", "test")
        with _as(db_manager, FR) as fr:
            resp = fr.patch(f"/licenses/{licence.id}/budget", json={"token_budget_override": 0})
        assert resp.status_code == 200, resp.text
        assert resp.json()["token_budget_override"] == 0
        assert _column(db_manager, licence) == 0
        assert pushes[0]["seen"] == 0

    def test_an_omitted_override_is_422_not_a_clear(self, db_manager, licence, pushes):
        db_manager.put_tenant_grant(CONVEYOR, "fr", "test")
        db_manager.set_license_token_budget_override(UUID(str(licence.id)), 9000)
        with _as(db_manager, FR) as fr:
            resp = fr.patch(f"/licenses/{licence.id}/budget", json={"reason": "typo"})
        assert resp.status_code == 422, resp.text
        assert _column(db_manager, licence) == 9000
        assert pushes == []


class TestAccess:
    """The guard is ``_authorize_issuance``: ISSUE the app AND own-or-grant the
    tenant. Each arm is exercised on its own refusal code."""

    def test_a_client_that_does_not_issue_the_app_is_refused(self, db_manager, licence, pushes):
        db_manager.put_tenant_grant(CONVEYOR, "fr", "test")
        with _as(db_manager, STRANGER) as stranger:
            resp = stranger.patch(
                f"/licenses/{licence.id}/budget", json={"token_budget_override": 5}
            )
        assert resp.status_code == 403, resp.text
        assert _err(resp) == "app_not_issuable_by_caller"
        assert _column(db_manager, licence) is None
        assert pushes == []

    def test_an_issuer_without_tenant_access_is_refused(self, db_manager, licence, pushes):
        """commonpractice DOES issue inference-router; it holds no grant here."""
        with _as(db_manager, ISSUER_WITHOUT_GRANT) as cp:
            resp = cp.patch(f"/licenses/{licence.id}/budget", json={"token_budget_override": 5})
        assert resp.status_code == 403, resp.text
        assert _err(resp) == "tenant_not_accessible"
        assert _column(db_manager, licence) is None
        assert pushes == []

    def test_an_absent_licence_is_404(self, db_manager, licence, pushes):
        db_manager.put_tenant_grant(CONVEYOR, "fr", "test")
        with _as(db_manager, FR) as fr:
            resp = fr.patch(f"/licenses/{uuid.uuid4()}/budget", json={"token_budget_override": 5})
        assert resp.status_code == 404, resp.text
        assert resp.json() == {"detail": "License not found"}
        assert pushes == []

    def test_a_revoked_grant_loses_access(self, db_manager, licence, pushes):
        db_manager.put_tenant_grant(CONVEYOR, "fr", "test")
        db_manager.revoke_tenant_grant(CONVEYOR, "fr")
        with _as(db_manager, FR) as fr:
            resp = fr.patch(f"/licenses/{licence.id}/budget", json={"token_budget_override": 5})
        assert resp.status_code == 403, resp.text
        assert _err(resp) == "tenant_not_accessible"
        assert _column(db_manager, licence) is None
        assert pushes == []

    def test_license_manager_is_refused(self, db_manager, licence, pushes):
        db_manager.put_tenant_grant(CONVEYOR, "fr", "test")
        with _as(db_manager, FR, roles=["license-manager"]) as fr:
            resp = fr.patch(f"/licenses/{licence.id}/budget", json={"token_budget_override": 5})
        assert resp.status_code == 403, resp.text
        assert _column(db_manager, licence) is None
        assert pushes == []
