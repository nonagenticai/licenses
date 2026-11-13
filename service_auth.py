"""Keycloak service-account token acquisition for outbound calls.

Used by `_provision_budget` to authenticate licenses → billing-service requests.
billing-service requires a service-account JWT bearing one of its
`INTERNAL_SERVICE_ROLES` — `billing-internal`, `onboarding-internal` or
`license-admin` — on the `require_internal` endpoints it uses for budget and
quota provisioning.

The token this module hands out is ALSO the bearer for the two money-config
writes on the offering-provisioning path (`main._provision_offering_markup` /
`_provision_access_charge`), and those are gated separately and more narrowly —
see those functions. Their gate is not restated here: it is derived from
billing's own source into `tests/support/billing_contract.json`, and duplicating
it in prose is how this docstring came to carry a claim that was already two
revisions out of date.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import httpx

from auth.config import KeycloakSettings, get_settings

logger = logging.getLogger(__name__)

_TOKEN_FETCH_TIMEOUT_SECONDS = 5.0
_TOKEN_REFRESH_LEEWAY_SECONDS = 30

# ⚠️ Load-bearing, do not remove. This is the SECOND of TWO outbound call sites
# against the Cloudflare-fronted Keycloak host; the first is the JWKS fetch in
# `auth/jwt_verify.py`. The full rationale — and the live measurement behind it —
# is the `_JWKS_HEADERS` comment there; read it rather than trusting this summary.
# Short version: library-default User-Agents get HTTP 403 / `error code: 1010`
# from Cloudflare's bot filter. Re-measured against THIS endpoint 2026-09-11 from
# the live licenses pod: a `client_credentials` POST with Python's default UA →
# 403, the same request with an explicit UA → a valid token.
#
# ⚠️ THEY COME IN PAIRS. Only the JWKS site was mitigated in 2026-07; this one
# survived on luck — Cloudflare's rule matches `Python-urllib/*` but has not yet
# matched `python-httpx/*`, which is what this client sends by default and what
# `license-authz` access logs show real services sending. If you add a third call
# to this host, it needs the same header.
#
# The block is a property of the HOST, not of one endpoint, so the knob is
# host-scoped: `KEYCLOAK_USER_AGENT`. `JWKS_USER_AGENT` is honoured as the
# legacy spelling of the same knob so that an operator rotating the UA under
# pressure moves BOTH call sites by setting one variable. Neither is required —
# the default works with nothing set, which is the only safe default for a
# header whose absence fails closed.
_TOKEN_FETCH_USER_AGENT = os.getenv(
    "KEYCLOAK_USER_AGENT",
    os.getenv(
        "JWKS_USER_AGENT",
        "Mozilla/5.0 (compatible; nonagentic-licenses/1.0; +https://nonagentic.ai)",
    ),
)
_TOKEN_FETCH_HEADERS = {"User-Agent": _TOKEN_FETCH_USER_AGENT}


class ServiceTokenProvider:
    """Acquires and caches a Keycloak service-account JWT via client credentials.

    Concurrent callers coalesce behind a single in-flight fetch via
    `_lock` so we never thunder-herd the Keycloak token endpoint on
    cold start.
    """

    def __init__(self, settings: KeycloakSettings | None = None) -> None:
        s = settings or get_settings()
        self._token_url: str = s.service_token_url
        self._client_id: str = s.service_client_id
        self._client_secret: str = s.service_client_secret
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self._token_url and self._client_id and self._client_secret)

    async def get_token(self) -> str | None:
        """Return a cached or fresh service-account JWT, or None if not configured."""
        if not self.configured:
            return None

        if self._token and time.monotonic() < self._expires_at:
            return self._token

        async with self._lock:
            # Re-check after acquiring the lock — a peer may have refreshed.
            if self._token and time.monotonic() < self._expires_at:
                return self._token

            try:
                async with httpx.AsyncClient(
                    timeout=_TOKEN_FETCH_TIMEOUT_SECONDS,
                    headers=_TOKEN_FETCH_HEADERS,
                ) as client:
                    resp = await client.post(
                        self._token_url,
                        data={
                            "grant_type": "client_credentials",
                            "client_id": self._client_id,
                            "client_secret": self._client_secret,
                        },
                    )
            except httpx.HTTPError:
                logger.warning(
                    "Service-account token fetch failed (network/timeout) at %s",
                    self._token_url,
                    exc_info=True,
                )
                return None

            if resp.status_code != 200:
                # Body is intentionally truncated — Keycloak error responses
                # usually fit but may include reflected client_id.
                logger.warning(
                    "Service-account token fetch returned %d: %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return None

            body = resp.json()
            self._token = body["access_token"]
            expires_in = body.get("expires_in", 300)
            self._expires_at = time.monotonic() + expires_in - _TOKEN_REFRESH_LEEWAY_SECONDS
            return self._token


_provider: ServiceTokenProvider | None = None


def get_service_token_provider() -> ServiceTokenProvider:
    """Module-level singleton accessor."""
    global _provider
    if _provider is None:
        _provider = ServiceTokenProvider()
    return _provider
