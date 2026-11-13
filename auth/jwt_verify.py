"""Cryptographic verification of inbound Keycloak JWTs.

Why this module exists
----------------------
``auth/middleware.py`` used to accept a Bearer JWT by manually base64-decoding
its payload — it never read the signature segment at all. Roles
(``realm_access``) and ``tenant_id`` were taken verbatim from that unverified
payload, so any caller able to reach the service could mint a
``license-admin`` identity for any tenant with a three-line request. The
docstring justified it as "trusts that upstream ext_authz has already
validated the signature", but ``licenses.nonagentic.ai`` is not in the ext_authz
``AUTH_REQUIRED_HOSTS`` allowlist and any in-mesh caller reaching the pod
directly bypasses the gateway filter entirely. See the 2026-05-03 SECURITY note
in ``auth/middleware.py`` — item 3 of its own follow-up list is this file.

Why issuers, not a realm
------------------------
``KeycloakSettings.realm_name`` (env ``KEYCLOAK_REALM``) is **not** usable for
inbound verification: production has it set to ``nonagentic``, a realm that
returns 404 (verified live 2026-07-20). It is only meaningful for the *outbound*
service-account credential, which already overrides it via ``SERVICE_TOKEN_URL``.

Inbound tokens legitimately arrive from **two** realms:
  * ``portal``      — portal-bff, fr backend license reads
  * ``secure-apps`` — fr backend admin writes, commonpractice admin client

So verification is keyed on the token's ``iss`` claim, matched against an
explicit allowlist, and the signing key is fetched from *that* issuer's JWKS.
Pinning ``issuer=`` is what makes multi-realm acceptance safe: without it,
"accept two realms" degrades into "accept whichever key happens to verify".
"""

from __future__ import annotations

import logging
import os
import threading
import time
from functools import lru_cache
from typing import Any

import jwt
from jwt import PyJWKClient
from jwt.exceptions import InvalidTokenError, PyJWKClientError

logger = logging.getLogger(__name__)

# Keycloak signs with RS256 by default. Pinning the algorithm list is what
# defeats `alg: none` and the HS256-signed-with-the-RSA-public-key confusion
# attack — never widen this to include a symmetric algorithm.
ALLOWED_ALGORITHMS = ["RS256"]

# Small leeway so ordinary clock skew between Keycloak and this pod does not
# 401 a token that every SA client caches for 30-60s.
EXP_LEEWAY_SECONDS = int(os.getenv("JWT_EXP_LEEWAY_SECONDS", "60"))

_JWKS_CACHE_KEYS = 16
_JWKS_CACHE_TTL_SECONDS = int(os.getenv("JWKS_TTL_SECONDS", "600"))

# PyJWKClient's default is 30s. That is far too long here: the fetch is a
# BLOCKING urllib call. It no longer runs on the event loop (the middleware
# offloads verification via ``asyncio.to_thread``), but it still occupies one of
# the pod's 8 executor threads for its duration, so the ceiling stays low.
_JWKS_TIMEOUT_SECONDS = float(os.getenv("JWKS_TIMEOUT_SECONDS", "5"))

# Minimum interval between two CACHE-BYPASSING JWKS refreshes for one issuer.
#
# ⚠️ Load-bearing against an unauthenticated amplification, do not remove.
# ``PyJWKClient.get_signing_key`` refreshes the JWK set with ``refresh=True``
# — deliberately bypassing ``jwk_set_cache`` — whenever the token's ``kid`` is
# absent from the cached set, and its per-key ``lru_cache`` does not memoise the
# raising call. So an unsigned token carrying an allowlisted ``iss`` and a
# RANDOM ``kid`` bought one blocking outbound HTTPS request per inbound request,
# with no upper bound, BEFORE any signature was checked and before any role
# dependency ran. Measured on the pre-fix tree: 11 fetches for 10 requests.
#
# A real key rotation is still picked up promptly — Keycloak publishes the new
# key before signing with it, and this only throttles the *miss* path; the
# normal TTL refresh (``_JWKS_CACHE_TTL_SECONDS``) is untouched.
_JWKS_REFRESH_MIN_INTERVAL_SECONDS = float(os.getenv("JWKS_REFRESH_MIN_INTERVAL_SECONDS", "30"))

