"""A VALID token must verify through the REAL JWKS lookup, end to end.

Why this module exists
----------------------
Every other valid-token test in the suite goes through the autouse
``_trust_test_signing_key`` fixture (``tests/conftest.py``), which replaces
``auth.jwt_verify._signing_key`` — the whole PyJWKClient lookup — with a lambda
returning the test public key. ``test_legitimately_signed_token_still_authenticates``
included. The only module that restores the real lookup,
``test_no_jwks_fetch_on_event_loop.py``, does so with bogus ``kid``s that are
EXPECTED to 401. So nothing proved that a correctly signed token is ACCEPTED by
``client.match_kid(client.get_signing_keys(), kid)`` on pyjwt 2.15, whose
``PyJWKClient`` now caches a parsed ``PyJWKSet`` instead of the raw dict.

How the stub is bypassed
------------------------
Copied from ``test_no_jwks_fetch_on_event_loop.py``: the real ``_signing_key`` is
captured at IMPORT time (before any autouse fixture has run) and the
``real_jwks`` fixture puts it back with ``monkeypatch.setattr``. The network is
replaced one layer BELOW pyjwt, at ``urllib.request.OpenerDirector.open`` — the
sink of pyjwt 2.14+'s ``build_opener(...).open(...)`` — so ``PyJWKClient``,
``PyJWKSet``, ``match_kid`` and ``jwt.decode`` all run unmodified.

Built-in discriminator: every token here is signed with keys generated in THIS
module, never with ``tests/support/jwt_keys``. Were the autouse stub still in
place, it would hand back the support key and every positive test below would
fail its signature check — and the fetch-count floors would read 0.

No message-text asserts, no network, no database.
"""

from __future__ import annotations

import io
import json
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from functools import cache
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm
from jwt.exceptions import ExpiredSignatureError, InvalidSignatureError, InvalidTokenError

import auth.jwt_verify as jv
from auth.middleware import AuthMiddleware

# Captured at import time, i.e. before `_trust_test_signing_key` replaces the
# module attribute for each test. The only handle on the real implementation.
_REAL_SIGNING_KEY = jv._signing_key

# Production derives the issuer allowlist from KEYCLOAK_URL + the two realms
# (auth/jwt_verify.py `trusted_issuers`), so these tests do the same rather than
# use the KEYCLOAK_TRUSTED_ISSUERS override the autouse fixture sets.
KEYCLOAK_URL = "https://auth.roundtrip.test"
SECURE_ISSUER = f"{KEYCLOAK_URL}/realms/secure-apps"
PORTAL_ISSUER = f"{KEYCLOAK_URL}/realms/portal"
SECURE_JWKS_URL = f"{SECURE_ISSUER}/protocol/openid-connect/certs"
PORTAL_JWKS_URL = f"{PORTAL_ISSUER}/protocol/openid-connect/certs"

KID_A = "secure-apps-key-a"
KID_B = "secure-apps-key-b"
KID_PORTAL = "portal-key-p"
KID_UNPUBLISHED = "never-published-kid"

ADMIN_CLAIMS = {"realm_access": {"roles": ["license-admin"]}, "preferred_username": "rt"}


