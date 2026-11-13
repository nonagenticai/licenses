"""FastAPI dependencies for authentication."""

from typing import cast

from fastapi import HTTPException, Request, status

from .models import User

# Backward compatibility alias
KeycloakUser = User


def get_current_user(request: Request) -> User:
    """Get authenticated user from request.

    Usage:
        @app.get("/api/items")
        async def get_items(user: User = Depends(get_current_user)):
            return {"user": user.preferred_username}
    """
    if not hasattr(request.state, "user"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required"
        )
    return cast(User, request.state.user)


def require_role(role: str):
    """Require specific role.

    Usage:
        @app.post("/admin/endpoint")
        async def admin_only(user: User = Depends(require_role("admin"))):
            return {"message": "Admin access"}
    """

    def checker(request: Request) -> User:
        user = get_current_user(request)
        if not user.has_realm_role(role):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=f"Role '{role}' required"
            )
        return user

    return checker


def require_any_role(*roles: str):
    """Require any of specified roles.

    Usage:
        @app.get("/editor/endpoint")
        async def editor(user: User = Depends(require_any_role("editor", "admin"))):
            return {"message": "Access granted"}
    """

    def checker(request: Request) -> User:
        user = get_current_user(request)
        if not any(user.has_realm_role(r) for r in roles):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"One of roles required: {', '.join(roles)}",
            )
        return user

    return checker


def require_all_roles(*roles: str):
    """Require all of specified roles.

    Args:
        *roles: One or more role names

    Returns:
        Dependency function
    """

    def checker(request: Request) -> User:
        user = get_current_user(request)
        user_roles = user.get_all_roles()
        missing_roles = [role for role in roles if role not in user_roles]
        if missing_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing required roles: {', '.join(missing_roles)}",
            )
        return user

    return checker


# The shared internal-service role was rotated `metering-internal` ->
# `service-internal` fleet-wide; the legacy `metering-internal` was retired
# here once every SA carried `service-internal`.
INTERNAL_SERVICE_ROLES = {
    "service-internal",
    "onboarding-internal",
    "license-admin",
}


def require_internal_service(request: Request) -> User:
    """Require caller to be an internal service with a service-account JWT.

    Accepts any of: service-internal, onboarding-internal, or license-admin roles.
    """
    user = get_current_user(request)
    user_roles = set(user.get_all_roles())
    if not user_roles & INTERNAL_SERVICE_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Internal service role required",
        )
    return user


# GET /licenses/active is a pure read (resolves the active license for a single
# (tenant_id, app_key) -> introspection response, no side effects). Reading
# active-license status is a read operation, so a least-privilege service that
# only holds `license-reader` should be allowed to call it. Accept it here in
# ADDITION to the internal-service roles, WITHOUT widening
# INTERNAL_SERVICE_ROLES itself -- that constant also guards the broader
# /internal/licenses* listing endpoints, which enumerate licenses across the
# fleet and remain internal-service-only. Uses get_all_roles() (realm + client)
# to preserve require_internal_service's exact accept-semantics.
ACTIVE_LICENSE_READ_ROLES = INTERNAL_SERVICE_ROLES | {"license-reader"}


def require_internal_or_reader(request: Request) -> User:
    """Gate for the read-only GET /licenses/active endpoint.

    Accepts the internal-service roles OR license-reader (a pure-read role).
    Does NOT grant access to any write or /internal/* listing surface.
    """
    user = get_current_user(request)
    user_roles = set(user.get_all_roles())
    if not user_roles & ACTIVE_LICENSE_READ_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Internal service or license-reader role required",
        )
    return user


# ---------------------------------------------------------------------------
# Tenant registry read (GET /tenants)
# ---------------------------------------------------------------------------
#
# ``GET /tenants`` returns the whole customer roster to a ``license-admin`` and
# only the caller's OWN tenant to anyone else (the 2026-07-27 roster-leak fix).
# ``billing-service`` needs the roster — its unregistered-tenant gate has to
# answer "is this tenant_id in the licences registry?" for tenant ids it
# observes in cost events, i.e. for tenants that are by definition not its own.
# It holds no ``license-admin`` (it must never be able to create, rename or
# archive a tenant) and its own tenant scope is useless to it, so it got a 403
# and the gate shipped OFF.
#
# The fix is a dedicated read-only role rather than handing billing
# ``license-admin`` or adding it to ``INTERNAL_SERVICE_ROLES``: this grants
# exactly one capability — enumerate the tenant registry — and nothing else in
# this service honours it.
TENANT_REGISTRY_READER_ROLE = "tenant-registry-reader"


