"""Integration tests for licenses plan endpoints."""

import uuid

import pytest

pytestmark = pytest.mark.integration


class TestPlanCreate:
    def test_create_plan(self, licenses_client, test_app):
        code = f"plan-{uuid.uuid4().hex[:8]}"
        r = licenses_client.post(
            "/plans",
            json={
                "app_id": test_app["id"],
                "code": code,
                "name": f"Test Plan {code}",
            },
        )
        assert r.status_code == 201, r.text
        data = r.json()
        assert data["code"] == code
        assert data["app_id"] == test_app["id"]

    def test_create_plan_with_region(self, licenses_client, test_app, test_region):
        code = f"plan-{uuid.uuid4().hex[:8]}"
        r = licenses_client.post(
            "/plans",
            json={
                "app_id": test_app["id"],
                "code": code,
                "name": f"Regional Plan {code}",
                "region_id": test_region["id"],
                "duration_days": 30,
            },
        )
        assert r.status_code == 201, r.text
        data = r.json()
        assert data["region_id"] == test_region["id"]
        assert data["duration_days"] == 30

    def test_create_plan_missing_fields(self, licenses_client):
        r = licenses_client.post("/plans", json={})
        assert r.status_code == 422, r.text


class TestPlanList:
    def test_list_plans(self, licenses_client):
        r = licenses_client.get("/plans")
        assert r.status_code == 200, r.text
        assert isinstance(r.json(), list)

    def test_list_plans_filter_by_app(self, licenses_client, test_app, test_plan):
        r = licenses_client.get("/plans", params={"app_id": test_app["id"]})
        assert r.status_code == 200, r.text
        plans = r.json()
        assert isinstance(plans, list)
        assert all(p["app_id"] == test_app["id"] for p in plans)


class TestPlanGet:
    def test_get_plan_by_id(self, licenses_client, test_plan):
        r = licenses_client.get(f"/plans/{test_plan['id']}")
        assert r.status_code == 200, r.text
        assert r.json()["id"] == test_plan["id"]

    def test_get_nonexistent_plan(self, licenses_client):
        r = licenses_client.get(f"/plans/{uuid.uuid4()}")
        assert r.status_code in (404, 500), r.text


class TestPlanDelete:
    def test_delete_plan(self, licenses_client, test_app):
        """DELETE /plans/{id} succeeds when no licenses reference the plan."""
        code = f"plan-del-{uuid.uuid4().hex[:8]}"
        plan = licenses_client.post(
            "/plans",
            json={"app_id": test_app["id"], "code": code, "name": f"Del Plan {code}"},
        ).json()

        r = licenses_client.delete(f"/plans/{plan['id']}")
        assert r.status_code == 204, r.text

        r = licenses_client.get(f"/plans/{plan['id']}")
        assert r.status_code == 404, r.text

    def test_delete_plan_conflict(self, licenses_client, test_app, test_plan, test_license):
        """DELETE /plans/{id} returns 409 when licenses still reference it."""
        r = licenses_client.delete(f"/plans/{test_plan['id']}")
        assert r.status_code == 409, r.text

    def test_delete_nonexistent_plan(self, licenses_client):
        r = licenses_client.delete(f"/plans/{uuid.uuid4()}")
        assert r.status_code == 404, r.text
