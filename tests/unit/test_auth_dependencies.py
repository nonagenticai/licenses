"""
Unit tests for auth dependency functions.

Tests get_current_user(), require_role(), require_any_role(), require_all_roles().
"""

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from auth.dependencies import (
    get_current_user,
    require_all_roles,
    require_any_role,
    require_internal_or_reader,
    require_internal_service,
    require_role,
)
from auth.models import KeycloakUser


def create_mock_request(user: KeycloakUser | None = None) -> MagicMock:
    """Create a mock request with optional user."""
    mock_request = MagicMock()
    mock_request.state = MagicMock()

    if user is not None:
        mock_request.state.user = user
    else:
        # Delete user attribute to simulate unauthenticated request
        del mock_request.state.user

    return mock_request


class TestGetCurrentUser:
    """Tests for get_current_user dependency."""

    def test_returns_user_when_authenticated(self, admin_user):
        """Test that get_current_user returns the user when authenticated."""
        mock_request = create_mock_request(admin_user)

        result = get_current_user(mock_request)

        assert result == admin_user
        assert result.sub == admin_user.sub
        assert result.preferred_username == admin_user.preferred_username

    def test_raises_401_when_not_authenticated(self):
        """Test that get_current_user raises 401 when not authenticated."""
        mock_request = create_mock_request(user=None)

        with pytest.raises(HTTPException) as exc_info:
            get_current_user(mock_request)

        assert exc_info.value.status_code == 401
        assert "Authentication required" in exc_info.value.detail

    def test_returns_different_users(self, admin_user, reader_user):
        """Test that different users are returned correctly."""
        admin_request = create_mock_request(admin_user)
        reader_request = create_mock_request(reader_user)

        admin_result = get_current_user(admin_request)
        reader_result = get_current_user(reader_request)

        assert admin_result.sub != reader_result.sub
        assert admin_result.preferred_username == "admin"
        assert reader_result.preferred_username == "reader"


class TestRequireRole:
    """Tests for require_role dependency factory."""

    def test_allows_user_with_required_role(self, admin_user):
        """Test that user with required role is allowed."""
        mock_request = create_mock_request(admin_user)
        checker = require_role("license-admin")

        result = checker(mock_request)

        assert result == admin_user

    def test_denies_user_without_required_role(self, reader_user):
        """Test that user without required role is denied."""
        mock_request = create_mock_request(reader_user)
        checker = require_role("license-admin")

        with pytest.raises(HTTPException) as exc_info:
            checker(mock_request)

        assert exc_info.value.status_code == 403
        assert "license-admin" in exc_info.value.detail

    def test_denies_unauthenticated_user(self):
        """Test that unauthenticated user is denied."""
        mock_request = create_mock_request(user=None)
        checker = require_role("license-admin")

        with pytest.raises(HTTPException) as exc_info:
            checker(mock_request)

        assert exc_info.value.status_code == 401

    def test_denies_user_with_no_roles(self, no_role_user):
        """Test that user with no roles is denied."""
        mock_request = create_mock_request(no_role_user)
        checker = require_role("license-reader")

        with pytest.raises(HTTPException) as exc_info:
            checker(mock_request)

        assert exc_info.value.status_code == 403

    def test_different_roles(self, admin_user, manager_user, reader_user):
        """Test require_role with different role requirements."""
        admin_checker = require_role("license-admin")
        manager_checker = require_role("license-manager")
        reader_checker = require_role("license-reader")

        # Admin can access admin-only
        admin_result = admin_checker(create_mock_request(admin_user))
        assert admin_result.sub == admin_user.sub

        # Manager can access manager-only
        manager_result = manager_checker(create_mock_request(manager_user))
        assert manager_result.sub == manager_user.sub

        # Reader can access reader-only
        reader_result = reader_checker(create_mock_request(reader_user))
        assert reader_result.sub == reader_user.sub

        # Admin cannot access manager-only (they don't have that role)
        with pytest.raises(HTTPException) as exc_info:
            manager_checker(create_mock_request(admin_user))
        assert exc_info.value.status_code == 403


