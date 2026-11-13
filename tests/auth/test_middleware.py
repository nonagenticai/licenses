"""
Tests for AuthMiddleware.
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth.middleware import LICENSE_TOKEN_ROLES
from tests.support.jwt_keys import TEST_ISSUER, private_key_pem


class TestMiddlewarePublicEndpoints:
    """Test that public endpoints bypass authentication."""

    def test_health_bypasses_auth(self, client: TestClient, mock_no_auth):
        """GET /health should work without auth."""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"

    def test_health_live_bypasses_auth(self, client: TestClient, mock_no_auth):
        """GET /health/live should work without auth."""
        response = client.get("/health/live")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "alive"

    def test_health_ready_bypasses_auth(self, client: TestClient, mock_no_auth):
        """GET /health/ready should work without auth."""
        response = client.get("/health/ready")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ready"

    def test_docs_bypasses_auth(self, client: TestClient, mock_no_auth):
        """GET /docs should work without auth."""
        response = client.get("/docs")
        assert response.status_code == 200

    def test_openapi_bypasses_auth(self, client: TestClient, mock_no_auth):
        """GET /openapi.json should work without auth."""
        response = client.get("/openapi.json")
        assert response.status_code == 200

    def test_introspect_bypasses_auth(self, client: TestClient, mock_no_auth):
        """POST /license/introspect should work without auth."""
        response = client.post("/license/introspect", json={"token": "any-token"})
        assert response.status_code == 200
        data = response.json()
        assert data["active"] is False

    def test_root_bypasses_auth(self, client: TestClient, mock_no_auth):
        """GET / should work without auth."""
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 307  # Redirect to docs


class TestMiddlewareProtectedEndpoints:
    """Test that protected endpoints require authentication."""

    def test_licenses_requires_auth(self, client: TestClient, mock_no_auth):
        """GET /licenses requires authentication."""
        response = client.get("/licenses")
        assert response.status_code == 401
        data = response.json()
        assert "error" in data
        assert (
            "Missing" in data["error"]["message"]
            or "authentication" in data["error"]["message"].lower()
        )

    def test_apps_requires_auth(self, client: TestClient, mock_no_auth):
        """GET /apps requires authentication."""
        response = client.get("/apps")
        assert response.status_code == 401
        data = response.json()
        assert "error" in data
        assert (
            "Missing" in data["error"]["message"]
            or "authentication" in data["error"]["message"].lower()
        )

    def test_plans_requires_auth(self, client: TestClient, mock_no_auth):
        """GET /plans requires authentication."""
        response = client.get("/plans")
        assert response.status_code == 401
        data = response.json()
        assert "error" in data
        assert (
            "Missing" in data["error"]["message"]
            or "authentication" in data["error"]["message"].lower()
        )

    def test_regions_requires_auth(self, client: TestClient, mock_no_auth):
        """GET /regions requires authentication."""
        response = client.get("/regions")
        assert response.status_code == 401
        data = response.json()
        assert "error" in data
        assert (
            "Missing" in data["error"]["message"]
            or "authentication" in data["error"]["message"].lower()
        )

    def test_revoke_requires_auth(self, client: TestClient, mock_no_auth):
        """POST /license/revoke requires authentication."""
        response = client.post("/license/revoke", json={"token": "some-token"})
        assert response.status_code == 401
        data = response.json()
        assert "error" in data
        assert (
            "Missing" in data["error"]["message"]
            or "authentication" in data["error"]["message"].lower()
        )

    def test_create_license_requires_auth(self, client: TestClient, mock_no_auth):
        """POST /licenses/user/{id} requires authentication."""
        response = client.post(
            "/licenses/user/test-user",
            json={"app_id": "some-id", "plan_id": "some-plan"},
        )
        assert response.status_code == 401
        data = response.json()
        assert "error" in data
        assert (
            "Missing" in data["error"]["message"]
            or "authentication" in data["error"]["message"].lower()
        )


class TestByoModeAuthExemption:
    """The no-token byo-mode LOOKUP exemption must be scoped to the exact route
    shapes, not a bare ``endswith('/byo-mode')`` suffix.

    Regression for an over-broad auth-exemption: a suffix test GET-exempts ANY
    path ending in ``/byo-mode`` (including a crafted path under a protected
    prefix), which is an auth-bypass primitive on a financial-services boundary.
    """

    # The one seed tenant registered by the per-test truncate fixture (conftest).
    _SEED_TENANT_ID = "01000000-0000-7000-8000-000000000001"

    def test_real_byo_mode_get_is_auth_exempt(self, client: TestClient, mock_no_auth):
        """The genuine GET /tenants/{id}/byo-mode is reachable with NO token
        (the intentional S2S lookup) — it resolves the seed tenant, not 401/403."""
        response = client.get(f"/tenants/{self._SEED_TENANT_ID}/byo-mode")
        assert response.status_code == 200
        assert response.json()["tenant_id"] == self._SEED_TENANT_ID

    # The seed tenant's NAME (conftest `_truncate_all`), the selector shape the
    # exemption no longer covers.
    _SEED_TENANT_NAME = "seed-tenant-primary"

    def test_name_selector_without_token_is_401(self, client: TestClient, mock_no_auth):
        """The exemption is UUID-only. A tokenless NAME lookup used to resolve —
        a name-to-UUID oracle for anyone on the pod CIDR — and now takes the
        normal auth path for the protected ``/tenants`` prefix: 401, no body
        that names the tenant's UUID."""
        response = client.get(f"/tenants/{self._SEED_TENANT_NAME}/byo-mode")
        assert response.status_code == 401
        assert "error" in response.json()
        assert self._SEED_TENANT_ID not in response.text

    def test_name_selector_with_valid_auth_still_resolves(self, auth_client_admin: TestClient):
        """Narrowing the exemption must not change the route for authenticated
        callers: by-name lookup with a valid bearer answers exactly as before."""
        response = auth_client_admin.get(f"/tenants/{self._SEED_TENANT_NAME}/byo-mode")
        assert response.status_code == 200
        assert response.json()["tenant_id"] == self._SEED_TENANT_ID

    def test_unknown_name_with_valid_auth_is_404_not_401(self, auth_client_admin: TestClient):
        """The authenticated by-name path keeps its own 404 — the 401 is only
        ever the auth boundary, never a disguised not-found."""
        response = auth_client_admin.get("/tenants/no-such-tenant/byo-mode")
        assert response.status_code == 404

    def test_crafted_byo_mode_suffix_under_protected_prefix_is_not_exempt(
        self, client: TestClient, mock_no_auth
    ):
        """A crafted GET path that merely ENDS in /byo-mode but is NOT the real
        route (here under the protected /licenses prefix) must NOT be exempted —
        it fails closed with 401. Under the old suffix match it was wrongly
        exempted (then 404'd)."""
        response = client.get("/licenses/00000000-0000-0000-0000-000000000000/byo-mode")
        assert response.status_code == 401
        assert "error" in response.json()

    def test_byo_mode_open_route_regex_is_exact(self):
        """Unit-pin the matcher: only the ONE real byo-mode shape qualifies."""
        from auth.middleware import _BYO_MODE_OPEN_ROUTES

        # Legitimate, intentionally-open shape.
        assert _BYO_MODE_OPEN_ROUTES.match("/tenants/abc/byo-mode")
        assert _BYO_MODE_OPEN_ROUTES.match("/tenants/01000000-0000-7000-8000-000000000001/byo-mode")
        # The PER-APP shape is NOT exempt (changed 2026-08-29).
        #
        # This assertion was inverted, and the inversion is the point. The
        # pattern carried a second alternation for
        # ``/tenants/{sel}/apps/{key}/byo-mode`` while that path had only PUT and
        # DELETE — so for GET it was DORMANT and matched only requests that then
        # 404'd. Nothing could depend on it, which is why removing it is safe.
        #
        # It was still a trap, though not an auth bypass. That GET now exists,
        # and MEASURED with the alternation restored it answers 401 to an
        # authenticated license-admin: the exemption skips the middleware that
        # populates ``request.state.user``, and the route's own
        # ``Depends(require_any_role(...))`` then refuses everyone. This line
        # keeps the route reachable, not merely closed.
        assert not _BYO_MODE_OPEN_ROUTES.match("/tenants/abc/apps/kanban/byo-mode")
        # Over-broad / crafted paths a bare suffix would have exempted.
        assert not _BYO_MODE_OPEN_ROUTES.match("/evil/byo-mode")
        assert not _BYO_MODE_OPEN_ROUTES.match("/licenses/abc/byo-mode")
        assert not _BYO_MODE_OPEN_ROUTES.match("/tenants/abc/def/byo-mode")
        assert not _BYO_MODE_OPEN_ROUTES.match("/tenants/abc/apps/kanban/extra/byo-mode")
        assert not _BYO_MODE_OPEN_ROUTES.match("/tenants/abc/byo-mode/extra")

    def test_tenants_is_a_protected_prefix(self):
        """The exemption exists BECAUSE /tenants is protected.

        Nothing pinned this, and ``main.get_tenant_byo_mode``'s docstring asserted the
        opposite ("/tenants is not a PROTECTED_PREFIX") for long enough to reach the
        published OpenAPI description. This is the anchor that makes that claim
        permanently false, so it cannot be reintroduced as a plausible-sounding reason
        to delete the carve-out.
        """
        from auth.middleware import PROTECTED_PREFIXES

        assert "/tenants" in PROTECTED_PREFIXES

    def test_an_unresolvable_tenant_is_logged_at_warning(
        self, client: TestClient, mock_no_auth, caplog
    ):
        """A 404 here means byo_required went UNENFORCED for that run.

        The caller fails open on any non-200, so this 404 IS the silent-bypass signal
        — and it used to produce no log line anywhere. Measured on the deployed pods:
        34 of these in one 90-second burst, 0 log lines in the whole 12-hour window.

        The level is asserted, not just the message: demoting it to DEBUG or INFO
        restores the original invisibility while leaving a name-only check green.
        """
        import logging

        unknown = "00000000-0000-7000-8000-0000000000ff"
        with caplog.at_level(logging.DEBUG, logger="main"):
            response = client.get(f"/tenants/{unknown}/byo-mode")

        assert response.status_code == 404
        records = [r for r in caplog.records if "byo_mode_tenant_unresolved" in r.getMessage()]
        assert records, (
            "an unenforced byo_required lookup must not be silent; got "
            f"{[r.getMessage() for r in caplog.records]}"
        )
        assert records[0].levelno == logging.WARNING
        assert unknown in records[0].getMessage()

    def test_a_resolvable_tenant_logs_nothing(self, client: TestClient, mock_no_auth, caplog):
        """Non-vacuity: the happy path is every real request and must stay silent."""
        import logging

        with caplog.at_level(logging.DEBUG, logger="main"):
            response = client.get(f"/tenants/{self._SEED_TENANT_ID}/byo-mode")

        assert response.status_code == 200
        assert [r for r in caplog.records if "byo_mode_tenant_unresolved" in r.getMessage()] == []


