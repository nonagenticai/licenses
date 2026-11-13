"""``DELETE /licenses/user/{subject_id}`` owes billing the same teardowns a revoke does.

⛔ THE DEFECT. The route revoked (soft path) or destroyed (hard path) every licence
of a subject on a tenant, but only ``revoke_token_or_license`` ever spawned
``_teardown_quota_config`` / ``_teardown_token_budget``. Billing's ``license_sync``
token budgets for the deleted licences therefore stayed ACTIVE, and billing's
reconcile reported them as ``licence_budget_divergence_detected_not_repaired``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

import main
from models import Tenant

SEED_TENANT = "01000000-0000-7000-8000-000000000001"


@pytest.fixture
def registered_tenant(db_manager):
    session = db_manager.get_session()
    try:
        session.merge(Tenant(id=UUID(SEED_TENANT), name="teardown-test", owner_key="platform"))
        session.commit()
    finally:
        session.close()


def _kwargs(mock: AsyncMock) -> list[dict]:
    return [dict(c.kwargs) for c in mock.call_args_list]


class TestDeleteUserLicencesTearsDownBilling:
    @pytest.mark.parametrize("soft", [True, False], ids=["soft", "hard"])
    def test_every_deleted_licence_gets_both_teardowns(
        self,
        auth_client_admin: TestClient,
        monkeypatch,
        registered_tenant,
        seed_license,
        soft: bool,
    ) -> None:
        monkeypatch.setattr(main, "LICENSE_SOFT_DELETE", soft, raising=True)
        subject_id = UUID(str(seed_license.subject_id))
        expected = [{"license_id": str(seed_license.id), "tenant_id": SEED_TENANT}]

        with (
            patch("main._teardown_quota_config", new_callable=AsyncMock) as quota,
            patch("main._teardown_token_budget", new_callable=AsyncMock) as budget,
        ):
            response = auth_client_admin.delete(
                f"/licenses/user/{subject_id}", params={"confirm_licenses": 1}
            )

        assert response.status_code == 200, response.text
        assert response.json()["deleted"] == 1
        assert _kwargs(budget) == expected, (
            "the deleted licence's token budget was never torn down — billing keeps it "
            f"spendable; calls={budget.call_args_list}"
        )
        assert _kwargs(quota) == expected
        budget.assert_awaited_once()
        quota.assert_awaited_once()

    def test_a_refused_delete_tears_down_nothing(
        self, auth_client_admin: TestClient, monkeypatch, registered_tenant, seed_license
    ) -> None:
        """Scope control: an unconfirmed soft delete is a 409 that writes nothing,
        so it must cost billing nothing either."""
        monkeypatch.setattr(main, "LICENSE_SOFT_DELETE", True, raising=True)
        subject_id = UUID(str(seed_license.subject_id))
        with (
            patch("main._teardown_quota_config", new_callable=AsyncMock) as quota,
            patch("main._teardown_token_budget", new_callable=AsyncMock) as budget,
        ):
            response = auth_client_admin.delete(f"/licenses/user/{subject_id}")
        assert response.status_code == 409, response.text
        assert budget.call_count == 0 and quota.call_count == 0

    def test_another_tenants_licence_is_not_torn_down(
        self,
        auth_client_admin: TestClient,
        monkeypatch,
        registered_tenant,
        seed_license,
        seed_other_tenant_license,
    ) -> None:
        """The same subject holds a licence on ANOTHER tenant; deleting on this
        tenant must not send billing a teardown for that one."""
        monkeypatch.setattr(main, "LICENSE_SOFT_DELETE", True, raising=True)
        subject_id = UUID(str(seed_license.subject_id))
        with (
            patch("main._teardown_quota_config", new_callable=AsyncMock),
            patch("main._teardown_token_budget", new_callable=AsyncMock) as budget,
        ):
            response = auth_client_admin.delete(
                f"/licenses/user/{subject_id}", params={"confirm_licenses": 1}
            )
        assert response.status_code == 200, response.text
        ids = {c["license_id"] for c in _kwargs(budget)}
        assert ids == {str(seed_license.id)}
        assert str(seed_other_tenant_license.id) not in ids
