"""Dependency licences (migration 059): ensure provisions the app_requires closure.

World: the live catalogue slice the conveyor / w2v bundles touch (issuers as on prod,
2026-10-15), the two bundles with their 059 members, the seeded edges
(models.APP_REQUIRES_SEED) and bundle callers (models.BUNDLE_CALLER_SEED). Ensure
runs through ``services.bundle_ensure`` (plan + apply, metering stubbed); minting runs
through the real route as the REAL minter clients (signed secure-apps JWTs whose azp
is an ``api_client`` row holding only ``bundle-token-minter``).

WHAT IS PINNED.
* ensure inserts ONE dependency licence per closure app (``is_dependency``, no
  bundle) beside the members, and grants each walked edge's caller + the bundle's
  callers on the tenant — never the tenant's own owner;
* DEDUPE: a tenant holding conveyor AND w2v has exactly ONE ``workers`` licence,
  in either ensure order; the w2v-first order PROMOTES the dependency licence when
  conveyor (which sells workers) adopts it; minting ``workers`` works with
  ``bundle=conveyor``, ``bundle=w2v`` and without ``bundle``, all on that licence;
* bundle-token mints a DEPENDENCY app (inference-router via conveyor), and w2v's
  minter mints ``workers`` for a merqi tenant that ensured ``w2v`` (merqi -> w2v ->
  worker_management, end-tenant token);
* owner scope (054): the fr minter mints ``workers`` on an fr tenant holding
  conveyor; the w2v minter on that tenant is 404 ``tenant_not_found``;
* user token issuance on a dependency licence is 403 ``dependency_licence``
  (``POST /licenses/{id}/tokens``, ``regenerate-token``), nothing minted; a member
  licence still mints (control);
* a cycle in app_requires refuses the ensure (``dependency_cycle``), nothing written.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import main
from auth.models import KeycloakUser
from models import APP_REQUIRES_SEED, BUNDLE_CALLER_SEED, DatabaseManager
from services import bundle_ensure
from tests.conftest import _make_mock_jwt

FR_MINTER = "fr-bundle-minter"  # -> fr (059)
W2V_MINTER = "w2v-bundle-minter"  # -> w2v (059)
BREAK_GLASS = "licenses-admin"

# app -> issuers, as on prod (the primary, read-only, 2026-10-15).
APPS: dict[str, tuple[str, ...]] = {
    "workers": ("fr", "platform"),
    "coding": ("fr", "platform"),
    "laya": ("fr", "platform"),
    "kag": ("fr", "kag"),
    "requirements": ("fr", "platform"),
    "procedures": ("fr", "platform"),
    "secrets": ("fr", "platform"),  # 050 default issuers; conveyor member since 065
    "skill-builder": ("fr", "platform"),  # 050 default issuers; conveyor member since 066
    "inference-router": ("commonpractice", "fr", "kag"),
    "mlx-backend": ("fr", "platform"),
    "mlx-ingestion-backend": ("fr", "platform"),
    "w2v": ("w2v",),
    "w2v-image": ("w2v",),
    "marketing": ("merqi",),
    "sales": ("merqi",),
}
BUNDLES = {
    "conveyor": (
        "workers",
        "coding",
        "laya",
        "kag",
        "requirements",
        "procedures",
        "secrets",
        "skill-builder",
    ),
    "w2v": ("w2v", "w2v-image"),
}
CONVEYOR_DEPS = {"inference-router", "mlx-backend", "mlx-ingestion-backend"}
W2V_DEPS = {"workers", "kag", "inference-router", "mlx-backend", "mlx-ingestion-backend"}


def _seed(db: DatabaseManager) -> None:
    for key, issuers in APPS.items():
        app = db.create_app(key=key, name=key, issuers=issuers)
        db.create_plan(app_id=UUID(str(app.id)), code="standard", name="Standard")
    with db.engine.begin() as conn:
        for bundle, members in BUNDLES.items():
            bid = conn.execute(
                text("INSERT INTO bundle (key, name) VALUES (:k, :k) RETURNING id"), {"k": bundle}
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO bundle_app (bundle_id, app_id) "
                    "SELECT :b, id FROM app WHERE key = ANY(:keys)"
                ),
                {"b": bid, "keys": list(members)},
            )
        for app, required, caller in APP_REQUIRES_SEED:
            conn.execute(
                text(
                    "INSERT INTO app_requires (app_id, required_app_id, caller_owner_key) "
                    "SELECT a.id, r.id, :c FROM app a, app r WHERE a.key = :a AND r.key = :r"
                ),
                {"a": app, "r": required, "c": caller},
            )
        for bundle, pairs in BUNDLE_CALLER_SEED.items():
            for owner, app in pairs:
                conn.execute(
                    text(
                        "INSERT INTO bundle_caller (bundle_id, owner_key, app_id) "
                        "SELECT b.id, :o, a.id FROM bundle b, app a "
                        "WHERE b.key = :b AND a.key = :a"
                    ),
                    {"b": bundle, "o": owner, "a": app},
                )


def _no_metering(*_a, **_k) -> str:
    return ""


def _ensure(db: DatabaseManager, tenant: str, bundle: str) -> bundle_ensure.EnsurePlan:
    ep = bundle_ensure.plan(db, tenant, bundle, ["standard"])
    return bundle_ensure.apply(db, ep, provisioned_by="test", provision=_no_metering)


def _licences(db: DatabaseManager, tenant: str) -> dict[str, list[tuple]]:
    """app key -> [(licence id, is_dependency, bundle key|None)] for ACTIVE licences."""
    out: dict[str, list[tuple]] = {}
    with db.engine.connect() as conn:
        for lid, key, dep, bkey in conn.execute(
            text(
                "SELECT l.id, a.key, l.is_dependency, b.key FROM license l "
                "JOIN app a ON a.id = l.app_id LEFT JOIN bundle b ON b.id = l.bundle_id "
                "WHERE l.tenant_id = :t AND l.status = 'active' ORDER BY a.key"
            ),
            {"t": tenant},
        ):
            out.setdefault(str(key), []).append((str(lid), bool(dep), bkey))
    return out


def _grantees(db: DatabaseManager, tenant: str) -> set[str]:
    """BROAD grants (tenant_app_grant)."""
    return {str(g.grantee_owner_key) for g in db.list_tenant_grants(tenant)}


def _dep_grants(db: DatabaseManager, tenant: str) -> set[tuple[str, str]]:
    """APP-SCOPED grants (tenant_dependency_grant, migration 059): (owner, app)."""
    with db.engine.connect() as conn:
        return {
            (str(o), str(k))
            for o, k in conn.execute(
                text(
                    "SELECT g.owner_key, a.key FROM tenant_dependency_grant g "
                    "JOIN app a ON a.id = g.app_id WHERE g.tenant_id = :t"
                ),
                {"t": tenant},
            )
        }


def _tokens(db: DatabaseManager) -> int:
    with db.engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM token")).scalar_one()


@contextmanager
def _minter(db: DatabaseManager, azp: str) -> Iterator[TestClient]:
    main.db_manager = db
    user = KeycloakUser(
        sub=f"service-account-{azp}",
        preferred_username=f"service-account-{azp}",
        realm_access={"roles": ["bundle-token-minter"]},
        resource_access={},
    )
    with TestClient(main.app, raise_server_exceptions=False) as client:
        client.headers["Authorization"] = f"Bearer {_make_mock_jwt(user, azp=azp)}"
        yield client


@contextmanager
def _admin(db: DatabaseManager, tenant: str) -> Iterator[TestClient]:
    main.db_manager = db
    user = KeycloakUser(
        sub="admin-user-uuid",
        preferred_username="admin",
        realm_access={"roles": ["license-admin"]},
        resource_access={},
    )
    with TestClient(main.app, raise_server_exceptions=False) as client:
        client.headers["Authorization"] = f"Bearer {_make_mock_jwt(user, azp=BREAK_GLASS)}"
        client.headers["X-Tenant-Id"] = tenant
        yield client


def _mint(client: TestClient, tenant: str, app: str, bundle: str | None = None):
    q = f"app={app}" + (f"&bundle={bundle}" if bundle else "")
    return client.post(f"/internal/tenants/{tenant}/bundle-token?{q}")


@pytest.fixture
def world(db_manager) -> DatabaseManager:
    _seed(db_manager)
    return db_manager


@pytest.fixture
def fr_tenant(world) -> str:
    return str(world.create_tenant(name="fr-conveyor-customer", owner_key="fr").id)


class TestEnsureProvisionsTheClosure:
    def test_conveyor_inserts_members_and_one_dependency_licence_per_closure_app(
        self, world, fr_tenant
    ):
        ep = _ensure(world, fr_tenant, "conveyor")
        assert not ep.refused
        lic = _licences(world, fr_tenant)
        assert set(lic) == set(BUNDLES["conveyor"]) | CONVEYOR_DEPS
        for app in BUNDLES["conveyor"]:
            assert [(d, b) for _, d, b in lic[app]] == [(False, "conveyor")], app
        for app in CONVEYOR_DEPS:
            assert [(d, b) for _, d, b in lic[app]] == [(True, None)], app
        assert {p.app_key for p in ep.apps if p.dependency} == CONVEYOR_DEPS

    def test_grants_are_app_scoped_and_never_the_owner(self, world, fr_tenant):
        _ensure(world, fr_tenant, "conveyor")
        # Every walked edge's (caller, required app); fr's bundle_caller row is skipped
        # because fr OWNS the tenant. No broad grant is added: only 050's platform one.
        assert _dep_grants(world, fr_tenant) == {
            ("kag", "workers"),
            ("kag", "mlx-backend"),
            ("kag", "mlx-ingestion-backend"),
            ("platform", "inference-router"),
        }
        assert _grantees(world, fr_tenant) == {"platform"}

    def test_bundle_caller_is_scoped_to_its_app_on_another_owners_tenant(self, world):
        tid = str(world.create_tenant(name="platform-conveyor", owner_key="platform").id)
        _ensure(world, tid, "conveyor")
        # 059: (fr, workers); 065: (fr, secrets); 066: (fr, skill-builder) — fr's
        # bundle_caller rows, nothing more.
        assert {g for g in _dep_grants(world, tid) if g[0] == "fr"} == {
            ("fr", "workers"),
            ("fr", "secrets"),
            ("fr", "skill-builder"),
        }
        assert _grantees(world, tid) == set()

    def test_a_second_ensure_writes_nothing(self, world, fr_tenant):
        _ensure(world, fr_tenant, "conveyor")
        before = _licences(world, fr_tenant)
        ep = _ensure(world, fr_tenant, "conveyor")
        assert {p.action for p in ep.apps} == {"exists"}
        assert _licences(world, fr_tenant) == before

    def test_a_cycle_refuses_the_ensure_and_writes_nothing(self, world, fr_tenant):
        with world.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO app_requires (app_id, required_app_id, caller_owner_key) "
                    "SELECT a.id, r.id, 'platform' FROM app a, app r "
                    "WHERE a.key = 'inference-router' AND r.key = 'workers'"
                )
            )
        with pytest.raises(bundle_ensure.EnsureError) as exc:
            bundle_ensure.plan_and_apply(world, fr_tenant, "conveyor", ["standard"], None, True)
        assert exc.value.code == "dependency_cycle"
        assert "workers -> inference-router -> workers" in str(exc.value)
        assert _licences(world, fr_tenant) == {}


class TestDedupe:
    @pytest.mark.parametrize("order", [("conveyor", "w2v"), ("w2v", "conveyor")])
    def test_conveyor_and_w2v_share_one_workers_licence(self, world, fr_tenant, order):
        for bundle in order:
            assert not _ensure(world, fr_tenant, bundle).refused, bundle
        lic = _licences(world, fr_tenant)
        for app, rows in lic.items():
            assert len(rows) == 1, (app, rows)
        # conveyor SELLS workers: in the w2v-first order the dependency licence was
        # adopted by conveyor and promoted (is_dependency cleared), not duplicated.
        assert [(d, b) for _, d, b in lic["workers"]] == [(False, "conveyor")]
        assert [(d, b) for _, d, b in lic["kag"]] == [(False, "conveyor")]
        assert [(d, b) for _, d, b in lic["inference-router"]] == [(True, None)]

        workers = lic["workers"][0][0]
        with _minter(world, FR_MINTER) as c:
            for bundle in (None, "conveyor", "w2v"):
                resp = _mint(c, fr_tenant, "workers", bundle)
                assert resp.status_code == 201, (bundle, resp.text)
                assert resp.json()["license_id"] == workers, bundle

    def test_a_shared_dependency_mints_without_bundle(self, world, fr_tenant):
        _ensure(world, fr_tenant, "conveyor")
        _ensure(world, fr_tenant, "w2v")
        ir = _licences(world, fr_tenant)["inference-router"][0][0]
        with _minter(world, FR_MINTER) as c:
            for bundle in (None, "conveyor", "w2v"):
                resp = _mint(c, fr_tenant, "inference-router", bundle)
                assert resp.status_code == 201, (bundle, resp.text)
                assert resp.json()["license_id"] == ir


class TestBundleToken:
    def test_fr_minter_mints_workers_and_w2v_minter_is_out_of_scope(self, world, fr_tenant):
        _ensure(world, fr_tenant, "conveyor")
        with _minter(world, FR_MINTER) as c:
            ok = _mint(c, fr_tenant, "workers")
        assert ok.status_code == 201, ok.text
        assert ok.json()["bundle"] == "conveyor"
        before = _tokens(world)
        with _minter(world, W2V_MINTER) as c:
            denied = _mint(c, fr_tenant, "workers")
        assert denied.status_code == 404, denied.text
        assert denied.json()["detail"]["reason"] == "tenant_not_found"
        assert _tokens(world) == before

    def test_a_dependency_app_mints_through_its_bundle(self, world, fr_tenant):
        _ensure(world, fr_tenant, "conveyor")
        ir = _licences(world, fr_tenant)["inference-router"][0][0]
        with _minter(world, FR_MINTER) as c:
            resp = _mint(c, fr_tenant, "inference-router", "conveyor")
        assert resp.status_code == 201, resp.text
        assert resp.json()["license_id"] == ir
        assert resp.json()["bundle"] == "conveyor"

    def test_an_app_outside_the_closure_is_not_in_the_bundle(self, world, fr_tenant):
        _ensure(world, fr_tenant, "conveyor")
        with _minter(world, FR_MINTER) as c:
            resp = _mint(c, fr_tenant, "w2v")
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"]["error"] == "app_not_in_bundle"

    def test_fr_mints_skill_builder_on_a_conveyor_tenant(self, world, fr_tenant):
        """066: a conveyor tenant (fr-owned) — fr mints app=skill-builder."""
        _ensure(world, fr_tenant, "conveyor")
        sb = _licences(world, fr_tenant)["skill-builder"][0][0]
        with _minter(world, FR_MINTER) as c:
            resp = _mint(c, fr_tenant, "skill-builder")
        assert resp.status_code == 201, resp.text
        assert resp.json()["license_id"] == sb
        assert resp.json()["bundle"] == "conveyor"

    def test_skill_builder_is_not_in_bundle_for_a_non_conveyor_tenant(self, world, fr_tenant):
        """066 negative: a tenant holding only `w2v` (no conveyor) is still refused
        app=skill-builder with 404 app_not_in_bundle, and nothing is minted."""
        assert not _ensure(world, fr_tenant, "w2v").refused
        before = _tokens(world)
        with _minter(world, FR_MINTER) as c:
            resp = _mint(c, fr_tenant, "skill-builder")
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"]["error"] == "app_not_in_bundle"
        assert _tokens(world) == before

    def test_w2v_mints_workers_for_the_merqi_end_tenant(self, world):
        """merqi -> w2v -> worker_management: merqi ensures `w2v` on its tenant; w2v's
        minter (granted by the ensure) mints that END tenant's `workers` licence."""
        merqi = str(world.create_tenant(name="merqi-customer", owner_key="merqi").id)
        assert not _ensure(world, merqi, "w2v").refused
        # w2v: 050's issuer grant (it alone issues `w2v`) — unchanged; kag only scoped.
        assert _grantees(world, merqi) == {"w2v", "platform"}
        assert {("w2v", "workers"), ("w2v", "kag"), ("kag", "workers")} <= _dep_grants(world, merqi)
        dep = _licences(world, merqi)["workers"]
        assert [(d, b) for _, d, b in dep] == [(True, None)]
        with _minter(world, W2V_MINTER) as c:
            resp = _mint(c, merqi, "workers")
        assert resp.status_code == 201, resp.text
        assert resp.json()["license_id"] == dep[0][0]
        assert resp.json()["tenant_id"] == merqi


