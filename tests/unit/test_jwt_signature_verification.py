"""Forged JWTs must not authenticate.

RED baseline (on origin/main @ fff1b4d, before auth/jwt_verify.py existed):
``test_forged_admin_jwt_is_rejected`` FAILED with 200 != 401 — a token whose
signature segment is the literal string ``forged`` authenticated as
``license-admin``. ``test_forged_jwt_cannot_fall_through_to_ext_authz_headers``
also FAILED, which is the subtler half: even once the signature is checked, a
forged JWT sent alongside attacker-supplied X-License-Id / X-Tenant-Id would
have fallen through to the header-trusting branch and still authenticated.

These tests drive the real ASGI stack (middleware + routes), not the verifier
in isolation, because the defect was in how ``dispatch`` sequenced the auth
methods — a unit test of ``verify_jwt`` alone would have passed on the
vulnerable code.
"""

import base64
import hashlib
import hmac
import json
import time
import uuid

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jwt.exceptions import InvalidTokenError

from auth.middleware import AuthMiddleware
from tests.support.jwt_keys import (
    TEST_ISSUER,
    private_key_pem,
    public_key,
    public_key_pem,
)

ADMIN_CLAIMS = {
    "preferred_username": "attacker",
    "realm_access": {"roles": ["license-admin"]},
}


def _b64url(data: dict) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def forged_jwt(**overrides) -> str:
    """A syntactically valid JWT with a garbage signature segment."""
    payload = {
        "sub": str(uuid.uuid4()),
        "iss": TEST_ISSUER,
        "exp": int(time.time()) + 3600,
        "iat": int(time.time()),
        **ADMIN_CLAIMS,
        **overrides,
    }
    return f"{_b64url({'alg': 'RS256', 'typ': 'JWT'})}.{_b64url(payload)}.forged"


def signed_jwt(**overrides) -> str:
    """A genuinely signed token from the trusted issuer."""
    payload = {
        "sub": str(uuid.uuid4()),
        "iss": TEST_ISSUER,
        "exp": int(time.time()) + 3600,
        "iat": int(time.time()),
        **ADMIN_CLAIMS,
        **overrides,
    }
    return jwt.encode(payload, private_key_pem(), algorithm="RS256")


@pytest.fixture
def probe_client(monkeypatch) -> TestClient:
    """Minimal app carrying ONLY the auth middleware.

    Deliberately isolated from the seeded-database fixtures: this asserts who
    the middleware authenticates, and nothing about business routes.
    ``/licenses/whoami`` sits under a PROTECTED_PREFIXES entry so the
    middleware actually engages.
    """
    monkeypatch.setenv("KEYCLOAK_TRUSTED_ISSUERS", TEST_ISSUER)

    import auth.jwt_verify as jv

    jv._jwks_client.cache_clear()
    monkeypatch.setattr(jv, "_signing_key", lambda token, issuer: public_key())

    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.get("/licenses/whoami")
    def whoami():  # pragma: no cover - trivial probe route
        return {"ok": True}

    return TestClient(app, raise_server_exceptions=False)


def test_forged_admin_jwt_is_rejected(probe_client):
    """The headline defect: an unsigned payload minted a license-admin."""
    resp = probe_client.get(
        "/licenses/whoami",
        headers={"Authorization": f"Bearer {forged_jwt()}"},
    )
    assert resp.status_code == 401, (
        "a JWT with a garbage signature authenticated — signature verification "
        f"is not engaged (got {resp.status_code})"
    )


def test_forged_jwt_cannot_fall_through_to_ext_authz_headers(probe_client):
    """Verification must not be bypassable by also sending the trusted headers.

    Rejecting the signature is not enough on its own: the branch below the JWT
    attempt trusts client-supplied X-License-Id / X-Tenant-Id, and nothing
    strips them on the pod-to-pod path. If a failed JWT fell through, the
    attacker would simply supply both and still authenticate.
    """
    resp = probe_client.get(
        "/licenses/whoami",
        headers={
            "Authorization": f"Bearer {forged_jwt()}",
            "X-License-Id": str(uuid.uuid4()),
            "X-Tenant-Id": str(uuid.uuid4()),
        },
    )
    assert resp.status_code == 401, (
        "a forged JWT fell through to the ext_authz-header branch and "
        f"authenticated (got {resp.status_code})"
    )


