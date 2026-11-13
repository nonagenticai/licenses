"""Migration 082 behaviour: the `agent` bundle and the mcp toolkit closure, through the
REAL ensure and bundle-token routes.

World: ``agent`` (issuer agent, 070), ``care-bus`` (issuer care-bus, 067), ``mcp`` and
its four backends, each with a ``standard`` plan; bundles ``agent`` = {agent} and
``care-bus`` = {care-bus} with their ensurers (models.BUNDLE_ENSURER_SEED) and EVERY
edge of models.APP_REQUIRES_SEED out of agent / care-bus / mcp. Callers are signed
secure-apps JWTs whose azp is the api_client row (boot seed, models.API_CLIENTS).

WHAT IS PINNED:
* the closure is TRANSITIVE: ensure of bundle `agent` on an agent-owned tenant gives
  agent (member) + mcp + coding / strata-rag / strata-ingest / laya (dependency
  licences), and writes tenant_dependency_grant (mcp, <backend>) for each backend —
  none for agent (the tenant's own owner);
* mcp-bundle-minter (owner mcp, owns no tenant) mints each backend on that tenant with
  ``bundle=agent`` passed explicitly and without it; a bundle the tenant does not hold
  is 404 ``bundle_not_held``; an app outside its grants (mcp, agent) is 403
  ``dependency_grant_scope``; a tenant with no mcp grant is 404;
* agent-bundle-minter mints the tenant's ``mcp`` licence;
* a care-bus tenant reaches the backends through care-bus -> mcp -> backend too.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from contextlib import contextmanager
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import main
from auth.models import KeycloakUser
from models import (
    API_CLIENTS,
    APP_REQUIRES_SEED,
    BUNDLE_ENSURER_SEED,
    TENANT_OWNERS,
    DatabaseManager,
    issuers_for_app_key,
)
from services.app_requires import Edge, closure
from tests.conftest import _make_mock_jwt
from tests.unit.test_budget_coverage import FakeBilling, _no_sleep, _patch_billing
from tests.unit.test_dependency_licences import _dep_grants, _licences, _mint, _minter

BACKENDS = {"coding", "strata-rag", "strata-ingest", "laya"}
MCP_MINTER = "mcp-bundle-minter"
AGENT_MINTER = "agent-bundle-minter"
AGENT_ADMIN = "agent-licenses-admin"
CARE_BUS_ADMIN = "care-bus-licenses-admin"
PRODUCTS = ("agent", "care-bus")


class TestSeeds:
    def test_owner_clients_ensurer_and_edges(self):
        assert ("mcp", "MCP") in TENANT_OWNERS
        assert ("secure-apps", MCP_MINTER, "mcp", False) in API_CLIENTS
        assert ("secure-apps", AGENT_MINTER, "agent", False) in API_CLIENTS
        assert BUNDLE_ENSURER_SEED["agent"] == ("agent",)
        assert {(a, r, c) for a, r, c in APP_REQUIRES_SEED if a == "mcp"} == {
            ("mcp", b, "mcp") for b in BACKENDS
        }
        assert ("agent", "mcp", "agent") in APP_REQUIRES_SEED
        # Migration 086: agent reaches inference-router directly (not via mcp).
        assert ("agent", "inference-router", "agent") in APP_REQUIRES_SEED
        assert not any(a == "mcp" and r == "inference-router" for a, r, _ in APP_REQUIRES_SEED)
        # mcp issues nothing: its backends stay dependency licences only.
        for key in (*BACKENDS, "mcp", "agent"):
            assert "mcp" not in issuers_for_app_key(key), key

    @pytest.mark.parametrize("product", PRODUCTS)
    def test_the_seed_closure_is_transitive(self, product):
        deps, walked = closure([product], [Edge(*e) for e in APP_REQUIRES_SEED])
        assert {"mcp", *BACKENDS} <= set(deps)
        assert {(e.caller, e.required) for e in walked} >= {("mcp", b) for b in BACKENDS}


@pytest.fixture
def world(db_manager, pinned_service_token, monkeypatch) -> Generator[DatabaseManager]:
    _no_sleep(monkeypatch)
    monkeypatch.setattr(main, "db_manager", db_manager)
    for key in (*PRODUCTS, "mcp", *sorted(BACKENDS)):
        app = db_manager.create_app(key=key, name=key, issuers=issuers_for_app_key(key))
        db_manager.create_plan(app_id=UUID(str(app.id)), code="standard", name="Standard")
    with db_manager.engine.begin() as conn:
        for product in PRODUCTS:
            bid = conn.execute(
                text("INSERT INTO bundle (key, name) VALUES (:k, :k) RETURNING id"),
                {"k": product},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO bundle_app (bundle_id, app_id) "
                    "SELECT :b, id FROM app WHERE key = :k"
                ),
                {"b": bid, "k": product},
            )
            for owner in BUNDLE_ENSURER_SEED[product]:
                conn.execute(
                    text("INSERT INTO bundle_ensurer (bundle_id, owner_key) VALUES (:b, :o)"),
                    {"b": bid, "o": owner},
                )
        for app, required, caller in APP_REQUIRES_SEED:
            if app not in (*PRODUCTS, "mcp"):
                continue
            conn.execute(
                text(
                    "INSERT INTO app_requires (app_id, required_app_id, caller_owner_key) "
                    "SELECT a.id, r.id, :c FROM app a, app r WHERE a.key = :a AND r.key = :r"
                ),
                {"a": app, "r": required, "c": caller},
            )
    patcher = _patch_billing(FakeBilling())
    try:
        yield db_manager
    finally:
        patcher.stop()


@contextmanager
def _admin(db: DatabaseManager, azp: str, tenant_id: str) -> Iterator[TestClient]:
    main.db_manager = db
    user = KeycloakUser(
        sub=f"service-account-{azp}",
        preferred_username=f"service-account-{azp}",
        realm_access={"roles": ["license-admin"]},
        resource_access={},
    )
    with TestClient(main.app, raise_server_exceptions=False) as client:
        client.headers["Authorization"] = f"Bearer {_make_mock_jwt(user, azp=azp)}"
        client.headers["X-Tenant-Id"] = tenant_id
        yield client


def _ensured(db: DatabaseManager, owner: str, bundle: str, admin: str) -> str:
    tenant = str(db.create_tenant(name=f"{owner}-customer", owner_key=owner).id)
    with _admin(db, admin, tenant) as c:
        resp = c.post(
            f"/internal/tenants/{tenant}/bundles/{bundle}/ensure", json={"plans": ["standard"]}
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["refused"] == []
    return tenant


@pytest.fixture
def agent_tenant(world) -> str:
    return _ensured(world, "agent", "agent", AGENT_ADMIN)


class TestAgentEnsureIsTransitive:
    def test_agent_member_plus_mcp_and_backends_as_dependencies(self, world, agent_tenant):
        lic = _licences(world, agent_tenant)
        assert set(lic) == {"agent", "mcp", *BACKENDS}
        assert [(d, b) for _, d, b in lic["agent"]] == [(False, "agent")]
        for key in ("mcp", *BACKENDS):
            assert [(d, b) for _, d, b in lic[key]] == [(True, None)], key

    def test_grants_mcp_each_backend_and_agent_nothing(self, world, agent_tenant):
        assert _dep_grants(world, agent_tenant) == {("mcp", b) for b in BACKENDS}


class TestMcpMinter:
    @pytest.mark.parametrize("backend", sorted(BACKENDS))
    @pytest.mark.parametrize("bundle", ["agent", None])
    def test_mints_each_backend_on_its_dependency_licence(
        self, world, agent_tenant, backend, bundle
    ):
        dep = _licences(world, agent_tenant)[backend]
        with _minter(world, MCP_MINTER) as c:
            resp = _mint(c, agent_tenant, backend, bundle)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert (body["tenant_id"], body["bundle"], body["app"]) == (
            agent_tenant,
            "agent",
            backend,
        )
        assert body["license_id"] == dep[0][0]

    def test_a_bundle_the_tenant_does_not_hold_is_404(self, world, agent_tenant):
        with _minter(world, MCP_MINTER) as c:
            resp = _mint(c, agent_tenant, "coding", "care-bus")
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"]["error"] == "no_bundle"

    @pytest.mark.parametrize("app", ["mcp", "agent"])
    def test_an_app_outside_its_grants_is_403(self, world, agent_tenant, app):
        with _minter(world, MCP_MINTER) as c:
            resp = _mint(c, agent_tenant, app, "agent")
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error"] == "dependency_grant_scope"

    def test_a_tenant_without_an_mcp_grant_is_404(self, world):
        bare = str(world.create_tenant(name="agent-bare", owner_key="agent").id)
        with _minter(world, MCP_MINTER) as c:
            resp = _mint(c, bare, "coding", "agent")
        assert resp.status_code == 404, resp.text


class TestAgentMinter:
    def test_mints_the_tenants_mcp_licence(self, world, agent_tenant):
        dep = _licences(world, agent_tenant)["mcp"]
        with _minter(world, AGENT_MINTER) as c:
            resp = _mint(c, agent_tenant, "mcp", "agent")
        assert resp.status_code == 201, resp.text
        assert resp.json()["license_id"] == dep[0][0]


class TestCareBusReachesTheBackends:
    def test_care_bus_tenant_gets_the_backends_and_mcp_mints_them(self, world):
        tenant = _ensured(world, "care-bus", "care-bus", CARE_BUS_ADMIN)
        assert {"mcp", *BACKENDS} <= set(_licences(world, tenant))
        assert _dep_grants(world, tenant) == {("mcp", b) for b in BACKENDS}
        with _minter(world, MCP_MINTER) as c:
            resp = _mint(c, tenant, "coding", "care-bus")
        assert resp.status_code == 201, resp.text
        assert resp.json()["bundle"] == "care-bus"
