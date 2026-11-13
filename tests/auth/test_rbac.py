"""
Tests for role-based access control (RBAC).
"""

from fastapi.testclient import TestClient


class TestAdminRoleAccess:
    """Test that admin role has full access."""

    def test_admin_can_list_licenses(self, auth_client_admin: TestClient, full_seed_data):
        """Admin can list licenses."""
        response = auth_client_admin.get("/licenses")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        # Seeded license for tenant-1 should appear
        license_ids = [lic["id"] for lic in data]
        assert str(full_seed_data["license"].id) in license_ids

    def test_admin_can_create_license(self, auth_client_admin: TestClient, seed_app, seed_plan):
        """Admin can create licenses."""
        response = auth_client_admin.post(
            "/licenses/user/admin-test-user",
            json={
                "app_id": str(seed_app.id),
                "plan_id": str(seed_plan.id),
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "active"
        assert data["app_id"] == str(seed_app.id)
        assert data["tenant_id"] == "01000000-0000-7000-8000-000000000001"

    def test_admin_can_revoke_token(self, auth_client_admin: TestClient, full_seed_data):
        """Admin can revoke tokens."""
        response = auth_client_admin.post(
            "/license/revoke", json={"token": full_seed_data["raw_token"]}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["revoked"] is True
        assert "message" in data

    def test_admin_can_create_app(self, auth_client_admin: TestClient):
        """Admin can create apps."""
        response = auth_client_admin.post("/apps", json={"key": "admin-app", "name": "Admin App"})
        assert response.status_code == 201
        data = response.json()
        assert data["key"] == "admin-app"
        assert data["name"] == "Admin App"

    def test_admin_can_list_apps(self, auth_client_admin: TestClient, seed_app):
        """Admin can list apps."""
        response = auth_client_admin.get("/apps")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        app_keys = [a["key"] for a in data]
        assert seed_app.key in app_keys

    def test_admin_can_create_plan(self, auth_client_admin: TestClient, seed_app):
        """Admin can create plans."""
        response = auth_client_admin.post(
            "/plans",
            json={
                "app_id": str(seed_app.id),
                "code": "admin-plan",
                "name": "Admin Plan",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["code"] == "admin-plan"
        assert data["app_id"] == str(seed_app.id)

    def test_admin_can_create_region(self, auth_client_admin: TestClient):
        """Admin can create regions."""
        response = auth_client_admin.post(
            "/regions", json={"code": "admin-region", "name": "Admin Region"}
        )
        assert response.status_code == 201
        data = response.json()
        assert data["code"] == "admin-region"
        assert data["name"] == "Admin Region"

    def test_admin_can_list_regions(self, auth_client_admin: TestClient, seed_region):
        """Admin can list regions."""
        response = auth_client_admin.get("/regions")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        region_codes = [r["code"] for r in data]
        assert seed_region.code in region_codes


class TestManagerRoleAccess:
    """Test manager role permissions."""

    def test_manager_can_create_license(self, auth_client_manager: TestClient, seed_app, seed_plan):
        """Manager can create licenses."""
        response = auth_client_manager.post(
            "/licenses/user/manager-test-user",
            json={
                "app_id": str(seed_app.id),
                "plan_id": str(seed_plan.id),
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["status"] == "active"
        assert data["app_id"] == str(seed_app.id)

    def test_manager_can_revoke_token(self, auth_client_manager: TestClient, full_seed_data):
        """Manager can revoke tokens."""
        response = auth_client_manager.post(
            "/license/revoke", json={"token": full_seed_data["raw_token"]}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["revoked"] is True

    def test_manager_can_create_token(self, auth_client_manager: TestClient, full_seed_data):
        """Manager can create tokens."""
        license_id = str(full_seed_data["license"].id)
        response = auth_client_manager.post(f"/licenses/{license_id}/tokens")
        assert response.status_code == 201
        data = response.json()
        assert "token" in data
        assert data["token_type"] == "api"

    def test_manager_can_regenerate_token(self, auth_client_manager: TestClient, full_seed_data):
        """Manager can rotate a license's token (write role).

        CONTRACT CHANGE 2026-07-27: regenerate-token used to require
        license-admin OR license-**reader**, so a manager — the write role that
        may already mint tokens via POST /licenses/{id}/tokens — was refused
        while a read-only caller was allowed. The route now matches its sibling
        mint route. provisioning is the only production consumer and its
        `provisioning-backend` SA holds license-admin + license-manager +
        license-reader, so this breaks nothing.
        """
        license_id = full_seed_data["license"].id
        response = auth_client_manager.post(f"/licenses/{license_id}/regenerate-token")
        assert response.status_code == 200, response.text
        assert response.json()["token"]

    def test_manager_cannot_list_licenses(self, auth_client_manager: TestClient):
        """Manager cannot list all licenses (requires admin/reader)."""
        response = auth_client_manager.get("/licenses")
        assert response.status_code == 403
        data = response.json()
        assert "license-admin" in data["detail"]
        assert "license-reader" in data["detail"]

    def test_manager_cannot_list_apps(self, auth_client_manager: TestClient):
        """Manager cannot list apps."""
        response = auth_client_manager.get("/apps")
        assert response.status_code == 403
        data = response.json()
        assert "license-admin" in data["detail"]
        assert "license-reader" in data["detail"]

    def test_manager_cannot_create_app(self, auth_client_manager: TestClient):
        """Manager cannot create apps (admin only)."""
        response = auth_client_manager.post(
            "/apps", json={"key": "manager-app", "name": "Manager App"}
        )
        assert response.status_code == 403
        data = response.json()
        assert "license-admin" in data["detail"]

    def test_manager_cannot_create_plan(self, auth_client_manager: TestClient, seed_app):
        """Manager cannot create plans (admin only)."""
        response = auth_client_manager.post(
            "/plans",
            json={
                "app_id": str(seed_app.id),
                "code": "manager-plan",
                "name": "Manager Plan",
            },
        )
        assert response.status_code == 403
        data = response.json()
        assert "license-admin" in data["detail"]

    def test_manager_cannot_create_region(self, auth_client_manager: TestClient):
        """Manager cannot create regions (admin only)."""
        response = auth_client_manager.post(
            "/regions", json={"code": "manager-region", "name": "Manager Region"}
        )
        assert response.status_code == 403
        data = response.json()
        assert "license-admin" in data["detail"]


class TestReaderRoleAccess:
    """Test reader role permissions."""

    def test_reader_can_list_licenses(self, auth_client_reader: TestClient, full_seed_data):
        """Reader can list licenses."""
        response = auth_client_reader.get("/licenses")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

    def test_reader_can_list_user_licenses_but_only_for_own_tenant(
        self,
        auth_client_reader: TestClient,
        full_seed_data,
        seed_other_tenant_license,
    ):
        """Reader lists a user's licenses — scoped to the reader's own tenant.

        CONTRACT CHANGE 2026-07-27 (cross-tenant IDOR close). This asserted only
        ``200 and len >= 1`` before, which the pre-fix code satisfied by
        returning the subject's licenses on EVERY tenant.

        Note *why* this fixture may name a subject that is not itself: the
        shared ``auth_client_reader`` signs its token with ``azp=fr-backend``,
        a TRUSTED multi-tenant proxy (auth.config.trusted_tenant_proxy_clients),
        which is the delegation path the w2v/fr license gates use. The
        ordinary-caller rule (caller must BE the subject) is covered in
        tests/auth/test_authz_idor.py, which mints non-proxy tokens on purpose.
        """
        subject_id = full_seed_data["subject"].keycloak_sub
        # The same subject also holds a license on tenant ...0002.
        assert str(seed_other_tenant_license.tenant_id) == "01000000-0000-7000-8000-000000000002"

        response = auth_client_reader.get(f"/licenses/user/{subject_id}")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        # The other tenant's license must NOT be in the body.
        assert {lic["tenant_id"] for lic in data} == {"01000000-0000-7000-8000-000000000001"}

    def test_reader_can_list_apps(self, auth_client_reader: TestClient, seed_app):
        """Reader can list apps."""
        response = auth_client_reader.get("/apps")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        app_keys = [a["key"] for a in data]
        assert seed_app.key in app_keys

    def test_reader_can_get_app(self, auth_client_reader: TestClient, seed_app):
        """Reader can get app details."""
        response = auth_client_reader.get(f"/apps/{seed_app.id}")
        assert response.status_code == 200
        data = response.json()
        assert data["key"] == seed_app.key

    def test_reader_can_list_plans(self, auth_client_reader: TestClient, seed_plan):
        """Reader can list plans."""
        response = auth_client_reader.get("/plans")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

    def test_reader_can_list_regions(self, auth_client_reader: TestClient, seed_region):
        """Reader can list regions."""
        response = auth_client_reader.get("/regions")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

    def test_reader_cannot_regenerate_token(self, auth_client_reader: TestClient, full_seed_data):
        """A READ role must not perform a destructive credential rotation.

        CONTRACT CHANGE 2026-07-27: regenerate-token accepted `license-reader`
        while revoking EVERY existing token for the license and minting a
        replacement. The ownership check on that route is TENANT-level, so a
        reader could rotate a sibling license's tokens and silently invalidate
        another workload's credentials. It matters most because an opaque
        license token self-introspects to exactly ["license-reader"]
        (auth/middleware.py LICENSE_TOKEN_ROLES) — so a mere token BEARER could
        call it. Now requires a write role, i.e. a Keycloak JWT.
        """
        license_id = full_seed_data["license"].id
        response = auth_client_reader.post(f"/licenses/{license_id}/regenerate-token")
        assert response.status_code == 403
        assert "role" in response.json()["detail"].lower()

    def test_reader_cannot_create_license(
        self, auth_client_reader: TestClient, seed_app, seed_plan
    ):
        """Reader cannot create licenses."""
        response = auth_client_reader.post(
            "/licenses/user/reader-test-user",
            json={
                "app_id": str(seed_app.id),
                "plan_id": str(seed_plan.id),
            },
        )
        assert response.status_code == 403
        data = response.json()
        assert "role" in data["detail"].lower()

    def test_reader_cannot_revoke_token(self, auth_client_reader: TestClient, full_seed_data):
        """Reader cannot revoke tokens."""
        response = auth_client_reader.post(
            "/license/revoke", json={"token": full_seed_data["raw_token"]}
        )
        assert response.status_code == 403
        data = response.json()
        assert "role" in data["detail"].lower()

    def test_reader_cannot_create_token(self, auth_client_reader: TestClient, full_seed_data):
        """Reader cannot create tokens."""
        license_id = str(full_seed_data["license"].id)
        response = auth_client_reader.post(f"/licenses/{license_id}/tokens")
        assert response.status_code == 403
        data = response.json()
        assert "role" in data["detail"].lower()

    def test_reader_cannot_create_app(self, auth_client_reader: TestClient):
        """Reader cannot create apps."""
        response = auth_client_reader.post(
            "/apps", json={"key": "reader-app", "name": "Reader App"}
        )
        assert response.status_code == 403
        data = response.json()
        assert "role" in data["detail"].lower()

    def test_reader_cannot_create_plan(self, auth_client_reader: TestClient, seed_app):
        """Reader cannot create plans."""
        response = auth_client_reader.post(
            "/plans",
            json={
                "app_id": str(seed_app.id),
                "code": "reader-plan",
                "name": "Reader Plan",
            },
        )
        assert response.status_code == 403
        data = response.json()
        assert "role" in data["detail"].lower()

    def test_reader_cannot_create_region(self, auth_client_reader: TestClient):
        """Reader cannot create regions."""
        response = auth_client_reader.post(
            "/regions", json={"code": "reader-region", "name": "Reader Region"}
        )
        assert response.status_code == 403
        data = response.json()
        assert "role" in data["detail"].lower()


class TestNoRoleAccess:
    """Test that users with no roles get 403 everywhere."""

    def test_no_role_cannot_list_licenses(self, auth_client_no_role: TestClient):
        """User with no roles cannot list licenses."""
        response = auth_client_no_role.get("/licenses")
        assert response.status_code == 403
        data = response.json()
        assert "role" in data["detail"].lower()

    def test_no_role_cannot_create_license(
        self, auth_client_no_role: TestClient, seed_app, seed_plan
    ):
        """User with no roles cannot create licenses."""
        response = auth_client_no_role.post(
            "/licenses/user/norole-test-user",
            json={
                "app_id": str(seed_app.id),
                "plan_id": str(seed_plan.id),
            },
        )
        assert response.status_code == 403
        data = response.json()
        assert "role" in data["detail"].lower()

    def test_no_role_cannot_revoke_token(self, auth_client_no_role: TestClient, full_seed_data):
        """User with no roles cannot revoke tokens."""
        response = auth_client_no_role.post(
            "/license/revoke", json={"token": full_seed_data["raw_token"]}
        )
        assert response.status_code == 403
        data = response.json()
        assert "role" in data["detail"].lower()

    def test_no_role_cannot_list_apps(self, auth_client_no_role: TestClient):
        """User with no roles cannot list apps."""
        response = auth_client_no_role.get("/apps")
        assert response.status_code == 403
        data = response.json()
        assert "role" in data["detail"].lower()

    def test_no_role_cannot_list_plans(self, auth_client_no_role: TestClient):
        """User with no roles cannot list plans."""
        response = auth_client_no_role.get("/plans")
        assert response.status_code == 403
        data = response.json()
        assert "role" in data["detail"].lower()

    def test_no_role_cannot_list_regions(self, auth_client_no_role: TestClient):
        """User with no roles cannot list regions."""
        response = auth_client_no_role.get("/regions")
        assert response.status_code == 403
        data = response.json()
        assert "role" in data["detail"].lower()

    def test_no_role_cannot_create_app(self, auth_client_no_role: TestClient):
        """User with no roles cannot create apps."""
        response = auth_client_no_role.post(
            "/apps", json={"key": "norole-app", "name": "No Role App"}
        )
        assert response.status_code == 403
        data = response.json()
        assert "role" in data["detail"].lower()


class TestRBACErrorMessages:
    """Test that RBAC errors have clear messages."""

    def test_403_error_includes_required_role(
        self, auth_client_reader: TestClient, seed_app, seed_plan
    ):
        """403 errors should mention the required role."""
        response = auth_client_reader.post(
            "/licenses/user/test-user",
            json={
                "app_id": str(seed_app.id),
                "plan_id": str(seed_plan.id),
            },
        )
        assert response.status_code == 403
        data = response.json()
        # Should mention what roles are required
        assert "role" in data["detail"].lower()

    def test_admin_only_endpoint_mentions_admin(self, auth_client_manager: TestClient):
        """Admin-only endpoints should mention admin role in error."""
        response = auth_client_manager.post("/apps", json={"key": "test", "name": "Test"})
        assert response.status_code == 403
        data = response.json()
        assert "license-admin" in data["detail"]
