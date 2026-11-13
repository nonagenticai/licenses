"""Authentication module for licenses service."""

from .config import KeycloakSettings, get_settings
from .dependencies import get_current_user, require_any_role, require_internal_service, require_role
from .middleware import AuthMiddleware
from .models import (
    KeycloakUser,  # KeycloakUser is alias for backward compat
    User,
)

# NOTE: there is deliberately no second token verifier exported here. `TokenValidator` /
# `get_validator` (auth/validator.py) verified signatures against a single realm PEM and
# checked neither `iss` nor `aud`, which across this deployment's two Keycloak realms made
# either realm's token acceptable as the other's. Nothing called it, but exporting it here
# made it read like the blessed API. Token verification is auth/jwt_verify.verify_jwt,
# reached from AuthMiddleware; tests/unit/test_single_jwt_verifier.py holds the line.
__all__ = [
    "KeycloakSettings",
    "get_settings",
    "AuthMiddleware",
    "User",
    "KeycloakUser",  # Backward compatibility alias
    "get_current_user",
    "require_role",
    "require_any_role",
    "require_internal_service",
]
