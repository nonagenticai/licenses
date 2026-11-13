"""Unit tests for auth/tenant.py — get_tenant_id() and require_tenant_id().

Covers X-Tenant-Id header, JWT tenant_id claim, JWT subject fallback, and
missing tenant errors.
"""

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from auth.tenant import get_tenant_id, require_tenant_id


class TestGetTenantId:
    """Tests for get_tenant_id dependency."""

    @pytest.mark.asyncio
    async def test_tenant_from_resolved_user(self):
        """Stage 2: tenant comes from the reconciled user, NOT the raw header."""
        tenant = "01000000-0000-7000-8000-000000000010"
        request = MagicMock()
        # A contradicting raw header must be ignored — the middleware already
        # reconciled the authoritative tenant onto request.state.user.
        request.headers = {"X-Tenant-Id": "01000000-0000-7000-8000-0000000000ff"}
        request.state = MagicMock()
        request.state.user = SimpleNamespace(tenant_id=tenant, sub="user-sub")

        result = await get_tenant_id(request)
        assert result == tenant

    @pytest.mark.asyncio
    async def test_subject_without_tenants_row_raises_401(self):
        """Subject fallback is NO LONGER blind: a sub with no tenants row 401s.

        Previously this returned ``str(user.sub)`` unconditionally, silently
        trusting a user id as a tenant on the read path. Now the sub must
        resolve to a real ``tenants`` row or reads fail closed.
        """
        request = MagicMock()
        request.headers = {}
        request.state = MagicMock()
        request.state.user = MagicMock(sub="019e00aa-0000-7000-8000-0000000000cd", tenant_id=None)

        with patch("main.db_manager.get_tenant", return_value=None) as m_get:
            with pytest.raises(HTTPException) as exc_info:
                await get_tenant_id(request)

        assert exc_info.value.status_code == 401
        assert "Missing tenant context" in exc_info.value.detail
        m_get.assert_called_once_with("019e00aa-0000-7000-8000-0000000000cd")

    @pytest.mark.asyncio
    async def test_subject_with_tenants_row_returns_canonical_id_and_warns(self, caplog):
        """Subject fallback is allowed ONLY when it resolves to a tenants row.

        On that (legacy portal-seed) path we return the tenant's canonical id
        and log a WARNING naming the subject so the producer fixes its token
        emission (missing X-Tenant-Id and tenant_id claim)."""
        tenant_uuid = "019e00aa-0000-7000-8000-0000000000ab"
        request = MagicMock()
        request.headers = {}
        request.state = MagicMock()
        request.state.user = MagicMock(sub=tenant_uuid, tenant_id=None)
        fake_tenant = MagicMock(id=tenant_uuid)

        with patch("main.db_manager.get_tenant", return_value=fake_tenant) as m_get:
            with caplog.at_level(logging.WARNING):
                result = await get_tenant_id(request)

        assert result == tenant_uuid
        m_get.assert_called_once_with(tenant_uuid)
        assert any(
            "subject" in r.message.lower() and r.levelno == logging.WARNING for r in caplog.records
        ), "expected a WARNING naming the subject fallback"

    @pytest.mark.asyncio
    async def test_non_uuid_subject_never_resolves_by_name_401(self):
        """A NON-UUID subject must 401 WITHOUT a get_tenant lookup.

        ``db_manager.get_tenant`` resolves by id OR human name; passing a raw
        non-UUID sub could match a DIFFERENT tenant's name → a cross-tenant read
        (service-isolation break). The subject is parsed as a UUID first, so a
        non-UUID subject (e.g. a service-account name that equals a tenant name)
        falls through to 401 and never reaches the name resolver."""
        request = MagicMock()
        request.headers = {}
        request.state = MagicMock()
        # A non-UUID sub that is ALSO a real tenant's human name.
        request.state.user = MagicMock(sub="portal-render-e2e", tenant_id=None)

        with patch("main.db_manager.get_tenant") as m_get:
            with pytest.raises(HTTPException) as exc_info:
                await get_tenant_id(request)

        assert exc_info.value.status_code == 401
        assert "Missing tenant context" in exc_info.value.detail
        # The name resolver is never reached for a non-UUID subject.
        m_get.assert_not_called()

    @pytest.mark.asyncio
    async def test_prefers_tenant_id_claim_over_subject(self):
        """A validated tenant_id claim wins over the subject fallback."""
        tenant = "0195f48b-d000-7000-8000-000000000003"
        request = MagicMock()
        request.headers = {}
        request.state = MagicMock()
        request.state.user = MagicMock(sub="a-user-id", tenant_id=tenant)

        result = await get_tenant_id(request)
        assert result == tenant

    @pytest.mark.asyncio
    async def test_non_uuid_tenant_id_claim_raises_400(self):
        """A tenant_id claim that is not a UUID fails loud, never absorbed."""
        request = MagicMock()
        request.headers = {}
        request.state = MagicMock()
        request.state.user = MagicMock(sub="a-user-id", tenant_id="n8n-dev")

        with pytest.raises(HTTPException) as exc_info:
            await get_tenant_id(request)
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_missing_tenant_raises_401(self):
        """Raises HTTPException 401 when no tenant context is available."""
        request = MagicMock()
        request.headers = {}
        # Simulate no user attribute on state
        request.state = MagicMock(spec=[])

        with pytest.raises(HTTPException) as exc_info:
            await get_tenant_id(request)

        assert exc_info.value.status_code == 401
        assert "Missing tenant context" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_missing_tenant_no_user_attribute(self):
        """Raises 401 when state has no user attribute at all."""
        request = MagicMock()
        request.headers = {}
        # Use a plain object without user attribute
        request.state = object()

        with pytest.raises(HTTPException) as exc_info:
            await get_tenant_id(request)

        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ws", ["   ", "\t", "\n", " \t\n "])
    async def test_whitespace_only_header_falls_through_to_subject_check(self, ws):
        """Whitespace-only X-Tenant-Id is treated as absent: it falls through to
        the subject check (which now 401s when the sub has no tenants row),
        never passing the whitespace as a tenant value."""
        request = MagicMock()
        request.headers = {"X-Tenant-Id": ws}
        request.state = MagicMock()
        request.state.user = MagicMock(sub="019e00aa-0000-7000-8000-0000000000ef", tenant_id=None)

        with patch("main.db_manager.get_tenant", return_value=None):
            with pytest.raises(HTTPException) as exc_info:
                await get_tenant_id(request)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_whitespace_only_header_and_no_user_raises_401(self):
        """Whitespace-only header with no JWT fallback raises 401, same as absent header."""
        request = MagicMock()
        request.headers = {"X-Tenant-Id": "   "}
        request.state = MagicMock(spec=[])

        with pytest.raises(HTTPException) as exc_info:
            await get_tenant_id(request)
        assert exc_info.value.status_code == 401