class TestByoModeExemptionIsDocumentedTruthfully:
    """The exemption is load-bearing and invisible: one regex in ``dispatch`` is all
    that keeps this route open, and its consumer FAILS OPEN when it closes.

    A wrong docstring here is not cosmetic — it is the argument a future reader will
    use to delete the carve-out. It is also *published*: FastAPI copies it into the
    OpenAPI ``description``, so the claim ships to every schema consumer.
    """

    #: The claim that was false for months, in the docstring and in docs/openapi.json.
    RETIRED_CLAIM = "not a PROTECTED_PREFIX"
    #: Naming the real mechanism, the caller, and the fail-open value it returns.
    REQUIRED = ("_BYO_MODE_OPEN_ROUTES", "worker_management", "local_allowed")

    @staticmethod
    def _missing(text: str, required: tuple[str, ...]) -> list[str]:
        return [needle for needle in required if needle not in text]

    def _docstring(self) -> str:
        import main

        doc = main.get_tenant_byo_mode.__doc__
        assert doc, "the route lost its docstring entirely"
        return doc

    def test_docstring_does_not_repeat_the_retired_claim(self):
        """Kills: reverting the docstring to the prefix-based explanation."""
        assert self.RETIRED_CLAIM not in self._docstring()

    def test_docstring_names_the_real_mechanism_and_the_failopen_caller(self):
        """Kills: satisfying the test above by deleting the paragraph instead of
        correcting it. A reader who is not told WHY it is open, and what silently
        breaks when it closes, is the reader who removes it."""
        assert self._missing(self._docstring(), self.REQUIRED) == []

    def test_the_docstrings_pointer_is_a_real_symbol(self):
        """The docstring points at ``_BYO_MODE_OPEN_ROUTES``. Pin the SYMBOL, not a line
        number, so a rename turns this red instead of leaving the docstring stale."""
        import auth.middleware

        assert hasattr(auth.middleware, "_BYO_MODE_OPEN_ROUTES")

    def test_the_predicate_can_say_no(self):
        """Non-vacuity: a docstring missing everything must report everything missing."""
        assert self._missing("nothing here", self.REQUIRED) == list(self.REQUIRED)
        assert self.RETIRED_CLAIM in f"...{self.RETIRED_CLAIM}..."

    def test_the_published_openapi_description_carries_the_correction(self):
        """The docstring is copied into docs/openapi.json. Kills, BY NAME, the
        "edited the docstring, forgot to regenerate" half-fix — independently of the
        drift gate, which would also catch it but reports it as generic drift."""
        import json
        from pathlib import Path

        spec = json.loads(
            (Path(__file__).resolve().parents[2] / "docs" / "openapi.json").read_text()
        )
        description = spec["paths"]["/tenants/{name_or_id}/byo-mode"]["get"]["description"]

        assert self.RETIRED_CLAIM not in description
        assert "_BYO_MODE_OPEN_ROUTES" in description


