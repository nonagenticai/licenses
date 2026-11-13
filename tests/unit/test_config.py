"""
Unit tests for auth/config.py — KeycloakSettings and get_settings().

Covers constructor defaults, env-var overrides, and computed properties.
"""

import os
from unittest.mock import patch

import pytest

from auth.config import KeycloakSettings, get_settings


@pytest.fixture(autouse=True)
def _force_dev_environment(monkeypatch):
    """Default to ENVIRONMENT=development so the production fail-fast validator
    does not interfere with default/override/property tests. Tests that need
    production-mode set ENVIRONMENT=production explicitly via patch.dict.
    """
    monkeypatch.setenv("ENVIRONMENT", "development")


class TestKeycloakSettingsDefaults:
    """Test KeycloakSettings with default values (no env vars).

    Defaults are exercised under ENVIRONMENT=development; production-mode
    fail-fast behavior is covered separately in
    TestKeycloakSettingsProductionValidation.
    """

    def test_default_keycloak_url_empty(self):
        """Default keycloak_url is empty (must come from gitops env in prod)."""
        with patch.dict(os.environ, {"ENVIRONMENT": "development"}, clear=True):
            settings = KeycloakSettings()
        assert settings.keycloak_url == ""

    def test_default_service_client_id_empty(self):
        """Default service_client_id is empty string."""
        with patch.dict(os.environ, {"ENVIRONMENT": "development"}, clear=True):
            settings = KeycloakSettings()
        assert settings.service_client_id == ""

    def test_default_service_client_secret_empty(self):
        """Default service_client_secret is empty string."""
        with patch.dict(os.environ, {"ENVIRONMENT": "development"}, clear=True):
            settings = KeycloakSettings()
        assert settings.service_client_secret == ""


class TestKeycloakSettingsProductionValidation:
    """Production-mode fail-fast when required env vars are missing."""

    def test_production_missing_keycloak_url_raises(self):
        """ENVIRONMENT=production + empty KEYCLOAK_URL → RuntimeError."""
        import pytest

        with patch.dict(os.environ, {"ENVIRONMENT": "production"}, clear=True):
            with pytest.raises(RuntimeError, match="KEYCLOAK_URL"):
                KeycloakSettings()

    def test_production_with_keycloak_url_succeeds(self):
        """ENVIRONMENT=production + KEYCLOAK_URL set → constructs OK."""
        with patch.dict(
            os.environ,
            {"ENVIRONMENT": "production", "KEYCLOAK_URL": "https://kc.prod.example"},
            clear=True,
        ):
            settings = KeycloakSettings()
        assert settings.keycloak_url == "https://kc.prod.example"


class TestKeycloakSettingsEnvOverrides:
    """Test KeycloakSettings reads from environment variables."""

    def test_keycloak_url_from_env(self):
        """KEYCLOAK_URL env var overrides default."""
        with patch.dict(os.environ, {"KEYCLOAK_URL": "https://kc.example.com"}):
            settings = KeycloakSettings()
        assert settings.keycloak_url == "https://kc.example.com"

    def test_service_client_secret_from_env(self):
        """SERVICE_CLIENT_SECRET env var overrides default."""
        with patch.dict(os.environ, {"SERVICE_CLIENT_SECRET": "svc-secret"}):
            settings = KeycloakSettings()
        assert settings.service_client_secret == "svc-secret"


class TestKeycloakSettingsProperties:
    """Test computed property methods on KeycloakSettings."""

    def test_token_url_property(self):
        """token_url is constructed from keycloak_url and realm."""
        with patch.dict(
            os.environ,
            {
                "KEYCLOAK_URL": "https://kc.test",
                "KEYCLOAK_REALM": "my-realm",
            },
        ):
            settings = KeycloakSettings()
        assert settings.token_url == "https://kc.test/realms/my-realm/protocol/openid-connect/token"

    def test_service_token_url_falls_back_to_token_url(self):
        """Without SERVICE_TOKEN_URL, service_token_url == the realm-derived token_url."""
        with patch.dict(
            os.environ,
            {"KEYCLOAK_URL": "https://kc.test", "KEYCLOAK_REALM": "my-realm"},
            clear=False,
        ):
            os.environ.pop("SERVICE_TOKEN_URL", None)
            settings = KeycloakSettings()
        assert settings.service_token_url == settings.token_url

    def test_service_token_url_override_wins(self):
        """SERVICE_TOKEN_URL overrides the realm-derived token_url (secure-apps SA path)."""
        override = "http://keycloak.keycloak.svc.cluster.local:8080/realms/secure-apps/protocol/openid-connect/token"
        with patch.dict(
            os.environ,
            {
                "KEYCLOAK_URL": "https://kc.test",
                "KEYCLOAK_REALM": "nonagentic",
                "SERVICE_TOKEN_URL": override,
            },
        ):
            settings = KeycloakSettings()
        assert settings.service_token_url == override
        # inbound token_url stays realm-derived, unaffected by the SA override
        assert (
            settings.token_url == "https://kc.test/realms/nonagentic/protocol/openid-connect/token"
        )


