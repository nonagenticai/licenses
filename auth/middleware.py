"""Authentication middleware.

JWTs are verified locally (signature/expiry/issuer, auth/jwt_verify.py) — the
service does NOT trust ext_authz for validation. Extracts user identity from:
1. Authorization Bearer JWT — verified here; the token's own tenant claim is
   authoritative. A client X-Tenant-Id may retarget another tenant only to a
   tenant the caller's OWNER (api_client, migration 050) owns or holds a grant
   on; a cross_owner (break-glass) client may retarget any tenant.
2. Opaque license token (introspected via /license/introspect) — tenant comes
   from the introspection result, never client headers.
A protected request with no Bearer token fails closed (401).
"""

import asyncio
import logging
import os
import re
import uuid
from collections.abc import Callable
from typing import cast

import httpx
from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse
from jwt.exceptions import InvalidTokenError
from starlette.middleware.base import BaseHTTPMiddleware

from .jwt_verify import looks_like_jwt, verify_jwt
from .models import User
from .tenant import validate_tenant_uuid

logger = logging.getLogger(__name__)

# Public endpoints that don't require authentication
PUBLIC_ENDPOINTS: set[str] = {
    "/",
    "/favicon.ico",
    "/health",
    "/health/ready",
    "/health/live",
    "/licenses/health",
    # Prometheus scrapes this by POD IP off the `kubernetes-pods` job. It is
    # listed here EXPLICITLY rather than relying on "not in
    # PROTECTED_PREFIXES, therefore open": if a future prefix change makes
    # the default deny, an unlisted /metrics would start 401-ing and the
    # expiry series would vanish silently -- which is the exact failure the
    # LicenseExpiryCollectorDown / absent() alerts exist to catch, but there
    # is no reason to depend on an alert for something a set entry fixes.
    "/metrics",
    "/docs",
    "/redoc",
    "/openapi.json",
}

# Protected path prefixes; everything else is open by default
PROTECTED_PREFIXES: set[str] = {
    "/licenses",
    "/apps",
    "/plans",
    "/regions",
    "/license/revoke",
    "/internal",
    "/tenants",
    "/offerings",
}

# Roles granted to license-token-authenticated users (validated via introspect).
# Only reader access — admin/manager roles require a Keycloak JWT.
LICENSE_TOKEN_ROLES: list[str] = [
    "license-reader",
]

# The ONLY no-token S2S endpoints that are intentionally auth-exempt: the
# byo-mode LOOKUP routes (GET), which worker_management calls on the trusted
# network with no license token to enforce byo_required on no-token worker runs.
# Match the EXACT route shapes, not a bare ``endswith("/byo-mode")`` suffix — a
# suffix test would auth-exempt ANY GET path ending in ``/byo-mode`` (e.g. a
# future protected route, or a crafted ``/evil/byo-mode``), turning a naming
# coincidence into an auth bypass. Anchored + single-segment (``[^/]+``) so only
# Anchored + single-segment (``[^/]+``) so only the real
# ``/tenants/{name_or_id}/byo-mode`` shape qualifies. The write variants
# (PATCH/PUT/DELETE) are NOT exempt here — they keep their ``license-admin``
# role dependency; only the GET method reaches this exemption.
#
# ⚠️ THE PER-APP SHAPE WAS REMOVED FROM THIS PATTERN (2026-08-29), and removing
# it cannot break a caller. A second alternation used to match
# ``/tenants/{sel}/apps/{app_key}/byo-mode`` — but on that path only PUT and
# DELETE existed, so for GET it was DORMANT: every request it matched went on
# to 404 for want of a route. Nothing could depend on it.
#
# It was still a trap, though NOT the one it looks like. Adding the obvious
# missing ``GET`` on that path — which this change does — does not open the
# route. MEASURED, by restoring the alternation and re-running: an authenticated
# **license-admin** receives 401. This exemption ends in ``return await
# call_next(request)`` WITHOUT authenticating, so ``request.state.user`` is never
# populated; the tenant-wide GET can live with that because it has no role
# dependency, but the per-app GET carries ``Depends(require_any_role(...))``,
# which then refuses everyone. The result is a route usable by nobody, failing
# in the one way an operator will not suspect: only on this path, only with a
# perfectly good admin token.
#
# An exemption that pre-authorises a route nobody has written yet cannot be
# reviewed, because at review time there is nothing to look at.
#
# The per-app GET is an admin/reader read of provisioning configuration
# (including ``offering_code`` provenance), not the S2S entitlement lookup that
# worker_management fails open on. It has no reason to be exempt. If a tokenless
# per-app lookup is ever genuinely needed, add the alternation back THEN — in the
# change that needs it, and drop the route's role dependency in the same breath.
# ── Caller ownership (migration 050) ─────────────────────────────────────────
# The middleware has no database of its own; main.py registers these two seams at
# import time (``register_ownership_hooks``). Both are BLOCKING and are always run
# via ``asyncio.to_thread``.
#   resolve_client(realm, azp) -> (owner_key, cross_owner) | None
#   can_access(owner_key, tenant_id) -> bool   (owns it, or holds an active grant)
OwnerResolver = Callable[[str, str], "tuple[str, bool] | None"]
TenantAccessCheck = Callable[[str, str], bool]
_owner_resolver: OwnerResolver | None = None
_tenant_access: TenantAccessCheck | None = None


