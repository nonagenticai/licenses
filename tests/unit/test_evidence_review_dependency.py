"""Evidence Review as a dependency of kag (migration 062): kag -> evidence-review, caller kag.

kag-frontend (secure-apps/kag-frontend-bundle-minter, owner kag, 060) calls Evidence
Review directly with the tenant's OWN evidence-review licence, minted through the REAL
POST /internal/tenants/{t}/bundle-token?app=evidence-review route.

World: the 060 test world (059's catalogue slice + kag-research's members) plus the
``evidence-review`` app carrying its 062 issuers (models.APP_ISSUER_OVERRIDES), with the
seeded edges (models.APP_REQUIRES_SEED — the 062 edge included).

WHAT IS PINNED.
* ensuring ``kag-research`` on a kag tenant inserts ONE evidence-review DEPENDENCY
  licence (is_dependency, no bundle); no grant is written (kag owns the tenant);
* the kag-frontend minter mints app=evidence-review there: 201, on that licence, and the
  token introspects ACTIVE with app key ``evidence-review``;
* a non-kag tenant kag holds no grant on is 404 ``tenant_not_found``, nothing minted;
* a kag tenant that never ensured a bundle with kag in its closure has no ER licence and
  the mint is 404 (no bundle) — the licence comes ONLY from the closure;
* the scope note: ensuring ``conveyor`` on an fr tenant now also grants (kag,
  evidence-review), and nothing beyond 059's kag grants plus that one; the kag-frontend
  minter mints evidence-review there (201) but any app OUTSIDE its dependency grant —
  e.g. conveyor's members ``coding`` / ``kag`` — is 403 ``dependency_grant_scope``
  (reason ``app_outside_dependency_grant``), nothing minted.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from models import APP_REQUIRES_SEED, DatabaseManager, issuers_for_app_key
from tests.unit.test_dependency_licences import (
    _dep_grants,
    _ensure,
    _licences,
    _mint,
    _minter,
    _tokens,
)
from tests.unit.test_kag_frontend_bundle_minter import APPS as M060_APPS
from tests.unit.test_kag_frontend_bundle_minter import BUNDLES as M060_BUNDLES
from tests.unit.test_kag_frontend_bundle_minter import _seed as _seed_060

KAG_FE_MINTER = "kag-frontend-bundle-minter"
ER = "evidence-review"


def _seed(db: DatabaseManager) -> None:
    # The ER app first, so _seed_060's APP_REQUIRES_SEED loop finds both ends of 062's edge.
    app = db.create_app(key=ER, name="Evidence Review", issuers=issuers_for_app_key(ER))
    db.create_plan(app_id=UUID(str(app.id)), code="standard", name="Standard")
    _seed_060(db)


@pytest.fixture
def world(db_manager) -> DatabaseManager:
    _seed(db_manager)
    return db_manager


@pytest.fixture
def kag_tenant(world) -> str:
    tid = str(world.create_tenant(name="kag-er-customer", owner_key="kag").id)
    ep = _ensure(world, tid, "kag-research")
    assert not ep.refused, ep.refused
    return tid


def test_the_edge_and_issuers_are_seeded():
    assert ("kag", ER, "kag") in APP_REQUIRES_SEED
    assert set(issuers_for_app_key(ER)) == {"kag", "platform"}
    assert ER not in M060_APPS  # control: the 060 world alone has no ER app


class TestEnsureOnAKagTenant:
    def test_creates_the_evidence_review_dependency_licence(self, world, kag_tenant):
        lic = _licences(world, kag_tenant)
        assert [(d, b) for _, d, b in lic[ER]] == [(True, None)]
        assert set(lic) == set(M060_BUNDLES["kag-research"]) | {
            "workers",
            "mlx-backend",
            "mlx-ingestion-backend",
            ER,
        }
        assert not {g for g in _dep_grants(world, kag_tenant) if g[0] == "kag"}

    def test_a_second_ensure_writes_nothing(self, world, kag_tenant):
        before = _licences(world, kag_tenant)
        ep = _ensure(world, kag_tenant, "kag-research")
        assert not ep.refused
        assert _licences(world, kag_tenant) == before


class TestMint:
    def test_kag_frontend_mints_evidence_review_and_it_introspects_as_er(self, world, kag_tenant):
        expected = _licences(world, kag_tenant)[ER][0][0]
        before = _tokens(world)
        with _minter(world, KAG_FE_MINTER) as c:
            resp = _mint(c, kag_tenant, ER)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["license_id"] == expected
        assert body["tenant_id"] == kag_tenant
        assert body["app"] == ER
        assert _tokens(world) == before + 1
        info = world.introspect_token(body["token"])
        assert info is not None and info["active"] is True, info
        assert info["app"]["key"] == ER

    @pytest.mark.parametrize("owner", ["fr", "commonpractice"])
    def test_a_non_kag_tenant_is_refused(self, world, owner):
        tid = str(world.create_tenant(name=f"{owner}-er-customer", owner_key=owner).id)
        assert not _ensure(world, tid, "inference-router").refused
        before = _tokens(world)
        with _minter(world, KAG_FE_MINTER) as c:
            resp = _mint(c, tid, ER)
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"]["reason"] == "tenant_not_found"
        assert _tokens(world) == before

    def test_a_kag_tenant_without_kag_in_its_closure_has_no_er(self, world):
        tid = str(world.create_tenant(name="kag-ir-only", owner_key="kag").id)
        assert not _ensure(world, tid, "inference-router").refused
        assert ER not in _licences(world, tid)
        with _minter(world, KAG_FE_MINTER) as c:
            resp = _mint(c, tid, ER)
        assert resp.status_code == 404, resp.text


class TestScopeNote:
    def test_conveyor_on_an_fr_tenant_adds_only_kag_evidence_review(self, world):
        tid = str(world.create_tenant(name="fr-conveyor-er", owner_key="fr").id)
        assert not _ensure(world, tid, "conveyor").refused
        kag_grants = {a for o, a in _dep_grants(world, tid) if o == "kag"}
        assert kag_grants == {"workers", "mlx-backend", "mlx-ingestion-backend", ER}
        assert [(d, b) for _, d, b in _licences(world, tid)[ER]] == [(True, None)]

    def test_kag_frontend_on_that_fr_tenant_mints_er_but_nothing_outside_its_grant(self, world):
        tid = str(world.create_tenant(name="fr-conveyor-er-mint", owner_key="fr").id)
        assert not _ensure(world, tid, "conveyor").refused
        with _minter(world, KAG_FE_MINTER) as c:
            ok = _mint(c, tid, ER)  # control: the grant really admits ER here
            assert ok.status_code == 201, ok.text
            assert ok.json()["app"] == ER
            before = _tokens(world)
            for app in ("coding", "kag"):  # conveyor MEMBERS, not kag's dependency grant
                resp = _mint(c, tid, app)
                assert resp.status_code == 403, (app, resp.text)
                detail = resp.json()["detail"]
                assert detail["error"] == "dependency_grant_scope", detail
                assert detail["reason"] == "app_outside_dependency_grant", detail
                assert ER in detail["granted_apps"] and app not in detail["granted_apps"]
        assert _tokens(world) == before