class TestRequireAnyRole:
    """Tests for require_any_role dependency factory."""

    def test_allows_user_with_first_role(self, admin_user):
        """Test user with first required role is allowed."""
        mock_request = create_mock_request(admin_user)
        checker = require_any_role("license-admin", "license-manager")

        result = checker(mock_request)

        assert result == admin_user

    def test_allows_user_with_second_role(self, manager_user):
        """Test user with second required role is allowed."""
        mock_request = create_mock_request(manager_user)
        checker = require_any_role("license-admin", "license-manager")

        result = checker(mock_request)

        assert result == manager_user

    def test_denies_user_with_neither_role(self, reader_user):
        """Test user without any required role is denied."""
        mock_request = create_mock_request(reader_user)
        checker = require_any_role("license-admin", "license-manager")

        with pytest.raises(HTTPException) as exc_info:
            checker(mock_request)

        assert exc_info.value.status_code == 403
        assert "license-admin" in exc_info.value.detail
        assert "license-manager" in exc_info.value.detail

    def test_denies_unauthenticated_user(self):
        """Test unauthenticated user is denied."""
        mock_request = create_mock_request(user=None)
        checker = require_any_role("license-admin", "license-reader")

        with pytest.raises(HTTPException) as exc_info:
            checker(mock_request)

        assert exc_info.value.status_code == 401

    def test_with_single_role(self, admin_user):
        """Test require_any_role with single role behaves like require_role."""
        mock_request = create_mock_request(admin_user)
        checker = require_any_role("license-admin")

        result = checker(mock_request)

        assert result == admin_user

    def test_with_three_roles(self, reader_user):
        """Test require_any_role with three possible roles."""
        mock_request = create_mock_request(reader_user)
        checker = require_any_role("license-admin", "license-manager", "license-reader")

        result = checker(mock_request)

        assert result == reader_user


class TestRequireAllRoles:
    """Tests for require_all_roles dependency factory."""

    def test_allows_user_with_all_roles(self):
        """Test user with all required roles is allowed."""
        multi_role_user = KeycloakUser(
            sub="multi-role-uuid",
            preferred_username="multirole",
            realm_access={"roles": ["license-admin", "license-manager", "license-reader"]},
        )
        mock_request = create_mock_request(multi_role_user)
        checker = require_all_roles("license-admin", "license-manager")

        result = checker(mock_request)

        assert result == multi_role_user

    def test_denies_user_missing_one_role(self, admin_user):
        """Test user missing one role is denied."""
        mock_request = create_mock_request(admin_user)
        checker = require_all_roles("license-admin", "license-manager")

        with pytest.raises(HTTPException) as exc_info:
            checker(mock_request)

        assert exc_info.value.status_code == 403
        assert "license-manager" in exc_info.value.detail

    def test_denies_user_with_no_required_roles(self, no_role_user):
        """Test user with none of the required roles is denied."""
        mock_request = create_mock_request(no_role_user)
        checker = require_all_roles("license-admin", "license-reader")

        with pytest.raises(HTTPException) as exc_info:
            checker(mock_request)

        assert exc_info.value.status_code == 403
        # Both missing roles should be mentioned
        assert "license-admin" in exc_info.value.detail
        assert "license-reader" in exc_info.value.detail

    def test_denies_unauthenticated_user(self):
        """Test unauthenticated user is denied."""
        mock_request = create_mock_request(user=None)
        checker = require_all_roles("license-admin")

        with pytest.raises(HTTPException) as exc_info:
            checker(mock_request)

        assert exc_info.value.status_code == 401

    def test_with_single_role(self, admin_user):
        """Test require_all_roles with single role behaves like require_role."""
        mock_request = create_mock_request(admin_user)
        checker = require_all_roles("license-admin")

        result = checker(mock_request)

        assert result == admin_user

    def test_reports_all_missing_roles(self, reader_user):
        """Test that all missing roles are reported in error."""
        mock_request = create_mock_request(reader_user)
        checker = require_all_roles("license-admin", "license-manager", "special-role")

        with pytest.raises(HTTPException) as exc_info:
            checker(mock_request)

        error_detail = exc_info.value.detail
        assert "license-admin" in error_detail
        assert "license-manager" in error_detail
        assert "special-role" in error_detail