@cache
def _key(name: str) -> rsa.RSAPrivateKey:
    """One throwaway RSA keypair per logical name, generated once per process."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _pem(name: str) -> bytes:
    return _key(name).private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _jwk(name: str, kid: str) -> dict[str, Any]:
    jwk = json.loads(RSAAlgorithm.to_jwk(_key(name).public_key()))
    jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return jwk


def _jwks_documents() -> dict[str, bytes]:
    """What each realm's certs endpoint serves.

    secure-apps publishes TWO keys (rotation overlap, as Keycloak does);
    portal publishes a different one. "rogue" is never published anywhere.
    """
    return {
        SECURE_JWKS_URL: json.dumps({"keys": [_jwk("a", KID_A), _jwk("b", KID_B)]}).encode(),
        PORTAL_JWKS_URL: json.dumps({"keys": [_jwk("p", KID_PORTAL)]}).encode(),
    }


def _token(
    *,
    key: str = "a",
    kid: str | None = KID_A,
    iss: str = SECURE_ISSUER,
    exp_offset: int = 3600,
    **extra: Any,
) -> str:
    now = int(time.time())
    payload = {
        "sub": str(uuid.uuid4()),
        "iss": iss,
        "iat": now,
        "exp": now + exp_offset,
        **ADMIN_CLAIMS,
        **extra,
    }
    headers = {"kid": kid} if kid is not None else None
    return jwt.encode(payload, _pem(key), algorithm="RS256", headers=headers)


class _FakeJwksEndpoint:
    """Stands in for ``urllib.request.OpenerDirector.open``.

    Installed as a plain (non-descriptor) class attribute, so pyjwt's
    ``opener.open(request, timeout=...)`` arrives here WITHOUT the opener
    instance. Routes by URL; an unrouted URL is a connection error, exactly
    what pyjwt would see from a dead endpoint.
    """

    def __init__(self, documents: dict[str, bytes]) -> None:
        self._documents = documents
        self._lock = threading.Lock()
        self.urls: list[str] = []
        self.user_agents: list[str | None] = []

    def __call__(self, request, data=None, timeout=None):  # noqa: ANN001 - urllib shape
        url = request.full_url
        with self._lock:
            self.urls.append(url)
            # urllib.request.Request stores header names `.capitalize()`d.
            self.user_agents.append(request.get_header("User-agent"))
        body = self._documents.get(url)
        if body is None:
            raise urllib.error.URLError(f"unrouted test URL {url}")
        return io.BytesIO(body)

    @property
    def count(self) -> int:
        with self._lock:
            return len(self.urls)


@pytest.fixture
def real_jwks(monkeypatch) -> Iterator[_FakeJwksEndpoint]:
    """Restore the real ``_signing_key`` and serve JWKS from memory.

    Runs after the autouse ``_trust_test_signing_key`` (autouse fixtures are
    instantiated first), so the ``setattr`` below wins for this test and
    monkeypatch undoes it at teardown.
    """
    # Guard against capturing a stub: if this module were ever imported after
    # something replaced the attribute, every test below would silently test it.
    assert _REAL_SIGNING_KEY.__module__ == "auth.jwt_verify"
    assert _REAL_SIGNING_KEY.__qualname__ == "_signing_key"
    monkeypatch.setattr(jv, "_signing_key", _REAL_SIGNING_KEY)

    monkeypatch.delenv("KEYCLOAK_TRUSTED_ISSUERS", raising=False)
    monkeypatch.setenv("KEYCLOAK_URL", KEYCLOAK_URL)
    assert jv.trusted_issuers() == (PORTAL_ISSUER, SECURE_ISSUER)

    # Pin the env-derived module knobs the fetch counts below depend on, so a
    # JWKS_* variable exported on the runner cannot change what is measured.
    monkeypatch.setattr(jv, "_JWKS_CACHE_TTL_SECONDS", 600)
    monkeypatch.setattr(jv, "_JWKS_REFRESH_MIN_INTERVAL_SECONDS", 30.0)

    # Module-level singletons: per-issuer PyJWKClient (lru_cache) and the
    # per-issuer forced-refresh throttle. Fresh on entry AND exit.
    jv._jwks_client.cache_clear()
    jv._reset_jwks_refresh_throttle()

    endpoint = _FakeJwksEndpoint(_jwks_documents())
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", endpoint)
    try:
        yield endpoint
    finally:
        jv._jwks_client.cache_clear()
        jv._reset_jwks_refresh_throttle()


# --------------------------------------------------------------------------- #
# Positive paths
# --------------------------------------------------------------------------- #


def test_valid_token_verifies_through_the_real_jwks_lookup(real_jwks):
    token = _token(key="a", kid=KID_A)
    claims = jv.verify_jwt(token)

    assert claims["iss"] == SECURE_ISSUER
    assert claims["sub"] == jwt.decode(token, options={"verify_signature": False})["sub"]
    # Floor + exactness: one cold-cache fetch, from the token's issuer's endpoint.
    assert real_jwks.urls == [SECURE_JWKS_URL]
    # The Cloudflare bot-block pin (jwt_verify `_JWKS_HEADERS`) actually reaches the wire.
    ua = real_jwks.user_agents[0] or ""
    assert ua and "urllib" not in ua.lower()


def test_second_published_kid_is_served_from_the_cached_set(real_jwks):
    """Two kids, one fetch: the second lookup hits pyjwt 2.15's cached PyJWKSet."""
    assert jv.verify_jwt(_token(key="a", kid=KID_A))["iss"] == SECURE_ISSUER
    assert real_jwks.count == 1

    assert jv.verify_jwt(_token(key="b", kid=KID_B))["iss"] == SECURE_ISSUER
    assert jv.verify_jwt(_token(key="a", kid=KID_A))["iss"] == SECURE_ISSUER
    assert real_jwks.count == 1, f"cached-set hit refetched: {real_jwks.urls}"


def test_each_issuer_resolves_keys_from_its_own_jwks(real_jwks):
    claims = jv.verify_jwt(_token(key="p", kid=KID_PORTAL, iss=PORTAL_ISSUER))
    assert claims["iss"] == PORTAL_ISSUER
    assert real_jwks.urls == [PORTAL_JWKS_URL]


