"""The ``care-bus`` bundle (migration 078) on a care-bus-owned tenant, through the REAL
routes as care-bus's own clients.

World: ``care-bus`` (issuer care-bus, 067), ``mcp`` (050 default issuers) and the strata
apps (models.APP_ISSUER_OVERRIDES), each with a ``standard`` plan; the ``care-bus``
bundle = {care-bus} with its ensurer (models.BUNDLE_ENSURER_SEED) and care-bus's edges
(models.APP_REQUIRES_SEED). Callers are signed secure-apps JWTs whose azp is the
api_client row: care-bus-licenses-admin (license-admin, 068) and care-bus-bundle-minter
(bundle-token-minter, 078). Billing is faked.

WHAT IS PINNED (the early-access approve + login + tool-call sequence):
* ensure as care-bus-licenses-admin: 200, care-bus is the bundle member licence, mcp /
  strata-rag / strata-ingest are DEPENDENCY licences (no bundle); no grant at all
  (care-bus owns the tenant; platform already holds its 050 grant);
* POST /licenses/user/{sub} for app care-bus (subject_realm care-bus) creates the
  person's OWN care-bus licence and NOTHING else — the tools stay the tenant's shared
  dependency licences; it is idempotent (login re-ensure) and does not disturb ensure
  in either order;
* care-bus may NOT issue a tool licence directly (403 app_not_issuable_by_caller);
* bundle-token: care-bus-bundle-minter mints each tool on the tenant (201, on the
  dependency licence); care-bus-licenses-admin is 403 (role); introspect of the minted
  token answers app=strata-rag and the tenant.
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
from models import APP_REQUIRES_SEED, BUNDLE_ENSURER_SEED, DatabaseManager, issuers_for_app_key
from tests.conftest import _make_mock_jwt
from tests.unit.test_budget_coverage import FakeBilling, _no_sleep, _patch_billing
from tests.unit.test_dependency_licences import (
    _dep_grants,
    _grantees,
    _licences,
    _mint,
    _minter,
    _tokens,
)

ADMIN = "care-bus-licenses-admin"  # -> care-bus (068), license-admin
MINTER = "care-bus-bundle-minter"  # -> care-bus (078), bundle-token-minter
TOOLS = {"mcp", "strata-rag", "strata-ingest"}
PERSON = "6f1c2b3a-4d5e-4f60-8a9b-0c1d2e3f4a5b"  # a care-bus realm Keycloak sub


@pytest.fixture
def world(db_manager, pinned_service_token, monkeypatch) -> Generator[DatabaseManager]:
    _no_sleep(monkeypatch)
    monkeypatch.setattr(main, "db_manager", db_manager)
    for key in ("care-bus", *sorted(TOOLS)):
        app = db_manager.create_app(key=key, name=key, issuers=issuers_for_app_key(key))
        db_manager.create_plan(app_id=UUID(str(app.id)), code="standard", name="Standard")
    with db_manager.engine.begin() as conn:
        bid = conn.execute(
            text("INSERT INTO bundle (key, name) VALUES ('care-bus', 'Care Bus') RETURNING id")
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO bundle_app (bundle_id, app_id) SELECT :b, id FROM app WHERE key = 'care-bus'"
            ),
            {"b": bid},
        )
        for owner in BUNDLE_ENSURER_SEED["care-bus"]:
            conn.execute(
                text("INSERT INTO bundle_ensurer (bundle_id, owner_key) VALUES (:b, :o)"),
                {"b": bid, "o": owner},
            )
        for app, required, caller in APP_REQUIRES_SEED:
            if app != "care-bus":
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


@pytest.fixture
def tenant(world) -> str:
    return str(world.create_tenant(name="care-bus-customer", owner_key="care-bus").id)


@contextmanager
def _admin(db: DatabaseManager, tenant_id: str) -> Iterator[TestClient]:
    main.db_manager = db
    user = KeycloakUser(
        sub=f"service-account-{ADMIN}",
        preferred_username=f"service-account-{ADMIN}",
        realm_access={"roles": ["license-admin"]},
        resource_access={},
    )
    with TestClient(main.app, raise_server_exceptions=False) as client:
        client.headers["Authorization"] = f"Bearer {_make_mock_jwt(user, azp=ADMIN)}"
        client.headers["X-Tenant-Id"] = tenant_id
        yield client


def _ensure(client: TestClient, tenant_id: str):
    return client.post(
        f"/internal/tenants/{tenant_id}/bundles/care-bus/ensure", json={"plans": ["standard"]}
    )


def _ids(db: DatabaseManager, key: str) -> dict[str, str]:
    app = db.get_app_by_key(key)
    assert app is not None
    plan = next(p for p in db.list_plans(UUID(str(app.id))) if str(p.code) == "standard")
    return {"app_id": str(app.id), "plan_id": str(plan.id)}


def _member(client: TestClient, db: DatabaseManager, key: str = "care-bus"):
    return client.post(
        f"/licenses/user/{PERSON}", json={**_ids(db, key), "subject_realm": "care-bus"}
    )


def _by_app(db: DatabaseManager, tenant_id: str) -> dict[str, list[tuple[bool, str | None, str]]]:
    """app -> [(is_dependency, bundle, subject sub)] for ACTIVE licences."""
    out: dict[str, list[tuple[bool, str | None, str]]] = {}
    with db.engine.connect() as conn:
        for key, dep, bkey, sub in conn.execute(
            text(
                "SELECT a.key, l.is_dependency, b.key, s.keycloak_sub FROM license l "
                "JOIN app a ON a.id = l.app_id JOIN subject s ON s.id = l.subject_id "
                "LEFT JOIN bundle b ON b.id = l.bundle_id "
                "WHERE l.tenant_id = :t AND l.status = 'active' ORDER BY a.key, s.keycloak_sub"
            ),
            {"t": tenant_id},
        ):
            out.setdefault(str(key), []).append((bool(dep), bkey, str(sub)))
    return out


class TestEnsure:
    def test_care_bus_plus_its_tools_as_dependency_licences(self, world, tenant):
        with _admin(world, tenant) as c:
            resp = _ensure(c, tenant)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["tenant_id"] == tenant and body["bundle"] == "care-bus"
        assert body["refused"] == []
        assert {k: (v["decision"], v["dependency"]) for k, v in body["apps"].items()} == {
            "care-bus": ("insert", False),
            **{t: ("insert", True) for t in TOOLS},
        }
        lic = _by_app(world, tenant)
        assert lic["care-bus"] == [(False, "care-bus", f"svc:{tenant}:care-bus")]
        for t in TOOLS:
            assert lic[t] == [(True, None, f"svc:{tenant}:{t}")], t
        # care-bus owns the tenant: no app-scoped grant, and no broad grant beyond
        # platform's 050 infrastructure grant.
        assert _dep_grants(world, tenant) == set()
        assert _grantees(world, tenant) <= {"platform"}

    def test_a_second_ensure_writes_nothing(self, world, tenant):
        with _admin(world, tenant) as c:
            _ensure(c, tenant)
            before = _by_app(world, tenant)
            again = _ensure(c, tenant)
        assert again.status_code == 200, again.text
        assert {v["decision"] for v in again.json()["apps"].values()} == {"exists"}
        assert _by_app(world, tenant) == before


class TestPerUserLicence:
    def test_creates_only_the_persons_care_bus_licence(self, world, tenant):
        with _admin(world, tenant) as c:
            _ensure(c, tenant)
            before = _by_app(world, tenant)
            resp = _member(c, world)
            again = _member(c, world)  # login re-ensure: idempotent
        assert resp.status_code == 201, resp.text
        assert again.status_code == 201 and again.json()["id"] == resp.json()["id"]
        after = _by_app(world, tenant)
        assert set(after["care-bus"]) == {*before["care-bus"], (False, None, PERSON)}
        for t in TOOLS:  # the tools stay the tenant's ONE shared dependency licence
            assert after[t] == before[t], t

    def test_without_ensure_it_creates_no_tool_licence(self, world, tenant):
        with _admin(world, tenant) as c:
            resp = _member(c, world)
        assert resp.status_code == 201, resp.text
        assert set(_by_app(world, tenant)) == {"care-bus"}

    def test_a_member_first_does_not_block_ensure(self, world, tenant):
        with _admin(world, tenant) as c:
            assert _member(c, world).status_code == 201
            resp = _ensure(c, tenant)
        assert resp.status_code == 200, resp.text
        assert resp.json()["refused"] == []
        assert set(_by_app(world, tenant)["care-bus"]) == {
            (False, "care-bus", f"svc:{tenant}:care-bus"),
            (False, None, PERSON),
        }

    @pytest.mark.parametrize("tool", sorted(TOOLS))
    def test_care_bus_cannot_issue_a_tool_directly(self, world, tenant, tool):
        with _admin(world, tenant) as c:
            resp = _member(c, world, tool)
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error"] == "app_not_issuable_by_caller"


class TestBundleToken:
    @pytest.mark.parametrize("tool", sorted(TOOLS))
    def test_the_minter_mints_each_tool_on_its_dependency_licence(self, world, tenant, tool):
        with _admin(world, tenant) as c:
            _ensure(c, tenant)
        dep = _licences(world, tenant)[tool]
        with _minter(world, MINTER) as c:
            resp = _mint(c, tenant, tool)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert set(body) == {
            "token",
            "expires_at",
            "tenant_id",
            "bundle",
            "app",
            "license_id",
            "token_id",
        }
        assert (body["tenant_id"], body["bundle"], body["app"]) == (tenant, "care-bus", tool)
        assert body["license_id"] == dep[0][0]

    def test_the_licences_admin_client_cannot_mint(self, world, tenant):
        with _admin(world, tenant) as c:
            _ensure(c, tenant)
            resp = _mint(c, tenant, "strata-rag")
        assert resp.status_code == 403, resp.text

    def test_introspect_answers_the_tool_and_the_tenant(self, world, tenant):
        with _admin(world, tenant) as c:
            _ensure(c, tenant)
        with _minter(world, MINTER) as c:
            minted = _mint(c, tenant, "strata-rag").json()
            resp = c.post("/license/introspect", json={"token": minted["token"]})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["active"] is True and body["status"] == "active"
        assert body["tenant_id"] == tenant
        assert body["license_id"] == minted["license_id"]
        assert body["app"]["key"] == "strata-rag"
        assert body["plan"]["code"] == "standard"


def _revoke(client: TestClient, licence_id: str):
    return client.post("/license/revoke", json={"license_id": licence_id})


def _active_tools(db: DatabaseManager, tenant_id: str) -> set[str]:
    return {k for k in _by_app(db, tenant_id) if k in TOOLS}


def _bundle_licence(db: DatabaseManager, tenant_id: str) -> str:
    return next(lid for lid, _dep, b in _licences(db, tenant_id)["care-bus"] if b == "care-bus")


class TestOffboarding:
    """Tool dependency licences live while ANY active non-dependency care-bus licence
    (the bundle licence or a person's member licence) is on the tenant; revoking the
    last one releases them (#292 cascade, ``_dependency_roots``)."""

    def test_revoking_the_bundle_licence_alone_releases_the_tools(self, world, tenant):
        with _admin(world, tenant) as c:
            _ensure(c, tenant)
            deps = {t: _licences(world, tenant)[t][0][0] for t in TOOLS}
            resp = _revoke(c, _bundle_licence(world, tenant))
        assert resp.status_code == 200, resp.text
        assert set(resp.json()["released_dependency_ids"]) == set(deps.values())
        assert _active_tools(world, tenant) == set()

    def test_a_person_licence_keeps_the_tools_until_it_is_revoked(self, world, tenant):
        with _admin(world, tenant) as c:
            _ensure(c, tenant)
            person = _member(c, world).json()["id"]
            first = _revoke(c, _bundle_licence(world, tenant))
            assert first.status_code == 200, first.text
            assert first.json()["released_dependency_ids"] == []
            assert _active_tools(world, tenant) == TOOLS
            second = _revoke(c, person)
        assert second.status_code == 200, second.text
        assert len(second.json()["released_dependency_ids"]) == len(TOOLS)
        assert _active_tools(world, tenant) == set()

    def test_bundle_token_is_refused_after_the_tools_are_released(self, world, tenant):
        with _admin(world, tenant) as c:
            _ensure(c, tenant)
            assert _revoke(c, _bundle_licence(world, tenant)).status_code == 200
        before = _tokens(world)
        with _minter(world, MINTER) as c:
            resp = _mint(c, tenant, "strata-rag")
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"]["error"] == "no_app_licence"
        assert _tokens(world) == before
