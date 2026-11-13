"""
Tenant isolation for the licenses service.

Tenant IDs are uuid (uuid7 going forward, but any version validates).
The legacy text labels ("n8n-dev"/"gerard-dev"/"george-dev") are
``tenant_name`` values — human labels — and are NOT valid tenant_ids.

Any non-uuid value arriving at this boundary is a producer bug
(upstream service misconfigured, stale token, hardcoded fixture).
We fail loud with HTTP 400 so the producer is forced to fix its
emission rather than absorbing legacy values via a backward-compat
shim. (No shim — see commit history and feedback memory
``feedback-no-backcompat-shim.md``.)

SECURITY: Never trust user-supplied tenant fields in body/query.
Only trust the X-Tenant-Id header (injected by ext_authz) or the
JWT subject from a validated token.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import uuid

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)


# A tenant_name per the registry's own CHECK constraint (``ck_tenants_name_format``)
# is ``^[a-z0-9][a-z0-9-]*$``. Every legacy text label this error exists to explain
# ("n8n-dev", "portal-render-e2e") matches it; no credential does. A licence token is
# ``secrets.token_urlsafe(32)`` (43 chars, mixed case and underscores) and a Keycloak
# JWT carries dots and mixed case, so neither can pass. The length cap is belt-and-
# braces for the vanishingly unlikely all-lowercase token.
_SAFE_TENANT_LABEL = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_MAX_ECHO_CHARS = 32


def _safe_tenant_value(value: object) -> str:
    """Render an invalid tenant value for a log line or a 400 body, without leaking.

    Returns a quoted value when it is provably not a credential, and an
    unquoted ``<...>`` description otherwise. The description keeps what an
    operator actually needs to correlate -- the auth scheme if there is one, the
    length, and a short digest that is stable across log lines -- while never
    reproducing the secret itself.
    """
    if not isinstance(value, str):
        return f"<{type(value).__name__}>"
    text = value.strip()
    scheme, _, rest = text.partition(" ")
    if scheme.lower() in ("bearer", "basic", "token") and rest:
        return f"<{scheme.lower()} credential redacted, {len(text)} chars>"
    if len(text) <= _MAX_ECHO_CHARS and _SAFE_TENANT_LABEL.fullmatch(text):
        return repr(text)
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]
    return f"<redacted, {len(text)} chars, sha256:{digest}>"


def validate_tenant_uuid(value: str | None, *, source: str = "tenant_id") -> str:
    """Return ``value`` as a canonical UUID string, or raise HTTPException.

    This module's contract is to "fail loud so the producer is forced to fix its
    emission". It used to fail loud only at the CALLER: the rejection was raised
    with no log call at all, and ``main.http_exception_handler`` is deliberately
    scoped to 5xx, so nothing downstream logged it either. Measured on the
    deployed pods over a 12-hour window: 35 non-2xx responses, **0** WARNING
    lines, 0 ERROR lines — including a 400 that a scheduled n8n workflow has
    presumably been taking every night unnoticed. An operator cannot fix a
    producer they cannot see.

    WARNING, not ERROR, and no traceback — the same reasoning ``main._client_error``
    already documents for the sites it covers: the cause is the request, and a
    severity a caller can drive at will by sending garbage is a liability on a
    chain that runs log-level-based health checks. The message is the whole
    diagnosis.

    Raises:
        HTTPException 401: if ``value`` is empty / ``None``.
        HTTPException 400: if ``value`` is non-empty but not a UUID.
            The error body names the source (header / introspect field) and,
            when it is provably not a credential, the offending value, so
            producers can self-diagnose. A value carrying an auth scheme, or one
            that does not match the tenant-name shape, is reduced to a length
            and a short digest -- see ``_safe_tenant_value``.
    """
    if not value:
        logger.warning("tenant_context_missing: source=%s", source)
        raise HTTPException(status_code=401, detail="Missing tenant context")
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        # Echo the offending value only when it is SAFE to echo. The original
        # reasoning here -- "it is not a secret, a tenant_id is a public
        # identifier the gateway injects" -- was true when written and is false
        # now: the fleet's n8n transport convention deliberately carries
        # ``X-Tenant-Id: Bearer <licenceToken>``, so the most likely malformed
        # value on this exact path is a LIVE CREDENTIAL. Echoing it put that
        # credential in a 400 response body and in the log line below.
        # ``_safe_tenant_value`` keeps the diagnosis and drops the secret.
        shown = _safe_tenant_value(value)
        logger.warning("tenant_id_not_a_uuid: source=%s value=%s", source, shown)
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid {source} {shown}: must be a UUID. "
                "Legacy text labels (e.g. 'n8n-dev') are tenant_name "
                "values, not tenant_ids."
            ),
        ) from exc


async def get_tenant_id(request: Request) -> str:
    """Extract and validate tenant ID from a trusted source.

    Priority:
        1. X-Tenant-Id header (injected by ext_authz at the gateway).
        2. JWT ``tenant_id`` claim (validated upstream). This is the
           canonical tenant carrier the rest of the platform relies on
           (kag/fr/w2v) and the introspect path already trusts it.
        3. JWT subject — LEGACY best-effort fallback for tokens carrying
           neither a header nor a ``tenant_id`` claim (e.g. the portal-seed
           flow that deliberately sets ``tenant_id == sub``). A bare subject
           is a USER id, not a tenant, so it is accepted ONLY when it resolves
           to an existing ``tenants`` row — a genuine self-tenant. Otherwise we
           fail closed (401) rather than silently trusting a user id as a
           tenant on the read path.

    Write paths that PERSIST a tenant_id (license creation) must depend on
    :func:`require_tenant_id` instead — it refuses the subject fallback
    entirely. Storing a user ``sub`` as a tenant mis-keyed billing budgets when
    a workspace-admin token arrived without ext_authz (see billing memo
    2026-07-20); the strict dependency fails loud so the producer fixes its
    emission rather than silently corrupting downstream tenant keys.

    NOTE: the subject fallback issues ONE read-only ``db_manager.get_tenant``
    lookup (a DB round-trip) — acceptable on this read dependency and only
    reached when a token arrives without a header and without a tenant_id claim.

    Raises:
        HTTPException 401: no tenant context, or a subject that resolves to no
            ``tenants`` row.
        HTTPException 400: header or claim value is not a UUID.
    """
    # Stage 2 header-trust close: the operative tenant comes from the
    # already-reconciled ``request.state.user.tenant_id`` (the middleware made the
    # verified token identity authoritative and honored the X-Tenant-Id header
    # only for admins / trusted proxies), NOT from re-reading the raw header here.
    # Reading the header first re-opened the cross-tenant pivot the middleware
    # closed. The subject-fallback below is retained for claim-less tokens.
    user = getattr(request.state, "user", None)
    if user:
        if getattr(user, "tenant_id", None):
            return validate_tenant_uuid(user.tenant_id, source="resolved.tenant_id")
        sub = getattr(user, "sub", None)
        if sub:
            # A bare subject is only a candidate self-tenant if it is a UUID.
            # ``db_manager.get_tenant`` resolves by id OR human NAME, so passing
            # a raw non-UUID sub could match a DIFFERENT tenant's name → a
            # cross-tenant read (service-isolation break). Parse to a canonical
            # UUID first; a non-UUID subject falls through to 401.
            try:
                canonical_sub = str(uuid.UUID(str(sub)))
            except ValueError, AttributeError, TypeError:
                canonical_sub = None
            if canonical_sub is not None:
                # Deferred import: ``main`` imports this module at load time, so
                # a module-level import would be circular. By call time ``main``
                # is fully initialised. The singleton is the same object the
                # mint guard uses, so tests patch ``main.db_manager.get_tenant``.
                from main import db_manager

                # Sync SQLAlchemy call off the event loop (mirrors main.py:783).
                tenant = await asyncio.to_thread(db_manager.get_tenant, canonical_sub)
                if tenant is not None:
                    realm_owner = getattr(user, "realm_owner_key", None)
                    if isinstance(realm_owner, str):
                        # Migration 072: a self-tenant fallback is still a tenant
                        # the realm's owner must own or be granted. Deferred import:
                        # middleware imports this module at load time.
                        from .middleware import realm_owner_mismatch_detail, realm_owner_permits

                        if not await asyncio.to_thread(
                            realm_owner_permits, realm_owner, str(tenant.id)
                        ):
                            raise HTTPException(
                                status_code=403, detail=realm_owner_mismatch_detail()
                            )
                    logger.warning(
                        "licenses read-path fell back to JWT subject %r, which "
                        "resolves to existing tenant %s. The producer omitted "
                        "both X-Tenant-Id and a tenant_id claim — fix its token "
                        "emission; a bare subject is a user id, not a tenant.",
                        sub,
                        tenant.id,
                    )
                    return str(tenant.id)

    raise HTTPException(status_code=401, detail="Missing tenant context")


async def require_tenant_id(request: Request) -> str:
    """Like :func:`get_tenant_id` but WITHOUT the JWT-subject fallback.

    For write paths that persist a tenant_id (e.g. license creation), where
    silently storing a token subject as a tenant corrupts downstream billing.
    Requires a trusted tenant: the X-Tenant-Id header (ext_authz) or an
    explicit JWT ``tenant_id`` claim. Fails loud (401) otherwise so the
    producer fixes its emission instead of persisting a user id as a tenant.

    Raises:
        HTTPException 401: no header and no tenant_id claim.
        HTTPException 400: header or claim value is not a UUID.
    """
    # Stage 2 header-trust close: source the tenant from the already-reconciled
    # ``request.state.user.tenant_id`` (middleware authority), not the raw header.
    user = getattr(request.state, "user", None)
    if user and getattr(user, "tenant_id", None):
        return validate_tenant_uuid(user.tenant_id, source="resolved.tenant_id")

    raise HTTPException(
        status_code=401,
        detail=(
            "Missing tenant context: this operation requires an X-Tenant-Id "
            "header or a tenant_id claim. A bare token subject is a user id, "
            "not a tenant."
        ),
    )