# ⚠️ Load-bearing, do not remove. `auth.nonagentic.ai` is fronted by Cloudflare,
# which answers PyJWKClient's default `Python-urllib/X.Y` User-Agent with
# **HTTP 403 / error code 1010** (bot block). Measured from the live licenses
# pod 2026-07-20: urllib UA → 403, browser UA → 200. Without this header every
# JWKS fetch fails, `verify_jwt` raises, and EVERY legitimately signed token
# gets a 401 — i.e. a total outage of the JWT auth path that fails closed and
# would look like "the fix broke auth".
_JWKS_HEADERS = {
    "User-Agent": os.getenv(
        "JWKS_USER_AGENT",
        "Mozilla/5.0 (compatible; nonagentic-licenses/1.0; +https://nonagentic.ai)",
    )
}

# Realms whose tokens are legitimately presented to this service. Kept as
# realm names (not full URLs) so the allowlist survives a Keycloak host move.
DEFAULT_TRUSTED_REALMS = ("portal", "secure-apps")


def _keycloak_base_url() -> str:
    # .strip() before .rstrip("/"): a stray space in the secret would otherwise
    # produce an issuer that can never match, failing closed but silently.
    return os.getenv("KEYCLOAK_URL", "").strip().rstrip("/")


def trusted_issuers() -> tuple[str, ...]:
    """Issuers whose tokens this service will accept.

    ``KEYCLOAK_TRUSTED_ISSUERS`` (comma-separated, full issuer URLs) wins when
    set. Otherwise the set is derived from ``KEYCLOAK_URL`` + the two realms
    that actually mint tokens for us. Deliberately NOT derived from
    ``KEYCLOAK_REALM`` — see the module docstring.
    """
    explicit = os.getenv("KEYCLOAK_TRUSTED_ISSUERS", "").strip()
    if explicit:
        return tuple(i.strip().rstrip("/") for i in explicit.split(",") if i.strip())

    base = _keycloak_base_url()
    if not base:
        return ()
    return tuple(f"{base}/realms/{realm}" for realm in DEFAULT_TRUSTED_REALMS)


@lru_cache(maxsize=_JWKS_CACHE_KEYS)
def _jwks_client(issuer: str) -> PyJWKClient:
    """One JWKS client per trusted issuer.

    PyJWKClient handles ``kid`` selection and key rotation, which the old
    ``TokenValidator.get_public_key()`` (an ``lru_cache``'d single PEM) did not.
    """
    return PyJWKClient(
        f"{issuer}/protocol/openid-connect/certs",
        cache_keys=True,
        lifespan=_JWKS_CACHE_TTL_SECONDS,
        headers=_JWKS_HEADERS,
        timeout=_JWKS_TIMEOUT_SECONDS,
        # ONE refresh limiter, and it is ours (`_JWKS_REFRESH_MIN_INTERVAL_SECONDS`).
        # pyjwt 2.14 added its own 30s kid-miss cooldown, but only inside
        # `get_signing_key`, which `_signing_key` deliberately does not call — so
        # it is inert on our path today. Pinned to 0 so it stays that way if the
        # lookup is ever re-routed through `get_signing_key`: pyjwt's cooldown is
        # stamped by EVERY successful fetch (initial and TTL-expiry included),
        # which would add up to 30s of rotation lag on top of our own throttle.
        cooldown_duration=0,
    )


# Last cache-bypassing refresh per issuer, guarded because request handling is
# offloaded to a thread pool and several threads can miss the same `kid` at once.
_last_forced_refresh: dict[str, float] = {}
_refresh_lock = threading.Lock()


def _reset_jwks_refresh_throttle() -> None:
    """Clear the per-issuer refresh throttle. Test seam; not called in prod."""
    with _refresh_lock:
        _last_forced_refresh.clear()


