"""The ``kag-service`` bundle (migration 063) = {kag}: ensure on kag's service tenant.

World: kag_bundle_minter's slice (059's catalogue + kag-research + the single-app
``inference-router`` bundle) plus ``kag-service`` and the ``evidence-review`` app (062,
with its models.APP_ISSUER_OVERRIDES issuers), with the seeded edges
(models.APP_REQUIRES_SEED — 062's kag -> evidence-review included). Ensure runs through ``services.bundle_ensure`` (plan +
apply, metering stubbed); minting runs through the REAL bundle-token route as
secure-apps/kag-bundle-minter (061).

WHAT IS PINNED (the svc-kag case, a PLATFORM-owned tenant that already holds its
inference-router licence in the ``inference-router`` bundle):
* control: ``kag-research`` there is refused ``other_bundle`` on inference-router —
  the refusal that motivated 063;
* ``kag-service`` there is NOT refused: kag is a member licence (bundle kag-service);
  workers / mlx-backend / mlx-ingestion-backend / evidence-review are DEPENDENCY
  licences (is_dependency, no bundle); inference-router is SHARED (``exists``), still
  one licence, still in its own bundle;
* grants: the APP-SCOPED (kag, workers / mlx-backend / mlx-ingestion-backend /
  evidence-review); none for platform (it OWNS the tenant); no broad grant;
* kag-bundle-minter mints those four dependency apps on the tenant (201) and nothing
  outside its grants (inference-router: 403 ``dependency_grant_scope``);
* a second ensure writes nothing; on a kag-owned tenant kag-service yields the same
  licences and NO kag grant (never the owner).
"""

from __future__ import annotations

from uuid import UUID

import pytest

from models import DatabaseManager, issuers_for_app_key
from services import bundle_ensure
from tests.unit.test_dependency_licences import (
    _dep_grants,
    _ensure,
    _grantees,
    _licences,
    _mint,
    _minter,
    _tokens,
)
from tests.unit.test_kag_bundle_minter import BUNDLES as KAG_MINTER_BUNDLES
from tests.unit.test_kag_bundle_minter import KAG_MINTER
from tests.unit.test_kag_bundle_minter import _seed as _seed_kag_minter_world

ER = "evidence-review"
# kag's direct app_requires (caller kag): 059's three plus 062's evidence-review.
KAG_DIRECT_DEPS = {"workers", "mlx-backend", "mlx-ingestion-backend", ER}
# ... plus workers -> inference-router (caller platform), transitive.
KAG_SERVICE_DEPS = KAG_DIRECT_DEPS | {"inference-router"}


@pytest.fixture
def world(db_manager, monkeypatch) -> DatabaseManager:
    monkeypatch.setattr(
        "tests.unit.test_kag_bundle_minter.BUNDLES",
        {**KAG_MINTER_BUNDLES, "kag-service": ("kag",)},
    )
    # The ER app first, so the seed's APP_REQUIRES_SEED loop finds both ends of 062's edge.
    app = db_manager.create_app(key=ER, name="Evidence Review", issuers=issuers_for_app_key(ER))
    db_manager.create_plan(app_id=UUID(str(app.id)), code="standard", name="Standard")
    _seed_kag_minter_world(db_manager)
    return db_manager


@pytest.fixture
def svc_kag(world) -> str:
    """A platform-owned service tenant already holding inference-router in its bundle."""
    tid = str(world.create_tenant(name="svc-kag", owner_key="platform").id)
    ep = _ensure(world, tid, "inference-router")
    assert not ep.refused, ep.refused
    return tid


class TestSvcKagTenant:
    def test_control_kag_research_is_refused_other_bundle(self, world, svc_kag):
        ep = bundle_ensure.plan(world, svc_kag, "kag-research", ["tenant-pooled", "standard"])
        assert {(p.app_key, p.refuse_reason) for p in ep.refused} == {
            ("inference-router", bundle_ensure.REFUSE_OTHER_BUNDLE)
        }

    def test_kag_service_yields_kag_plus_its_closure(self, world, svc_kag):
        ir_before = _licences(world, svc_kag)["inference-router"]
        ep = _ensure(world, svc_kag, "kag-service")
        assert not ep.refused, [(p.app_key, p.refuse_reason) for p in ep.refused]
        assert {p.app_key for p in ep.apps if not p.dependency} == {"kag"}
        assert {p.app_key for p in ep.apps if p.dependency} == KAG_SERVICE_DEPS
        assert {p.app_key: p.action for p in ep.apps if p.dependency}["inference-router"] == (
            "exists"
        )

        lic = _licences(world, svc_kag)
        assert set(lic) == {"kag"} | KAG_SERVICE_DEPS
        assert [(d, b) for _, d, b in lic["kag"]] == [(False, "kag-service")]
        for app in KAG_DIRECT_DEPS:
            assert [(d, b) for _, d, b in lic[app]] == [(True, None)], app
        # Shared, not duplicated: the tenant's one inference-router licence, untouched.
        assert lic["inference-router"] == ir_before
        assert [(d, b) for _, d, b in lic["inference-router"]] == [(False, "inference-router")]

    def test_grants_are_app_scoped_kag_only_never_the_owner(self, world, svc_kag):
        _ensure(world, svc_kag, "kag-service")
        assert _dep_grants(world, svc_kag) == {("kag", app) for app in KAG_DIRECT_DEPS}
        assert _grantees(world, svc_kag) == set()

    def test_kag_minter_mints_the_dependencies_and_nothing_outside_its_grants(self, world, svc_kag):
        _ensure(world, svc_kag, "kag-service")
        lic = _licences(world, svc_kag)
        before = _tokens(world)
        with _minter(world, KAG_MINTER) as c:
            oks = {app: _mint(c, svc_kag, app) for app in sorted(KAG_DIRECT_DEPS)}
            other = _mint(c, svc_kag, "inference-router")
        for app, ok in oks.items():
            assert ok.status_code == 201, (app, ok.text)
            assert ok.json()["license_id"] == lic[app][0][0]
        assert other.status_code == 403, other.text
        assert other.json()["detail"]["error"] == "dependency_grant_scope"
        assert _tokens(world) == before + len(KAG_DIRECT_DEPS)

    def test_a_second_ensure_writes_nothing(self, world, svc_kag):
        _ensure(world, svc_kag, "kag-service")
        before = (_licences(world, svc_kag), _dep_grants(world, svc_kag))
        ep = _ensure(world, svc_kag, "kag-service")
        assert {p.action for p in ep.apps} == {"exists"}
        assert (_licences(world, svc_kag), _dep_grants(world, svc_kag)) == before


class TestKagOwnedTenant:
    def test_same_licences_and_no_grant_for_the_owner(self, world):
        tid = str(world.create_tenant(name="kag-customer", owner_key="kag").id)
        ep = _ensure(world, tid, "kag-service")
        assert not ep.refused
        lic = _licences(world, tid)
        assert set(lic) == {"kag"} | KAG_SERVICE_DEPS
        assert [(d, b) for _, d, b in lic["kag"]] == [(False, "kag-service")]
        for app in KAG_SERVICE_DEPS:
            assert [(d, b) for _, d, b in lic[app]] == [(True, None)], app
        # kag owns the tenant (no kag grant); platform's workers -> inference-router edge.
        assert _dep_grants(world, tid) == {("platform", "inference-router")}