def test_valid_token_is_accepted_by_the_auth_middleware(real_jwks):
    """One HTTP request through the real AuthMiddleware on a protected prefix.

    Same minimal app as ``test_jwt_signature_verification.probe_client`` — no
    database — but with the REAL key lookup underneath.
    """
    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.get("/licenses/whoami")
    def whoami():  # pragma: no cover - trivial probe route
        return {"ok": True}

    valid = _token(key="b", kid=KID_B)
    rogue = _token(key="rogue", kid=KID_B)
    with TestClient(app, raise_server_exceptions=False) as client:
        ok = client.get("/licenses/whoami", headers={"Authorization": f"Bearer {valid}"})
        forged = client.get("/licenses/whoami", headers={"Authorization": f"Bearer {rogue}"})

    assert ok.status_code == 200, f"valid token rejected via the real JWKS path: {ok.status_code}"
    assert forged.status_code == 401
    assert real_jwks.urls == [SECURE_JWKS_URL]


# --------------------------------------------------------------------------- #
# Negative paths. Fetch counts follow jwt_verify._signing_key's semantics:
#   cold cache            -> 1 fetch
#   kid hit               -> no further fetch (signature/claims fail in decode)
#   kid miss, unthrottled -> +1 cache-bypassing refresh
#   kid miss, throttled   -> no fetch at all
#   untrusted iss         -> rejected before any fetch
# --------------------------------------------------------------------------- #


def test_published_kid_signed_by_an_unpublished_key_is_rejected(real_jwks):
    with pytest.raises(InvalidSignatureError):
        jv.verify_jwt(_token(key="rogue", kid=KID_A))
    assert real_jwks.count == 1  # kid matched; no refresh, signature failed


def test_expired_token_is_rejected(real_jwks):
    # 3600s past exp, far outside EXP_LEEWAY_SECONDS.
    with pytest.raises(ExpiredSignatureError):
        jv.verify_jwt(_token(key="a", kid=KID_A, exp_offset=-3600))
    assert real_jwks.count == 1


def test_untrusted_issuer_is_rejected_before_any_fetch(real_jwks):
    with pytest.raises(InvalidTokenError):
        jv.verify_jwt(_token(key="a", kid=KID_A, iss="https://evil.test/realms/secure-apps"))
    assert real_jwks.count == 0


def test_key_from_one_trusted_realm_is_not_accepted_for_the_other(real_jwks):
    """iss=portal but signed with a secure-apps key: portal's JWKS lacks the kid."""
    with pytest.raises(InvalidTokenError):
        jv.verify_jwt(_token(key="a", kid=KID_A, iss=PORTAL_ISSUER))
    # cold fetch + one forced refresh, both against PORTAL's endpoint only
    assert real_jwks.urls == [PORTAL_JWKS_URL, PORTAL_JWKS_URL]


def test_unknown_kid_is_rejected_and_the_refresh_is_throttled(real_jwks):
    with pytest.raises(InvalidTokenError):
        jv.verify_jwt(_token(key="rogue", kid=KID_UNPUBLISHED))
    assert real_jwks.count == 2  # cold fetch + one cache-bypassing refresh

    with pytest.raises(InvalidTokenError):
        jv.verify_jwt(_token(key="rogue", kid="another-unknown-kid"))
    assert real_jwks.count == 2  # throttled: refused without touching the network

    # The throttle only gates the MISS path; a published kid still verifies
    # from the cached set with no further fetch.
    assert jv.verify_jwt(_token(key="b", kid=KID_B))["iss"] == SECURE_ISSUER
    assert real_jwks.count == 2


def test_token_without_kid_is_rejected(real_jwks):
    with pytest.raises(InvalidTokenError):
        jv.verify_jwt(_token(key="a", kid=None))
    assert real_jwks.count == 0  # no kid -> nothing to match, nothing fetched


def test_audience_is_deliberately_not_bound(real_jwks):
    """PINS CURRENT BEHAVIOUR, not a desired property.

    jwt_verify passes ``verify_aud: False`` on purpose (Keycloak's ``aud``
    varies per SA client; binding it is its own change). A wrong ``aud`` is
    therefore ACCEPTED today. When ``aud`` is bound, flip this to a rejection.
    """
    claims = jv.verify_jwt(_token(key="a", kid=KID_A, aud="some-other-service"))
    assert claims["aud"] == "some-other-service"


# Migration 072: these drive AuthMiddleware on a DB-less minimal app; no realm is owned.
pytestmark = pytest.mark.usefixtures("no_realm_owner")