def _signing_key(token: str, issuer: str) -> Any:
    """Resolve the signing key for ``token`` from ``issuer``'s JWKS.

    Deliberately NOT ``get_signing_key_from_jwt``: that helper's ``kid``-miss
    path forces an uncached fetch on every call, which an unauthenticated caller
    controls (see ``_JWKS_REFRESH_MIN_INTERVAL_SECONDS``). The lookup is spelled
    out here so the refresh can be throttled per issuer while the cached-hit
    path — the one every legitimate token takes — stays exactly as before.

    Seam: tests monkeypatch this to return a locally generated public key
    instead of reaching the network. ``tests/unit/test_no_jwks_fetch_on_event_loop.py``
    deliberately restores the real implementation and patches BELOW it, at
    ``urllib.request.OpenerDirector.open``, because that is the only level at which the
    blocking call is observable.
    """
    client = _jwks_client(issuer)
    kid = jwt.get_unverified_header(token).get("kid")

    # Cached set first. This is the hit path for every legitimately signed
    # token and it performs no I/O once the set is warm.
    signing_key = client.match_kid(client.get_signing_keys(), kid) if kid else None
    if signing_key is not None:
        return signing_key.key

    # Miss. A refresh is warranted — Keycloak may have rotated — but only if we
    # have not just done one for this issuer.
    now = time.monotonic()
    with _refresh_lock:
        last = _last_forced_refresh.get(issuer)
        may_refresh = last is None or (now - last) >= _JWKS_REFRESH_MIN_INTERVAL_SECONDS
        if may_refresh:
            _last_forced_refresh[issuer] = now

    if not may_refresh:
        # Fail closed WITHOUT touching the network. The caller turns this into a
        # 401 exactly as it would any other resolution failure.
        logger.warning(
            "JWKS refresh throttled for issuer %s (kid %r not in the cached set, "
            "last refresh %.1fs ago) — refusing the token without a fetch",
            issuer,
            kid,
            now - (last or now),
        )
        raise PyJWKClientError(f"unknown kid {kid!r} and JWKS refresh is throttled")

    signing_key = client.match_kid(client.get_signing_keys(refresh=True), kid) if kid else None
    if signing_key is None:
        raise PyJWKClientError(f"unable to find a signing key matching kid {kid!r}")
    return signing_key.key


def looks_like_jwt(token: str) -> bool:
    """True when ``token`` is shaped like a JWS compact serialization.

    Opaque license tokens are NOT three dot-separated segments, so they keep
    falling through to the introspect path untouched. This predicate is what
    lets the middleware tell "a JWT that failed verification" (→ 401, never
    fall through) apart from "not a JWT at all" (→ try the other auth methods).
    """
    parts = token.split(".")
    return len(parts) == 3 and all(parts)


def verify_jwt(token: str, *, allowed: tuple[str, ...] | None = None) -> dict[str, Any]:
    """Verify signature, expiry and issuer; return the trusted claims.

    ``allowed`` overrides the issuer allowlist (default ``trusted_issuers()``);
    ``verify_user_jwt`` passes ``user_trusted_issuers()``.

    Raises:
        InvalidTokenError: signature invalid, expired, unknown/absent issuer,
            wrong algorithm, or the JWKS could not be resolved. Callers MUST
            treat any raise as "unauthenticated" — there is no fail-open path.
    """
    if allowed is None:
        allowed = trusted_issuers()
    if not allowed:
        # Refuse to guess. An empty allowlist means KEYCLOAK_URL was never
        # configured; accepting anything here would reinstate the very hole
        # this module exists to close.
        raise InvalidTokenError("no trusted issuers configured")

    # Read `iss` from the UNVERIFIED payload purely to pick which JWKS to
    # fetch. Nothing is trusted from this read: the issuer must be in the
    # allowlist, and `jwt.decode` below re-checks `iss` against the pinned
    # value after verifying the signature.
    try:
        unverified = jwt.decode(token, options={"verify_signature": False})
    except Exception as exc:  # malformed token
        raise InvalidTokenError(f"malformed token: {exc}") from exc

    issuer = str(unverified.get("iss") or "").rstrip("/")
    if issuer not in allowed:
        raise InvalidTokenError(f"untrusted issuer: {issuer or '<missing>'}")

    try:
        key = _signing_key(token, issuer)
    except Exception as exc:
        # A JWKS fetch failure is an authentication failure, never a bypass.
        logger.warning("JWKS resolution failed for issuer %s: %s", issuer, exc)
        raise InvalidTokenError(f"could not resolve signing key: {exc}") from exc

    claims: dict[str, Any] = jwt.decode(
        token,
        key,
        algorithms=ALLOWED_ALGORITHMS,
        issuer=issuer,
        leeway=EXP_LEEWAY_SECONDS,
        options={
            "verify_signature": True,
            "verify_exp": True,
            "verify_iss": True,
            # Keycloak's `aud` varies per client (portal-bff, fr-backend,
            # commonpractice, …). Binding it needs an audit of what each SA
            # client actually mints and would cause a second, independent
            # breakage wave — deliberately left to its own change.
            "verify_aud": False,
            "require": ["exp", "iss"],
        },
    )
    return claims