def register_ownership_hooks(resolver: OwnerResolver, access: TenantAccessCheck) -> None:
    """Install the api_client resolver and the tenant-access check (main.py)."""
    global _owner_resolver, _tenant_access
    _owner_resolver = resolver
    _tenant_access = access


def attach_caller_owner(user: User) -> None:
    """Set ``user.owner_key`` / ``user.cross_owner`` from api_client. BLOCKING.

    Keyed on the VERIFIED (iss realm, azp) only. Unknown client, no azp, or a
    resolver fault all leave ``owner_key=None`` — which every write path refuses
    (fail closed). Never falls back to a default owner.
    """
    user.owner_key = None
    user.cross_owner = False
    if _owner_resolver is None or not user.azp or not user.realm:
        return
    try:
        resolved = _owner_resolver(user.realm, user.azp)
    except Exception:
        logger.warning(
            "owner_resolution_failed: realm=%s azp=%s (treated as unknown client)",
            user.realm,
            user.azp,
            exc_info=True,
        )
        return
    if resolved is not None:
        user.owner_key, user.cross_owner = resolved[0], bool(resolved[1])


def caller_may_retarget(user: User, tenant_id: str) -> bool:
    """May this caller act on ``tenant_id`` via X-Tenant-Id? BLOCKING."""
    if user.cross_owner:
        return True
    if not user.owner_key or _tenant_access is None:
        return False
    try:
        return bool(_tenant_access(user.owner_key, tenant_id))
    except Exception:
        logger.warning(
            "tenant_access_check_failed: owner=%s tenant=%s (refused)",
            user.owner_key,
            tenant_id,
            exc_info=True,
        )
        return False


# ── Realm ownership (migration 072, realm-per-app P0) ────────────────────────
# A Keycloak realm with a ``realm_owner`` row belongs to ONE app. A realm admin of
# that app controls every claim its tokens carry — tenant_id and realm roles
# included — so a token from that realm may act only on tenants the realm's owner
# OWNS or holds an active tenant_app_grant on. Applied to EVERY verified JWT from
# such a realm (user or service account alike): S2S callers live in secure-apps,
# which has no row, so this never touches them. A realm with no row (portal,
# secure-apps) keeps the pre-072 behaviour exactly.
RealmOwnerResolver = Callable[[str], "str | None"]
_realm_owner_resolver: RealmOwnerResolver | None = None

#: Stable error code for a refused cross-realm tenant.
REALM_OWNER_MISMATCH = "realm_owner_mismatch"


def register_realm_owner_hook(resolver: RealmOwnerResolver) -> None:
    """Install the realm_owner resolver (main.py)."""
    global _realm_owner_resolver
    _realm_owner_resolver = resolver


def resolve_realm_owner(realm: str | None) -> str | None:
    """owner_key for ``realm`` or None. BLOCKING. A resolver FAULT propagates:
    callers must refuse rather than treat an unknown answer as "no row"."""
    if _realm_owner_resolver is None or not realm:
        return None
    return _realm_owner_resolver(realm)


def attach_realm_owner(user: User) -> None:
    """Set ``user.realm_owner_key`` from the VERIFIED iss realm. BLOCKING; raises on fault."""
    user.realm_owner_key = resolve_realm_owner(user.realm)


def realm_owner_permits(realm_owner_key: str | None, tenant_id: str | None) -> bool:
    """May a token whose realm is owned by ``realm_owner_key`` act on ``tenant_id``? BLOCKING.

    True when the realm has no owner (pre-072 behaviour). Otherwise the owner must
    own the tenant or hold an active grant on it; a missing tenant, a missing access
    hook or a fault all refuse (fail closed).
    """
    if realm_owner_key is None:
        return True
    if not tenant_id or _tenant_access is None:
        return False
    try:
        return bool(_tenant_access(realm_owner_key, str(tenant_id)))
    except Exception:
        logger.warning(
            "realm_owner_check_failed: owner=%s tenant=%s (refused)",
            realm_owner_key,
            tenant_id,
            exc_info=True,
        )
        return False


