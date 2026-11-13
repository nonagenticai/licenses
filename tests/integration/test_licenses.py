"""Integration tests for licenses license endpoints."""

import uuid

import pytest

from tests.integration.conftest import delete_app_confirmed

pytestmark = pytest.mark.integration


class TestLicenseList:
    def test_list_licenses(self, licenses_client):
        r = licenses_client.get("/licenses")
        assert r.status_code == 200, r.text
        assert isinstance(r.json(), list)

    def test_list_licenses_filter_by_status(self, licenses_client, test_license):
        r = licenses_client.get("/licenses", params={"status": "active"})
        assert r.status_code == 200, r.text
        assert isinstance(r.json(), list)

    def test_list_licenses_filter_by_app(self, licenses_client, test_app, test_license):
        r = licenses_client.get("/licenses", params={"app_id": test_app["id"]})
        assert r.status_code == 200, r.text
        lics = r.json()
        assert isinstance(lics, list)
        ids = [lic["id"] for lic in lics]
        assert test_license["id"] in ids


class TestCreateUserLicense:
    def test_create_license(self, licenses_client, test_app, test_plan):
        subject_id = f"test-subj-{uuid.uuid4().hex[:8]}"
        r = licenses_client.post(
            f"/licenses/user/{subject_id}",
            json={"app_id": test_app["id"], "plan_id": test_plan["id"]},
        )
        assert r.status_code == 201, r.text
        data = r.json()
        # subject_id in response is the internal Subject UUID, not the input string
        assert "subject_id" in data
        assert data["app_id"] == test_app["id"]
        assert data["plan_id"] == test_plan["id"]
        licenses_client.post("/license/revoke", json={"license_id": data["id"]})

    def test_create_license_with_metadata(self, licenses_client, test_app, test_plan):
        subject_id = f"test-subj-{uuid.uuid4().hex[:8]}"
        r = licenses_client.post(
            f"/licenses/user/{subject_id}",
            json={
                "app_id": test_app["id"],
                "plan_id": test_plan["id"],
                "metadata": {"source": "integration-test"},
            },
        )
        assert r.status_code == 201, r.text
        assert r.json()["metadata"] == {"source": "integration-test"}
        licenses_client.post("/license/revoke", json={"license_id": r.json()["id"]})

    def test_create_license_invalid_app(self, licenses_client, test_plan):
        r = licenses_client.post(
            f"/licenses/user/test-subj-{uuid.uuid4().hex[:8]}",
            json={"app_id": str(uuid.uuid4()), "plan_id": test_plan["id"]},
        )
        assert r.status_code in (400, 404, 500), r.text


class TestListUserLicenses:
    def test_list_user_licenses(self, licenses_client, test_license):
        subject_id = test_license["subject_id"]
        r = licenses_client.get(f"/licenses/user/{subject_id}")
        assert r.status_code == 200, r.text
        lics = r.json()
        assert isinstance(lics, list)
        ids = [lic["id"] for lic in lics]
        assert test_license["id"] in ids

    def test_list_user_licenses_nonexistent(self, licenses_client):
        r = licenses_client.get(f"/licenses/user/nonexistent-{uuid.uuid4().hex[:8]}")
        # API returns 404 for unknown subjects (not empty list)
        assert r.status_code == 404, r.text


class TestDeleteUserLicenses:
    def test_delete_user_licenses(self, licenses_client, test_app, test_plan):
        """DELETE /licenses/user/{subject_id} hard-deletes all licenses for subject."""
        subj = f"del-subj-{uuid.uuid4().hex[:8]}"
        # At most one ACTIVE license per (tenant, app, subject) is enforced
        # (idempotent provisioning + the uq_license_active_tenant_app_subject
        # partial index), so two identical POSTs yield ONE license. To exercise
        # deleting MULTIPLE licenses for a subject, provision across two apps.
        r1 = licenses_client.post(
            f"/licenses/user/{subj}",
            json={"app_id": test_app["id"], "plan_id": test_plan["id"]},
        )
        assert r1.status_code == 201, r1.text

        key2 = f"inttest-{uuid.uuid4().hex[:8]}"
        a2 = licenses_client.post("/apps", json={"key": key2, "name": f"App {key2}"})
        assert a2.status_code == 201, a2.text
        app2 = a2.json()
        try:
            code2 = f"plan-{uuid.uuid4().hex[:8]}"
            p2 = licenses_client.post(
                "/plans", json={"app_id": app2["id"], "code": code2, "name": f"Plan {code2}"}
            )
            assert p2.status_code == 201, p2.text
            r2 = licenses_client.post(
                f"/licenses/user/{subj}",
                json={"app_id": app2["id"], "plan_id": p2.json()["id"]},
            )
            assert r2.status_code == 201, r2.text

            # Soft delete is the DEFAULT as of this branch, so the endpoint refuses
            # an unconfirmed delete with 409 and names its own blast radius. Read
            # the count back rather than hardcoding it: this test's subject is the
            # DELETE of two licences, not the confirmation arithmetic.
            radius = licenses_client.delete(f"/licenses/user/{subj}")
            assert radius.status_code == 409, radius.text
            count = radius.json()["detail"]["licenses"]
            r = licenses_client.delete(f"/licenses/user/{subj}?confirm_licenses={count}")
            assert r.status_code == 200, r.text
            assert r.json()["deleted"] == 2
        finally:
            delete_app_confirmed(licenses_client, app2["id"])

    def test_delete_user_licenses_nonexistent(self, licenses_client):
        r = licenses_client.delete(f"/licenses/user/nonexistent-{uuid.uuid4().hex[:8]}")
        assert r.status_code == 404, r.text
