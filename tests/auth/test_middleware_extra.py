"""
Additional middleware tests to cover remaining uncovered lines.

Covers:
- X-Tenant-Id only fallback (lines 116-122)
- JWT without sub claim (line 158)
- JWT decode exception (lines 172-173)
- Introspect non-200 status (lines 211-212)
- Introspect inactive token (lines 216-217)
"""

import base64
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from fastapi.testclient import TestClient

from auth.middleware import AuthMiddleware
from tests.support.jwt_keys import TEST_ISSUER, private_key_pem


def _b64url(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def _mock_jwt(payload: dict) -> str:
    """A REAL RS256-signed token — the middleware verifies signatures now.

    Defaults exp/iss so callers only supply the claims under test.
    """
    claims = {
        "exp": int(time.time()) + 3600,
        "iat": int(time.time()),
        "iss": TEST_ISSUER,
        **payload,
    }
    return jwt.encode(claims, private_key_pem(), algorithm="RS256")


class TestExtractUserFromJwtClaims:
    """_extract_user_from_jwt must surface the tenant_id / organization_id
    claims so get_tenant_id/require_tenant_id use them instead of the subject.
    Pure unit test (no DB, no TestClient)."""

    def _mw(self) -> AuthMiddleware:
        return AuthMiddleware(app=MagicMock())

    def test_populates_tenant_id_claim(self) -> None:
        token = _mock_jwt(
            {
                "sub": "e148e8af-3751-49bf-a0fa-56ab46d31ddc",
                "tenant_id": "0195f48b-d000-7000-8000-000000000003",
                "organization_id": "org-1",
            }
        )
        user = self._mw()._extract_user_from_jwt(token)
        assert user is not None
        assert user.tenant_id == "0195f48b-d000-7000-8000-000000000003"
        assert user.organization_id == "org-1"
        # effective_tenant_id must be the claim, NOT the subject
        assert user.effective_tenant_id == "0195f48b-d000-7000-8000-000000000003"

    def test_no_tenant_claim_leaves_tenant_id_none(self) -> None:
        token = _mock_jwt({"sub": "a-user-sub"})
        user = self._mw()._extract_user_from_jwt(token)
        assert user is not None
        assert user.tenant_id is None
        # Falls back to sub only via effective_tenant_id (best-effort scoping)
        assert user.effective_tenant_id == "a-user-sub"


class TestMiddlewareTenantIdOnlyFallback:
    """Stage 2 hole `b2`: a no-token + X-Tenant-Id request is now rejected at auth.

    Previously the header-only path minted a roleless User (which every route's
    role dep then 403'd). That roleless-user fallback was removed — a protected
    request with no Bearer token now fails closed with 401 before any route runs.
    """

    def test_tenant_id_header_only_is_rejected_401(self, db_manager):
        """X-Tenant-Id with no Authorization → 401 (no roleless-user fallback)."""
        import main

        with patch("main.db_manager", db_manager):
            with TestClient(main.app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/licenses",
                    headers={"X-Tenant-Id": "01000000-0000-7000-8000-000000000011"},
                )
                assert response.status_code == 401

    def test_tenant_id_only_no_minimal_user_minted(self, db_manager):
        """No-token header-only request never reaches a route (401 at middleware)."""
        import main

        with patch("main.db_manager", db_manager):
            with TestClient(main.app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/licenses",
                    headers={"X-Tenant-Id": "01000000-0000-7000-8000-000000000012"},
                )
                assert response.status_code == 401


class TestMiddlewareJWTWithoutSub:
    """Test JWT token without 'sub' claim (line 158)."""

    def _make_jwt_without_sub(self):
        """Create a JWT token with no sub claim."""
        return _mock_jwt(
            {
                "preferred_username": "no-sub-user",
                "email": "test@example.com",
            }
        )

    def test_jwt_without_sub_fails_decode(self, db_manager):
        """JWT without sub returns None from _extract_user_from_jwt."""
        import main

        jwt_no_sub = self._make_jwt_without_sub()

        with patch("main.db_manager", db_manager):
            with TestClient(main.app, raise_server_exceptions=False) as client:
                # No ext_authz headers, introspect unavailable, JWT has no sub
                response = client.get(
                    "/licenses",
                    headers={"Authorization": f"Bearer {jwt_no_sub}"},
                )
                assert response.status_code == 401
                data = response.json()
                assert "error" in data
                assert data["error"]["message"]


class TestMiddlewareJWTDecodeException:
    """Test JWT decode exception path (lines 172-173)."""

    def test_corrupted_jwt_payload_triggers_exception(self, db_manager):
        """JWT with corrupted base64 payload triggers exception in decode."""
        import main

        # Create a JWT-like token with valid header but corrupted payload
        header = {"alg": "RS256", "typ": "JWT"}
        header_b64 = base64.urlsafe_b64encode(json.dumps(header).encode()).rstrip(b"=").decode()

        # Corrupted payload (not valid base64)
        corrupted_jwt = f"{header_b64}.!!!not-valid-base64!!!.signature"

        with patch("main.db_manager", db_manager):
            with TestClient(main.app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/licenses",
                    headers={"Authorization": f"Bearer {corrupted_jwt}"},
                )
                assert response.status_code == 401
                data = response.json()
                assert "error" in data
                assert data["error"]["message"]

    def test_jwt_with_invalid_json_payload(self, db_manager):
        """JWT with valid base64 but invalid JSON payload."""
        import main

        header = {"alg": "RS256", "typ": "JWT"}

        def b64url_str(s: str) -> str:
            return base64.urlsafe_b64encode(s.encode()).rstrip(b"=").decode()

        def b64url_dict(d: dict) -> str:
            return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

        # Valid header, invalid JSON in payload
        bad_jwt = f"{b64url_dict(header)}.{b64url_str('not-json-data')}.sig"

        with patch("main.db_manager", db_manager):
            with TestClient(main.app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/licenses",
                    headers={"Authorization": f"Bearer {bad_jwt}"},
                )
                assert response.status_code == 401
                data = response.json()
                assert "error" in data
                assert data["error"]["message"]


class TestMiddlewareIntrospectNon200:
    """Test introspect returning non-200 (lines 211-212)."""

    def test_introspect_returns_500(self, db_manager):
        """Introspect returning 500 falls through to 401."""
        import main

        mock_response = MagicMock()
        mock_response.status_code = 500

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("main.db_manager", db_manager):
            with patch("auth.middleware.httpx.AsyncClient", return_value=mock_client):
                with TestClient(main.app, raise_server_exceptions=False) as client:
                    response = client.get(
                        "/licenses",
                        headers={"Authorization": "Bearer opaque-token-here"},
                    )
                    assert response.status_code == 401
                    data = response.json()
                    assert "error" in data
                    assert data["error"]["message"]

    def test_introspect_returns_404(self, db_manager):
        """Introspect returning 404 falls through to 401."""
        import main

        mock_response = MagicMock()
        mock_response.status_code = 404

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("main.db_manager", db_manager):
            with patch("auth.middleware.httpx.AsyncClient", return_value=mock_client):
                with TestClient(main.app, raise_server_exceptions=False) as client:
                    response = client.get(
                        "/licenses",
                        headers={"Authorization": "Bearer opaque-token-here"},
                    )
                    assert response.status_code == 401
                    data = response.json()
                    assert "error" in data
                    assert data["error"]["message"]


class TestMiddlewareIntrospectInactiveToken:
    """Test introspect returning active=false (lines 216-217)."""

    def test_introspect_inactive_returns_401(self, db_manager):
        """Introspect returning active=false results in 401."""
        import main

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "active": False,
            "reason": "token_expired",
        }

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("main.db_manager", db_manager):
            with patch("auth.middleware.httpx.AsyncClient", return_value=mock_client):
                with TestClient(main.app, raise_server_exceptions=False) as client:
                    response = client.get(
                        "/licenses",
                        headers={"Authorization": "Bearer revoked-opaque-token"},
                    )
                    assert response.status_code == 401
                    data = response.json()
                    assert "error" in data
                    assert data["error"]["message"]


# Migration 072: these drive AuthMiddleware on a DB-less minimal app; no realm is owned.
pytestmark = pytest.mark.usefixtures("no_realm_owner")
