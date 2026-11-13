"""Tenant names / external_refs are unique per owner among LIVE tenants (migration 053).

An archived tenant must never block its name or external_ref from being registered
again by the same owner, a NAME resolves to the live row only (an archived tenant is
reachable by id), and restoring an archived tenant into a name / external_ref a live
tenant now holds is a clean 409, never an IntegrityError / 500.

Authenticates as REAL owning clients (see test_tenant_ownership.py), never as the
break-glass default, except where break-glass is the subject.
"""

from __future__ import annotations

import logging
import uuid

import pytest

from models import DatabaseManager, TenantNameTakenError
from tests.unit.test_tenant_ownership import BREAK_GLASS, CP, FR, _as, _err, _tenant


def _archive(db: DatabaseManager, tid: str) -> None:
    t = db.set_tenant_archived(tid, True)
    assert t is not None and t.archived_at is not None


class TestReRegisterAnArchivedName:
    def test_post_reuses_an_archived_name_with_a_new_tenant(self, db_manager):
        with _as(db_manager, FR) as fr:
            old = fr.post("/tenants", json={"name": "churn"})
            assert old.status_code == 201, old.text
            _archive(db_manager, old.json()["id"])
            new = fr.post("/tenants", json={"name": "churn"})
            # Control: while the new one is live, the name is taken again.
            again = fr.post("/tenants", json={"name": "churn"})
        assert new.status_code == 201, new.text
        assert new.json()["id"] != old.json()["id"]
        assert new.json()["archived_at"] is None
        assert again.status_code == 409 and _err(again) == "tenant_name_taken", again.text
        # The archived row is untouched and still reachable by id.
        assert db_manager.get_tenant(old.json()["id"]).archived_at is not None

    def test_put_register_reuses_an_archived_name(self, db_manager):
        old_id, new_id = str(uuid.uuid7()), str(uuid.uuid7())
        with _as(db_manager, CP) as cp:
            assert cp.put(f"/tenants/{old_id}", json={"name": "cp-churn"}).status_code == 201
            _archive(db_manager, old_id)
            resp = cp.put(f"/tenants/{new_id}", json={"name": "cp-churn"})
        assert resp.status_code == 201, resp.text
        assert resp.json()["id"] == new_id

    def test_rename_into_a_name_only_an_archived_tenant_holds(self, db_manager):
        gone = _tenant(db_manager, "fr", "old-name")
        _archive(db_manager, gone)
        live = _tenant(db_manager, "fr", "other")
        with _as(db_manager, FR) as fr:
            resp = fr.patch(f"/tenants/{live}", json={"name": "old-name"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == "old-name"


class TestNameLookupIgnoresArchived:
    def test_name_resolves_to_the_live_row(self, db_manager):
        gone = _tenant(db_manager, "fr", "dup")
        _archive(db_manager, gone)
        live = _tenant(db_manager, "fr", "dup")
        with _as(db_manager, FR) as fr:
            resp = fr.get("/tenants/dup")
            by_id = fr.get(f"/tenants/{gone}")
        assert resp.status_code == 200 and resp.json()["id"] == live, resp.text
        assert by_id.status_code == 200 and by_id.json()["archived_at"] is not None
        assert db_manager.get_tenant("dup").id == db_manager.get_tenant(live).id

    def test_an_archived_tenant_is_not_reachable_by_name(self, db_manager):
        gone = _tenant(db_manager, "fr", "retired")
        _archive(db_manager, gone)
        with _as(db_manager, FR) as fr:
            assert fr.get("/tenants/retired").status_code == 404
            # ...but by id it is, and it can be restored by id.
            assert fr.get(f"/tenants/{gone}").status_code == 200
            back = fr.patch(f"/tenants/{gone}/archive", json={"archived": False})
        assert back.status_code == 200 and back.json()["archived_at"] is None, back.text
        assert db_manager.get_tenant("retired") is not None

    def test_unscoped_lookup_skips_another_owners_archived_namesake(self, db_manager, caplog):
        live = _tenant(db_manager, "fr", "twice")
        gone = _tenant(db_manager, "commonpractice", "twice")
        with caplog.at_level(logging.WARNING):
            # Control: both live -> ambiguous, never a pick.
            assert db_manager.get_tenant("twice") is None
        _archive(db_manager, gone)
        assert str(db_manager.get_tenant("twice").id) == live

    def test_break_glass_name_lookup_ignores_archived(self, db_manager):
        live = _tenant(db_manager, "fr", "bg-name")
        _archive(db_manager, _tenant(db_manager, "commonpractice", "bg-name"))
        with _as(db_manager, BREAK_GLASS) as admin:
            resp = admin.get("/tenants/bg-name")
        assert resp.status_code == 200 and resp.json()["id"] == live, resp.text


class TestExternalRefIdempotencyIgnoresArchived:
    def test_reprovisioning_an_archived_ref_creates_a_new_live_tenant(self, db_manager):
        body = {"name": "acme", "external_ref": "acme-slug"}
        with _as(db_manager, FR) as fr:
            first = fr.post("/tenants", json=body)
            retry = fr.post("/tenants", json=body)
            _archive(db_manager, first.json()["id"])
            again = fr.post("/tenants", json=body)
            retry_again = fr.post("/tenants", json=body)
        assert (first.status_code, retry.status_code) == (201, 200)
        assert retry.json()["id"] == first.json()["id"]  # control: live ref is idempotent
        assert again.status_code == 201, again.text
        assert again.json()["id"] != first.json()["id"]
        assert again.json()["archived_at"] is None
        assert retry_again.status_code == 200 and retry_again.json()["id"] == again.json()["id"]
        assert (
            db_manager.get_tenant_by_external_ref("fr", "acme-slug").id
            == db_manager.get_tenant(again.json()["id"]).id
        )


class TestUnarchiveIntoAConflict:
    def test_restoring_into_a_live_name_is_409_and_writes_nothing(self, db_manager):
        gone = _tenant(db_manager, "fr", "taken")
        _archive(db_manager, gone)
        live = _tenant(db_manager, "fr", "taken")
        with _as(db_manager, FR) as fr:
            resp = fr.patch(f"/tenants/{gone}/archive", json={"archived": False})
        assert resp.status_code == 409, resp.text
        assert _err(resp) == "tenant_name_taken"
        assert db_manager.get_tenant(gone).archived_at is not None
        assert db_manager.get_tenant(live).archived_at is None

    def test_restoring_into_a_live_external_ref_is_409(self, db_manager):
        body = {"name": "ref-a", "external_ref": "same-ref"}
        with _as(db_manager, FR) as fr:
            old = fr.post("/tenants", json=body).json()["id"]
            _archive(db_manager, old)
            assert fr.post("/tenants", json={**body, "name": "ref-b"}).status_code == 201
            resp = fr.patch(f"/tenants/{old}/archive", json={"archived": False})
        assert resp.status_code == 409, resp.text
        assert _err(resp) == "tenant_external_ref_taken"
        assert db_manager.get_tenant(old).archived_at is not None

    def test_a_namesake_of_another_owner_does_not_block_restore(self, db_manager):
        gone = _tenant(db_manager, "fr", "cross")
        _archive(db_manager, gone)
        _tenant(db_manager, "commonpractice", "cross")
        with _as(db_manager, FR) as fr:
            resp = fr.patch(f"/tenants/{gone}/archive", json={"archived": False})
        assert resp.status_code == 200, resp.text

    def test_the_index_race_is_also_a_name_taken_error(self, db_manager, monkeypatch):
        """The pre-check is bypassed, so the partial unique index has to answer —
        and that answer must still be TenantNameTakenError, not an IntegrityError."""
        gone = _tenant(db_manager, "fr", "racy")
        _archive(db_manager, gone)
        _tenant(db_manager, "fr", "racy")
        monkeypatch.setattr(
            DatabaseManager, "_refuse_restore_into_taken_identity", staticmethod(lambda s, t: None)
        )
        with pytest.raises(TenantNameTakenError):
            db_manager.set_tenant_archived(gone, False)
        assert db_manager.get_tenant(gone).archived_at is not None