def realm_owner_mismatch_detail() -> dict[str, str]:
    return {
        "error": REALM_OWNER_MISMATCH,
        "message": (
            "the token's Keycloak realm belongs to an app that neither owns nor holds "
            "a grant on this tenant"
        ),
    }


_BYO_MODE_OPEN_ROUTES = re.compile(r"^/tenants/(?P<selector>[^/]+)/byo-mode$")

# The exemption is narrowed to UUID selectors — the shape every real caller uses.
#
# The route resolves ``name_or_id``, so a tokenless caller handing it a NAME got
# back that tenant's UUID and BYO posture — a name-to-UUID oracle for anyone who
# can reach the pod CIDR. A UUID lookup tells the caller nothing it did not
# already know, so the tokenless path is kept for UUIDs only; a name selector
# now goes through normal auth like the rest of ``/tenants`` and 401s without a
# valid bearer. Authenticated callers can still look a tenant up by name.
#
# Why this was staged and not shipped outright (2026-08-18, PR #156): the S2S
# callers FAIL OPEN on any non-200 (see the route's own docstring), so narrowing
# against a caller that passes a name would have silently disabled byo_required
# enforcement fleet-wide with no error anywhere. The evidence gathered since:
#   * `worker_management/src/api/auth_middleware.py` resolves ``X-Tenant-Id`` to a
#     canonical UUID (``_trusted_network_tenant``) BEFORE it calls this route;
#   * `billing-service/src/services/byo_mode.py` passes the ``tenant_id`` column;
#   * 23 of 23 live byo-mode requests used a UUID;
#   * the ``byo_mode_open_route_name_lookup`` warning below stayed at zero.
# The warning is kept: it now marks a tokenless name lookup that was REFUSED,
# which is still the fact needed to catch a caller no grep found.
#
# ``_is_tenant_uuid`` is the server's own resolver definition (``uuid.UUID``),
# not a dashed-hex regex — see that function for why the shape test had to be
# fixed FIRST: a stricter shape here would 401 a caller the route itself accepts.


def _is_tenant_uuid(selector: str) -> bool:
    """Is ``selector`` a UUID *as this service resolves one*?

    This was a strict dashed-hex regex. That is not the server's definition, and
    the disagreement ran in BOTH directions — measured, not argued:

        0195f48b-d000-7000-8000-000000000001        uuid.UUID ok | regex ok
        0195f48bd0007000800000000000000 (no dashes) uuid.UUID ok | regex NO  <-
        {0195f48b-d000-7000-8000-000000000001}      uuid.UUID ok | regex NO  <-
        urn:uuid:0195f48b-d000-7000-8000-0000000001 uuid.UUID ok | regex NO  <-
        "0195f48b-d000-7000-8000-000000000001\n"    uuid.UUID NO | regex ok  <-

    (``$`` matches before a trailing newline, which is the permissive direction.)

    Both directions matter, and the second is a landmine. The route resolves
    ``name_or_id`` through the UUID constructor, so the three middle spellings ARE
    valid tenant ids that return the tenant — while the regex logged each as a
    suspected tenant-NAME oracle probe. Not merely noise: the log line exists to
    decide whether this auth exemption can be narrowed to UUIDs, and a false
    positive argues for leaving the hole open.

    The other direction is worse. The narrowing this instrumentation was written to
    justify would have 401'd those three spellings — and the only caller,
    ``worker_management``, FAILS OPEN on any non-200, returning ``local_allowed``.
    Shipping that follow-up on a wrong shape test would have handed any pod-CIDR
    caller a deliberate ``byo_required`` bypass: exactly what the instrumentation
    exists to prevent.

    Tenant names are constrained to ``^[a-z0-9][a-z0-9-]*$``, so ``urn:uuid:...``
    and ``{...}`` can never BE names — they were unambiguous false positives.

    Using the constructor makes this the server's own definition by construction,
    so the two cannot drift apart again.
    """
    try:
        uuid.UUID(selector)
    except ValueError, AttributeError, TypeError:
        return False
    return True


# Self-introspect URL (this service's own introspect endpoint, called locally)
_SELF_INTROSPECT_PORT = os.getenv("PORT", "8544")