class TestNoUserTokenOnADependencyLicence:
    def test_create_and_regenerate_are_403_and_mint_nothing(self, world, fr_tenant):
        _ensure(world, fr_tenant, "conveyor")
        lic = _licences(world, fr_tenant)
        dep, member = lic["inference-router"][0][0], lic["workers"][0][0]
        before = _tokens(world)
        with _admin(world, fr_tenant) as c:
            created = c.post(f"/licenses/{dep}/tokens?expires_days=1")
            regen = c.post(f"/licenses/{dep}/regenerate-token")
        for resp in (created, regen):
            assert resp.status_code == 403, resp.text
            assert resp.json()["detail"]["error"] == "dependency_licence"
        assert _tokens(world) == before
        with _admin(world, fr_tenant) as c:  # control: a member licence still mints
            ok = c.post(f"/licenses/{member}/tokens?expires_days=1")
        assert ok.status_code == 201, ok.text

    def test_internal_licences_publishes_the_marker(self, world, fr_tenant):
        _ensure(world, fr_tenant, "conveyor")
        with _admin(world, fr_tenant) as c:
            resp = c.get(f"/internal/licenses?tenant_id={fr_tenant}")
        assert resp.status_code == 200, resp.text
        marks = {r["app"]["key"]: r["is_dependency"] for r in resp.json()}
        assert {k for k, v in marks.items() if v} == CONVEYOR_DEPS