class TestMiddlewareTokenValidation:
    """Test token validation in middleware."""

    def test_missing_token_returns_401(self, db_manager):
        """Request without Authorization header returns 401."""
        with patch("main.db_manager", db_manager):
            from main import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/licenses")
                assert response.status_code == 401
                data = response.json()
                assert "error" in data
                assert "Missing" in data["error"]["message"]

    def test_invalid_bearer_format_returns_401(self, db_manager):
        """Request with malformed Authorization header returns 401."""
        with patch("main.db_manager", db_manager):
            from main import app

            with TestClient(app, raise_server_exceptions=False) as client:
                # Wrong format - not "Bearer <token>"
                response = client.get("/licenses", headers={"Authorization": "Basic abc123"})
                assert response.status_code == 401
                data = response.json()
                assert "error" in data
                assert "Missing" in data["error"]["message"]

    def test_opaque_token_without_introspect_returns_401(self, db_manager):
        """Request with opaque token (not JWT) returns 401 when introspect is unavailable."""
        import main

        with patch("main.db_manager", db_manager):
            with TestClient(main.app, raise_server_exceptions=False) as client:
                # Token without proper JWT format (not 3 parts)
                # With no ext_authz headers and introspect unavailable, should return 401
                response = client.get(
                    "/licenses", headers={"Authorization": "Bearer some-opaque-token"}
                )
                assert response.status_code == 401
                data = response.json()
                assert "Invalid" in data["error"]["message"]

    def test_opaque_garbage_token_with_forged_headers_is_401(self, db_manager):
        """SECURITY (Stage 1, 2026-07-23): an opaque token that does NOT introspect,
        even with X-License-Id + X-Tenant-Id headers present, must be REJECTED.

        This is the header-trust hole: the old code short-circuited to trust the
        headers and granted `license-reader` on the header's tenant WITHOUT
        validating the token — so a garbage token + a forged X-Tenant-Id read any
        tenant's data. With introspect unavailable here, the garbage token yields
        no active introspection → 401.
        """
        import main

        with patch("main.db_manager", db_manager):
            with TestClient(main.app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/licenses",
                    headers={
                        "Authorization": "Bearer some-opaque-token",
                        "X-Tenant-Id": "0195f48b-d000-7000-8000-000000000001",
                        "X-License-Id": "some-license-id",
                    },
                )
                assert response.status_code == 401
                data = response.json()
                assert "error" in data

    def test_opaque_token_introspect_ignores_forged_tenant_header(self, db_manager):
        """A forged X-Tenant-Id must not become the operative tenant on the OPAQUE
        token path: the introspection result is authoritative.

        This test asserts the RESOLVED tenant — the value the route actually filters
        on — because nothing else here can discriminate. Until this was fixed the
        assertions were ``200`` + ``isinstance(body, list)`` + ``post.assert_awaited()``,
        none of which changes when the middleware starts trusting the header; the
        forged value appeared in no assertion at all. The body cannot discriminate
        either: neither tenant owns a licence, so it is ``[]`` in both the pass and the
        fail world.
        """
        import main
        from models import DatabaseManager

        introspected_tenant = "0195f48b-d000-7000-8000-000000000001"
        forged_tenant = "0195f48b-d000-7000-8000-0000000000ff"

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "active": True,
            "tenant_id": introspected_tenant,
            "license_id": "test-license-id",
            "subject_id": "test-subject-id",
        }
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)

        with (
            patch("main.db_manager", db_manager),
            patch("auth.middleware.httpx.AsyncClient", return_value=mock_client),
            patch.object(DatabaseManager, "list_licenses", return_value=[]) as list_spy,
            TestClient(main.app, raise_server_exceptions=False) as client,
        ):
            response = client.get(
                "/licenses",
                headers={
                    "Authorization": "Bearer valid-opaque-token",
                    "X-Tenant-Id": forged_tenant,
                    "X-License-Id": "forged-license-id",
                },
            )

        # Valid token → introspected → license-reader → RBAC passes.
        assert response.status_code == 200
        # The token was actually introspected (headers did not short-circuit it).
        mock_client.post.assert_awaited()

        # THE assertion: which tenant reached the query.
        list_spy.assert_called_once()
        operative_tenant = list_spy.call_args.kwargs["tenant_id"]
        assert operative_tenant == introspected_tenant, (
            "the introspected tenant must be the operative one"
        )
        assert operative_tenant != forged_tenant, "the forged X-Tenant-Id became operative"

    def test_opaque_token_without_a_header_still_resolves_the_introspected_tenant(self, db_manager):
        """Positive control for the test above.

        Without this, a middleware that ignored the resolved tenant entirely — or that
        hardcoded the introspected value — would look identical to a correct one. This
        pins that the introspected tenant is genuinely PLUMBED THROUGH, not merely that
        the forged header is discarded.
        """
        import main
        from models import DatabaseManager

        introspected_tenant = "0195f48b-d000-7000-8000-00000000000a"

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "active": True,
            "tenant_id": introspected_tenant,
            "license_id": "test-license-id",
            "subject_id": "test-subject-id",
        }
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)

        with (
            patch("main.db_manager", db_manager),
            patch("auth.middleware.httpx.AsyncClient", return_value=mock_client),
            patch.object(DatabaseManager, "list_licenses", return_value=[]) as list_spy,
            TestClient(main.app, raise_server_exceptions=False) as client,
        ):
            response = client.get(
                "/licenses", headers={"Authorization": "Bearer valid-opaque-token"}
            )

        assert response.status_code == 200
        assert list_spy.call_args.kwargs["tenant_id"] == introspected_tenant

    def test_opaque_token_with_successful_introspect_passes(self, db_manager):
        """Request with opaque token should pass when introspect succeeds."""

        import main

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "active": True,
            "tenant_id": "0195f48b-d000-7000-8000-000000000001",
            "license_id": "test-license-id",
            "subject_id": "test-subject-id",
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
                        headers={"Authorization": "Bearer valid-opaque-token"},
                    )
                    # Introspected user gets LICENSE_TOKEN_ROLES (license-reader) so RBAC passes
                    assert response.status_code == 200

        # `isinstance(body, list)` used to stand here. It is a type check on the route's
        # declared response_model, so it holds in every world this test could be in —
        # including the one where the middleware never introspected at all. Assert
        # instead that the OPAQUE token is what was sent for introspection, which is the
        # proposition the test name actually makes.
        mock_client.post.assert_awaited_once()
        await_args = mock_client.post.await_args
        assert await_args is not None, "introspect was never awaited"
        assert await_args.kwargs["json"] == {"token": "valid-opaque-token"}

    def test_valid_token_passes_through(self, db_manager, admin_user):
        """Request with valid token should pass through to handler."""

        import main

        # A genuinely signed token — the middleware verifies signatures.
        mock_jwt = jwt.encode(
            {
                "sub": admin_user.sub,
                "preferred_username": admin_user.preferred_username,
                "email": admin_user.email,
                "realm_access": admin_user.realm_access,
                "resource_access": {},
                "iss": TEST_ISSUER,
                # Migration 050: an X-Tenant-Id retarget needs an owning client
                # that owns/holds a grant on the tenant, or the break-glass one.
                "azp": "licenses-admin",
                "iat": int(time.time()),
                "exp": int(time.time()) + 3600,
            },
            private_key_pem(),
            algorithm="RS256",
        )

        with patch("main.db_manager", db_manager):
            with TestClient(main.app, raise_server_exceptions=False) as client:
                response = client.get(
                    "/licenses",
                    headers={
                        "Authorization": f"Bearer {mock_jwt}",
                        "X-Tenant-Id": "01000000-0000-7000-8000-000000000001",
                    },
                )
                # Admin JWT has license-admin role, so RBAC passes
                assert response.status_code == 200
                data = response.json()
                assert isinstance(data, list)