class TestEffectiveTenantId:
    """Tests for the effective_tenant_id property."""

    def test_effective_tenant_id_uses_tenant_id(self):
        """effective_tenant_id returns tenant_id when set."""
        user = KeycloakUser(
            sub="user-sub",
            preferred_username="test",
            tenant_id="explicit-tenant",
        )
        assert user.effective_tenant_id == "explicit-tenant"

    def test_effective_tenant_id_falls_back_to_sub(self):
        """effective_tenant_id returns sub when tenant_id is None."""
        user = KeycloakUser(
            sub="user-sub-fallback",
            preferred_username="test",
            tenant_id=None,
        )
        assert user.effective_tenant_id == "user-sub-fallback"

    def test_effective_tenant_id_empty_string_falls_back(self):
        """effective_tenant_id returns sub when tenant_id is empty string."""
        user = KeycloakUser(
            sub="user-sub",
            preferred_username="test",
            tenant_id="",
        )
        # Empty string is falsy, so falls back to sub
        assert user.effective_tenant_id == "user-sub"


class TestKeycloakUserModel:
    """Tests for KeycloakUser model methods."""

    def test_has_realm_role_true(self, admin_user):
        """Test has_realm_role returns True when user has role."""
        assert admin_user.has_realm_role("license-admin") is True

    def test_has_realm_role_false(self, reader_user):
        """Test has_realm_role returns False when user lacks role."""
        assert reader_user.has_realm_role("license-admin") is False

    def test_has_client_role_true(self):
        """Test has_client_role returns True when user has client role."""
        user = KeycloakUser(
            sub="test-user",
            preferred_username="test",
            resource_access={"my-client": {"roles": ["client-admin"]}},
        )
        assert user.has_client_role("my-client", "client-admin") is True

    def test_has_client_role_false_wrong_client(self):
        """Test has_client_role returns False for wrong client."""
        user = KeycloakUser(
            sub="test-user",
            preferred_username="test",
            resource_access={"my-client": {"roles": ["client-admin"]}},
        )
        assert user.has_client_role("other-client", "client-admin") is False

    def test_has_client_role_false_wrong_role(self):
        """Test has_client_role returns False for wrong role."""
        user = KeycloakUser(
            sub="test-user",
            preferred_username="test",
            resource_access={"my-client": {"roles": ["client-admin"]}},
        )
        assert user.has_client_role("my-client", "other-role") is False

    def test_get_all_roles(self):
        """Test get_all_roles returns all realm and client roles."""
        user = KeycloakUser(
            sub="test-user",
            preferred_username="test",
            realm_access={"roles": ["realm-role-1", "realm-role-2"]},
            resource_access={
                "client-a": {"roles": ["client-a-role"]},
                "client-b": {"roles": ["client-b-role-1", "client-b-role-2"]},
            },
        )

        all_roles = user.get_all_roles()

        assert "realm-role-1" in all_roles
        assert "realm-role-2" in all_roles
        assert "client-a-role" in all_roles
        assert "client-b-role-1" in all_roles
        assert "client-b-role-2" in all_roles
        assert len(all_roles) == 5

    def test_get_all_roles_empty(self):
        """Test get_all_roles with no roles."""
        user = KeycloakUser(
            sub="test-user",
            preferred_username="test",
            realm_access={},
            resource_access={},
        )

        all_roles = user.get_all_roles()

        assert all_roles == []

    def test_require_role_method_success(self, admin_user):
        """Test KeycloakUser.require_role succeeds with correct role."""
        # Should not raise
        admin_user.require_role("license-admin")

    def test_require_role_method_failure(self, reader_user):
        """Test KeycloakUser.require_role raises with missing role."""
        with pytest.raises(HTTPException) as exc_info:
            reader_user.require_role("license-admin")

        assert exc_info.value.status_code == 403