class TestDependencyGrantScope:
    """A scoped grant (tenant_dependency_grant) mints its app only; a broad
    (tenant_app_grant) grant behaves exactly as before 059."""

    @pytest.fixture
    def platform_tenant(self, world) -> str:
        tid = str(world.create_tenant(name="platform-conveyor", owner_key="platform").id)
        assert not _ensure(world, tid, "conveyor").refused
        return tid

    def test_scoped_grant_mints_its_app_and_is_403_for_another_bundle_app(
        self, world, platform_tenant
    ):
        before = _tokens(world)
        with _minter(world, FR_MINTER) as c:
            ok = _mint(c, platform_tenant, "workers")
            other = _mint(c, platform_tenant, "coding")  # same bundle, not in scope
        assert ok.status_code == 201, ok.text
        assert other.status_code == 403, other.text
        detail = other.json()["detail"]
        assert detail["error"] == "dependency_grant_scope"
        assert detail["granted_apps"] == ["secrets", "skill-builder", "workers"]
        assert _tokens(world) == before + 1

    def test_fr_mints_secrets_for_a_tenant_it_does_not_own(self, world, platform_tenant):
        """065 (George 2026-10-16): fr proves the tenant to secrets with a minted
        app=secrets licence — the conveyor MEMBER licence, through the scoped grant."""
        secrets = _licences(world, platform_tenant)["secrets"]
        assert [(d, b) for _, d, b in secrets] == [(False, "conveyor")]
        with _minter(world, FR_MINTER) as c:
            resp = _mint(c, platform_tenant, "secrets")
        assert resp.status_code == 201, resp.text
        assert resp.json()["license_id"] == secrets[0][0]
        assert resp.json()["tenant_id"] == platform_tenant

    def test_fr_mints_skill_builder_for_a_tenant_it_does_not_own(self, world, platform_tenant):
        """066 (George 2026-10-16, fr P1.5): fr mints app=skill-builder for a conveyor
        tenant it does not own — the conveyor MEMBER licence, through the scoped grant."""
        sb = _licences(world, platform_tenant)["skill-builder"]
        assert [(d, b) for _, d, b in sb] == [(False, "conveyor")]
        with _minter(world, FR_MINTER) as c:
            resp = _mint(c, platform_tenant, "skill-builder")
        assert resp.status_code == 201, resp.text
        assert resp.json()["license_id"] == sb[0][0]
        assert resp.json()["tenant_id"] == platform_tenant

    def test_scoped_grant_cannot_read_the_tenant(self, world, platform_tenant):
        assert not world.owner_can_access_tenant("fr", platform_tenant)
        main.db_manager = world
        user = KeycloakUser(
            sub="service-account-fr-backend",
            preferred_username="fr-backend",
            realm_access={"roles": ["license-admin"]},
            resource_access={},
        )
        with TestClient(main.app, raise_server_exceptions=False) as c:
            c.headers["Authorization"] = f"Bearer {_make_mock_jwt(user, azp='fr-backend')}"
            resp = c.get(f"/tenants/{platform_tenant}")
        assert resp.status_code == 404, resp.text

    def test_no_grant_at_all_is_still_404(self, world, platform_tenant):
        with _minter(world, W2V_MINTER) as c:
            resp = _mint(c, platform_tenant, "workers")
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"]["reason"] == "tenant_not_found"

    def test_a_broad_grant_still_mints_any_bundle_app(self, world, platform_tenant):
        world.put_tenant_grant(platform_tenant, "fr", "test")
        with _minter(world, FR_MINTER) as c:
            for app in ("workers", "coding", "inference-router"):
                resp = _mint(c, platform_tenant, app)
                assert resp.status_code == 201, (app, resp.text)
