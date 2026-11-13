"""Strict uuid7 validation at tenant_id ingress points.

Replaces the deleted test_tenant_normalize.py. The licenses service
no longer maps legacy text labels — non-UUID tenant_ids fail loud
with HTTP 400 (or 401 if missing entirely). This is the durable fix
in place of a backward-compat shim. See:
- ``auth/tenant.py`` module docstring
- memory ``feedback-no-backcompat-shim.md``
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from auth.tenant import get_tenant_id, validate_tenant_uuid

VALID_UUID7 = "0195f48b-d000-7000-8000-000000000001"


# --------------------------------------------------------------------- #
# Unit: validate_tenant_uuid                                             #
# --------------------------------------------------------------------- #


class TestValidateTenantUuid:
    def test_accepts_canonical_uuid7(self) -> None:
        assert validate_tenant_uuid(VALID_UUID7) == VALID_UUID7

    def test_accepts_arbitrary_valid_uuid(self) -> None:
        random = str(uuid.uuid4())
        assert validate_tenant_uuid(random) == random

    def test_normalizes_uppercase_uuid_to_lowercase(self) -> None:
        # uuid.UUID() round-trips to canonical lowercase string.
        assert validate_tenant_uuid(VALID_UUID7.upper()) == VALID_UUID7

    @pytest.mark.parametrize("legacy", ["n8n-dev", "gerard-dev", "george-dev"])
    def test_rejects_legacy_text_label_with_400(self, legacy: str) -> None:
        with pytest.raises(HTTPException) as exc_info:
            validate_tenant_uuid(legacy)
        assert exc_info.value.status_code == 400
        assert legacy in str(exc_info.value.detail)
        assert "tenant_name" in str(exc_info.value.detail)

    def test_rejects_arbitrary_garbage_with_400(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            validate_tenant_uuid("not-a-uuid-at-all")
        assert exc_info.value.status_code == 400

    def test_rejects_truncated_uuid_with_400(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            validate_tenant_uuid(VALID_UUID7[:-2])
        assert exc_info.value.status_code == 400

    @pytest.mark.parametrize("missing", [None, ""])
    def test_missing_value_raises_401(self, missing: str | None) -> None:
        with pytest.raises(HTTPException) as exc_info:
            validate_tenant_uuid(missing)
        assert exc_info.value.status_code == 401
        assert "Missing tenant context" in str(exc_info.value.detail)

    def test_source_label_appears_in_400_detail(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            validate_tenant_uuid("n8n-dev", source="X-Tenant-Id")
        assert "X-Tenant-Id" in str(exc_info.value.detail)


# --------------------------------------------------------------------- #
# Unit: get_tenant_id dependency                                         #
# --------------------------------------------------------------------- #


def _make_request(headers: dict[str, str], state_user: Any | None = None) -> Request:
    scope: dict[str, Any] = {
        "type": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    request = Request(scope=scope)
    if state_user is not None:
        request.state.user = state_user
    return request


class TestTenantRejectionIsVisibleToTheOperator:
    """This module's contract is "fail loud so the producer fixes its emission".

    It used to fail loud only at the CALLER. The rejection was raised with no log
    call, and ``main.http_exception_handler`` is deliberately scoped to 5xx, so
    nothing downstream logged it either. Measured on the deployed pods over 12
    hours: 35 non-2xx responses, 0 WARNING lines, 0 ERROR lines.

    Every test here asserts the LEVEL as well as the message. A mutant that
    demotes these to DEBUG or INFO restores the original invisibility while
    leaving a name-only assertion green.
    """

    def test_a_legacy_text_label_is_logged_at_warning_with_the_offending_value(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger="auth.tenant"):
            with pytest.raises(HTTPException) as exc_info:
                validate_tenant_uuid("n8n-dev", source="X-Tenant-Id")

        assert exc_info.value.status_code == 400
        records = [r for r in caplog.records if "tenant_id_not_a_uuid" in r.getMessage()]
        assert records, (
            f"the rejection was invisible; got {[r.getMessage() for r in caplog.records]}"
        )
        assert records[0].levelno == logging.WARNING, (
            f"expected WARNING, got {records[0].levelname} — a demoted level is the "
            "original defect wearing a passing test"
        )
        # The offending value IS the diagnosis: without it the operator learns only
        # that something, somewhere, was malformed.
        assert "n8n-dev" in records[0].getMessage()
        assert "X-Tenant-Id" in records[0].getMessage()

    def test_missing_tenant_context_is_logged_at_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger="auth.tenant"):
            with pytest.raises(HTTPException) as exc_info:
                validate_tenant_uuid("", source="X-Tenant-Id")

        assert exc_info.value.status_code == 401
        records = [r for r in caplog.records if "tenant_context_missing" in r.getMessage()]
        assert records, "a 401 at the tenant boundary must not be silent"
        assert records[0].levelno == logging.WARNING

    def test_a_valid_tenant_logs_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        """Non-vacuity, and the thing that keeps this cheap: the happy path — which is
        every real request — must stay silent, or these lines become the log volume."""
        with caplog.at_level(logging.DEBUG, logger="auth.tenant"):
            assert validate_tenant_uuid(VALID_UUID7) == VALID_UUID7
        assert caplog.records == []


class TestGetTenantId:
    def test_uses_resolved_user_tenant_id(self) -> None:
        # Stage 2: get_tenant_id sources the tenant from the middleware-reconciled
        # request.state.user.tenant_id, NOT the raw X-Tenant-Id header.
        user = SimpleNamespace(tenant_id=VALID_UUID7, sub=None)
        req = _make_request({}, state_user=user)
        assert asyncio.run(get_tenant_id(req)) == VALID_UUID7

    def test_raw_header_is_not_trusted(self) -> None:
        # A raw X-Tenant-Id with no reconciled user tenant is ignored — the
        # header is no longer a trusted tenant source here (header validation +
        # admin/proxy reconciliation happen in the middleware). Fails closed 401.
        req = _make_request({"x-tenant-id": VALID_UUID7})
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(get_tenant_id(req))
        assert exc_info.value.status_code == 401

    def test_subject_without_tenants_row_raises_401(self) -> None:
        """The subject fallback is no longer blind: a sub with no ``tenants``
        row fails closed (401) instead of being trusted as a tenant."""
        sub_uuid = str(uuid.uuid4())

        class FakeUser:
            sub = sub_uuid
            tenant_id = None

        req = _make_request({}, state_user=FakeUser())
        with patch("main.db_manager.get_tenant", return_value=None):
            with pytest.raises(HTTPException) as exc_info:
                asyncio.run(get_tenant_id(req))
        assert exc_info.value.status_code == 401

    def test_subject_with_tenants_row_returns_canonical_id(self) -> None:
        """The subject fallback is honored ONLY when it resolves to a real
        ``tenants`` row, returning that tenant's canonical id."""
        sub_uuid = str(uuid.uuid4())

        class FakeUser:
            sub = sub_uuid
            tenant_id = None

        req = _make_request({}, state_user=FakeUser())
        fake_tenant = MagicMock(id=sub_uuid)
        with patch("main.db_manager.get_tenant", return_value=fake_tenant) as m_get:
            result = asyncio.run(get_tenant_id(req))
        assert result == sub_uuid
        m_get.assert_called_once_with(sub_uuid)

    def test_prefers_tenant_id_claim_over_subject(self) -> None:
        class FakeUser:
            sub = str(uuid.uuid4())
            tenant_id = VALID_UUID7

        req = _make_request({}, state_user=FakeUser())
        assert asyncio.run(get_tenant_id(req)) == VALID_UUID7

    def test_no_header_no_user_raises_401(self) -> None:
        req = _make_request({})
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(get_tenant_id(req))
        assert exc_info.value.status_code == 401