class TestRequireTenantId:
    """require_tenant_id() is the strict variant for write paths: it accepts
    a header or a tenant_id claim but NEVER falls back to the subject."""

    @pytest.mark.asyncio
    async def test_resolved_user_tenant_used_header_ignored(self):
        """Stage 2: require_tenant_id uses the reconciled user tenant, not header."""
        tenant = "01000000-0000-7000-8000-000000000010"
        request = MagicMock()
        # Raw header differs and must be ignored (no longer header-first).
        request.headers = {"X-Tenant-Id": "01000000-0000-7000-8000-0000000000ff"}
        request.state = MagicMock()
        request.state.user = SimpleNamespace(sub="a-user-id", tenant_id=tenant)

        assert await require_tenant_id(request) == tenant

    @pytest.mark.asyncio
    async def test_tenant_id_claim_accepted(self):
        tenant = "0195f48b-d000-7000-8000-000000000003"
        request = MagicMock()
        request.headers = {}
        request.state = MagicMock()
        request.state.user = MagicMock(sub="a-user-id", tenant_id=tenant)

        assert await require_tenant_id(request) == tenant

    @pytest.mark.asyncio
    async def test_subject_fallback_refused_raises_401(self):
        """The whole point: a bare subject must NOT become a tenant."""
        request = MagicMock()
        request.headers = {}
        request.state = MagicMock()
        request.state.user = MagicMock(sub="a-user-sub", tenant_id=None)

        with pytest.raises(HTTPException) as exc_info:
            await require_tenant_id(request)
        assert exc_info.value.status_code == 401
        assert "not a tenant" in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_non_uuid_claim_raises_400(self):
        request = MagicMock()
        request.headers = {}
        request.state = MagicMock()
        request.state.user = MagicMock(sub="a-user-sub", tenant_id="george-dev")

        with pytest.raises(HTTPException) as exc_info:
            await require_tenant_id(request)
        assert exc_info.value.status_code == 400
