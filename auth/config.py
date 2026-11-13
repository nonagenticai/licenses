"""Keycloak configuration.

Scope note — what this module is NOT
------------------------------------
This holds the OUTBOUND service-account credential (``SERVICE_CLIENT_ID`` /
``SERVICE_CLIENT_SECRET`` / ``SERVICE_TOKEN_URL``) and the tenant-proxy
allowlist. It has no part in verifying INBOUND tokens: that is
``auth/jwt_verify.py``, which resolves signing keys per issuer via PyJWKClient
and derives nothing from this class.

Seven members were removed on 2026-08-17 because an AST sweep of the shipped
tree found zero readers of any of them (``tests/unit/test_config_surface_is_consumed.py``
now enforces that):

* ``client_id`` / ``client_secret`` / ``ssl_verify`` — and the
  ``KEYCLOAK_CLIENT_ID`` / ``KEYCLOAK_CLIENT_SECRET`` / ``KEYCLOAK_SSL_VERIFY``
  reads behind them. Their only consumer was ``auth/validator.py``, deleted in
  PR #140, which passed them to a ``python-keycloak`` client. Nothing in this
  service authenticates AS a Keycloak client on the inbound path.
* ``server_url`` / ``realm`` — bare aliases of ``keycloak_url`` / ``realm_name``
  with no callers; the deleted validator read the underlying names directly.
* ``jwks_url`` — actively a trap, not merely unused: it derived a JWKS URL from
  ``KEYCLOAK_REALM``, which production sets to ``nonagentic``, a realm that
  returns 404. See the ``auth/jwt_verify.py`` docstring.
* ``ca_cert_path`` — read ``CA_CERT_PATH``; no Python consumer. The var is still
  set in ``docker-compose.yml`` for the local Keycloak stack, which is why the
  compose file was left alone.
"""

import os
from functools import lru_cache

# Service URLs MUST come from the environment in production. We keep an
# empty-string fallback here (no hardcoded cluster-local or private-suffix
# hostname literal) and fail loud at startup when production-mode is detected
# with the var unset.
_REQUIRED_IN_PRODUCTION: tuple[str, ...] = ("keycloak_url",)


class KeycloakSettings:
    """Keycloak configuration settings."""

    def __init__(self) -> None:
        self.keycloak_url: str = os.getenv("KEYCLOAK_URL", "")
        # Inbound token verification does NOT use this realm — see the
        # `auth/jwt_verify.py` docstring. It survives only because the OUTBOUND
        # service-account `token_url` is derived from it when SERVICE_TOKEN_URL
        # is unset.
        self.realm_name: str = os.getenv("KEYCLOAK_REALM", "secure-apps")

        # Service-to-Service Authentication
        self.service_client_id: str = os.getenv("SERVICE_CLIENT_ID", "")
        self.service_client_secret: str = os.getenv("SERVICE_CLIENT_SECRET", "")
        # Explicit token endpoint for the outbound service-account credential.
        # The SA client (`licenses`) + its internal-service role live in the
        # `secure-apps` realm, which is NOT the realm this service validates
        # inbound tokens against (KEYCLOAK_REALM). Deriving the SA token URL from
        # KEYCLOAK_REALM would target the wrong realm, so this override points the
        # client-credentials fetch straight at secure-apps. Empty → fall back to
        # the KEYCLOAK_REALM-derived `token_url` (backward compatible).
        self.service_token_url_override: str = os.getenv("SERVICE_TOKEN_URL", "")

        environment = os.getenv("ENVIRONMENT", "production").lower()
        if environment == "production":
            missing = [n for n in _REQUIRED_IN_PRODUCTION if not getattr(self, n)]
            if missing:
                raise RuntimeError(
                    "KeycloakSettings missing required env vars in production: "
                    f"{[m.upper() for m in missing]}. Set them in the Deployment's "
                    "env block, or in your .env file if you are running it directly."
                )

    @property
    def token_url(self) -> str:
        return f"{self.keycloak_url}/realms/{self.realm_name}/protocol/openid-connect/token"

    @property
    def service_token_url(self) -> str:
        """Token endpoint for the outbound service-account (client-credentials).

        Uses the explicit SERVICE_TOKEN_URL override when set (points at the
        secure-apps realm where the `licenses` SA client lives); otherwise falls
        back to the KEYCLOAK_REALM-derived ``token_url``.
        """
        return self.service_token_url_override or self.token_url


@lru_cache
def get_settings() -> KeycloakSettings:
    """Get cached Keycloak settings instance."""
    return KeycloakSettings()


# The env allowlist of trusted multi-tenant-proxy `azp`s (TRUSTED_TENANT_PROXY_CLIENTS)
# was REMOVED by migration 050: a proxy is just a client with an owner. Which
# tenants a client may retarget via X-Tenant-Id is now decided from the
# `api_client` table (tenants it owns or holds a grant on; cross_owner: any) — see
# auth/middleware.py.
