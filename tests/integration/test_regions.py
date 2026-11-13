"""Integration tests for licenses region endpoints."""

import uuid

import pytest

pytestmark = pytest.mark.integration


class TestRegionCreate:
    def test_create_region(self, licenses_client):
        code = f"test-{uuid.uuid4().hex[:8]}"
        r = licenses_client.post("/regions", json={"code": code, "name": f"Test Region {code}"})
        assert r.status_code == 201, r.text
        data = r.json()
        assert data["code"] == code
        assert "id" in data

    def test_create_region_inactive(self, licenses_client):
        code = f"test-{uuid.uuid4().hex[:8]}"
        r = licenses_client.post(
            "/regions",
            json={"code": code, "name": f"Inactive Region {code}", "active": False},
        )
        assert r.status_code == 201, r.text
        assert r.json()["active"] is False

    def test_create_region_missing_fields(self, licenses_client):
        r = licenses_client.post("/regions", json={})
        assert r.status_code == 422, r.text


class TestRegionList:
    def test_list_regions(self, licenses_client):
        r = licenses_client.get("/regions")
        assert r.status_code == 200, r.text
        assert isinstance(r.json(), list)

    def test_list_regions_active_only(self, licenses_client):
        r = licenses_client.get("/regions", params={"active_only": True})
        assert r.status_code == 200, r.text
        regions = r.json()
        assert isinstance(regions, list)
        # All returned regions should be active
        for region in regions:
            assert region.get("active") is True