# --------------------------------------------------------------------- #
# Integration: AuthMiddleware via TestClient                             #
# --------------------------------------------------------------------- #


class TestAuthMiddlewareStrictValidation:
    """Hits a protected endpoint with various X-Tenant-Id values and
    asserts the middleware rejects non-UUID values at the boundary
    instead of absorbing them via a shim."""

    @pytest.mark.parametrize("legacy", ["n8n-dev", "gerard-dev", "george-dev"])
    def test_legacy_text_xtenantid_rejected_with_400(
        self, client: TestClient, mock_no_auth: None, legacy: str
    ) -> None:
        response = client.get("/licenses", headers={"X-Tenant-Id": legacy})
        assert response.status_code == 400
        body = response.json()
        assert "error" in body
        assert legacy in body["error"]["message"]
        assert "tenant_name" in body["error"]["message"]

    def test_garbage_xtenantid_rejected_with_400(
        self, client: TestClient, mock_no_auth: None
    ) -> None:
        response = client.get("/licenses", headers={"X-Tenant-Id": "totally-not-a-uuid"})
        assert response.status_code == 400

    def test_uuid_xtenantid_passes_validation(self, client: TestClient, mock_no_auth: None) -> None:
        # Valid UUID — middleware accepts and creates minimal-user request.
        # Downstream may still 401/403 due to roles, but NOT 400 from the boundary.
        response = client.get("/licenses", headers={"X-Tenant-Id": VALID_UUID7})
        assert response.status_code != 400
