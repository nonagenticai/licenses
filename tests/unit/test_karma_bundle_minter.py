"""karma's OWN outbound bundle-token minter (migration 064): secure-apps/karma-bundle-minter
-> owner kag, minting the END tenant's per-app token for karma.karma's outbound hops through
the REAL POST /internal/tenants/{t}/bundle-token route (signed secure-apps JWT whose azp is
the 064 ``api_client`` row, holding only ``bundle-token-minter``).

World: the same one tests/unit/test_kag_bundle_minter.py builds (059 catalogue slice plus
kag-research's members and the single-app ``inference-router`` bundle).

WHAT IS PINNED.
* it is registered to kag, not cross-owner, and is a DISTINCT client from kag's (061) and
  kag-frontend's (060) minters — one Keycloak client and secret per service;
* on a kag-owned tenant with ``kag-research`` ensured it mints ``workers`` (a dependency
  licence via kag -> workers) and ``inference-router`` (a member), each on the tenant's one
  licence for that app;
* an fr-owned tenant kag holds no grant on is 404 ``tenant_not_found``, nothing minted.
"""

from __future__ import annotations

import pytest

from models import API_CLIENTS, DatabaseManager
from tests.unit.test_dependency_licences import _ensure, _licences, _mint, _minter, _tokens
from tests.unit.test_kag_bundle_minter import KAG_FE_MINTER, KAG_MINTER, _seed

KARMA_MINTER = "karma-bundle-minter"  # -> kag (064)
KARMA_APPS = ["inference-router", "workers"]


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
    assert [c for c in API_CLIENTS if c[1] == KARMA_MINTER] == [
        ("secure-apps", KARMA_MINTER, "kag", False)
    ]
    # one client per service: never kag's or kag-frontend's client reused
    assert KARMA_MINTER not in (KAG_MINTER, KAG_FE_MINTER)
    assert ("secure-apps", KAG_MINTER, "kag", False) in API_CLIENTS
    assert ("secure-apps", KAG_FE_MINTER, "kag", False) in API_CLIENTS


@pytest.mark.parametrize("app", KARMA_APPS)
def test_mints_on_a_kag_tenant_on_the_tenants_one_licence(world, kag_tenant, app):
    expected = _licences(world, kag_tenant)[app][0][0]
    before = _tokens(world)
    with _minter(world, KARMA_MINTER) as c:
        resp = _mint(c, kag_tenant, app)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["license_id"] == expected
    assert body["tenant_id"] == kag_tenant
    assert body["app"] == app
    assert _tokens(world) == before + 1


def test_a_tenant_kag_holds_no_grant_on_is_404(world):
    tid = str(world.create_tenant(name="fr-customer", owner_key="fr").id)
    assert not _ensure(world, tid, "inference-router").refused
    before = _tokens(world)
    with _minter(world, KARMA_MINTER) as c:
        for app in KARMA_APPS:
            resp = _mint(c, tid, app)
            assert resp.status_code == 404, (app, resp.text)
            assert resp.json()["detail"]["reason"] == "tenant_not_found", app
    assert _tokens(world) == before
