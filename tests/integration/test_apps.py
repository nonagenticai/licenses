"""Integration tests for licenses app endpoints."""

import uuid

import pytest

from tests.integration.conftest import delete_app_confirmed

pytestmark = pytest.mark.integration


class TestAppCreate:
    def test_create_app(self, licenses_client):
        key = f"inttest-{uuid.uuid4().hex[:8]}"
        r = licenses_client.post("/apps", json={"key": key, "name": f"Test App {key}"})
        assert r.status_code == 201, r.text
        data = r.json()
        assert data["key"] == key
        assert "id" in data

    def test_create_app_with_description(self, licenses_client):
        key = f"inttest-{uuid.uuid4().hex[:8]}"
        r = licenses_client.post(
            "/apps",
            json={
                "key": key,
                "name": f"Test App {key}",
                "description": "An integration test app",
            },
        )
        assert r.status_code == 201, r.text
        assert r.json()["description"] == "An integration test app"

    def test_create_app_missing_fields(self, licenses_client):
        r = licenses_client.post("/apps", json={})
        assert r.status_code == 422, r.text


class TestAppList:
    def test_list_apps(self, licenses_client):
        r = licenses_client.get("/apps")
        assert r.status_code == 200, r.text
        assert isinstance(r.json(), list)

    def test_list_apps_contains_created(self, licenses_client, test_app):
        r = licenses_client.get("/apps")
        assert r.status_code == 200, r.text
        ids = [a["id"] for a in r.json()]
        assert test_app["id"] in ids


class TestAppGet:
    def test_get_app_by_id(self, licenses_client, test_app):
        r = licenses_client.get(f"/apps/{test_app['id']}")
        assert r.status_code == 200, r.text
        assert r.json()["id"] == test_app["id"]

    def test_get_app_by_key(self, licenses_client, test_app):
        r = licenses_client.get(f"/apps/{test_app['key']}")
        assert r.status_code == 200, r.text
        assert r.json()["key"] == test_app["key"]

    def test_get_nonexistent_app(self, licenses_client):
        r = licenses_client.get(f"/apps/{uuid.uuid4()}")
        assert r.status_code in (404, 500), r.text


class TestAppPlans:
    def test_list_app_plans(self, licenses_client, test_app, test_plan):
        r = licenses_client.get(f"/apps/{test_app['id']}/plans")
        assert r.status_code == 200, r.text
        plans = r.json()
        assert isinstance(plans, list)
        ids = [p["id"] for p in plans]
        assert test_plan["id"] in ids


class TestAppLicenses:
    def test_list_app_licenses(self, licenses_client, test_app, test_license):
        r = licenses_client.get(f"/apps/{test_app['id']}/licenses")
        assert r.status_code == 200, r.text
        lics = r.json()
        assert isinstance(lics, list)
        ids = [lic["id"] for lic in lics]
        assert test_license["id"] in ids


class TestAppDelete:
    @staticmethod
    def _app_with_licence(licenses_client):
        """Create an app + plan + licence, so a delete has a real blast radius."""
        key = f"inttest-del-{uuid.uuid4().hex[:8]}"
        app = licenses_client.post("/apps", json={"key": key, "name": f"Del App {key}"}).json()
        plan = licenses_client.post(
            "/plans",
            json={"app_id": app["id"], "code": "del-plan", "name": "Del Plan"},
        ).json()
        subj = f"del-subj-{uuid.uuid4().hex[:8]}"
        r = licenses_client.post(
            f"/licenses/user/{subj}",
            json={"app_id": app["id"], "plan_id": plan["id"]},
        )
        # Asserted, not assumed: an unseeded licence would turn both tests below
        # into vacuous passes -- the 409 case would quietly become a 204 case.
        assert r.status_code == 201, r.text
        return app

    def test_delete_app_unconfirmed_refuses(self, licenses_client):
        """An unconfirmed cascade that would destroy licences REFUSES with 409.

        This is the entire point of the guard. `app` is a GLOBAL catalogue row
        (no tenant_id) and `license.app_id` is ON DELETE CASCADE, so this delete
        reaches EVERY tenant's licences, and the service has no audit table --
        the refusal body is the only chance an operator gets to see the radius.
        Nothing else pins the refusal, so without this a refactor can silently
        restore the unguarded 204.
        """
        app = self._app_with_licence(licenses_client)
        try:
            r = licenses_client.delete(f"/apps/{app['id']}")
            assert r.status_code == 409, r.text

            detail = r.json()["detail"]
            assert detail["error"] == "cascade_requires_confirmation", detail
            assert detail["app_id"] == app["id"], detail
            assert detail["licenses"] >= 1, detail
            assert detail["plans"] >= 1, detail
            assert detail["tenants"], detail
            assert f"confirm_licenses={detail['licenses']}" in detail["remedy"], detail

            # A refusal must destroy nothing.
            assert licenses_client.get(f"/apps/{app['id']}").status_code == 200
        finally:
            delete_app_confirmed(licenses_client, app["id"])

    def test_delete_app_cascades(self, licenses_client):
        """DELETE /apps/{id} removes app, its plans, licenses, and tokens.

        The cascade still has to reach licences -- only the call had to change.
        The count is derived the way a real caller derives it, from the radius
        the unconfirmed 409 reports, rather than hardcoded.
        """
        app = self._app_with_licence(licenses_client)

        radius = licenses_client.delete(f"/apps/{app['id']}").json()["detail"]
        assert radius["licenses"] >= 1, radius
        assert radius["plans"] >= 1, radius

        r = licenses_client.delete(f"/apps/{app['id']}?confirm_licenses={radius['licenses']}")
        assert r.status_code == 204, r.text

        # Verify app is gone
        r = licenses_client.get(f"/apps/{app['id']}")
        assert r.status_code == 404, r.text

        # ...and that the cascade actually reached the licences it counted.
        r = licenses_client.get("/licenses", params={"app_id": app["id"]})
        assert r.status_code == 200, r.text
        assert r.json() == [], r.text

    def test_delete_nonexistent_app(self, licenses_client):
        r = licenses_client.delete(f"/apps/{uuid.uuid4()}")
        assert r.status_code == 404, r.text