@pytest.mark.parametrize("alg", ["none", "NONE", "nOnE"])
def test_alg_none_is_rejected(probe_client, alg):
    """`alg: none` must never be honoured.

    The signature segment is deliberately NON-EMPTY. An earlier version of
    this test used the canonical `header.payload.` form, which has an empty
    third segment — `looks_like_jwt` returns False for that, so the request
    never reached `verify_jwt` and the 401 came from the introspect fallback
    instead. It passed even with signature verification disabled, i.e. it was
    vacuous. Keep the garbage signature so the crypto path is really exercised.
    """
    payload = {
        "sub": str(uuid.uuid4()),
        "iss": TEST_ISSUER,
        "exp": int(time.time()) + 3600,
        **ADMIN_CLAIMS,
    }
    token = f"{_b64url({'alg': alg, 'typ': 'JWT'})}.{_b64url(payload)}.notempty"
    resp = probe_client.get("/licenses/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_hs256_signed_with_the_rsa_public_key_is_rejected(probe_client):
    """Classic algorithm-confusion: HS256 keyed on the RSA *public* key.

    Pinning ``algorithms=["RS256"]`` is what defeats this. Without the pin,
    the public key is a value the attacker also knows, so they could mint a
    valid MAC.
    """
    # Hand-rolled: PyJWT refuses to *encode* HS256 with an asymmetric key, but
    # an attacker has no such scruples — they craft the bytes directly. This
    # must exercise our verifier, not PyJWT's encoder guard.
    header = _b64url({"alg": "HS256", "typ": "JWT"})
    payload = _b64url(
        {
            "sub": str(uuid.uuid4()),
            "iss": TEST_ISSUER,
            "exp": int(time.time()) + 3600,
            **ADMIN_CLAIMS,
        }
    )
    signing_input = f"{header}.{payload}".encode()
    mac = hmac.new(public_key_pem(), signing_input, hashlib.sha256).digest()
    token = f"{header}.{payload}.{base64.urlsafe_b64encode(mac).rstrip(b'=').decode()}"
    resp = probe_client.get("/licenses/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_untrusted_issuer_is_rejected(probe_client):
    """A validly signed token from a realm we do not trust is still a 401."""
    token = signed_jwt(iss="https://evil.example.com/realms/portal")
    resp = probe_client.get("/licenses/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_expired_token_is_rejected(probe_client):
    """`exp` was previously copied into the model and never enforced."""
    token = signed_jwt(exp=int(time.time()) - 3600, iat=int(time.time()) - 7200)
    resp = probe_client.get("/licenses/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_legitimately_signed_token_still_authenticates(probe_client):
    """The regression that matters most — real callers must keep working."""
    resp = probe_client.get(
        "/licenses/whoami",
        headers={"Authorization": f"Bearer {signed_jwt()}"},
    )
    assert resp.status_code == 200, (
        f"a correctly signed token from a trusted issuer was rejected "
        f"({resp.status_code}) — this would break every SA client"
    )


def test_opaque_license_token_is_not_treated_as_a_jwt():
    """Opaque tokens must keep reaching the introspect path untouched."""
    from auth.jwt_verify import looks_like_jwt

    assert looks_like_jwt("nla_" + "x" * 40) is False
    assert looks_like_jwt("a.b") is False
    assert looks_like_jwt("a..c") is False
    assert looks_like_jwt(f"{_b64url({'alg': 'RS256'})}.{_b64url({'sub': 'x'})}.sig") is True


def test_jwks_client_sends_a_non_urllib_user_agent():
    """Regression pin for a would-be production outage.

    `auth.nonagentic.ai` is behind Cloudflare, which answers PyJWKClient's
    default `Python-urllib/X.Y` User-Agent with HTTP 403 (error code 1010).
    Measured from the live licenses pod 2026-07-20. With the default UA every
    JWKS fetch fails, so every legitimately signed token 401s.
    """
    import auth.jwt_verify as jv

    jv._jwks_client.cache_clear()
    client = jv._jwks_client("https://auth.example.com/realms/portal")
    ua = (client.headers or {}).get("User-Agent", "")
    assert ua and "urllib" not in ua.lower(), (
        f"JWKS client would be blocked by Cloudflare's bot filter (UA={ua!r})"
    )
    jv._jwks_client.cache_clear()


def test_jwks_timeout_is_short_enough_not_to_stall_the_event_loop():
    """The JWKS fetch is a BLOCKING call on the async request path.

    PyJWKClient defaults to 30s; a slow Keycloak would stall the worker.
    """
    import auth.jwt_verify as jv

    assert jv._JWKS_TIMEOUT_SECONDS <= 10


def test_verification_fails_closed_when_no_issuers_configured(monkeypatch):
    """An unconfigured allowlist must reject, never accept."""
    import auth.jwt_verify as jv

    monkeypatch.delenv("KEYCLOAK_TRUSTED_ISSUERS", raising=False)
    monkeypatch.setenv("KEYCLOAK_URL", "")
    assert jv.trusted_issuers() == ()
    with pytest.raises(InvalidTokenError):
        jv.verify_jwt(signed_jwt())


def test_whitespace_in_keycloak_url_still_yields_matchable_issuers(monkeypatch):
    """A stray space in the secret must not silently break every match."""
    import auth.jwt_verify as jv

    monkeypatch.delenv("KEYCLOAK_TRUSTED_ISSUERS", raising=False)
    monkeypatch.setenv("KEYCLOAK_URL", "  https://auth.example.com/  ")
    assert jv.trusted_issuers() == (
        "https://auth.example.com/realms/portal",
        "https://auth.example.com/realms/secure-apps",
    )


# Migration 072: these drive AuthMiddleware on a DB-less minimal app; no realm is owned.
pytestmark = pytest.mark.usefixtures("no_realm_owner")