def user_from_verified_jwt(token: str) -> User | None:
    """Extract user from a CRYPTOGRAPHICALLY VERIFIED JWT.

    Returns None when the token is not a JWT at all (an opaque license
    token), so the caller can fall through to the introspect path. Raises
    nothing: a JWT that fails verification also yields None, and the
    caller distinguishes the two cases via ``looks_like_jwt`` so a forged
    token can never fall through to a weaker branch.

    Every claim below — roles included — comes from ``verify_jwt``'s
    return value, i.e. from a payload whose signature, expiry and issuer
    were checked. See ``auth/jwt_verify.py`` for why this is issuer-keyed.
    """
    if not looks_like_jwt(token):
        return None

    try:
        payload = verify_jwt(token)
    except InvalidTokenError as e:
        logger.warning("JWT verification failed: %s", e)
        return None

    try:
        sub = payload.get("sub")
        if not sub:
            return None

        return User(
            sub=sub,
            preferred_username=payload.get("preferred_username", sub),
            email=payload.get("email"),
            email_verified=payload.get("email_verified", False),
            name=payload.get("name"),
            realm_access=payload.get("realm_access", {}),
            resource_access=payload.get("resource_access", {}),
            exp=payload.get("exp"),
            iat=payload.get("iat"),
            iss=payload.get("iss"),
            # Verified minting client_id — with the iss realm, the api_client key
            # that resolves the caller's OWNER (migration 050), which decides the
            # X-Tenant-Id reconciliation in dispatch().
            azp=payload.get("azp"),
            # Canonical tenant carrier.
            #
            # ⚠️ CORRECTED 2026-08-17. This said "the dispatch loop still
            # lets an ext_authz X-Tenant-Id header override this" — hole `a`,
            # stated in the present tense, in the very function that PRODUCES
            # `tenant_id`. Stage 2 closed it, and migration 050 narrowed it to
            # ownership: the header retargets a tenant only when the caller's
            # owner (api_client, keyed on verified iss realm + azp) owns it or
            # holds an active grant on it (cross_owner: any, audited); a
            # known owner naming any other tenant is a 403, and an unknown
            # client's header is a 403 against a present claim or ignored when
            # there is none (see the reconciliation in `dispatch`). When there
            # is no header, get_tenant_id/require_tenant_id use this claim rather than
            # falling back to the subject (a user id).
            tenant_id=payload.get("tenant_id"),
            organization_id=payload.get("organization_id"),
        )
    # Verified claims can still be structurally wrong (e.g. realm_access
    # not a dict), which surfaces as ValueError from pydantic coercion or
    # AttributeError. Anything else is unexpected and should propagate.
    except (ValueError, AttributeError) as e:
        logger.debug("JWT claim extraction failed: %s", e)
        return None


