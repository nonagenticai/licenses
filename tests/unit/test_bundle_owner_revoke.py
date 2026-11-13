"""The tenant's OWNER may revoke what its bundles minted (revoke paths only).

CP offboards a tenant (archive) and must then revoke the inert bundle licences
of apps it does not issue (``workers`` is issued by ``platform``). The widening
is bundle rows on the owner's own tenant — never a non-bundle row, never another
owner, never a mint. Also pins the ``licenses.access`` log line.
"""

from __future__ import annotations

import logging
import uuid
from uuid import UUID

from sqlalchemy import text

from models import DatabaseManager, Token
from tests.unit.test_bundle_ensure_route import fake  # noqa: F401 — fixture
from tests.unit.test_tenant_ownership import (
    BREAK_GLASS,
    CP,
    FR,
    _app,
    _as,
    _bundle,
    _err,
    _mint,
    _tenant,
)


def _setup(db: DatabaseManager, name: str) -> tuple[str, UUID, UUID, UUID]:
    """A CP-owned tenant, the platform-issued ``workers`` app in CP's
    ``n8n-nodes`` bundle. Returns (tenant, app, plan, bundle)."""
    app_id, plan_id = _app(db, "workers", ("platform",))
    _bundle(db, "n8n-nodes", app_id)
    bundle = db.get_bundle_by_key("n8n-nodes")
    assert bundle is not None
    return _tenant(db, "commonpractice", name), app_id, plan_id, UUID(str(bundle.id))


def _licence(
    db: DatabaseManager,
    tenant: str,
    app_id: UUID,
    plan_id: UUID,
    bundle_id: UUID | None,
    sub: str | None = None,
) -> str:
    """Minted by break-glass (CP cannot mint ``workers``), then stamped as the
    bundle ensure would stamp it."""
    with _as(db, BREAK_GLASS, tenant=tenant) as admin:
        resp = _mint(admin, app_id, plan_id, sub=sub)
    assert resp.status_code == 201, resp.text
    lic_id = resp.json()["id"]
    if bundle_id is not None:
        with db.engine.begin() as conn:
            conn.execute(
                text("UPDATE license SET bundle_id = :b WHERE id = :l"),
                {"b": str(bundle_id), "l": lic_id},
            )
    return lic_id


def _status(db: DatabaseManager, lic_id: str) -> str:
    lic = db.get_license(UUID(lic_id))
    assert lic is not None
    return str(lic.status)


class TestRevokeByLicenceId:
    def test_owner_revokes_a_bundle_row_on_an_archived_tenant(
        self,
        db_manager,
        fake,  # noqa: F811
        caplog,
    ):
        tid, app_id, plan_id, bundle_id = _setup(db_manager, "cp-offboarded")
        lic = _licence(db_manager, tid, app_id, plan_id, bundle_id)
        db_manager.set_tenant_archived(tid, True)
        with caplog.at_level(logging.INFO), _as(db_manager, CP, tenant=tid) as cp:
            resp = cp.post("/license/revoke", json={"license_id": lic})
        assert resp.status_code == 200, resp.text
        assert resp.json()["revoked"] is True
        assert _status(db_manager, lic) == "revoked"
        audit = [r.getMessage() for r in caplog.records if "bundle_owner_revoke" in r.getMessage()]
        assert len(audit) == 1, audit
        line = audit[0]
        for part in (
            f"azp={CP}",
            "owner=commonpractice",
            f"tenant={tid}",
            f"licence={lic}",
            f"bundle={bundle_id}",
            "tenant_archived=True",
        ):
            assert part in line, line

    def test_owner_cannot_revoke_a_non_bundle_row_it_does_not_issue(
        self,
        db_manager,
        fake,  # noqa: F811
    ):
        tid, app_id, plan_id, _bundle_id = _setup(db_manager, "cp-plain")
        lic = _licence(db_manager, tid, app_id, plan_id, None)
        with _as(db_manager, CP, tenant=tid) as cp:
            resp = cp.post("/license/revoke", json={"license_id": lic})
        assert resp.status_code == 403, resp.text
        assert _err(resp) == "app_not_issuable_by_caller"
        assert _status(db_manager, lic) == "active"

    def test_another_owner_with_a_grant_cannot_revoke_a_bundle_row(
        self,
        db_manager,
        fake,  # noqa: F811
    ):
        tid, app_id, plan_id, bundle_id = _setup(db_manager, "cp-granted")
        lic = _licence(db_manager, tid, app_id, plan_id, bundle_id)
        db_manager.put_tenant_grant(tid, "fr", "test")
        with _as(db_manager, FR, tenant=tid) as fr:
            resp = fr.post("/license/revoke", json={"license_id": lic})
        assert resp.status_code == 403, resp.text
        assert _err(resp) == "app_not_issuable_by_caller"
        assert _status(db_manager, lic) == "active"

    def test_mint_is_not_widened(self, db_manager, fake):  # noqa: F811
        tid, app_id, plan_id, _bundle_id = _setup(db_manager, "cp-nomint")
        with _as(db_manager, CP, tenant=tid) as cp:
            resp = _mint(cp, app_id, plan_id)
        assert resp.status_code == 403, resp.text
        assert _err(resp) == "app_not_issuable_by_caller"


