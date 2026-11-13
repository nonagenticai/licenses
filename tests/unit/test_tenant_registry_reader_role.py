"""``GET /tenants`` — the ``tenant-registry-reader`` role (billing-service fix).

billing-service's unregistered-tenant gate must answer "is this tenant_id in the
licences registry?" for tenant ids it observes on cost events, i.e. for tenants
that are never its own. Before this role the only gate that could answer was
``license-admin``, which can also create / rename / archive tenants — so billing
got a 403 here and **shipped that gate turned OFF**.

Both sides are pinned, because a one-sided authorization test proves nothing:

* ADMIT  — ``tenant-registry-reader`` sees the FULL roster.
* REFUSE — it cannot write the registry, and is not an internal-service ticket.
* UNTOUCHED — a bare ``license-reader`` still sees only its own tenant (the
  2026-07-27 roster-leak fix).

(Extracted from ``test_w2v_entitlement_route.py`` when the dead
``GET /entitlements/w2v/active`` route was reverted; this fix merely rode the
same PR and is unrelated to it.)
"""

from __future__ import annotations

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from auth.models import User
from tests.conftest import _make_authenticated_client


def _user(sub: str, roles: list[str]) -> User:
    return User(
        sub=sub,
        preferred_username=sub,
        email=f"{sub}@test.com",
        realm_access={"roles": roles},
        resource_access={},
        iss="https://keycloak.example.com/realms/secure-apps",
    )


@pytest.fixture
def auth_client_tenant_registry_reader(db_manager) -> Generator[TestClient]:
    """A caller holding ONLY ``tenant-registry-reader`` (the billing-service fix)."""
    yield from _make_authenticated_client(
        db_manager, _user("billing-service-sa", ["tenant-registry-reader"])
    )


class TestTenantRegistryReader:
    """``GET /tenants`` — the 403 that shipped billing-service's gate turned OFF."""

    def test_tenant_registry_reader_sees_the_full_roster(
        self, auth_client_tenant_registry_reader: TestClient, db_manager
    ):
        db_manager.create_tenant(name="some-other-customer", owner_key="platform")
        resp = auth_client_tenant_registry_reader.get("/tenants")
        assert resp.status_code == 200, resp.text
        names = {t["name"] for t in resp.json()}
        assert "some-other-customer" in names
        assert len(names) > 1, "roster read collapsed to the caller's own tenant"

    def test_bare_license_reader_still_sees_only_its_own_tenant(
        self, auth_client_reader: TestClient, db_manager
    ):
        """The 2026-07-27 roster-leak fix is untouched by the new role."""
        db_manager.create_tenant(name="must-not-leak-to-reader", owner_key="platform")
        resp = auth_client_reader.get("/tenants")
        assert resp.status_code == 200, resp.text
        assert "must-not-leak-to-reader" not in resp.text
        assert len(resp.json()) <= 1

    def test_tenant_registry_reader_cannot_write_the_registry(
        self, auth_client_tenant_registry_reader: TestClient
    ):
        """Read-only by construction: the role admits the listing and no mutation."""
        created = auth_client_tenant_registry_reader.post(
            "/tenants", json={"name": "reader-should-not-create-this"}
        )
        assert created.status_code == 403, created.text
        renamed = auth_client_tenant_registry_reader.patch(
            "/tenants/some-name", json={"name": "renamed"}
        )
        assert renamed.status_code == 403, renamed.text

    def test_tenant_registry_reader_is_refused_on_the_internal_identity_map(
        self, auth_client_tenant_registry_reader: TestClient
    ):
        """It is not an internal-service ticket."""
        assert auth_client_tenant_registry_reader.get("/internal/tenants").status_code == 403
