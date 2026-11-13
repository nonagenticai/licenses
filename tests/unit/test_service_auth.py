"""Unit tests for service_auth.ServiceTokenProvider."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs

import httpx
import pytest

import service_auth


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Each test gets a fresh module-level provider."""
    service_auth._provider = None
    yield
    service_auth._provider = None


def _provider_with(
    token_url: str = "https://kc/realms/x/protocol/openid-connect/token",
) -> service_auth.ServiceTokenProvider:
    settings = MagicMock()
    settings.service_token_url = token_url
    settings.service_client_id = "licenses"
    settings.service_client_secret = "secret"
    return service_auth.ServiceTokenProvider(settings=settings)


class TestConfigured:
    def test_unconfigured_when_secret_missing(self):
        settings = MagicMock(
            service_token_url="x", service_client_id="licenses", service_client_secret=""
        )
        assert service_auth.ServiceTokenProvider(settings).configured is False

    def test_unconfigured_when_token_url_missing(self):
        settings = MagicMock(
            service_token_url="", service_client_id="licenses", service_client_secret="s"
        )
        assert service_auth.ServiceTokenProvider(settings).configured is False

    def test_configured_when_all_set(self):
        assert _provider_with().configured is True


class TestGetToken:
    async def test_returns_none_when_not_configured(self):
        settings = MagicMock(service_token_url="", service_client_id="", service_client_secret="")
        provider = service_auth.ServiceTokenProvider(settings)
        assert await provider.get_token() is None

    async def test_fresh_fetch_caches_token(self):
        provider = _provider_with()
        post_resp = MagicMock(status_code=200)
        post_resp.json.return_value = {"access_token": "tok-1", "expires_in": 300}

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=post_resp)
        with patch("service_auth.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            assert await provider.get_token() == "tok-1"
            # Second call hits the cache, not Keycloak.
            assert await provider.get_token() == "tok-1"
            assert mock_client.post.await_count == 1

    async def test_non_200_returns_none_and_logs(self, caplog):
        provider = _provider_with()
        post_resp = MagicMock(status_code=401, text='{"error":"unauthorized_client"}')

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=post_resp)
        with patch("service_auth.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            with caplog.at_level("WARNING"):
                assert await provider.get_token() is None
            assert any("401" in r.message for r in caplog.records)

    async def test_http_error_returns_none_and_logs(self, caplog):
        provider = _provider_with()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("boom"))
        with patch("service_auth.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            with caplog.at_level("WARNING"):
                assert await provider.get_token() is None
            assert any("token fetch failed" in r.message for r in caplog.records)

    async def test_concurrent_callers_coalesce(self):
        """N concurrent get_token() calls on a cold cache hit Keycloak exactly once."""
        provider = _provider_with()
        post_resp = MagicMock(status_code=200)
        post_resp.json.return_value = {"access_token": "tok-coalesce", "expires_in": 300}

        # Block the first POST briefly so the others queue behind the lock.
        post_event = asyncio.Event()

        async def slow_post(*args, **kwargs):
            await post_event.wait()
            return post_resp

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=slow_post)
        with patch("service_auth.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            tasks = [asyncio.create_task(provider.get_token()) for _ in range(5)]
            # Give all tasks a chance to enter and queue on the lock.
            await asyncio.sleep(0)
            post_event.set()
            results = await asyncio.gather(*tasks)

        assert results == ["tok-coalesce"] * 5
        assert mock_client.post.await_count == 1


#: User-Agent substrings that identify a library default. Cloudflare's bot rule
#: is what these must not match; `python-httpx` is what this client sends with no
#: header set (httpx is pinned at 0.28.1, and `license-authz` access logs show
#: real services arriving as exactly `python-httpx/0.28.1`).
_LIBRARY_DEFAULT_UA_MARKERS = ("python-httpx", "urllib", "python-requests", "aiohttp")


async def _captured_token_request(provider) -> httpx.Request:
    """Run one real token fetch over a mock transport and return the wire request.

    Deliberately NOT a MagicMock'd client like the tests above: the point here is
    the header httpx ACTUALLY sends after merging its own defaults with ours, so
    the assertion holds wherever the header is attached (client constructor or
    the `post` call) and cannot pass just because a mock recorded a kwarg.
    """
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"access_token": "tok-ua", "expires_in": 300})

    real_cls = httpx.AsyncClient

    def _client_over_mock_transport(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_cls(*args, **kwargs)

    with patch("service_auth.httpx.AsyncClient", _client_over_mock_transport):
        token = await provider.get_token()

    # Anti-vacuity: a UA assertion over an empty capture list proves nothing.
    assert token == "tok-ua", f"the fetch path did not complete (token={token!r})"
    assert len(captured) == 1, f"expected exactly one outbound POST, captured {len(captured)}"
    return captured[0]


class TestCloudflareUserAgent:
    """The token endpoint is the SECOND of two call sites behind Cloudflare.

    `auth.nonagentic.ai` answers library-default User-Agents with HTTP 403 /
    `error code: 1010`. The JWKS fetch was mitigated in 2026-07 (see
    `auth/jwt_verify.py::_JWKS_HEADERS`); this POST was left bare and survived
    only because the rule matches `Python-urllib/*` and has not yet matched
    `python-httpx/*`. Re-measured live 2026-09-11: default UA → 403, explicit
    UA → a valid token.
    """

    async def test_token_fetch_sends_a_non_default_user_agent(self):
        request = await _captured_token_request(_provider_with())

        ua = request.headers.get("user-agent", "")
        assert ua, "the token POST carried no User-Agent at all"
        lowered = ua.lower()
        assert not any(m in lowered for m in _LIBRARY_DEFAULT_UA_MARKERS), (
            f"the service-account token POST would be 403'd by Cloudflare's bot "
            f"filter (UA={ua!r}). Set an explicit User-Agent — see "
            f"`_TOKEN_FETCH_HEADERS` in service_auth.py."
        )

    async def test_the_default_ua_marker_list_actually_matches_httpx(self):
        """Negative control: the predicate must be able to FAIL.

        Without this, `_LIBRARY_DEFAULT_UA_MARKERS` could drift out of step with
        httpx's default (a rename, a version bump) and the test above would pass
        for an unmitigated client — reporting green on the exact defect it exists
        to catch.
        """
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={})

        # A bare client — no headers kwarg — i.e. the pre-fix shape.
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await client.post("https://kc/realms/x/protocol/openid-connect/token", data={})

        assert len(captured) == 1
        ua = captured[0].headers.get("user-agent", "").lower()
        assert any(m in ua for m in _LIBRARY_DEFAULT_UA_MARKERS), (
            f"an unmitigated httpx client sent UA={ua!r}, which none of "
            f"{_LIBRARY_DEFAULT_UA_MARKERS} match — the marker list no longer "
            f"recognises a library default, so the sibling test cannot fail."
        )

    async def test_request_body_and_url_are_unchanged(self):
        """The mitigation is headers-only; nothing about the grant may move."""
        request = await _captured_token_request(_provider_with())

        assert request.method == "POST"
        assert str(request.url) == "https://kc/realms/x/protocol/openid-connect/token"
        # Field presence, not values: asserting the fixture's fake credential
        # value here would buy nothing and would plant a `client_secret=...`
        # literal for the secret scanner to flag.
        fields = parse_qs(request.content.decode())
        assert fields.get("grant_type") == ["client_credentials"]
        assert fields.get("client_id") == ["licenses"]
        assert "client_secret" in fields


class TestSingleton:
    def test_get_service_token_provider_returns_same_instance(self):
        a = service_auth.get_service_token_provider()
        b = service_auth.get_service_token_provider()
        assert a is b