class TestRequireInternalService:
    """Tests for require_internal_service dependency."""

    def test_rejects_retired_metering_internal_role(self):
        """Retirement regression: the legacy `metering-internal` role is no
        longer accepted after the fleet rename to `service-internal`.
        """
        user = KeycloakUser(
            sub="legacy-svc",
            preferred_username="legacy-service",
            realm_access={"roles": ["metering-internal"]},
        )
        with pytest.raises(HTTPException) as exc_info:
            require_internal_service(create_mock_request(user))
        assert exc_info.value.status_code == 403

    def test_allows_service_internal_role(self):
        """Rotation: the new shared `service-internal` role is accepted."""
        user = KeycloakUser(
            sub="service-svc",
            preferred_username="service-account",
            realm_access={"roles": ["service-internal"]},
        )
        result = require_internal_service(create_mock_request(user))
        assert result == user

    def test_allows_onboarding_internal_role(self):
        user = KeycloakUser(
            sub="onboarding-svc",
            preferred_username="onboarding-service",
            realm_access={"roles": ["onboarding-internal"]},
        )
        result = require_internal_service(create_mock_request(user))
        assert result == user

    def test_allows_license_admin_role(self, admin_user):
        result = require_internal_service(create_mock_request(admin_user))
        assert result == admin_user

    def test_denies_reader_role(self, reader_user):
        with pytest.raises(HTTPException) as exc_info:
            require_internal_service(create_mock_request(reader_user))
        assert exc_info.value.status_code == 403

    def test_denies_unauthenticated(self):
        with pytest.raises(HTTPException) as exc_info:
            require_internal_service(create_mock_request(user=None))
        assert exc_info.value.status_code == 401


class TestRequireInternalOrReader:
    """Tests for require_internal_or_reader — the read-gate for GET /licenses/active.

    Accepts the internal-service roles OR license-reader, without widening
    INTERNAL_SERVICE_ROLES (which guards the /internal/licenses* listing surface).
    """

    def test_allows_license_reader_role(self, reader_user):
        """license-reader (a pure-read role) is accepted for the active-status read."""
        result = require_internal_or_reader(create_mock_request(reader_user))
        assert result == reader_user

    def test_allows_service_internal_role(self):
        user = KeycloakUser(
            sub="service-svc",
            preferred_username="service-account",
            realm_access={"roles": ["service-internal"]},
        )
        result = require_internal_or_reader(create_mock_request(user))
        assert result == user

    def test_allows_license_admin_role(self, admin_user):
        result = require_internal_or_reader(create_mock_request(admin_user))
        assert result == admin_user

    def test_allows_internal_role_via_client_roles(self):
        """Preserves require_internal_service semantics: an internal role carried
        as a CLIENT role (resource_access), not a realm role, is still accepted."""
        user = KeycloakUser(
            sub="client-svc",
            preferred_username="client-service",
            realm_access={"roles": []},
            resource_access={"some-client": {"roles": ["service-internal"]}},
        )
        result = require_internal_or_reader(create_mock_request(user))
        assert result == user

    def test_denies_unrelated_role(self):
        """A caller with neither an internal role nor license-reader is 403."""
        user = KeycloakUser(
            sub="nobody",
            preferred_username="no-license-roles",
            realm_access={"roles": ["some-other-role"]},
        )
        with pytest.raises(HTTPException) as exc_info:
            require_internal_or_reader(create_mock_request(user))
        assert exc_info.value.status_code == 403

    def test_denies_unauthenticated(self):
        with pytest.raises(HTTPException) as exc_info:
            require_internal_or_reader(create_mock_request(user=None))
        assert exc_info.value.status_code == 401
