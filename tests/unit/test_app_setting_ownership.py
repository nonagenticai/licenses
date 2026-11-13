"""Per-APP tenant settings are gated on the APP, not only on reaching the tenant.

``PUT/DELETE /tenants/{t}/apps/{app}/byo-mode`` and ``POST/DELETE
/tenants/{t}/offerings/{app}/{code}`` used to admit the tenant's OWNER for ANY
app. Owning the tenant is not owning the app: the owner must now ISSUE the app
(``app_issuer``) or have brought it onto that tenant through its own bundle (a
licence there with ``bundle_id`` set) — else 403 ``app_not_administrable_by_caller``
with the audited cross_owner break-glass. Grantees keep the issuer rule
(``TestPerAppSettings`` in test_tenant_ownership.py). Every test signs as a REAL
owning client, never the conftest's break-glass default.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable

from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import text

from tests.unit.test_bundle_ensure_route import fake  # noqa: F401 — fixture
from tests.unit.test_bundle_owner_revoke import _licence, _setup
from tests.unit.test_tenant_ownership import BREAK_GLASS, CP, FR, _app, _as, _err, _tenant

DENIED = "app_not_administrable_by_caller"
BYO = {"byo_mode": "platform_only"}


def _calls(tid: str, app_key: str) -> dict[str, Callable[[TestClient], Response]]:
    """The four per-app writes. ``provision`` names an offering that does not
    exist, so an ADMITTED call answers 404 "Offering not found" without any
    billing hop, while a refused one is the 403 from the resolver that runs first."""
    return {
        "put": lambda c: c.put(f"/tenants/{tid}/apps/{app_key}/byo-mode", json=BYO),
        "clear": lambda c: c.delete(f"/tenants/{tid}/apps/{app_key}/byo-mode"),
        "provision": lambda c: c.post(f"/tenants/{tid}/offerings/{app_key}/no-such-offering"),
        "unprovision": lambda c: c.delete(f"/tenants/{tid}/offerings/{app_key}/standard"),
    }


ADMITTED = {"put": 200, "clear": 200, "provision": 404, "unprovision": 200}


def _run(db, azp: str, tid: str, app_key: str) -> dict[str, Response]:
    with _as(db, azp) as client:
        return {name: call(client) for name, call in _calls(tid, app_key).items()}


class TestOwnerMustIssueTheApp:
    def test_owner_non_issuer_is_refused_on_all_four_and_nothing_changes(self, db_manager):
        _app(db_manager, "w2v", ("w2v",))
        tid = _tenant(db_manager, "fr", f"fr-{uuid.uuid4().hex[:6]}")
        db_manager.set_tenant_app_byo_mode(tid, "w2v", "byo_required")
        for name, resp in _run(db_manager, FR, tid, "w2v").items():
            assert resp.status_code == 403, (name, resp.text)
            assert _err(resp) == DENIED, name
        row = db_manager.get_tenant_app_byo_mode(tid, "w2v")
        assert row is not None and row["byo_mode"] == "byo_required", row

    def test_owner_issuer_is_admitted_on_all_four(self, db_manager):
        _app(db_manager, "kag", ("fr", "platform"))
        tid = _tenant(db_manager, "fr", f"fr-{uuid.uuid4().hex[:6]}")
        for name, resp in _run(db_manager, FR, tid, "kag").items():
            assert resp.status_code == ADMITTED[name], (name, resp.text)

    def test_unknown_app_on_own_tenant_keeps_its_404(self, db_manager):
        tid = _tenant(db_manager, "fr", f"fr-{uuid.uuid4().hex[:6]}")
        with _as(db_manager, FR) as fr:
            resp = fr.put(f"/tenants/{tid}/apps/no-such-app/byo-mode", json=BYO)
        assert resp.status_code == 404, resp.text

    def test_break_glass_owner_non_issuer_is_admitted_and_audited(self, db_manager, caplog):
        app_id, _plan = _app(db_manager, "w2v", ("w2v",))
        tid = _tenant(db_manager, "platform", f"pf-{uuid.uuid4().hex[:6]}")
        with caplog.at_level(logging.WARNING), _as(db_manager, BREAK_GLASS) as admin:
            resp = admin.put(f"/tenants/{tid}/apps/w2v/byo-mode", json=BYO)
        assert resp.status_code == 200, resp.text
        audit = [r.getMessage() for r in caplog.records if "cross_owner_action" in r.getMessage()]
        assert any("action=tenant_app_byo_mode" in m and f"app_id={app_id}" in m for m in audit), (
            audit
        )


class TestOwnerBundleApp:
    """CP owns the tenant and ensured the ``n8n-nodes`` bundle that brought the
    platform-issued ``workers`` app onto it: CP manages that app's per-app
    posture there even though it does not issue it."""

    def test_bundle_minted_licence_admits_the_owner_on_all_four(self, db_manager, fake):  # noqa: F811
        tid, app_id, plan_id, bundle_id = _setup(db_manager, f"cp-{uuid.uuid4().hex[:6]}")
        _licence(db_manager, tid, app_id, plan_id, bundle_id)
        for name, resp in _run(db_manager, CP, tid, "workers").items():
            assert resp.status_code == ADMITTED[name], (name, resp.text)

    def test_a_non_bundle_licence_does_not_admit(self, db_manager, fake):  # noqa: F811
        """The discriminator is ``bundle_id``: the same licence without it refuses."""
        tid, app_id, plan_id, _bundle_id = _setup(db_manager, f"cp-{uuid.uuid4().hex[:6]}")
        _licence(db_manager, tid, app_id, plan_id, None)
        for name, resp in _run(db_manager, CP, tid, "workers").items():
            assert resp.status_code == 403, (name, resp.text)
            assert _err(resp) == DENIED, name
        assert db_manager.get_tenant_app_byo_mode(tid, "workers") is None

    def test_a_revoked_bundle_licence_admits_only_the_clearing_calls(self, db_manager, fake):  # noqa: F811
        """After offboarding revoked the bundle licence the owner may still clear /
        unprovision, but may not set BYO or provision (which writes billing markup)."""
        tid, app_id, plan_id, bundle_id = _setup(db_manager, f"cp-{uuid.uuid4().hex[:6]}")
        lic = _licence(db_manager, tid, app_id, plan_id, bundle_id)
        with _as(db_manager, BREAK_GLASS, tenant=tid) as admin:
            assert admin.post("/license/revoke", json={"license_id": lic}).status_code == 200
        results = _run(db_manager, CP, tid, "workers")
        for name in ("put", "provision"):
            assert results[name].status_code == 403, (name, results[name].text)
            assert _err(results[name]) == DENIED, name
        for name in ("clear", "unprovision"):
            assert results[name].status_code == ADMITTED[name], (name, results[name].text)
        assert db_manager.get_tenant_app_byo_mode(tid, "workers") is None

    def test_an_expired_bundle_licence_still_reading_active_admits_only_clearing(
        self,
        db_manager,
        fake,  # noqa: F811
    ):
        """``status`` stays 'active' after ``expires_at`` passes; effective validity
        must decide, so an expired bundle licence cannot set or provision."""
        tid, app_id, plan_id, bundle_id = _setup(db_manager, f"cp-{uuid.uuid4().hex[:6]}")
        lic = _licence(db_manager, tid, app_id, plan_id, bundle_id)
        with db_manager.engine.begin() as conn:
            conn.execute(
                text("UPDATE license SET expires_at = now() - interval '1 day' WHERE id = :l"),
                {"l": lic},
            )
        assert str(db_manager.get_license(uuid.UUID(lic)).status) == "active"
        results = _run(db_manager, CP, tid, "workers")
        for name in ("put", "provision"):
            assert results[name].status_code == 403, (name, results[name].text)
        for name in ("clear", "unprovision"):
            assert results[name].status_code == ADMITTED[name], (name, results[name].text)

    def test_owner_break_glass_audit_names_the_tenant(self, db_manager, caplog):
        """A cross_owner write through the owner path is audited WITH its tenant."""
        _app(db_manager, "w2v", ("w2v",))
        tid = _tenant(db_manager, "platform", f"pf-{uuid.uuid4().hex[:6]}")
        with caplog.at_level(logging.WARNING), _as(db_manager, BREAK_GLASS) as admin:
            assert admin.put(f"/tenants/{tid}/apps/w2v/byo-mode", json=BYO).status_code == 200
        audit = [r.getMessage() for r in caplog.records if "cross_owner_action" in r.getMessage()]
        assert any(f"tenant={tid}" in m for m in audit), audit

    def test_a_bundle_licence_on_another_tenant_does_not_admit(self, db_manager, fake):  # noqa: F811
        tid, app_id, plan_id, bundle_id = _setup(db_manager, f"cp-{uuid.uuid4().hex[:6]}")
        other = _tenant(db_manager, "commonpractice", f"cp-other-{uuid.uuid4().hex[:6]}")
        _licence(db_manager, other, app_id, plan_id, bundle_id)
        with _as(db_manager, CP) as cp:
            resp = cp.put(f"/tenants/{tid}/apps/workers/byo-mode", json=BYO)
        assert resp.status_code == 403, resp.text
        assert _err(resp) == DENIED