#: Default realms whose END-USER tokens a trusted relay may forward in
#: ``X-Forwarded-User-Authorization``. ``portal`` only, so behaviour is unchanged
#: until gitops sets ``USER_TRUSTED_ISSUERS`` (realm-per-app P0). ``secure-apps``
#: mints service accounts, not people, and is never a user realm.
DEFAULT_USER_TRUSTED_REALMS = ("portal",)


def _issuer_for(entry: str) -> str:
    """A ``USER_TRUSTED_ISSUERS`` entry is a full issuer URL or a bare realm name.

    A bare realm name is expanded against ``KEYCLOAK_URL`` (same base as
    ``trusted_issuers``), so the allowlist survives a Keycloak host move.
    """
    entry = entry.strip().rstrip("/")
    if "://" in entry:
        return entry
    base = _keycloak_base_url()
    return f"{base}/realms/{entry}" if base and entry else ""


def user_trusted_issuers() -> tuple[str, ...]:
    """Issuers whose END-USER tokens ``verify_user_jwt`` accepts.

    ``USER_TRUSTED_ISSUERS`` (comma-separated; full issuer URLs or realm names)
    wins when set, else ``DEFAULT_USER_TRUSTED_REALMS``. Independent of
    ``KEYCLOAK_TRUSTED_ISSUERS`` when set: that list governs the caller's own
    ``Authorization`` (mostly S2S), this one governs whose USERS may be forwarded.
    Unset, it is the ``portal`` subset of ``trusted_issuers()`` — unchanged.
    """
    explicit = os.getenv("USER_TRUSTED_ISSUERS", "").strip()
    if explicit:
        return tuple(i for i in (_issuer_for(e) for e in explicit.split(",") if e.strip()) if i)
    # Unset: EXACTLY the pre-P0 behaviour — the portal-realm subset of
    # ``trusted_issuers()`` (never a widening of it).
    suffixes = tuple(f"/realms/{realm}" for realm in DEFAULT_USER_TRUSTED_REALMS)
    return tuple(i for i in trusted_issuers() if i.endswith(suffixes))


def realm_of_issuer(issuer: str) -> str | None:
    """``https://host/realms/<realm>`` -> ``<realm>`` (None when not a realm issuer)."""
    issuer = issuer.rstrip("/")
    if "/realms/" not in issuer:
        return None
    return issuer.split("/realms/")[-1] or None


def verify_user_jwt(token: str) -> tuple[dict[str, Any], str]:
    """Verify a forwarded END-USER token; return ``(claims, realm)``.

    Signature, expiry, RS256 and ``issuer=`` pinning exactly as ``verify_jwt``,
    but against ``user_trusted_issuers()`` instead of ``trusted_issuers()``; the
    token must also name a subject. The returned realm is what callers key
    ``realm_owner`` on (cross-realm tenant forgery, realm-per-app P0).

    Used for the end-user token a trusted relay forwards in
    ``X-Forwarded-User-Authorization``. Raises ``InvalidTokenError`` on any
    failure; callers MUST refuse the request, never fall back to a weaker path.
    """
    claims = verify_jwt(token, allowed=user_trusted_issuers())
    issuer = str(claims.get("iss") or "").rstrip("/")
    realm = realm_of_issuer(issuer)
    if not realm:
        raise InvalidTokenError(f"forwarded token issuer names no realm: {issuer}")
    if not claims.get("sub"):
        raise InvalidTokenError("forwarded token has no sub")
    return claims, realm
