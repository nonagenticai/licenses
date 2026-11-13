"""Migration 085 behaviour: secure-apps/w2v-cell-m4 with ONLY ``license-reader``.

The m4 Studio cell reads the licences of the tenants it SERVES, which are not its own:
its licence gate calls ``GET /licenses/active?tenant_id=<served>`` and its provider
resolver ``GET /tenants/{id}/apps/w2v/byo-mode`` (the per-app override), both with
``X-Tenant-Id: <served>`` (w2v_orchestrator ``LicensesClient`` / w2v_common
``LicensesReadClient``). Through the REAL middleware and routes, with real signed
secure-apps JWTs:

* with the api_client row (owner w2v) the X-Tenant-Id retarget is admitted for a
  w2v-owned tenant and a tenant granted to w2v: /licenses/active answers the real
  verdict (200 / 404 ``no_active_license``) and the per-app override read answers;
* a tenant neither owned by nor granted to w2v is 403 ``tenant_not_accessible``;
* CONTROL — the same request from a client with NO api_client row is refused (the
  pre-085 state that forced gitops #4935 onto ``service-internal``);
* CONTROL — ``service-internal`` alone cannot read the per-app override at all (the
  route admits only license-admin / license-reader), so that read was broken under
  either role before 085;
* the effective posture ``GET /tenants/{uuid}/byo-mode?app=w2v`` is auth-exempt and
  answers regardless of the caller.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

import jwt
import pytest
from fastapi.testclient import TestClient

import main
from models import API_CLIENTS, DatabaseManager, issuers_for_app_key
from tests.support.jwt_keys import TEST_ISSUER, private_key_pem

CELL = "w2v-cell-m4"
UNREGISTERED = "w2v-cell-unregistered"
APP = "w2v"


def _token(azp: str, roles: list[str]) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": f"service-account-{azp}",
        "preferred_username": f"service-account-{azp}",
        "realm_access": {"roles": roles},
        "resource_access": {},
        "azp": azp,
        "iss": TEST_ISSUER,
        "iat": now - 5,
        "exp": now + 3600,
    }
    return jwt.encode(payload, private_key_pem(), algorithm="RS256")


@contextmanager
def _client(db: DatabaseManager, azp: str, roles: list[str], tenant: str) -> Iterator[TestClient]:
    original = main.db_manager
    main.db_manager = db
    try:
        with TestClient(main.app, raise_server_exceptions=False) as client:
            client.headers["Authorization"] = f"Bearer {_token(azp, roles)}"
            client.headers["X-Tenant-Id"] = tenant
            yield client
    finally:
        main.db_manager = original


def _tenant(db: DatabaseManager, owner: str) -> str:
    return str(db.create_tenant(name=f"t-{uuid.uuid4().hex[:10]}", owner_key=owner).id)


def _err(resp) -> str | None:
    detail = resp.json().get("detail")
    return detail.get("error") if isinstance(detail, dict) else None


def _active(c: TestClient, tenant: str):
    return c.get("/licenses/active", params={"tenant_id": tenant, "app_key": APP})


@pytest.fixture
def world(db_manager) -> DatabaseManager:
    app = db_manager.create_app(key=APP, name=APP, issuers=issuers_for_app_key(APP))
    db_manager.create_plan(app_id=UUID(str(app.id)), code="standard", name="Standard")
    return db_manager


def _license(db: DatabaseManager, tenant: str) -> str:
    app = db.get_app_by_key(APP)
    assert app is not None
    plan = next(p for p in db.list_plans(UUID(str(app.id))) if p.code == "standard")
    subject = db.create_or_get_subject(f"user-{uuid.uuid4().hex[:8]}", "w2v")
    lic = db.create_license(
        subject_id=UUID(str(subject.id)),
        app_id=UUID(str(app.id)),
        plan_id=UUID(str(plan.id)),
        tenant_id=tenant,
    )
    return str(lic.id)


class TestSeed:
    def test_the_cell_is_a_w2v_client_not_cross_owner(self):
        assert ("secure-apps", CELL, "w2v", False) in API_CLIENTS
        assert not any(c[1] == UNREGISTERED for c in API_CLIENTS)


class TestActiveLicence:
    def test_w2v_tenant_licensed_is_200(self, world):
        tenant = _tenant(world, "w2v")
        lic = _license(world, tenant)
        with _client(world, CELL, ["license-reader"], tenant) as c:
            r = _active(c, tenant)
        assert r.status_code == 200, r.text
        assert r.json()["tenant_id"] == tenant
        assert r.json()["license_id"] == lic

    def test_w2v_tenant_unlicensed_is_the_real_404(self, world):
        tenant = _tenant(world, "w2v")
        with _client(world, CELL, ["license-reader"], tenant) as c:
            r = _active(c, tenant)
        assert r.status_code == 404, r.text
        assert _err(r) == "no_active_license"

    def test_tenant_granted_to_w2v_is_readable(self, world):
        tenant = _tenant(world, "fr")
        world.put_tenant_grant(tenant, "w2v", granted_by="test")
        _license(world, tenant)
        with _client(world, CELL, ["license-reader"], tenant) as c:
            r = _active(c, tenant)
        assert r.status_code == 200, r.text

    def test_foreign_tenant_is_refused(self, world):
        mine = _tenant(world, "w2v")  # positive control on the same client
        foreign = _tenant(world, "fr")
        _license(world, foreign)
        with _client(world, CELL, ["license-reader"], foreign) as c:
            refused = _active(c, foreign)
        with _client(world, CELL, ["license-reader"], mine) as c:
            control = _active(c, mine)
        assert refused.status_code == 403, refused.text
        assert _err(refused) == "tenant_not_accessible"
        assert control.status_code == 404, control.text

    def test_query_tenant_other_than_the_header_is_refused(self, world):
        mine = _tenant(world, "w2v")
        foreign = _tenant(world, "fr")
        _license(world, foreign)
        with _client(world, CELL, ["license-reader"], mine) as c:
            r = _active(c, foreign)
        assert r.status_code == 403, r.text

    def test_control_without_the_row_the_reader_cannot_read(self, world):
        tenant = _tenant(world, "w2v")
        _license(world, tenant)
        with _client(world, UNREGISTERED, ["license-reader"], tenant) as c:
            r = _active(c, tenant)
        assert r.status_code in (401, 403), r.text


class TestPerAppByoOverride:
    def _override(self, db: DatabaseManager, tenant: str) -> None:
        assert db.set_tenant_app_byo_mode(tenant, APP, "byo_required") is not None

    def test_reader_reads_the_override(self, world):
        tenant = _tenant(world, "w2v")
        self._override(world, tenant)
        with _client(world, CELL, ["license-reader"], tenant) as c:
            r = c.get(f"/tenants/{tenant}/apps/{APP}/byo-mode")
        assert r.status_code == 200, r.text
        assert r.json()["byo_mode"] == "byo_required"

    def test_foreign_tenant_override_is_refused(self, world):
        foreign = _tenant(world, "fr")
        self._override(world, foreign)
        with _client(world, CELL, ["license-reader"], foreign) as c:
            r = c.get(f"/tenants/{foreign}/apps/{APP}/byo-mode")
        assert r.status_code == 403, r.text
        assert _err(r) == "tenant_not_accessible"

    def test_control_service_internal_cannot_read_the_override(self, world):
        tenant = _tenant(world, "w2v")
        self._override(world, tenant)
        with _client(world, CELL, ["service-internal"], tenant) as c:
            r = c.get(f"/tenants/{tenant}/apps/{APP}/byo-mode")
        assert r.status_code == 403, r.text

    def test_control_without_the_row_the_override_is_not_readable(self, world):
        tenant = _tenant(world, "w2v")
        self._override(world, tenant)
        with _client(world, UNREGISTERED, ["license-reader"], tenant) as c:
            r = c.get(f"/tenants/{tenant}/apps/{APP}/byo-mode")
        assert r.status_code in (401, 403, 404), r.text
        assert r.status_code != 200

    def test_effective_posture_is_auth_exempt(self, world):
        tenant = _tenant(world, "w2v")
        self._override(world, tenant)
        with _client(world, CELL, ["license-reader"], tenant) as c:
            r = c.get(f"/tenants/{tenant}/byo-mode", params={"app": APP, "on_unresolved": "error"})
        assert r.status_code == 200, r.text
        assert r.json()["byo_mode"] == "byo_required"
