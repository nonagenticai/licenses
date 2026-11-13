"""``POST /internal/tenants/{id}/bundle-token`` is OWNER-SCOPED (migration 054).

THE HOLE. The route checked only the realm role ``bundle-token-minter`` and loaded the
tenant unscoped, so any holder minted a live bearer on ANY tenant's bundle licence.

WHAT IS PINNED. Every caller here is a REAL minter client (signed (secure-apps, azp)
that is a row of ``api_client``) holding ONLY ``bundle-token-minter`` — never the
conftest's break-glass default, which would make these pass vacuously.

* the tenant's owner mints -> 201;
* an owner holding an ACTIVE grant mints -> 201, also for an app it does NOT issue
  (sales -> ``w2v`` on CommonPractice's netcare is a live call);
* another owner's tenant -> 404 ``no_bundle`` / ``tenant_not_found`` (exactly like an
  absent tenant), nothing minted, with or without the role; a REVOKED grant likewise;
* an unregistered client -> 403 ``unknown_api_client``, nothing minted;
* break-glass (``licenses-admin``) -> 201, audited as ``cross_owner_action``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import main
from auth.models import KeycloakUser
from models import DatabaseManager
from tests.conftest import _make_mock_jwt
from tests.unit.test_bundle_licence import BUNDLE_KEY, _seed_bundle, provision_tenant_bundle

MARKETING = "marketing-bundle-minter"  # -> merqi
SALES = "sales-bundle-minter"  # -> merqi
WM = "worker-management-bundle-minter"  # -> platform
BREAK_GLASS = "licenses-admin"  # -> platform, cross_owner
UNKNOWN = "some-unregistered-minter"

APP = "kanban-t"  # issued by 'platform' only (_seed_bundle): merqi does NOT issue it


@contextmanager
def _as(db: DatabaseManager, azp: str) -> Iterator[TestClient]:
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


def _tenant_with_bundle(db: DatabaseManager, owner: str, name: str) -> str:
    tid = str(db.create_tenant(name=name, owner_key=owner).id)
    assert (
        provision_tenant_bundle.main(["--tenant", tid, "--bundle", BUNDLE_KEY, "--apply"], db=db)
        == 0
    )
    return tid


def _tokens(db: DatabaseManager) -> int:
    with db.engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM token")).scalar_one()


def _mint(client: TestClient, tenant: str):
    return client.post(f"/internal/tenants/{tenant}/bundle-token?app={APP}")


@pytest.fixture
def tenants(db_manager) -> dict[str, str]:
    _seed_bundle(db_manager)
    return {
        "merqi": _tenant_with_bundle(db_manager, "merqi", "merqi-own"),
        "cp": _tenant_with_bundle(db_manager, "commonpractice", "cp-customer"),
        "fr": _tenant_with_bundle(db_manager, "fr", "fr-customer"),
        "platform": _tenant_with_bundle(db_manager, "platform", "platform-internal"),
    }


def _assert_hidden(resp, db, before: int) -> None:
    assert resp.status_code == 404, resp.text
    detail = resp.json()["detail"]
    assert (detail["error"], detail["reason"]) == ("no_bundle", "tenant_not_found"), detail
    assert _tokens(db) == before, "a refused mint must write no token"


class TestOwnerScope:
    def test_owner_mints_on_its_own_tenant(self, db_manager, tenants):
        with _as(db_manager, MARKETING) as c:
            resp = _mint(c, tenants["merqi"])
        assert resp.status_code == 201, resp.text
        assert resp.json()["tenant_id"] == tenants["merqi"]

    def test_grantee_mints_an_app_it_does_not_issue(self, db_manager, tenants):
        db_manager.put_tenant_grant(tenants["cp"], "merqi", "test")
        before = _tokens(db_manager)
        with _as(db_manager, SALES) as c:
            resp = _mint(c, tenants["cp"])
        assert resp.status_code == 201, resp.text
        assert _tokens(db_manager) == before + 1

    def test_another_owners_tenant_is_404_and_mints_nothing(self, db_manager, tenants):
        before = _tokens(db_manager)
        with _as(db_manager, MARKETING) as c:
            _assert_hidden(_mint(c, tenants["fr"]), db_manager, before)
            _assert_hidden(_mint(c, tenants["cp"]), db_manager, before)
            _assert_hidden(_mint(c, tenants["platform"]), db_manager, before)

    def test_looks_exactly_like_an_absent_tenant(self, db_manager, tenants):
        before = _tokens(db_manager)
        with _as(db_manager, MARKETING) as c:
            foreign = _mint(c, tenants["fr"])
            absent = _mint(c, "01000000-0000-7000-8000-00000000dead")
        _assert_hidden(foreign, db_manager, before)
        assert foreign.json()["detail"]["reason"] == absent.json()["detail"]["reason"]
        assert foreign.json()["detail"]["error"] == absent.json()["detail"]["error"]

    def test_a_revoked_grant_no_longer_admits(self, db_manager, tenants):
        db_manager.put_tenant_grant(tenants["cp"], "merqi", "test")
        assert db_manager.revoke_tenant_grant(tenants["cp"], "merqi")
        before = _tokens(db_manager)
        with _as(db_manager, SALES) as c:
            _assert_hidden(_mint(c, tenants["cp"]), db_manager, before)

    def test_platform_reaches_other_owners_through_its_infrastructure_grant(
        self, db_manager, tenants
    ):
        """worker_management mints inference-router on every tenant: platform holds a
        grant on every non-platform tenant (written with the tenant, 050 rule 1)."""
        with _as(db_manager, WM) as c:
            for key in ("platform", "fr", "cp", "merqi"):
                resp = _mint(c, tenants[key])
                assert resp.status_code == 201, (key, resp.text)

    def test_unregistered_client_is_403_and_mints_nothing(self, db_manager, tenants):
        before = _tokens(db_manager)
        with _as(db_manager, UNKNOWN) as c:
            resp = _mint(c, tenants["merqi"])
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error"] == "unknown_api_client"
        assert _tokens(db_manager) == before

    def test_break_glass_mints_anywhere_and_is_audited(self, db_manager, tenants, caplog):
        with caplog.at_level(logging.WARNING, logger="main"), _as(db_manager, BREAK_GLASS) as c:
            resp = _mint(c, tenants["fr"])
        assert resp.status_code == 201, resp.text
        audit = [r.getMessage() for r in caplog.records if "cross_owner_action" in r.getMessage()]
        assert any(
            "action=bundle_token_mint" in m and f"tenant={tenants['fr']}" in m for m in audit
        ), audit
