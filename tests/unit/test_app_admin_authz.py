"""Per-app catalogue writes are gated on app OWNERSHIP, not just the role.

``POST /plans``, ``PATCH /plans/{id}``, ``DELETE /plans/{id}``, ``DELETE
/apps/{id}``, ``POST /offerings`` and ``DELETE /offerings/{app}/{code}`` used
to check only ``license-admin``: any license-admin client (fr-backend) could
create / alter / delete plans on CommonPractice's app or delete it. They now
require the caller's owner to be one of the app's issuers (``app_issuer``,
migration 050) — 403 ``app_not_administrable_by_caller`` — with the audited
cross_owner break-glass. Every test signs as a REAL owning client (never the
conftest's break-glass default, which would make the refusals pass vacuously).
"""

from __future__ import annotations

import logging
import uuid
from uuid import UUID

from tests.unit.test_tenant_ownership import (
    BREAK_GLASS,
    CP,
    FR,
    UNKNOWN,
    _app,
    _as,
    _err,
    _mint,
    _tenant,
)

DENIED = "app_not_administrable_by_caller"


def _cp_app(db) -> tuple[UUID, UUID]:
    """An app ONLY commonpractice issues, with one plan."""
    return _app(db, f"cp-{uuid.uuid4().hex[:8]}", ("commonpractice",))


def _app_key(db, app_id: UUID) -> str:
    return db.get_app(app_id).key


def _audit(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "cross_owner_action" in r.getMessage()]


class TestCreatePlan:
    def _body(self, app_id: UUID) -> dict:
        return {"app_id": str(app_id), "code": "foreign", "name": "Foreign"}

    def test_non_issuer_is_refused_and_nothing_is_written(self, db_manager):
        app_id, _ = _cp_app(db_manager)
        with _as(db_manager, FR) as fr:
            resp = fr.post("/plans", json=self._body(app_id))
        assert resp.status_code == 403, resp.text
        assert _err(resp) == DENIED
        assert {p.code for p in db_manager.list_plans(app_id=app_id)} == {"standard"}

    def test_issuer_creates(self, db_manager):
        app_id, _ = _cp_app(db_manager)
        with _as(db_manager, CP) as cp:
            resp = cp.post("/plans", json=self._body(app_id))
        assert resp.status_code == 201, resp.text
        assert {p.code for p in db_manager.list_plans(app_id=app_id)} == {"standard", "foreign"}

    def test_break_glass_creates_and_is_audited(self, db_manager, caplog):
        app_id, _ = _cp_app(db_manager)
        with caplog.at_level(logging.WARNING), _as(db_manager, BREAK_GLASS) as admin:
            resp = admin.post("/plans", json=self._body(app_id))
        assert resp.status_code == 201, resp.text
        audit = _audit(caplog)
        assert any("action=plan_create" in m and f"app_id={app_id}" in m for m in audit), audit

    def test_unknown_app_is_still_404_for_a_non_issuer(self, db_manager):
        with _as(db_manager, FR) as fr:
            resp = fr.post("/plans", json=self._body(uuid.uuid4()))
        assert resp.status_code == 404, resp.text

    def test_unregistered_client_is_refused(self, db_manager):
        app_id, _ = _cp_app(db_manager)
        with _as(db_manager, UNKNOWN) as anon:
            resp = anon.post("/plans", json=self._body(app_id))
        assert resp.status_code == 403, resp.text
        assert _err(resp) == "unknown_api_client"
        assert {p.code for p in db_manager.list_plans(app_id=app_id)} == {"standard"}