class TestMiddlewareErrorHandling:
    """Test middleware error handling."""

    def test_opaque_token_fails_gracefully(self, db_manager):
        """Opaque token (not JWT) falls through to introspect, then fails cleanly."""
        import main

        with patch("main.db_manager", db_manager):
            with TestClient(main.app, raise_server_exceptions=False) as client:
                # Token without proper JWT format (not 3 parts)
                # No ext_authz headers, introspect unavailable
                response = client.get("/licenses", headers={"Authorization": "Bearer some-token"})
                assert response.status_code == 401
                data = response.json()
                assert "error" in data
                assert (
                    "Invalid" in data["error"]["message"] or "expired" in data["error"]["message"]
                )

    def test_401_includes_www_authenticate_header(self, db_manager):
        """401 responses should include WWW-Authenticate header."""
        with patch("main.db_manager", db_manager):
            from main import app

            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.get("/licenses")
                assert response.status_code == 401
                assert "WWW-Authenticate" in response.headers
                assert response.headers["WWW-Authenticate"] == "Bearer"


class TestMiddlewareRaisedHTTPExceptionBecomesAResponse:
    """An HTTPException raised in MIDDLEWARE must still reach the caller as itself.

    THE DEFECT. ``main.global_exception_handler`` is registered for ``Exception``,
    which starlette installs as ``ServerErrorMiddleware``'s handler — the
    OUTERMOST layer. The ``HTTPException`` handler lives on
    ``ExceptionMiddleware``, the innermost one, wrapping only the router:

        ServerErrorMiddleware(handler=global_exception_handler)   <- outermost
          AuthMiddleware (BaseHTTPMiddleware)
            CORSMiddleware
              ExceptionMiddleware(handlers={HTTPException: http_exception_handler})
                router

    So an ``HTTPException`` raised in a route never reaches
    ``global_exception_handler`` — and one raised in ``AuthMiddleware`` reaches
    ONLY it. That branch used to be ``raise exc``, on the belief that re-raising
    would let "FastAPI's HTTPException handler return its intended response". It
    cannot: ``ServerErrorMiddleware`` awaits the handler and only then sends the
    result, so an exception raised inside it means nothing is ever written. The
    caller got **500 with an empty body**, losing the status, the JSON body and
    any headers.

    WHY THIS IS DRIVEN THROUGH THE REAL STACK. Calling
    ``global_exception_handler(request, exc)`` directly proves nothing about
    dispatch — it is the MIDDLEWARE POSITION that breaks it, and a direct call
    has no position. The existing runtime coverage in
    ``tests/unit/test_no_exception_leak.py`` calls ``http_exception_handler``
    directly for that reason and so could never have caught this.

    Note ``raise_server_exceptions=False`` is what makes the RED observable
    rather than a traceback: every client fixture in this repo sets it, which is
    why the defect would have surfaced in a test as a puzzling empty 500.
    """

    def _raising_middleware_app(self, status_code: int, detail):
        """A real app with this repo's two handlers and a middleware that raises."""
        from fastapi import FastAPI, HTTPException
        from starlette.middleware.base import BaseHTTPMiddleware

        import main

        app = FastAPI()
        # Register exactly as main.py does — the DECORATOR form. `add_exception_handler`
        # takes a handler typed `(Request, Exception)`, and these are narrowed to
        # `HTTPException`, which `ty` rejects; the decorator is both the production
        # shape and the type-clean one.
        app.exception_handler(HTTPException)(main.http_exception_handler)
        app.exception_handler(Exception)(main.global_exception_handler)

        class Raiser(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                if request.headers.get("X-Raise") == "yes":
                    raise HTTPException(status_code=status_code, detail=detail)
                return await call_next(request)

        app.add_middleware(Raiser)

        @app.get("/probe")
        def probe():
            return {"ok": True}

        return app

    def test_a_middleware_raised_400_is_not_flattened_into_an_empty_500(self):
        """RED on main: 500 with ``b''``."""
        app = self._raising_middleware_app(400, "Invalid X-Tenant-Id: must be a UUID")

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/probe", headers={"X-Raise": "yes"})

        assert response.status_code == 400, (
            f"expected the middleware's own status; got {response.status_code} "
            f"with body {response.content!r}"
        )
        assert response.content, "the response body was EMPTY — nothing was sent to the wire"
        assert response.json()["detail"] == "Invalid X-Tenant-Id: must be a UUID"

    def test_the_control_arm_still_serves(self):
        """NON-VACUITY: the same app, same client, without the raise header.

        Without this, an app that 400'd everything — or a client that never
        reached the route — would satisfy the assertion above.
        """
        app = self._raising_middleware_app(400, "unused")

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/probe")

        assert response.status_code == 200
        assert response.json() == {"ok": True}

    def test_a_middleware_raised_401_keeps_its_headers(self):
        """``WWW-Authenticate`` is contract — ``test_401_includes_www_authenticate_header``
        asserts it on the route path. A 401 raised in middleware must not lose it."""
        from fastapi import HTTPException
        from starlette.middleware.base import BaseHTTPMiddleware

        import main

        app = FastAPI()
        # Register exactly as main.py does — the DECORATOR form. `add_exception_handler`
        # takes a handler typed `(Request, Exception)`, and these are narrowed to
        # `HTTPException`, which `ty` rejects; the decorator is both the production
        # shape and the type-clean one.
        app.exception_handler(HTTPException)(main.http_exception_handler)
        app.exception_handler(Exception)(main.global_exception_handler)

        class Raiser(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                raise HTTPException(
                    status_code=401, detail="nope", headers={"WWW-Authenticate": "Bearer"}
                )

        app.add_middleware(Raiser)

        @app.get("/probe")
        def probe():  # pragma: no cover - the middleware always raises first
            return {"ok": True}

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/probe")

        assert response.status_code == 401
        assert response.headers.get("WWW-Authenticate") == "Bearer"

    def test_a_middleware_raised_5xx_is_still_scrubbed(self):
        """Delegating to ``http_exception_handler`` — rather than building a
        response inline — is what puts this path under the 5xx detail scrubber.

        Without this test that is an unfalsifiable claim in a docstring: an
        inline ``JSONResponse(status_code=exc.status_code, content={"detail":
        exc.detail})`` passes every other assertion in this class while shipping
        the DSN to the caller.
        """
        from fastapi import HTTPException
        from starlette.middleware.base import BaseHTTPMiddleware

        import main

        app = FastAPI()
        # Register exactly as main.py does — the DECORATOR form. `add_exception_handler`
        # takes a handler typed `(Request, Exception)`, and these are narrowed to
        # `HTTPException`, which `ty` rejects; the decorator is both the production
        # shape and the type-clean one.
        app.exception_handler(HTTPException)(main.http_exception_handler)
        app.exception_handler(Exception)(main.global_exception_handler)

        class Raiser(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                # The shape `_leaked_cause` exists to catch: a 5xx whose detail
                # was built from a caught exception.
                try:
                    raise RuntimeError(
                        "connection to server at host postgres.example.com port 5432 failed"
                    )
                except RuntimeError as exc:
                    raise HTTPException(status_code=500, detail=str(exc)) from exc

        app.add_middleware(Raiser)

        @app.get("/probe")
        def probe():  # pragma: no cover - the middleware always raises first
            return {"ok": True}

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/probe")

        assert response.status_code == 500
        assert "postgres.example.com" not in response.text, (
            "the caught cause reached the caller — this path is bypassing the 5xx "
            f"scrubber: {response.text[:200]}"
        )

    def test_a_non_http_exception_still_becomes_the_generic_500(self):
        """The OTHER branch must not regress: a genuine unhandled error still gets
        the sanitised envelope, and must NOT leak the exception text."""
        from fastapi import HTTPException
        from starlette.middleware.base import BaseHTTPMiddleware

        import main

        app = FastAPI()
        # Register exactly as main.py does — the DECORATOR form. `add_exception_handler`
        # takes a handler typed `(Request, Exception)`, and these are narrowed to
        # `HTTPException`, which `ty` rejects; the decorator is both the production
        # shape and the type-clean one.
        app.exception_handler(HTTPException)(main.http_exception_handler)
        app.exception_handler(Exception)(main.global_exception_handler)

        class Raiser(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                raise RuntimeError("dsn=postgresql://u:p@host/db")

        app.add_middleware(Raiser)

        @app.get("/probe")
        def probe():  # pragma: no cover - the middleware always raises first
            return {"ok": True}

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/probe")

        assert response.status_code == 500
        assert response.json()["error"]["message"] == "Internal server error"
        assert "postgresql://" not in response.text


class TestIntrospectTenantValidationDoesNotEscapeTheMiddleware:
    """A non-UUID tenant from OUR OWN introspect must 401, not crash the stack.

    ``_introspect_license_token`` calls ``validate_tenant_uuid`` on the
    ``tenant_id`` the introspect response carries. That raises ``HTTPException``,
    which is neither ``httpx.HTTPError`` nor ``ValueError`` — so the handler
    below it did not catch it and it escaped ``dispatch`` entirely, into the
    broken ``global_exception_handler`` path above.

    Reachability, stated honestly: introspect builds ``tenant_id`` as
    ``str(license.tenant_id)`` from a ``PGUUID`` column, so on a migrated
    Postgres it is always canonical. This needs a legacy row or a different
    introspect implementation. The handler defect it triggered was
    unconditional; this call site is the one live path into it.

    The remedy is 401, not the 400 the sibling call site returns: there the
    tenant came from the CLIENT's ``X-Tenant-Id`` header, so 400 tells the caller
    to fix its request. Here it came from our own response, and blaming the
    caller for our data would be wrong.
    """

    def test_a_non_uuid_tenant_from_introspect_returns_401_with_a_body(self, db_manager):
        import main

        class _Resp:
            status_code = 200

            @staticmethod
            def json():
                return {
                    "active": True,
                    "tenant_id": "n8n-dev",  # a REAL legacy label, not a UUID
                    "license_id": "019f0000-0000-7000-8000-000000000001",
                    "subject_id": "s-1",
                }

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **k):
                return _Resp()

        with patch("main.db_manager", db_manager), patch("httpx.AsyncClient", _Client):
            with TestClient(main.app, raise_server_exceptions=False) as client:
                response = client.get("/licenses", headers={"Authorization": "Bearer opaque-tok"})

        assert response.status_code == 401, (
            f"got {response.status_code} with body {response.content!r} — a 500 with an "
            "empty body is the signature of the exception escaping the middleware"
        )
        assert response.content, "empty body: the exception escaped instead of being handled"


class TestLicenseTokenRoles:
    """Verify LICENSE_TOKEN_ROLES only grants reader access, not admin/manager."""

    def test_license_token_roles_exclude_admin(self):
        assert "license-admin" not in LICENSE_TOKEN_ROLES

    def test_license_token_roles_exclude_manager(self):
        assert "license-manager" not in LICENSE_TOKEN_ROLES

    def test_license_token_roles_include_reader(self):
        assert "license-reader" in LICENSE_TOKEN_ROLES
