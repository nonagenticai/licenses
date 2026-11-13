"""kag-frontend's bundle-token minter (migration 060): secure-apps/kag-frontend-bundle-minter
-> owner kag, minting per-tenant tokens through the REAL
POST /internal/tenants/{t}/bundle-token route (signed secure-apps JWT whose azp is the
060 ``api_client`` row, holding only ``bundle-token-minter``).

World: the 059 catalogue slice (tests/unit/test_dependency_licences.py) plus kag-research's
other members (kag-graph, kag-orchestrator; 057) and the single-app ``inference-router``
bundle; the seeded edges (models.APP_REQUIRES_SEED) and bundle callers.

WHAT IS PINNED.
* on a kag-owned tenant with ``kag-research`` ensured, the minter mints ``workers`` (a
  DEPENDENCY licence reached via kag -> workers) and ``kag`` (a member), each on the
  tenant's one licence for that app;
* an fr- or commonpractice-owned tenant kag holds no grant on is 404
  ``tenant_not_found`` for every app, nothing minted (an fr minter mints there: control);
* an app outside kag-research's closure (``w2v``, ``coding``) is 404 ``app_not_in_bundle``;
* the reach 059 already gave owner kag: ensuring ``conveyor`` on an fr tenant writes the
  app-scoped grants (kag, workers / mlx-backend / mlx-ingestion-backend), so this minter
  mints ``workers`` there and no app outside them (``kag`` is 403
  ``dependency_grant_scope``).
"""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import text

from models import API_CLIENTS, APP_REQUIRES_SEED, BUNDLE_CALLER_SEED, DatabaseManager
from tests.unit.test_dependency_licences import (
    APPS as DEP_APPS,
)
from tests.unit.test_dependency_licences import (
    BUNDLES as DEP_BUNDLES,
)
from tests.unit.test_dependency_licences import (
    FR_MINTER,
    _dep_grants,
    _ensure,
    _licences,
    _mint,
    _minter,
    _tokens,
)

KAG_FE_MINTER = "kag-frontend-bundle-minter"  # -> kag (060)

# 057's kag-research members carry kag as an issuer (ADD only).
APPS: dict[str, tuple[str, ...]] = {
    **DEP_APPS,
    "kag-graph": ("platform", "fr", "kag"),
    "kag-orchestrator": ("platform", "fr", "kag"),
}
BUNDLES: dict[str, tuple[str, ...]] = {
    **DEP_BUNDLES,
    "kag-research": ("kag", "kag-graph", "kag-orchestrator", "inference-router"),
    "inference-router": ("inference-router",),
}
# kag-research's dependency closure: kag -> workers / mlx-*; inference-router is a member.
KAG_RESEARCH_DEPS = {"workers", "mlx-backend", "mlx-ingestion-backend"}


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


@pytest.fixture
def world(db_manager) -> DatabaseManager:
    _seed(db_manager)
    return db_manager


@pytest.fixture
def kag_tenant(world) -> str:
    tid = str(world.create_tenant(name="kag-research-customer", owner_key="kag").id)
    ep = _ensure(world, tid, "kag-research")
    assert not ep.refused, ep.refused
    return tid


def test_the_minter_is_registered_to_kag_and_not_cross_owner():
    assert ("secure-apps", KAG_FE_MINTER, "kag", False) in API_CLIENTS
    assert [c for c in API_CLIENTS if c[1] == KAG_FE_MINTER] == [
        ("secure-apps", KAG_FE_MINTER, "kag", False)
    ]


class TestMintsOnKagTenants:
    def test_kag_research_ensure_gives_workers_as_a_dependency_licence(self, world, kag_tenant):
        lic = _licences(world, kag_tenant)
        assert set(lic) == set(BUNDLES["kag-research"]) | KAG_RESEARCH_DEPS
        assert [(d, b) for _, d, b in lic["workers"]] == [(True, None)]
        assert [(d, b) for _, d, b in lic["kag"]] == [(False, "kag-research")]
        # kag OWNS the tenant: no grant is written for it (059: never the owner).
        assert not {g for g in _dep_grants(world, kag_tenant) if g[0] == "kag"}

    @pytest.mark.parametrize("app", ["workers", "kag"])
    def test_mints_workers_and_kag_on_the_tenants_one_licence(self, world, kag_tenant, app):
        expected = _licences(world, kag_tenant)[app][0][0]
        before = _tokens(world)
        with _minter(world, KAG_FE_MINTER) as c:
            resp = _mint(c, kag_tenant, app)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["license_id"] == expected
        assert body["tenant_id"] == kag_tenant
        assert body["app"] == app
        assert body["bundle"] == "kag-research"
        assert _tokens(world) == before + 1

    @pytest.mark.parametrize("app", ["w2v", "coding"])
    def test_an_app_outside_the_kag_research_closure_is_refused(self, world, kag_tenant, app):
        before = _tokens(world)
        with _minter(world, KAG_FE_MINTER) as c:
            resp = _mint(c, kag_tenant, app)
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"]["error"] == "app_not_in_bundle"
        assert _tokens(world) == before


class TestOtherOwnersTenants:
    @pytest.mark.parametrize("owner", ["fr", "commonpractice"])
    def test_a_tenant_kag_holds_no_grant_on_is_404_for_every_app(self, world, owner):
        tid = str(world.create_tenant(name=f"{owner}-customer", owner_key=owner).id)
        assert not _ensure(world, tid, "inference-router").refused
        assert not {g for g in _dep_grants(world, tid) if g[0] == "kag"}
        before = _tokens(world)
        with _minter(world, KAG_FE_MINTER) as c:
            for app in ("inference-router", "workers", "kag"):
                resp = _mint(c, tid, app)
                assert resp.status_code == 404, (app, resp.text)
                assert resp.json()["detail"]["reason"] == "tenant_not_found", app
        assert _tokens(world) == before
        if owner == "fr":  # control: the tenant's own owner mints there
            with _minter(world, FR_MINTER) as c:
                ok = _mint(c, tid, "inference-router")
            assert ok.status_code == 201, ok.text

    def test_059s_kag_dependency_grants_admit_only_their_apps(self, world):
        """Not a widening by 060: ensuring conveyor on an fr tenant grants owner kag the
        app-scoped (kag, workers / mlx-*) of its kag -> * edges, so every kag-owned
        minter mints `workers` there — and no app outside those grants."""
        tid = str(world.create_tenant(name="fr-conveyor-customer", owner_key="fr").id)
        assert not _ensure(world, tid, "conveyor").refused
        assert ("kag", "workers") in _dep_grants(world, tid)
        with _minter(world, KAG_FE_MINTER) as c:
            ok = _mint(c, tid, "workers")
            other = _mint(c, tid, "kag")
        assert ok.status_code == 201, ok.text
        assert other.status_code == 403, other.text
        assert other.json()["detail"]["error"] == "dependency_grant_scope"
        assert other.json()["detail"]["granted_apps"] == [
            "mlx-backend",
            "mlx-ingestion-backend",
            "workers",
        ]