# ---------------------------------------------------------------------------
# Tenant bundle-token mint (POST /internal/tenants/{tenant_id}/bundle-token)
# ---------------------------------------------------------------------------
#
# That route MINTS a live bearer token on ANY tenant's bundle licence. It was
# first guarded by ``require_internal_service``, i.e. ``INTERNAL_SERVICE_ROLES``
# — and gitops grants ``service-internal`` fleet-wide (licenses, n8n-metering,
# fr-backend, improvement-loop-service, billing-service, marketing, sales, …).
# Every one of those could therefore have minted a token for every tenant: a
# cross-tenant impersonation primitive handed out as a side effect of an
# unrelated shared role (PR #226 audit).
#
# Same shape as ``TENANT_REGISTRY_READER_ROLE``: a dedicated realm role that
# grants exactly this one capability, honoured by exactly this one route, and
# granted in gitops to exactly the resolver's calling service account.
#
# ⛔ NOT a member of INTERNAL_SERVICE_ROLES, and INTERNAL_SERVICE_ROLES does not
# admit here. ``license-admin`` is deliberately NOT admitted either:
#   * it is not an operator-only role in practice — ``service-account-fr-backend``
#     holds it (gitops realm-patch, measured 2026-08-11), so admitting it would
#     re-open this very hole for fr-backend;
#   * an operator who needs a bundle token already has the audited paths
#     (``scripts/provision_tenant_bundle.py``, the operator-token route); minting
#     a tenant's live bearer is a distinct capability that must be granted, and
#     revoked, per identity.
# Realm role only (``has_realm_role``), NOT ``get_all_roles()``: a client role of
# the same name on some other client must not satisfy it.
BUNDLE_TOKEN_MINTER_ROLE = "bundle-token-minter"


def require_bundle_token_minter(request: Request) -> User:
    """Gate for POST /internal/tenants/{tenant_id}/bundle-token.

    Requires EXACTLY the ``bundle-token-minter`` realm role. ``service-internal``,
    ``onboarding-internal`` and ``license-admin`` do NOT pass on their own; a
    licence-token caller (only ever ``license-reader``) never passes.
    """
    user = get_current_user(request)
    if not user.has_realm_role(BUNDLE_TOKEN_MINTER_ROLE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Role required: {BUNDLE_TOKEN_MINTER_ROLE}",
        )
    return user


# Realm roles admitted to POST /internal/tenants/{tenant_id}/bundles/{bundle}/ensure.
#
# ``bundle-provisioner`` was the ONLY role before migration 050, when the route could
# CREATE per-app licences on ANY tenant — so nothing broader (``service-internal`` is
# held fleet-wide, ``license-admin`` by fr-backend) could be admitted. Since 050 the
# route is OWNER-SCOPED (``main._resolve_owned_tenant``): the caller's app must own the
# tenant, a grantee is 403 ``tenant_owner_only`` and anyone else 404. The protection is
# therefore ownership, exactly as for the other owner writes, and the owner-write roles
# (``license-admin`` / ``license-manager``, ``main.OWNER_WRITE_ROLES``) are admitted
# beside ``bundle-provisioner``: CommonPractice's client
# (secure-apps/commonpractice-licenses-admin) holds only ``license-admin`` and must
# ensure bundles on its OWN tenants (George 2026-10-14, netcare onboarding).
# ``service-internal``, ``bundle-token-minter`` and ``license-reader`` still do NOT
# pass: minting a tenant's bearer is a different capability, granted per identity.
# Ownership alone is NOT enough (migration 052): the caller's owner must also be a
# ``bundle_ensurer`` of the bundle (main._authorize_bundle_ensurer), or any owner
# could ensure ``commonpractice`` on its own tenant and get a CP licence minted.
BUNDLE_PROVISIONER_ROLE = "bundle-provisioner"
BUNDLE_ENSURE_ROLES = (BUNDLE_PROVISIONER_ROLE, "license-admin", "license-manager")


def require_bundle_ensurer(request: Request) -> User:
    """Gate for POST /internal/tenants/{tenant_id}/bundles/{bundle_key}/ensure.

    Requires one of ``BUNDLE_ENSURE_ROLES``. The route then requires the caller's
    app to OWN the tenant (cross_owner break-glass excepted, audited).
    """
    user = get_current_user(request)
    if not any(user.has_realm_role(r) for r in BUNDLE_ENSURE_ROLES):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"One of roles required: {', '.join(BUNDLE_ENSURE_ROLES)}",
        )
    return user