class TestRemovedSurfaceStaysRemoved:
    """The seven members deleted on 2026-08-17 must not creep back.

    Their only consumer was ``auth/validator.py``, deleted in PR #140. These
    assertions are not redundant with ``test_config_surface_is_consumed.py``:
    that guard asks "does a consumer exist?", which a *new* caller would satisfy.
    This one pins the specific names, so re-adding
    ``client_id``/``client_secret`` as a Keycloak-client credential — the shape
    that made a secret get provisioned and rotated for nine months with no
    reader — is a deliberate act, not a silent restoration.

    ``jwks_url`` is the one worth spelling out: it derived a URL from
    ``KEYCLOAK_REALM``, which production sets to a realm that 404s. Inbound
    verification resolves JWKS per ISSUER in ``auth/jwt_verify.py``.
    """

    @pytest.mark.parametrize(
        "name",
        [
            "client_id",
            "client_secret",
            "ssl_verify",
            "server_url",
            "realm",
            "jwks_url",
            "ca_cert_path",
        ],
    )
    def test_member_is_gone(self, name):
        """The removed member is exposed neither on the class nor an instance."""
        with patch.dict(os.environ, {"ENVIRONMENT": "development"}, clear=True):
            settings = KeycloakSettings()
        assert not hasattr(settings, name), (
            f"{name!r} is back on KeycloakSettings. It was removed because an AST "
            "sweep found zero readers in the shipped tree; if it has a real "
            "consumer now, say so in the commit rather than restoring it quietly."
        )

    @pytest.mark.parametrize(
        "env_var", ["KEYCLOAK_CLIENT_ID", "KEYCLOAK_CLIENT_SECRET", "KEYCLOAK_SSL_VERIFY"]
    )
    def test_env_var_is_no_longer_read(self, env_var):
        """Setting the retired env var changes nothing the service can observe.

        Limit: this asserts the var is not read into any CURRENT public member.
        It does not prove no ``os.getenv`` call exists anywhere — that is what
        the source assertion below covers.
        """
        with patch.dict(os.environ, {"ENVIRONMENT": "development"}, clear=True):
            baseline = vars(KeycloakSettings())
        with patch.dict(
            os.environ, {"ENVIRONMENT": "development", env_var: "set-by-test"}, clear=True
        ):
            after = vars(KeycloakSettings())
        assert baseline == after, (
            f"{env_var} still influences KeycloakSettings state: {baseline} != {after}"
        )

    @pytest.mark.parametrize(
        "env_var",
        ["KEYCLOAK_CLIENT_ID", "KEYCLOAK_CLIENT_SECRET", "KEYCLOAK_SSL_VERIFY", "CA_CERT_PATH"],
    )
    def test_env_var_is_not_read_in_the_source(self, env_var):
        """No ``os.getenv`` for the retired var survives in auth/config.py.

        AST, not grep: the names still appear in this module's own docstring and
        in prose, and a substring match would be defeated by that. Scoped to
        ``auth/config.py`` deliberately — the vars legitimately remain in
        ``docker-compose.yml`` (local Keycloak stack) and in ``test-keycloak.sh``,
        which reads them straight from the shell, so a repo-wide assertion would
        be wrong.
        """
        import ast
        from pathlib import Path

        source = (Path(__file__).resolve().parents[2] / "auth" / "config.py").read_text()
        read_vars = {
            node.args[0].value
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"getenv", "get"}
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        }
        # Anti-vacuity: the walk must actually find the env reads that remain.
        assert "KEYCLOAK_URL" in read_vars, (
            f"the os.getenv walk found {read_vars or 'nothing'} — it is not seeing "
            "auth/config.py's env reads, so this assertion proves nothing."
        )
        assert env_var not in read_vars, (
            f"auth/config.py still reads {env_var}; the removal was incomplete."
        )


class TestGetSettings:
    """Test the get_settings() cached factory."""

    def test_get_settings_returns_keycloak_settings(self):
        """get_settings returns a KeycloakSettings instance."""
        # Clear lru_cache before test
        get_settings.cache_clear()
        result = get_settings()
        assert isinstance(result, KeycloakSettings)
