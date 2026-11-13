"""Integration: cross-realm tenant forgery is refused end to end (migration 072).

Drives the real app (lifespan: create_all -> apply_migrations -> boot seeds) and
checks the boot seed wrote realm_owner, then that a commonpractice-realm token is
admitted on a commonpractice tenant and refused (403 ``realm_owner_mismatch``) on
another owner's, while a portal-realm token is unchanged and an issuer outside the
allowlist is a 401. LOCAL mode only: the tokens are signed by the test key.
"""

from __future__ import annotations

import time
import uuid

import jwt
import pytest

from tests.integration.conftest import _is_local_mode
from tests.support.jwt_keys import TEST_ISSUER, private_key_pem

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _is_local_mode(), reason="signed with the local test key"),
]

KC = "https://keycloak.example.com"
CP_ISSUER = f"{KC}/realms/commonpractice"
PORTAL_ISSUER = f"{KC}/realms/portal"
NO_LICENCE_APP = "no-such-app-realm-owner-integration"


@pytest.fixture(autouse=True)
def _trust(monkeypatch):
    monkeypatch.setenv(
        "KEYCLOAK_TRUSTED_ISSUERS", ",".join((TEST_ISSUER, PORTAL_ISSUER, CP_ISSUER))
    )


def _bearer(iss: str, tenant_id: str, roles: list[str]) -> dict[str, str]:
    now = int(time.time())
    token = jwt.encode(
        {
            "sub": f"user-{uuid.uuid4().hex[:8]}",
            "preferred_username": "someone",
            "realm_access": {"roles": roles},
            "azp": "commonpractice-frontend",
            "iss": iss,
            "tenant_id": tenant_id,
            "iat": now - 5,
            "exp": now + 3600,
        },
        private_key_pem(),
        algorithm="RS256",
    )
    # Override the fixture client's default admin bearer + X-Tenant-Id.
    return {"Authorization": f"Bearer {token}", "X-Tenant-Id": tenant_id}


def _tenant(owner: str) -> str:
    import main

    return str(
        main.db_manager.create_tenant(name=f"rt-{uuid.uuid4().hex[:10]}", owner_key=owner).id
    )


def _active(client, tenant_id: str, headers: dict[str, str]):
    return client.get(
        "/licenses/active",
        params={"tenant_id": tenant_id, "app_key": NO_LICENCE_APP},
        headers=headers,
    )


def test_boot_seed_wrote_realm_owner(licenses_client):
    import main

    assert main.db_manager.resolve_realm_owner("commonpractice") == "commonpractice"
    assert main.db_manager.resolve_realm_owner("portal") is None


def test_cp_realm_token_on_cp_tenant_ok(licenses_client):
    t = _tenant("commonpractice")
    r = _active(licenses_client, t, _bearer(CP_ISSUER, t, ["license-reader"]))
    assert r.status_code == 404, r.text
    assert r.json()["detail"]["error"] == "no_active_license"


def test_cp_realm_token_claiming_another_owners_tenant_403(licenses_client):
    t = _tenant("fr")
    r = _active(licenses_client, t, _bearer(CP_ISSUER, t, ["license-reader"]))
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["error"] == "realm_owner_mismatch"


def test_portal_token_unchanged(licenses_client):
    t = _tenant("fr")
    r = _active(licenses_client, t, _bearer(PORTAL_ISSUER, t, ["license-reader"]))
    assert r.status_code == 404, r.text


def test_issuer_not_in_allowlist_401(licenses_client):
    t = _tenant("commonpractice")
    r = _active(licenses_client, t, _bearer(f"{KC}/realms/not-allowlisted", t, ["license-reader"]))
    assert r.status_code == 401, r.text


def test_s2s_realm_param_owned_realm_on_other_owners_tenant_403(licenses_client):
    t = _tenant("fr")
    params = {"tenant_id": t, "app_key": NO_LICENCE_APP, "realm": "commonpractice"}
    # The fixture client's default bearer: secure-apps break-glass S2S (license-admin).
    r = licenses_client.get("/licenses/active", params=params, headers={"X-Tenant-Id": t})
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["error"] == "realm_owner_mismatch"
    params["realm"] = "portal"
    r = licenses_client.get("/licenses/active", params=params, headers={"X-Tenant-Id": t})
    assert r.status_code == 404, r.text