class AuthMiddleware(BaseHTTPMiddleware):
    """Middleware that authenticates a request from a VERIFIED credential.

    Authentication methods (in priority order):
    1. JWT Bearer token - signature/expiry/issuer VERIFIED here (auth/jwt_verify.py)
    2. Opaque license token - INTROSPECTED via local /license/introspect endpoint;
       identity (tenant_id, roles) comes from the introspection result, never from
       client-supplied X-License-Id / X-Tenant-Id headers.
    3. Nothing else. A protected request with no Bearer token is a 401.

    ⚠️ This list used to carry a third method — "X-Tenant-Id header only, minimal
    user with no roles" — flagged "STILL HEADER-TRUSTING". It contradicted the
    ✅ CLOSED note for hole `b2` twelve lines below it in this same docstring:
    that fallback was REMOVED. A stale entry in a numbered list of auth methods is
    worse than a stale sentence, because it reads as an inventory of the trust
    surface — someone auditing this class would have counted three ways in, and
    one of them does not exist.

    SECURITY (2026-05-03 audit → role-escalation closed 2026-07-20 #78;
    opaque-token header-trust closed 2026-07-23 Stage 1):
    Path 1 previously base64-decoded the payload and never read the signature,
    on the stated assumption that upstream ext_authz had validated it. Roles
    (`realm_access`) and `tenant_id` came straight from that unverified
    payload, so a three-line request minted a `license-admin` for any tenant.

    The assumption was false for in-mesh traffic, which is the reachable case.
    `clusters/strato/services/licenses/networkpolicy.yaml` admits the whole pod
    CIDR (10.42.0.0/16) on the HBONE port, so any workload permitted by the
    namespace AuthorizationPolicies reaches this middleware *directly*, never
    traversing the ingress-gateway Lua filter that strips client-supplied
    X-Tenant-Id / X-License-Id.

    Path 1 now verifies cryptographically, and the opaque-token path (method 2)
    now ALWAYS introspects the token instead of short-circuiting to trust the
    ext_authz headers. Three properties keep the fixes from being silently
    bypassed, and all are load-bearing:
      - A token that IS shaped like a JWT but fails verification returns 401
        immediately. It must never fall through to the header paths.
      - An opaque (non-JWT) token is authenticated only by a *successful*
        introspection; a garbage token with forged X-License-Id / X-Tenant-Id
        headers no longer authenticates (it used to grant license-reader on the
        header's tenant). The introspected tenant is authoritative.
      - Opaque license tokens are not three-segment, so `looks_like_jwt` is
        False for them and the introspect path is reached exactly as before.

    ⚠️ SUPERSEDED IN PART by migration 050 (tenant ownership per app): the
    retarget rule below no longer reads "license-admin or an allowlisted proxy
    azp". A retarget is admitted only to a tenant the caller's OWNER (api_client,
    keyed on the verified iss realm + azp) owns or holds a grant on; a
    cross_owner client may retarget any tenant, audited as ``cross_owner_action``.

    ✅ CLOSED in Stage 2 (this change):
      - hole `a`: a client X-Tenant-Id can no longer OVERRIDE the verified JWT
        tenant for an ordinary caller. The header retargets a different tenant
        ONLY to a tenant the caller's owner (api_client) owns or holds an active
        grant on (cross_owner: any, audited) — the 050 model that replaced the
        env allowlist of proxy azps; a known owner naming any other tenant gets
        403 ``tenant_not_accessible``, an unknown client whose header contradicts
        a present claim gets 403, and an unknown claim-less client's header is
        ignored. get_tenant_id /
        require_tenant_id now consume the reconciled request.state.user.tenant_id
        instead of re-reading the raw header. Admin provisioning (create_license /
        require_tenant_id, X-Tenant-Id target) is preserved via the admin gate.
      - hole `b2`: the no-token + X-Tenant-Id roleless-user fallback was removed;
        a protected request without a Bearer now fails closed (401).
    The header is trustworthy for gateway traffic (ext_authz strips the client
    value and injects x-tenant-id == the token's own tenant), so this change
    cannot break real gateway users; the closed surface is direct in-mesh forgers.

    NOTE for whoever reads the old follow-up list (it was stale — verified
    2026-07-20): the only published route is the service's own deployment
    hostname, which IS in the gateway's ext_authz `AUTH_REQUIRED_HOSTS`
    allowlist. The referenced gateway authz policy lived in a retired cluster
    tree and no longer exists; the stale draft that staged its premise (which
    assumed the now-removed unverified-JWT decode) has been deleted. The edge
    is not the gap; pod-to-pod is, which is why verification belongs here.
    """

    def __init__(self, app, excluded_paths: list[str] | None = None):
        super().__init__(app)
        self.excluded_paths = set(excluded_paths or PUBLIC_ENDPOINTS)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process each request."""

        # Default-open: only enforce auth on protected prefixes
        path = request.url.path.rstrip("/") or "/"
        # The byo-mode lookup BY UUID is an intentionally-open S2S endpoint:
        # worker_management and billing-service call GET /tenants/{uuid}/byo-mode
        # on the trusted network with NO token to enforce byo_required on no-token
        # worker runs. Keep it open even though /tenants is a protected prefix
        # (tenant CRUD requires license-admin).
        # Match the EXACT route shape (_BYO_MODE_OPEN_ROUTES) — a bare
        # ``endswith("/byo-mode")`` would auth-exempt any GET path ending in
        # /byo-mode, an over-broad exemption on a financial-services boundary.
        if request.method == "GET" and (byo_match := _BYO_MODE_OPEN_ROUTES.fullmatch(path)):
            # Single group now: the per-app alternation (and its `app_selector`
            # group) was removed above. Reading a group that no longer exists
            # raises IndexError, so these two must change together.
            selector = byo_match.group("selector")
            if _is_tenant_uuid(selector):
                return cast(Response, await call_next(request))
            # A NAME selector is NOT exempt: tokenless, it was a name-to-UUID
            # oracle for anyone on the pod CIDR. It falls through to the normal
            # auth path below (/tenants is protected), so a valid bearer still
            # resolves by name and a missing one gets the usual 401. Logged
            # because no caller in the tree does this — if this line fires,
            # either a caller exists that no grep found (and it is now getting
            # 401s that worker_management-style callers convert to fail-open),
            # or someone is enumerating tenants. WARNING, not ERROR: the
            # severity is caller-drivable and the fleet runs log-level health
            # checks on ERROR.
            logger.warning(
                "byo_mode_open_route_name_lookup: refused tokenless exemption "
                "selector=%r path=%r client=%r",
                selector,
                path,
                request.client.host if request.client else None,
            )
        if self._is_excluded(path) or not self._is_protected(path):
            return cast(Response, await call_next(request))

        # Get trusted tenant_id from ext_authz header. tenant_id MUST be
        # a UUID — non-UUID values fail loud with 400 (no backward-compat
        # shim; see auth/tenant.py docstring).
        raw_tenant_id = request.headers.get("X-Tenant-Id")
        try:
            tenant_id: str | None = (
                validate_tenant_uuid(raw_tenant_id, source="X-Tenant-Id") if raw_tenant_id else None
            )
        except HTTPException as exc:
            return self._error_response(exc.status_code, str(exc.detail))

        # Try to extract user from Authorization header
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[7:]

            # Try JWT verification first.
            #
            # ⚠️ OFFLOADED, and it must stay offloaded. `_extract_user_from_jwt`
            # is synchronous and can perform BLOCKING network I/O: resolving the
            # signing key goes through PyJWKClient, whose fetch is a
            # `urllib.request.urlopen` bounded only by JWKS_TIMEOUT_SECONDS.
            # Called inline from this coroutine it stalled the whole worker —
            # every other in-flight request plus /health/live and /health/ready —
            # and an UNAUTHENTICATED caller could trigger it at will with an
            # unsigned token carrying an allowlisted `iss` and an unknown `kid`,
            # because key resolution happens before the signature is checked.
            # Measured on the pre-fix tree: 2/2 fetches on the loop thread.
            # Pinned by tests/unit/test_no_jwks_fetch_on_event_loop.py.
            user = await asyncio.to_thread(self._extract_user_from_jwt, token)
            if user:
                # Stage 2 header-trust close (hole `a`). The VERIFIED token
                # identity is authoritative for the operative tenant. A client
                # X-Tenant-Id may RETARGET a different tenant only when the
                # caller's owner (api_client) owns that tenant or holds an active
                # grant on it (cross_owner: any, audited). A known owner naming
                # any other tenant -> 403 tenant_not_accessible; an unknown
                # client whose header CONTRADICTS a present verified claim -> 403;
                # an unknown claim-less client's header is simply ignored (the
                # tenant then resolves from the verified claim/subject in
                # get_tenant_id, never from the attacker-controlled header).
                # In the legit gateway flow ext_authz strips the client header and
                # injects x-tenant-id == the token's own tenant, so header == claim
                # by construction and this branch never 403s real traffic.
                # Migration 050: the caller's owner comes from api_client, keyed
                # on the verified (iss realm, azp). The X-Tenant-Id retarget is
                # admitted ONLY to a tenant that owner owns or holds a grant on
                # (cross_owner: any, audited). This replaced both the env
                # allowlist of proxy azps and the blanket `license-admin` retarget:
                # holding a role no longer implies cross-app reach.
                await asyncio.to_thread(attach_caller_owner, user)
                if tenant_id and tenant_id != user.tenant_id:
                    if await asyncio.to_thread(caller_may_retarget, user, tenant_id):
                        if user.cross_owner:
                            logger.warning(
                                "cross_owner_action: action=retarget tenant=%s azp=%s "
                                "realm=%s sub=%s",
                                tenant_id,
                                user.azp,
                                user.realm,
                                user.sub,
                            )
                        user.tenant_id = tenant_id
                    elif user.owner_key is not None:
                        # A KNOWN owning app naming a tenant it neither owns nor
                        # holds a grant on: say so explicitly, so e.g. w2v can read
                        # "not onboarded to this app" instead of a generic 401.
                        logger.warning(
                            "tenant_not_accessible: retarget refused owner=%s azp=%s "
                            "claim=%s header=%s",
                            user.owner_key,
                            user.azp,
                            user.tenant_id,
                            tenant_id,
                        )
                        return JSONResponse(
                            status_code=403,
                            content={
                                "detail": {
                                    "error": "tenant_not_accessible",
                                    "message": (
                                        "the X-Tenant-Id tenant is neither owned by nor "
                                        "granted to the calling app"
                                    ),
                                }
                            },
                        )
                    elif user.tenant_id:
                        logger.warning(
                            "Blocked cross-tenant header override: azp=%s claim=%s header=%s",
                            user.azp,
                            user.tenant_id,
                            tenant_id,
                        )
                        return self._error_response(
                            403, "X-Tenant-Id does not match the authenticated tenant"
                        )
                    else:
                        # Unknown client (no owner) and no verified tenant claim to
                        # contradict: ignore the header. The resolver fails closed.
                        logger.warning(
                            "Ignoring X-Tenant-Id from non-proxy claim-less token: azp=%s",
                            user.azp,
                        )
                # Migration 072 (realm-per-app P0): a token from a realm owned by
                # an app may act only on that app's tenants (owned or granted).
                # Checked AFTER reconciliation so a retargeted tenant is covered too.
                try:
                    await asyncio.to_thread(attach_realm_owner, user)
                except Exception:
                    logger.warning(
                        "realm_owner_resolution_failed: realm=%s (refused)",
                        user.realm,
                        exc_info=True,
                    )
                    return JSONResponse(
                        status_code=503,
                        content={
                            "detail": {
                                "error": "realm_owner_unresolved",
                                "message": "the token realm's owner could not be resolved",
                            }
                        },
                    )
                if user.realm_owner_key is not None and user.tenant_id:
                    if not await asyncio.to_thread(
                        realm_owner_permits, user.realm_owner_key, user.tenant_id
                    ):
                        logger.warning(
                            "realm_owner_mismatch: realm=%s realm_owner=%s tenant=%s azp=%s sub=%s",
                            user.realm,
                            user.realm_owner_key,
                            user.tenant_id,
                            user.azp,
                            user.sub,
                        )
                        return JSONResponse(
                            status_code=403,
                            content={"detail": realm_owner_mismatch_detail()},
                        )
                request.state.user = user
                logger.debug(f"Authenticated via JWT: {user.preferred_username}")
                return cast(Response, await call_next(request))

            # A token SHAPED like a JWT that did not verify is a hard 401 — it
            # must NOT fall through to the introspect/header paths below, which
            # would otherwise re-admit the forged token's bearer.
            #
            # ⚠️ Scope, stated honestly — and CORRECTED 2026-08-17. This comment
            # used to list two holes as "STILL open" in the present tense. Both
            # had already been closed by Stage 2, which the class docstring above
            # records as ✅ CLOSED, and the closing code sits a few dozen lines above,
            # in the reconciliation block of `dispatch`. A stale present-tense note about a defect its own file
            # has fixed is worse than no note: it invites a reader to re-open the
            # hole "back" to a state that never existed, or to size a blast radius
            # against a threat model that is gone.
            #
            # The accurate history:
            #   - #78 (2026-07-20) closed the ROLE-ESCALATION half — a forged JWT
            #     can no longer mint `license-admin`.
            #   - Stage 1 (2026-07-23, below) closed the OPAQUE-TOKEN header-trust
            #     half — an opaque token is now always introspected, so a garbage
            #     token + forged X-Tenant-Id no longer grants `license-reader` on
            #     a chosen tenant.
            #   - Stage 2 closed BOTH remaining holes: a client X-Tenant-Id
            #     retargets a tenant only to one the caller's owner (api_client)
            #     owns or holds a grant on — migration 050 (see the
            #     reconciliation above, which 403s a refused retarget and ignores
            #     an unknown claim-less client's header), and the
            #     no-token X-Tenant-Id fallback was removed — a protected request
            #     without a Bearer now fails closed with 401 (see the end of this
            #     method).
            if looks_like_jwt(token):
                return self._error_response(401, "Invalid or expired token")

            # Not a JWT (opaque license token). Authenticate it by INTROSPECTING
            # the token itself — never by trusting client-supplied X-License-Id /
            # X-Tenant-Id headers.
            #
            # This closes the header-trust half left open by #78 (the docstring's
            # "separate change", Stage 1 — see plan
            # ~/.claude/plans/licenses-header-trust-close-b1-2026-07-23.md). The
            # old code short-circuited to `_user_from_ext_authz_headers` whenever
            # both headers were present, granting `license-reader` on the header's
            # tenant WITHOUT validating the bearer token at all — so an opaque
            # garbage token plus a forged X-Tenant-Id read any tenant's data. In
            # the LEGIT gateway flow the same request carries a *valid* license
            # token (Envoy forwards the original Authorization) and license-authz
            # injects x-tenant-id FROM that token's introspection
            # (license-authz/src/main.py:1212), so introspecting locally yields the
            # identical tenant — the header was never load-bearing, only the token
            # is. Introspect is a local 127.0.0.1 call, so always doing it is cheap.
            user = await self._introspect_license_token(token)
            if user:
                request.state.user = user
                logger.debug(f"Authenticated via token introspect: {user.sub}")
                return cast(Response, await call_next(request))

            # All methods failed
            return self._error_response(401, "Invalid or expired token")

        # Stage 2 (hole `b2`). No Authorization token on a protected prefix.
        # Legit callers always carry a Bearer here (the gateway forwards the end
        # user's token; internal SAs hold a JWT; opaque-token callers are handled
        # above). The old X-Tenant-Id-only fallback minted a roleless User on the
        # header's tenant — reachable capability today is nil (every protected
        # route has a role dep that a roleless user 403s, and the one intentional
        # no-token endpoint, GET /tenants/{id}/byo-mode, is exempted earlier), but
        # it was a latent footgun for any future dep-less route. Fail closed.
        return self._error_response(401, "Missing authentication token")

    def _is_excluded(self, path: str) -> bool:
        """Check if path is explicitly public."""
        if path in self.excluded_paths:
            return True
        prefixes = ["/docs", "/redoc", "/static", "/openapi.json", "/favicon.ico"]
        return any(path.startswith(p) for p in prefixes)

    def _is_protected(self, path: str) -> bool:
        """Check if path falls under protected prefixes."""
        return any(path.startswith(prefix) for prefix in PROTECTED_PREFIXES)

    def _extract_user_from_jwt(self, token: str) -> User | None:
        """Instance seam over the module-level ``user_from_verified_jwt``.

        The body moved to module scope so that a route the middleware does NOT
        protect can reuse the SAME definition of "authenticated" — see
        ``main.introspect_token``'s ``INTROSPECT_REQUIRE_AUTH`` branch. Kept as a
        method because ``dispatch`` offloads it by attribute and the middleware
        tests call it on an instance; it adds no behaviour of its own.
        """
        return user_from_verified_jwt(token)

    async def _introspect_license_token(self, token: str) -> User | None:
        """Introspect an opaque license token via the local introspect endpoint.

        For direct service-to-service calls that bypass the Istio gateway,
        we validate the opaque token by calling our own /license/introspect endpoint.
        """
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"http://127.0.0.1:{_SELF_INTROSPECT_PORT}/license/introspect",
                    json={"token": token},
                    timeout=3.0,
                )
                if resp.status_code != 200:
                    logger.debug(f"Token introspect returned {resp.status_code}")
                    return None

                data = resp.json()
                if not data.get("active"):
                    logger.debug("Token introspect: token not active")
                    return None

                raw_tenant = data.get("tenant_id", "")
                try:
                    tenant_id = (
                        validate_tenant_uuid(raw_tenant, source="introspect.tenant_id")
                        if raw_tenant
                        else ""
                    )
                except HTTPException:
                    # validate_tenant_uuid raises HTTPException, which is NOT an
                    # httpx.HTTPError and NOT a ValueError — so the handler below
                    # does not catch it and it escaped `dispatch` entirely. Raised
                    # from BaseHTTPMiddleware it lands in ServerErrorMiddleware,
                    # ABOVE ExceptionMiddleware, where it cannot be turned into a
                    # response (see main.global_exception_handler).
                    #
                    # A token whose introspection returns a non-UUID tenant is an
                    # unusable identity, and this method's contract for "cannot
                    # authenticate this token" is None -> the caller 401s. Same
                    # outcome as an introspect that fails or answers inactive.
                    #
                    # The sibling call site at the top of `dispatch` returns
                    # `self._error_response(exc.status_code, str(exc.detail))`
                    # instead, and that difference is deliberate: there the tenant
                    # came from the CLIENT's own X-Tenant-Id header, so echoing 400
                    # tells the caller to fix its request. Here it came from our
                    # own introspect response, so a 400 would blame the caller for
                    # our data.
                    logger.warning(
                        "Token introspect returned a non-UUID tenant_id %r; treating the "
                        "token as unauthenticated",
                        raw_tenant,
                    )
                    return None
                license_id = data.get("license_id", "")

                return User(
                    sub=data.get("subject_id", license_id),
                    preferred_username=(
                        f"license:{license_id[:8]}..." if license_id else tenant_id
                    ),
                    tenant_id=tenant_id,
                    realm_access={"roles": LICENSE_TOKEN_ROLES},
                )
        # httpx.HTTPError covers connect/read/timeout/status; ValueError covers
        # JSON decode failures from resp.json(). Anything else should propagate.
        except (httpx.HTTPError, ValueError) as e:
            logger.debug("Token introspect failed: %s", e)
            return None

    def _error_response(self, status_code: int, message: str) -> Response:
        """Return error response."""
        return JSONResponse(
            status_code=status_code,
            content={"error": {"message": message, "status": status_code}},
            headers={"WWW-Authenticate": "Bearer"} if status_code == 401 else {},
        )
