"""Retiring a plan through ``PATCH /plans/{plan_id}`` with ``revoked``.

A plan referenced by a licence cannot be DELETEd (409), so ``revoked`` is the
only way to take it out of the catalogue. Before this change ``PlanUpdate`` had
no ``revoked`` field (pydantic dropped it, the PATCH answered 400 "No fields to
update") and ``update_plan``'s allow-list did not include it either.

The contract pinned here:
* revoked plans vanish from ``GET /apps/{app_id}/plans`` (and ``GET /plans``);
* ``?include_revoked=true`` on the app listing shows them, with ``revoked: true``;
* licences ALREADY on a revoked plan keep validating and keep minting tokens;
* un-revoking (``false``) restores the plan to the listing;
* only ``license-admin`` may PATCH.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from tests.conftest import SEED_OTHER_TENANT_ID, SEED_TENANT_ID, _make_authenticated_client


def _codes(response) -> set[str]:
    assert response.status_code == 200, response.text
    return {p["code"] for p in response.json()}


class TestPlanRevoke:
    def test_patch_revoked_true_hides_plan_from_app_listing(
        self, auth_client_admin: TestClient, seed_app, seed_plan
    ):
        assert "trial" in _codes(auth_client_admin.get(f"/apps/{seed_app.id}/plans"))

        r = auth_client_admin.patch(f"/plans/{seed_plan.id}", json={"revoked": True})
        assert r.status_code == 200, r.text
        assert r.json()["revoked"] is True

        assert "trial" not in _codes(auth_client_admin.get(f"/apps/{seed_app.id}/plans"))
        assert "trial" not in _codes(auth_client_admin.get(f"/apps/{seed_app.key}/plans"))
        assert "trial" not in _codes(auth_client_admin.get("/plans"))
        # The plan itself is still readable by id.
        got = auth_client_admin.get(f"/plans/{seed_plan.id}")
        assert got.status_code == 200
        assert got.json()["revoked"] is True

    def test_include_revoked_shows_revoked_plan(
        self, auth_client_admin: TestClient, auth_client_reader: TestClient, seed_app, seed_plan
    ):
        auth_client_admin.patch(f"/plans/{seed_plan.id}", json={"revoked": True})

        r = auth_client_admin.get(f"/apps/{seed_app.id}/plans", params={"include_revoked": True})
        assert r.status_code == 200, r.text
        by_code = {p["code"]: p for p in r.json()}
        assert by_code["trial"]["revoked"] is True

        # Reader-gated exactly like the default listing.
        r = auth_client_reader.get(f"/apps/{seed_app.id}/plans", params={"include_revoked": "true"})
        assert "trial" in _codes(r)

    def test_unrevoke_restores_plan(self, auth_client_admin: TestClient, seed_app, seed_plan):
        auth_client_admin.patch(f"/plans/{seed_plan.id}", json={"revoked": True})
        r = auth_client_admin.patch(f"/plans/{seed_plan.id}", json={"revoked": False})
        assert r.status_code == 200, r.text
        assert r.json()["revoked"] is False
        assert "trial" in _codes(auth_client_admin.get(f"/apps/{seed_app.id}/plans"))

    def test_explicit_null_revoked_is_400_not_500(self, auth_client_admin: TestClient, seed_plan):
        r = auth_client_admin.patch(f"/plans/{seed_plan.id}", json={"revoked": None})
        assert r.status_code == 400, r.text

    def test_existing_licence_on_revoked_plan_still_validates(
        self, auth_client_admin: TestClient, full_seed_data
    ):
        plan = full_seed_data["plan"]
        lic = full_seed_data["license"]
        raw = full_seed_data["raw_token"]

        r = auth_client_admin.patch(f"/plans/{plan.id}", json={"revoked": True})
        assert r.status_code == 200, r.text

        intro = auth_client_admin.post("/license/introspect", json={"token": raw})
        assert intro.status_code == 200, intro.text
        body = intro.json()
        assert body["active"] is True
        assert body["license_id"] == str(lic.id)
        assert body["plan"]["code"] == plan.code

        # And the licence can still mint a fresh token (rotation keeps working).
        minted = auth_client_admin.post(f"/licenses/{lic.id}/tokens")
        assert minted.status_code == 201, minted.text
        again = auth_client_admin.post(
            "/license/introspect", json={"token": minted.json()["token"]}
        )
        assert again.json()["active"] is True

    def test_non_admin_patch_is_403(
        self, auth_client_reader: TestClient, auth_client_admin: TestClient, seed_app, seed_plan
    ):
        r = auth_client_reader.patch(f"/plans/{seed_plan.id}", json={"revoked": True})
        assert r.status_code == 403, r.text
        # Not applied.
        assert "trial" in _codes(auth_client_admin.get(f"/apps/{seed_app.id}/plans"))


@pytest.fixture
def other_tenant_admin(db_manager, admin_user):
    """Admin client acting for a DIFFERENT tenant from ``auth_client_admin``."""
    yield from _make_authenticated_client(db_manager, admin_user, tenant_id=SEED_OTHER_TENANT_ID)


class TestNoNewLicenceOnARevokedPlan:
    """``POST /licenses/user/{sub}`` refuses a revoked plan to a tenant with NO
    history on it (409 ``plan_revoked``), and only to that tenant (#258).

    Retiring a plan means no NEW tenants go on it. A tenant already on it keeps
    onboarding members, and CommonPractice changes licence terms by revoking and
    then re-minting on the same plan, so any prior licence row — revoked
    included — keeps the tenant on it. Token mint + introspect on an existing
    licence are pinned above in
    ``test_existing_licence_on_revoked_plan_still_validates``."""

    @staticmethod
    def _mint(client: TestClient, app, plan, sub: str):
        return client.post(
            f"/licenses/user/{sub}", json={"app_id": str(app.id), "plan_id": str(plan.id)}
        )

    def test_new_licence_on_a_revoked_plan_is_409_plan_revoked(
        self, auth_client_admin: TestClient, db_manager, seed_app, seed_plan
    ):
        r = auth_client_admin.patch(f"/plans/{seed_plan.id}", json={"revoked": True})
        assert r.status_code == 200, r.text

        r = self._mint(auth_client_admin, seed_app, seed_plan, "new-user-on-retired-plan")
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert detail["error"] == "plan_revoked"
        assert detail["plan_id"] == str(seed_plan.id)
        # Refused, not half-done: no licence row was written.
        assert db_manager.list_licenses(plan_id=seed_plan.id) == []

    def test_control_the_same_mint_on_an_active_plan_succeeds(
        self, auth_client_admin: TestClient, seed_app, seed_plan
    ):
        # Without this the 409 above could be any refusal of this request shape.
        r = self._mint(auth_client_admin, seed_app, seed_plan, "new-user-on-active-plan")
        assert r.status_code == 201, r.text

    def test_an_existing_active_licence_still_reads_back_after_the_plan_is_revoked(
        self, auth_client_admin: TestClient, full_seed_data
    ):
        app, plan, lic = full_seed_data["app"], full_seed_data["plan"], full_seed_data["license"]
        r = auth_client_admin.patch(f"/plans/{plan.id}", json={"revoked": True})
        assert r.status_code == 200, r.text

        # portal-bff POSTs on every session: the holder of a licence on a plan
        # retired AFTER it was issued must keep getting it back, not a 409.
        r = self._mint(auth_client_admin, app, plan, "test-user-sub")
        assert r.status_code == 201, r.text
        assert r.json()["id"] == str(lic.id)

    def test_a_new_member_of_a_tenant_already_on_the_retired_plan_is_onboarded(
        self, auth_client_admin: TestClient, db_manager, full_seed_data
    ):
        # The tenant holds an active licence on the plan (full_seed_data); the plan
        # is then retired. A NEW member of that tenant must still get a seat.
        app, plan = full_seed_data["app"], full_seed_data["plan"]
        assert auth_client_admin.patch(f"/plans/{plan.id}", json={"revoked": True}).is_success

        r = self._mint(auth_client_admin, app, plan, "brand-new-member-sub")
        assert r.status_code == 201, r.text
        assert r.json()["plan_id"] == str(plan.id)
        assert r.json()["id"] != str(full_seed_data["license"].id)

    def test_revoke_then_re_mint_on_the_retired_plan_succeeds(
        self, auth_client_admin: TestClient, db_manager, full_seed_data
    ):
        # CP's terms change: revoke the subject's licence, then mint again on the
        # SAME plan. At the re-mint the tenant holds no ACTIVE licence on it, so an
        # active-only exemption would leave the subject unlicensed.
        app, plan, lic = full_seed_data["app"], full_seed_data["plan"], full_seed_data["license"]
        assert auth_client_admin.patch(f"/plans/{plan.id}", json={"revoked": True}).is_success
        assert db_manager.revoke_license(lic.id)
        assert db_manager.list_licenses(plan_id=plan.id, status="active") == []

        r = self._mint(auth_client_admin, app, plan, "test-user-sub")
        assert r.status_code == 201, r.text
        assert r.json()["status"] == "active"
        assert r.json()["id"] != str(lic.id)

    def test_a_brand_new_tenant_on_the_retired_plan_is_409_even_while_another_tenant_holds_it(
        self, other_tenant_admin: TestClient, db_manager, full_seed_data
    ):
        # The exemption is per TENANT: SEED_TENANT_ID is on the plan, the other
        # tenant never was, so it may not join a retired plan.
        app, plan = full_seed_data["app"], full_seed_data["plan"]
        assert other_tenant_admin.patch(f"/plans/{plan.id}", json={"revoked": True}).is_success

        r = self._mint(other_tenant_admin, app, plan, "other-tenant-first-member")
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "plan_revoked"
        assert db_manager.list_licenses(plan_id=plan.id, tenant_id=SEED_OTHER_TENANT_ID) == []

    def test_a_licence_on_another_plan_of_the_same_app_does_not_exempt(
        self, auth_client_admin: TestClient, db_manager, full_seed_data
    ):
        # The exemption is per PLAN: the tenant holds an active licence on plan A
        # (trial) of this app, and has never been on plan B. Retiring B closes it
        # to this tenant too.
        app = full_seed_data["app"]
        plan_b = db_manager.create_plan(
            app_id=UUID(str(app.id)), code="retired-b", name="Retired B", duration_days=30
        )
        assert auth_client_admin.patch(f"/plans/{plan_b.id}", json={"revoked": True}).is_success

        r = self._mint(auth_client_admin, app, plan_b, "member-on-plan-b")
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "plan_revoked"

    def test_a_licence_on_the_plan_under_another_app_does_not_exempt(
        self, auth_client_admin: TestClient, db_manager, full_seed_data
    ):
        # The exemption is per APP as well: the tenant's history on this plan is
        # under `test-app`; a mint that names the same plan under a different app
        # has no history to lean on.
        plan = full_seed_data["plan"]
        other_app = db_manager.create_app(
            key="other-app", name="Other App", description="x", issuers=("platform",)
        )
        assert auth_client_admin.patch(f"/plans/{plan.id}", json={"revoked": True}).is_success

        r = self._mint(auth_client_admin, other_app, plan, "member-under-other-app")
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["error"] == "plan_revoked"

    def test_a_soft_deleted_licence_still_counts_as_history_on_the_plan(
        self, auth_client_admin: TestClient, db_manager, full_seed_data
    ):
        # Deliberate: a soft-deleted row is retained history, so the tenant is
        # still "on" the plan and may mint on it after it is retired.
        app, plan, subject = (
            full_seed_data["app"],
            full_seed_data["plan"],
            full_seed_data["subject"],
        )
        assert auth_client_admin.patch(f"/plans/{plan.id}", json={"revoked": True}).is_success
        assert (
            db_manager.soft_delete_licenses_for_subject(
                UUID(str(subject.id)), SEED_TENANT_ID, actor="test"
            )
            == 1
        )
        assert db_manager.list_licenses(plan_id=plan.id, tenant_id=SEED_TENANT_ID) == []
        assert (
            len(
                db_manager.list_licenses(
                    plan_id=plan.id, tenant_id=SEED_TENANT_ID, include_deleted=True
                )
            )
            == 1
        )

        r = self._mint(auth_client_admin, app, plan, "member-after-soft-delete")
        assert r.status_code == 201, r.text
