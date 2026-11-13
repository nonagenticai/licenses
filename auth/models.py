"""User models for authentication."""

from pydantic import BaseModel, Field


class User(BaseModel):
    """User model extracted from trusted headers or JWT claims."""

    sub: str = Field(..., description="User ID")
    preferred_username: str = Field(..., description="Username")
    email: str | None = None
    email_verified: bool = False
    name: str | None = None

    # Roles (from JWT realm_access or ext_authz headers)
    realm_access: dict = Field(default_factory=dict)
    resource_access: dict = Field(default_factory=dict)

    # Token metadata (optional, present when extracted from JWT)
    exp: int | None = None
    iat: int | None = None
    iss: str | None = None
    # Authorized party (Keycloak `azp` = the minting client_id). Cryptographically
    # verified as part of the signed token, so it is a trustworthy caller identity:
    # used to admit the multi-tenant proxy SAs (fr-backend / w2v-backend) that
    # legitimately retarget another tenant via X-Tenant-Id. None for opaque-token
    # and header-only users.
    azp: str | None = None

    # Tenant info (from X-Tenant-Id header or JWT)
    tenant_id: str | None = None
    organization_id: str | None = None

    # Migration 050: the app that OWNS this caller, resolved by the auth
    # middleware from the `api_client` table keyed on the VERIFIED token's
    # (iss realm, azp). None = unknown client, which is refused on every
    # tenant / licence / grant / bundle write. Never read from a body or header.
    owner_key: str | None = None
    # Break-glass: this client bypasses owner scoping (every use is audited).
    cross_owner: bool = False
    # Migration 072 (realm-per-app P0): the app that owns the VERIFIED token's
    # Keycloak realm (``realm_owner``), set by the auth middleware. None = the
    # realm has no row (pre-072 behaviour). When set, this caller may act only on
    # tenants that owner owns or holds an active grant on.
    realm_owner_key: str | None = None

    @property
    def realm(self) -> str | None:
        """The Keycloak realm of the verified issuer (``.../realms/<realm>``)."""
        if not self.iss or "/realms/" not in self.iss:
            return None
        return self.iss.rstrip("/").split("/realms/")[-1] or None

    @property
    def effective_tenant_id(self) -> str:
        """Get effective tenant ID (explicit tenant_id or fall back to sub)."""
        return self.tenant_id or self.sub

    def has_realm_role(self, role: str) -> bool:
        """Check if user has realm role."""
        roles = self.realm_access.get("roles", [])
        return role in roles

    def has_client_role(self, client_id: str, role: str) -> bool:
        """Check if user has client role."""
        client_roles = self.resource_access.get(client_id, {}).get("roles", [])
        return role in client_roles

    def get_all_roles(self) -> list[str]:
        """Get all roles (realm + client)."""
        realm_roles: list[str] = self.realm_access.get("roles", [])
        client_roles: list[str] = []
        for client_data in self.resource_access.values():
            client_roles.extend(client_data.get("roles", []))
        return list(realm_roles) + client_roles

    def require_role(self, role: str) -> None:
        """Require user to have specific realm role.

        Args:
            role: Required role name

        Raises:
            HTTPException: If user doesn't have the role
        """
        if not self.has_realm_role(role):
            from fastapi import HTTPException, status

            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Required role '{role}' not found",
            )


# Backward compatibility alias
KeycloakUser = User