class TestRevokeByToken:
    def test_owner_revokes_a_bundle_licences_token(self, db_manager, fake):  # noqa: F811
        tid, app_id, plan_id, bundle_id = _setup(db_manager, "cp-tok")
        lic = _licence(db_manager, tid, app_id, plan_id, bundle_id)
        tok, raw = db_manager.create_token(UUID(lic))
        db_manager.set_tenant_archived(tid, True)
        with _as(db_manager, CP, tenant=tid) as cp:
            resp = cp.post("/license/revoke", json={"token": raw})
        assert resp.status_code == 200, resp.text
        assert resp.json()["revoked"] is True
        row = db_manager.get_token_by_hash(Token.hash_token(raw))
        assert row is not None and row.revoked is True

    def test_owner_cannot_revoke_a_non_bundle_token(self, db_manager, fake):  # noqa: F811
        tid, app_id, plan_id, _bundle_id = _setup(db_manager, "cp-tok-plain")
        lic = _licence(db_manager, tid, app_id, plan_id, None)
        _tok, raw = db_manager.create_token(UUID(lic))
        with _as(db_manager, CP, tenant=tid) as cp:
            resp = cp.post("/license/revoke", json={"token": raw})
        assert resp.status_code == 403, resp.text
        assert _err(resp) == "app_not_issuable_by_caller"
        row = db_manager.get_token_by_hash(Token.hash_token(raw))
        assert row is not None and row.revoked is False

    def test_a_non_bundle_twin_refuses_the_whole_token_revoke(self, db_manager, fake):  # noqa: F811
        """The revoke kills every twin on the tenant, so one non-bundle twin of an
        app CP does not issue must refuse it — and revoke nothing."""
        tid, app_id, plan_id, bundle_id = _setup(db_manager, "cp-tok-twin")
        kag_app, kag_plan = _app(db_manager, "kag", ("platform",))
        bundled = _licence(db_manager, tid, app_id, plan_id, bundle_id)
        plain = _licence(db_manager, tid, kag_app, kag_plan, None, sub=str(uuid.uuid4()))
        _tok, raw = db_manager.create_token(UUID(bundled))
        twin, _raw2 = db_manager.create_token(UUID(plain))
        _share_hash(db_manager, twin.id, raw)
        with _as(db_manager, CP, tenant=tid) as cp:
            resp = cp.post("/license/revoke", json={"token": raw})
        assert resp.status_code == 403, resp.text
        assert _err(resp) == "app_not_issuable_by_caller"
        assert _unrevoked(db_manager, raw) == 2

    def test_a_refused_call_writes_no_bundle_owner_revoke_line(
        self,
        db_manager,
        fake,  # noqa: F811
        caplog,
    ):
        """Apps are authorized in app_id order. The bundle app is made to sort
        FIRST, so it is admitted before the plain twin's app is refused: the
        refused call must leave no ``bundle_owner_revoke`` record behind."""
        tid, app_id, plan_id, bundle_id = _setup(db_manager, "cp-tok-audit")
        other_app, other_plan = _app_sorting_after(db_manager, app_id)
        bundled = _licence(db_manager, tid, app_id, plan_id, bundle_id)
        plain = _licence(db_manager, tid, other_app, other_plan, None, sub=str(uuid.uuid4()))
        _tok, raw = db_manager.create_token(UUID(bundled))
        twin, _raw2 = db_manager.create_token(UUID(plain))
        _share_hash(db_manager, twin.id, raw)
        with caplog.at_level(logging.INFO), _as(db_manager, CP, tenant=tid) as cp:
            resp = cp.post("/license/revoke", json={"token": raw})
        assert resp.status_code == 403, resp.text
        assert _unrevoked(db_manager, raw) == 2
        audit = [r.getMessage() for r in caplog.records if "bundle_owner_revoke" in r.getMessage()]
        assert audit == [], audit

    def test_a_twin_on_another_tenant_does_not_skip_the_check(self, db_manager, fake):  # noqa: F811
        """The token's first row may live on ANOTHER tenant; the caller's own
        tenant's non-bundle twin must still be checked, not waved through."""
        tid, app_id, plan_id, _bundle_id = _setup(db_manager, "cp-tok-here")
        other = _tenant(db_manager, "platform", "elsewhere-" + uuid.uuid4().hex[:6])
        there = _licence(db_manager, other, app_id, plan_id, None)
        here = _licence(db_manager, tid, app_id, plan_id, None)
        _tok, raw = db_manager.create_token(UUID(there))
        twin, _raw2 = db_manager.create_token(UUID(here))
        _share_hash(db_manager, twin.id, raw)
        with _as(db_manager, CP, tenant=tid) as cp:
            resp = cp.post("/license/revoke", json={"token": raw})
        assert resp.status_code == 403, resp.text
        assert _unrevoked(db_manager, raw) == 2

    def test_an_issuer_cannot_revoke_a_twin_on_an_app_it_does_not_issue(
        self,
        db_manager,
        fake,  # noqa: F811
    ):
        tid, app_id, plan_id, _bundle_id = _setup(db_manager, "cp-tok-issuer")
        cp_app, cp_plan = _app(db_manager, "commonpractice", ("commonpractice",))
        own = _licence(db_manager, tid, cp_app, cp_plan, None)
        foreign = _licence(db_manager, tid, app_id, plan_id, None, sub=str(uuid.uuid4()))
        _tok, raw = db_manager.create_token(UUID(own))
        twin, _raw2 = db_manager.create_token(UUID(foreign))
        _share_hash(db_manager, twin.id, raw)
        with _as(db_manager, CP, tenant=tid) as cp:
            resp = cp.post("/license/revoke", json={"token": raw})
        assert resp.status_code == 403, resp.text
        assert _unrevoked(db_manager, raw) == 2


