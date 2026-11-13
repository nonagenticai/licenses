"""Tests for the canonical tenant create/read API (POST/GET /tenants).

Covers the foundation endpoints the provisioning service depends on:
admin-gated create with name-reuse idempotency, role-gated reads, and a guard
that the intentionally-open S2S byo-mode lookup stays open after /tenants became
a protected prefix.
"""

from __future__ import annotations

from uuid import UUID

from fastapi.testclient import TestClient

from models import DatabaseManager


def _is_uuid(value: str) -> bool:
    try:
        UUID(value)
        return True
    except ValueError, TypeError:
        return False


class TestCreateTenant:
    def test_create_success(self, auth_client_admin: TestClient):
        resp = auth_client_admin.post("/tenants", json={"name": "acme-corp"})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert _is_uuid(body["id"])
        assert body["name"] == "acme-corp"
        assert body["keycloak_realm"] is None
        # New tenants default to 'byo_required' (migration 058, licenses#273).
        assert body["byo_mode"] == "byo_required"

    def test_create_with_realm(self, auth_client_admin: TestClient):
        resp = auth_client_admin.post(
            "/tenants", json={"name": "acme-corp", "keycloak_realm": "portal-realm"}
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["keycloak_realm"] == "portal-realm"

    def test_create_is_idempotent_by_external_ref(self, auth_client_admin: TestClient):
        """Migration 050: a re-driven provisioning run is idempotent on the owning
        app's own id for the tenant (external_ref), never on the name."""
        body = {"name": "acme-corp", "external_ref": "acme-slug"}
        first = auth_client_admin.post("/tenants", json=body)
        assert first.status_code == 201, first.text
        second = auth_client_admin.post("/tenants", json=body)
        assert second.status_code == 200, second.text
        assert second.json()["id"] == first.json()["id"]

    def test_same_owner_same_name_is_409_not_the_existing_tenant(
        self, auth_client_admin: TestClient
    ):
        """Migration 050: name idempotency is GONE. It handed one app another app's
        tenant UUID; now a same-owner re-POST without external_ref is a conflict."""
        first = auth_client_admin.post("/tenants", json={"name": "acme-corp"})
        assert first.status_code == 201
        second = auth_client_admin.post("/tenants", json={"name": "acme-corp"})
        assert second.status_code == 409, second.text
        assert second.json()["detail"]["error"] == "tenant_name_taken"

    def test_create_bad_name_rejected(self, auth_client_admin: TestClient):
        for bad in ("Acme", "acme corp", "-acme", "acme_corp", ""):
            resp = auth_client_admin.post("/tenants", json={"name": bad})
            assert resp.status_code == 422, f"{bad!r} -> {resp.status_code}"

    def test_create_requires_admin_role(
        self, auth_client_reader: TestClient, auth_client_no_role: TestClient
    ):
        assert auth_client_reader.post("/tenants", json={"name": "acme-corp"}).status_code == 403
        assert auth_client_no_role.post("/tenants", json={"name": "acme-corp"}).status_code == 403

    def test_create_unauthenticated_rejected(self, client: TestClient):
        # No Authorization header at all — /tenants is a protected prefix now.
        assert client.post("/tenants", json={"name": "acme-corp"}).status_code == 401


class TestGetTenant:
    def test_get_by_name(self, auth_client_admin: TestClient):
        created = auth_client_admin.post("/tenants", json={"name": "acme-corp"}).json()
        resp = auth_client_admin.get("/tenants/acme-corp")
        assert resp.status_code == 200, resp.text
        assert resp.json()["id"] == created["id"]

    def test_get_by_id(self, auth_client_admin: TestClient):
        created = auth_client_admin.post("/tenants", json={"name": "acme-corp"}).json()
        resp = auth_client_admin.get(f"/tenants/{created['id']}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == "acme-corp"

    def test_get_not_found(self, auth_client_admin: TestClient):
        assert auth_client_admin.get("/tenants/no-such-tenant").status_code == 404

    def test_get_foreign_tenant_is_404_for_reader(
        self, auth_client_admin: TestClient, auth_client_reader: TestClient
    ):
        """CONTRACT CHANGE 2026-07-27: was `assert ... == 200`.

        That assertion encoded the leak — any license-reader could resolve ANY
        tenant by name or id. A foreign tenant is now indistinguishable from a
        non-existent one (404, not 403: a 403 would confirm the name exists).
        The reader fixture is authenticated on the seed tenant, not on
        `acme-corp`. See tests/auth/test_authz_idor.py.
        """
        auth_client_admin.post("/tenants", json={"name": "acme-corp"})
        assert auth_client_reader.get("/tenants/acme-corp").status_code == 404

    def test_get_own_tenant_allows_reader(self, auth_client_reader: TestClient):
        """A reader may still resolve its OWN tenant (positive control)."""
        resp = auth_client_reader.get("/tenants/01000000-0000-7000-8000-000000000001")
        assert resp.status_code == 200, resp.text

    def test_get_unauthenticated_rejected(self, client: TestClient):
        assert client.get("/tenants/acme-corp").status_code == 401


class TestListTenants:
    def test_list_newest_first(self, auth_client_admin: TestClient):
        for name in ("alpha-co", "beta-co", "gamma-co"):
            auth_client_admin.post("/tenants", json={"name": name})
        resp = auth_client_admin.get("/tenants")
        assert resp.status_code == 200, resp.text
        names = [t["name"] for t in resp.json()]
        assert {"alpha-co", "beta-co", "gamma-co"} <= set(names)

    def test_list_requires_role(self, client: TestClient):
        assert client.get("/tenants").status_code == 401


class TestByoModeLookupStaysOpen:
    """Regression guard: making /tenants protected must NOT close the open S2S
    byo-mode lookup that worker_management calls with no token."""

    def test_byo_mode_get_open_without_auth(self, db_manager: DatabaseManager, client: TestClient):
        tenant = db_manager.create_tenant(name="acme-corp", owner_key="platform")
        # `client` sends no Authorization and no X-Tenant-Id header.
        resp = client.get(f"/tenants/{tenant.id}/byo-mode")
        assert resp.status_code == 200, resp.text
        assert resp.json()["tenant_id"] == str(tenant.id)


class TestRegisterTenantExplicitId:
    """PUT /tenants/{id} — register a tenant whose UUID is owned by another service.

    commonpractice keeps its own tenants table and must satisfy the license-mint
    guard ("a NEW license may only be minted for a REGISTERED tenant") without
    letting this service mint a second, different id for the same tenant.
    """

    TID = "019fa2b2-1632-7c85-9403-8a7d9e74ab39"

    def test_register_uses_the_supplied_id(self, auth_client_admin: TestClient):
        resp = auth_client_admin.put(f"/tenants/{self.TID}", json={"name": "acme-explicit"})
        assert resp.status_code == 201, resp.text
        assert resp.json()["id"] == self.TID
        assert resp.json()["name"] == "acme-explicit"

    def test_register_is_idempotent(self, auth_client_admin: TestClient):
        first = auth_client_admin.put(f"/tenants/{self.TID}", json={"name": "acme-explicit"})
        assert first.status_code == 201, first.text
        second = auth_client_admin.put(f"/tenants/{self.TID}", json={"name": "acme-explicit"})
        assert second.status_code == 200, second.text
        assert second.json()["id"] == self.TID

    def test_registered_tenant_can_be_read_back(self, auth_client_admin: TestClient):
        auth_client_admin.put(f"/tenants/{self.TID}", json={"name": "acme-explicit"})
        resp = auth_client_admin.get(f"/tenants/{self.TID}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["id"] == self.TID

    def test_name_owned_by_another_tenant_is_409(self, auth_client_admin: TestClient):
        auth_client_admin.put(f"/tenants/{self.TID}", json={"name": "acme-explicit"})
        other = "019fa2b2-1632-7c85-9403-8a7d9e74ab40"
        resp = auth_client_admin.put(f"/tenants/{other}", json={"name": "acme-explicit"})
        assert resp.status_code == 409, resp.text

    def test_non_uuid_id_rejected(self, auth_client_admin: TestClient):
        resp = auth_client_admin.put("/tenants/not-a-uuid", json={"name": "acme-explicit"})
        assert resp.status_code == 400, resp.text

    def test_bad_name_rejected(self, auth_client_admin: TestClient):
        for bad in ("Acme", "acme corp", "-acme", "acme_corp", ""):
            resp = auth_client_admin.put(f"/tenants/{self.TID}", json={"name": bad})
            assert resp.status_code == 422, f"{bad!r} -> {resp.status_code}"

    def test_requires_admin_role(
        self, auth_client_reader: TestClient, auth_client_no_role: TestClient, client: TestClient
    ):
        body = {"name": "acme-explicit"}
        assert auth_client_reader.put(f"/tenants/{self.TID}", json=body).status_code == 403
        assert auth_client_no_role.put(f"/tenants/{self.TID}", json=body).status_code == 403
        assert client.put(f"/tenants/{self.TID}", json=body).status_code == 401


class TestCostCentreWritePath:
    """`cost_centre` (models.py:934) had no write path at all — settable only by
    raw SQL or a migration (027, 030). This is the write path: both tenant
    writers now accept and persist it. Never invented by this service — a
    caller must already know the answer."""

    def test_create_tenant_persists_cost_centre(self, auth_client_admin: TestClient):
        resp = auth_client_admin.post(
            "/tenants", json={"name": "acme-costed", "cost_centre": "customers"}
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["cost_centre"] == "customers"

    def test_create_tenant_without_cost_centre_stays_null(self, auth_client_admin: TestClient):
        resp = auth_client_admin.post("/tenants", json={"name": "acme-uncosted"})
        assert resp.status_code == 201, resp.text
        assert resp.json()["cost_centre"] is None

    def test_register_tenant_persists_cost_centre(self, auth_client_admin: TestClient):
        tid = "019fa2b2-1632-7c85-9403-8a7d9e74ac01"
        resp = auth_client_admin.put(
            f"/tenants/{tid}", json={"name": "acme-explicit-costed", "cost_centre": "ci"}
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["cost_centre"] == "ci"

    def test_register_tenant_does_not_overwrite_an_existing_cost_centre(
        self, auth_client_admin: TestClient
    ):
        """INSERT-only, exactly like `kind`/`byo_mode` — re-registering an
        existing id must never overwrite an operator's already-set value."""
        tid = "019fa2b2-1632-7c85-9403-8a7d9e74ac02"
        first = auth_client_admin.put(
            f"/tenants/{tid}", json={"name": "acme-registered-once", "cost_centre": "service"}
        )
        assert first.status_code == 201, first.text
        second = auth_client_admin.put(
            f"/tenants/{tid}", json={"name": "acme-registered-once", "cost_centre": "tests"}
        )
        assert second.status_code == 200, second.text
        assert second.json()["cost_centre"] == "service"


class TestRegisterTenantKind:
    """`kind` (migration 027) had no API write path: `PUT /tenants/{id}` could not
    set it, so every tenant registered over HTTP was born `'unclassified'` and had
    to be reclassified by SQL. The register body now accepts it — validated
    against `ck_tenants_kind`, applied ONLY on INSERT like `cost_centre`."""

    def test_the_api_vocabulary_is_the_check_constraint_vocabulary(self):
        from typing import get_args

        import main
        from models import TENANT_KINDS

        assert (
            get_args(main.TenantKind)
            == TENANT_KINDS
            == (
                "customer",
                "internal",
                "service",
                "test",
                "unclassified",
            )
        )

    def test_register_with_kind_persists_it(
        self, auth_client_admin: TestClient, db_manager: DatabaseManager
    ):
        tid = "019fa2b2-1632-7c85-9403-8a7d9e74ac11"
        resp = auth_client_admin.put(
            f"/tenants/{tid}",
            json={"name": "acme-kind-customer", "kind": "customer", "cost_centre": "customers"},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["kind"] == "customer"
        assert resp.json()["cost_centre"] == "customers"
        # Read the ROW, not just the echo: the response is built from the row,
        # but a default applied at INSERT time is exactly what this must refute.
        row = db_manager.get_tenant(tid)
        assert row is not None and row.kind == "customer"

    def test_register_with_internal_kind_persists_it(self, auth_client_admin: TestClient):
        tid = "019fa2b2-1632-7c85-9403-8a7d9e74ac12"
        resp = auth_client_admin.put(
            f"/tenants/{tid}", json={"name": "acme-kind-internal", "kind": "internal"}
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["kind"] == "internal"

    def test_invalid_kind_is_422_and_registers_nothing(
        self, auth_client_admin: TestClient, db_manager: DatabaseManager
    ):
        tid = "019fa2b2-1632-7c85-9403-8a7d9e74ac13"
        resp = auth_client_admin.put(
            f"/tenants/{tid}", json={"name": "acme-kind-typo", "kind": "custmer"}
        )
        assert resp.status_code == 422, resp.text
        assert db_manager.get_tenant(tid) is None

    def test_omitting_kind_keeps_the_unclassified_default(self, auth_client_admin: TestClient):
        tid = "019fa2b2-1632-7c85-9403-8a7d9e74ac14"
        resp = auth_client_admin.put(f"/tenants/{tid}", json={"name": "acme-kind-omitted"})
        assert resp.status_code == 201, resp.text
        assert resp.json()["kind"] == "unclassified"

    def test_reregistering_never_overwrites_an_existing_kind(self, auth_client_admin: TestClient):
        tid = "019fa2b2-1632-7c85-9403-8a7d9e74ac15"
        first = auth_client_admin.put(
            f"/tenants/{tid}", json={"name": "acme-kind-once", "kind": "internal"}
        )
        assert first.status_code == 201, first.text
        second = auth_client_admin.put(
            f"/tenants/{tid}", json={"name": "acme-kind-once", "kind": "customer"}
        )
        assert second.status_code == 200, second.text
        assert second.json()["kind"] == "internal"

    def test_ensure_tenant_refuses_an_unknown_kind_by_name(self, db_manager: DatabaseManager):
        """Scripts reach `ensure_tenant` with no Pydantic model in the way; an
        unknown kind must be a named ClientSafeError, not an opaque IntegrityError."""
        import pytest

        from models import ClientSafeError

        with pytest.raises(ClientSafeError, match="invalid kind"):
            db_manager.ensure_tenant(
                "019fa2b2-1632-7c85-9403-8a7d9e74ac16",
                name="acme-kind-script",
                kind="custmer",
                owner_key="platform",
            )