class TestUpdatePlan:
    def test_non_issuer_is_refused_and_nothing_is_written(self, db_manager):
        _, plan_id = _cp_app(db_manager)
        with _as(db_manager, FR) as fr:
            resp = fr.patch(f"/plans/{plan_id}", json={"name": "hijacked"})
        assert resp.status_code == 403, resp.text
        assert _err(resp) == DENIED
        assert db_manager.get_plan(plan_id).name == "Standard"

    def test_issuer_updates(self, db_manager):
        _, plan_id = _cp_app(db_manager)
        with _as(db_manager, CP) as cp:
            resp = cp.patch(f"/plans/{plan_id}", json={"name": "Renamed"})
        assert resp.status_code == 200, resp.text
        assert db_manager.get_plan(plan_id).name == "Renamed"

    def test_break_glass_updates_and_is_audited(self, db_manager, caplog):
        app_id, plan_id = _cp_app(db_manager)
        with caplog.at_level(logging.WARNING), _as(db_manager, BREAK_GLASS) as admin:
            resp = admin.patch(f"/plans/{plan_id}", json={"name": "BG"})
        assert resp.status_code == 200, resp.text
        audit = _audit(caplog)
        assert any("action=plan_update" in m and f"app_id={app_id}" in m for m in audit), audit

    def test_missing_plan_is_still_404_for_a_non_issuer(self, db_manager):
        with _as(db_manager, FR) as fr:
            resp = fr.patch(f"/plans/{uuid.uuid4()}", json={"name": "x"})
        assert resp.status_code == 404, resp.text


class TestDeletePlan:
    def test_non_issuer_is_refused_and_the_plan_survives(self, db_manager):
        _, plan_id = _cp_app(db_manager)
        with _as(db_manager, FR) as fr:
            resp = fr.delete(f"/plans/{plan_id}")
        assert resp.status_code == 403, resp.text
        assert _err(resp) == DENIED
        assert db_manager.get_plan(plan_id) is not None

    def test_issuer_deletes(self, db_manager):
        _, plan_id = _cp_app(db_manager)
        with _as(db_manager, CP) as cp:
            resp = cp.delete(f"/plans/{plan_id}")
        assert resp.status_code == 204, resp.text
        assert db_manager.get_plan(plan_id) is None

    def test_break_glass_deletes_and_is_audited(self, db_manager, caplog):
        app_id, plan_id = _cp_app(db_manager)
        with caplog.at_level(logging.WARNING), _as(db_manager, BREAK_GLASS) as admin:
            resp = admin.delete(f"/plans/{plan_id}")
        assert resp.status_code == 204, resp.text
        audit = _audit(caplog)
        assert any("action=plan_delete" in m and f"app_id={app_id}" in m for m in audit), audit

    def test_missing_plan_is_still_404_for_a_non_issuer(self, db_manager):
        with _as(db_manager, FR) as fr:
            resp = fr.delete(f"/plans/{uuid.uuid4()}")
        assert resp.status_code == 404, resp.text