def _app_sorting_after(db: DatabaseManager, first: UUID) -> tuple[UUID, UUID]:
    """A platform-issued app whose id sorts after ``first`` (ids are random)."""
    for _ in range(64):
        app_id, plan_id = _app(db, "plain-" + uuid.uuid4().hex[:8], ("platform",))
        if str(app_id) > str(first):
            return app_id, plan_id
    raise AssertionError("no app id sorted after the bundle app in 64 tries")


def _share_hash(db: DatabaseManager, token_id: object, raw: str) -> None:
    """Give ``token_id`` the same hash as ``raw`` — a twin, as production has."""
    with db.engine.begin() as conn:
        conn.execute(
            text("UPDATE token SET token_hash = :h WHERE id = :i"),
            {"h": Token.hash_token(raw), "i": str(token_id)},
        )


def _unrevoked(db: DatabaseManager, raw: str) -> int:
    with db.engine.begin() as conn:
        return int(
            conn.execute(
                text("SELECT count(*) FROM token WHERE token_hash = :h AND NOT revoked"),
                {"h": Token.hash_token(raw)},
            ).scalar_one()
        )


class TestDeleteUserLicences:
    def test_owner_deletes_a_subjects_bundle_rows(self, db_manager, fake):  # noqa: F811
        tid, app_id, plan_id, bundle_id = _setup(db_manager, "cp-del")
        sub = f"user-{uuid.uuid4().hex[:8]}"
        lic = _licence(db_manager, tid, app_id, plan_id, bundle_id, sub=sub)
        db_manager.set_tenant_archived(tid, True)
        with _as(db_manager, CP, tenant=tid) as cp:
            resp = cp.delete(f"/licenses/user/{sub}", params={"confirm_licenses": 1})
        assert resp.status_code == 200, resp.text
        assert _status(db_manager, lic) == "revoked"

    def test_one_non_bundle_row_refuses_the_whole_delete(self, db_manager, fake):  # noqa: F811
        tid, app_id, plan_id, bundle_id = _setup(db_manager, "cp-del-mixed")
        sub = f"user-{uuid.uuid4().hex[:8]}"
        bundled = _licence(db_manager, tid, app_id, plan_id, bundle_id, sub=sub)
        plain_app, plain_plan = _app(db_manager, "kag", ("platform",))
        plain = _licence(db_manager, tid, plain_app, plain_plan, None, sub=sub)
        with _as(db_manager, CP, tenant=tid) as cp:
            resp = cp.delete(f"/licenses/user/{sub}", params={"confirm_licenses": 2})
        assert resp.status_code == 403, resp.text
        assert _err(resp) == "app_not_issuable_by_caller"
        assert (_status(db_manager, bundled), _status(db_manager, plain)) == ("active", "active")


class TestAccessLog:
    def test_one_line_with_azp_and_no_query_or_token(self, db_manager, caplog):
        tid = _tenant(db_manager, "commonpractice", "cp-log")
        with caplog.at_level(logging.INFO, logger="licenses.access"), _as(db_manager, CP) as cp:
            resp = cp.get(f"/tenants/{tid}", params={"secret": "sq-leak"})
            cp.get("/health/live")
            cp.get("/health")
        assert resp.status_code == 200, resp.text
        lines = [r.getMessage() for r in caplog.records if r.name == "licenses.access"]
        assert len(lines) == 1, lines
        line = lines[0]
        for part in (
            "method=GET",
            f"path=/tenants/{tid}",
            "status=200",
            "duration_ms=",
            f"azp={CP}",
            "realm=secure-apps",
            "owner=commonpractice",
        ):
            assert part in line, line
        assert "sq-leak" not in line and "secret" not in line
        assert "Bearer" not in line and "authorization" not in line.lower()

    def test_unauthenticated_request_logs_dashes_and_final_status(self, db_manager, caplog):
        from fastapi.testclient import TestClient

        import main

        main.db_manager = db_manager
        with (
            caplog.at_level(logging.INFO, logger="licenses.access"),
            TestClient(main.app, raise_server_exceptions=False) as anon,
        ):
            resp = anon.get("/tenants")
        assert resp.status_code == 401, resp.text
        lines = [r.getMessage() for r in caplog.records if r.name == "licenses.access"]
        assert len(lines) == 1, lines
        assert "status=401" in lines[0]
        assert "azp=- realm=- owner=-" in lines[0], lines[0]