class TestDeleteApp:
    def test_non_issuer_is_refused_and_the_app_survives(self, db_manager):
        app_id, plan_id = _cp_app(db_manager)
        with _as(db_manager, FR) as fr:
            resp = fr.delete(f"/apps/{app_id}")
        assert resp.status_code == 403, resp.text
        assert _err(resp) == DENIED
        assert db_manager.get_app(app_id) is not None
        assert db_manager.get_plan(plan_id) is not None

    def test_issuer_deletes(self, db_manager):
        app_id, _ = _cp_app(db_manager)
        with _as(db_manager, CP) as cp:
            resp = cp.delete(f"/apps/{app_id}")
        assert resp.status_code == 204, resp.text
        assert db_manager.get_app(app_id) is None

    def test_break_glass_deletes_and_is_audited(self, db_manager, caplog):
        app_id, _ = _cp_app(db_manager)
        with caplog.at_level(logging.WARNING), _as(db_manager, BREAK_GLASS) as admin:
            resp = admin.delete(f"/apps/{app_id}")
        assert resp.status_code == 204, resp.text
        audit = _audit(caplog)
        assert any("action=app_delete" in m and f"app_id={app_id}" in m for m in audit), audit

    def test_missing_app_is_still_404_for_a_non_issuer(self, db_manager):
        with _as(db_manager, FR) as fr:
            resp = fr.delete(f"/apps/{uuid.uuid4()}")
        assert resp.status_code == 404, resp.text

    def test_non_issuer_gets_403_before_the_409_and_never_sees_the_radius(self, db_manager):
        """An app WITH licences: the issuer's unconfirmed delete is the 409 that
        carries the cascade counts (positive control — the 409 is reachable), and
        a non-issuer's identical call must be the 403, with no count in it."""
        app_id, plan_id = _cp_app(db_manager)
        tid = _tenant(db_manager, "commonpractice", f"cp-radius-{uuid.uuid4().hex[:6]}")
        with _as(db_manager, CP, tenant=tid) as cp:
            minted = _mint(cp, app_id, plan_id)
            assert minted.status_code == 201, minted.text
            control = cp.delete(f"/apps/{app_id}")
        assert control.status_code == 409, control.text
        assert control.json()["detail"]["licenses"] == 1

        with _as(db_manager, FR) as fr:
            unconfirmed = fr.delete(f"/apps/{app_id}")
            confirmed = fr.delete(f"/apps/{app_id}", params={"confirm_licenses": 1})
        for resp in (unconfirmed, confirmed):
            assert resp.status_code == 403, resp.text
            assert _err(resp) == DENIED
            detail = resp.json()["detail"]
            assert not {"licenses", "tokens", "plans", "tenants", "remedy"} & set(detail), detail
            assert tid not in resp.text
        assert db_manager.get_app(app_id) is not None
        assert db_manager.get_license(UUID(minted.json()["id"])) is not None


class TestOfferings:
    def _body(self, app_key: str) -> dict:
        return {"app_key": app_key, "code": "gated", "name": "Gated", "byo_mode": "local_allowed"}

    def test_non_issuer_upsert_is_refused_and_nothing_is_written(self, db_manager):
        app_id, _ = _cp_app(db_manager)
        key = _app_key(db_manager, app_id)
        with _as(db_manager, FR) as fr:
            resp = fr.post("/offerings", json=self._body(key))
        assert resp.status_code == 403, resp.text
        assert _err(resp) == DENIED
        assert db_manager.get_offering(key, "gated") is None

    def test_issuer_upserts_and_deletes(self, db_manager):
        app_id, _ = _cp_app(db_manager)
        key = _app_key(db_manager, app_id)
        with _as(db_manager, CP) as cp:
            created = cp.post("/offerings", json=self._body(key))
            assert created.status_code == 200, created.text
            deleted = cp.delete(f"/offerings/{key}/gated")
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["deleted"] is True

    def test_non_issuer_delete_is_refused_and_the_offering_survives(self, db_manager):
        app_id, _ = _cp_app(db_manager)
        key = _app_key(db_manager, app_id)
        db_manager.create_offering(key, "gated", "Gated", "local_allowed")
        with _as(db_manager, FR) as fr:
            resp = fr.delete(f"/offerings/{key}/gated")
        assert resp.status_code == 403, resp.text
        assert _err(resp) == DENIED
        assert db_manager.get_offering(key, "gated") is not None

    def test_break_glass_upsert_is_audited(self, db_manager, caplog):
        app_id, _ = _cp_app(db_manager)
        key = _app_key(db_manager, app_id)
        with caplog.at_level(logging.WARNING), _as(db_manager, BREAK_GLASS) as admin:
            resp = admin.post("/offerings", json=self._body(key))
        assert resp.status_code == 200, resp.text
        audit = _audit(caplog)
        assert any("action=offering_upsert" in m and f"app_id={app_id}" in m for m in audit), audit

    def test_unknown_app_keeps_its_answers(self, db_manager):
        with _as(db_manager, FR) as fr:
            up = fr.post("/offerings", json=self._body("no-such-app"))
            down = fr.delete("/offerings/no-such-app/gated")
        assert up.status_code == 404, up.text
        assert down.status_code == 200 and down.json()["deleted"] is False, down.text
