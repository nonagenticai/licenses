"""Revoking a tenant's licences RELEASES the dependency licences nothing requires any more.

THE DEFECT (measured live, 2026-10-21, read-only): bundle ensure (migration 059)
provisions the ``app_requires`` closure of a bundle as SHARED dependency licences
(``is_dependency``, ``bundle_id`` NULL). When w2v offboarded a tenant it revoked its
two bundle licences (w2v, w2v-image), and the six dependency licences (kag, workers,
evidence-review, mlx-backend, mlx-ingestion-backend, inference-router) stayed ACTIVE:
nothing released them, and the owner could not revoke them itself
(``_bundle_owner_may_revoke`` refused a bundle_id-NULL row, 403
``app_not_issuable_by_caller``).

WHAT IS PINNED.
* revoking the w2v bundle licences, in either order, releases all six dependencies
  in the revoke's own transaction, the response names them, and their tokens and
  ``tenant_dependency_grant`` rows go with them;
* a dependency still required by another ACTIVE licence (another owner's app, or
  another bundle) stays; only the unreachable ones go;
* the tenant's owner may revoke a dependency licence explicitly only when nothing
  requires it, else 409 ``dependency_still_required`` with nothing written; a
  non-owner without issuance is still 403;
* audit: ``metadata`` carries released_by / release_reason / released_at /
  released_by_revoke_of, and one ``dependency_released`` line per licence;
* idempotent: a re-revoke is 200, releases nothing more, rewrites no ``revoked_at``;
* the soft-delete path (``DELETE /licenses/user/{sub}``) releases the same way.
"""

from __future__ import annotations

import logging
from contextlib import AbstractContextManager
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from models import DEPENDENCY_EXPLICIT_REVOKE_REASON, DEPENDENCY_RELEASE_REASON, DatabaseManager
from tests.unit.test_bundle_ensure_route import fake  # noqa: F401 — fixture
from tests.unit.test_dependency_licences import _dep_grants, _ensure, _licences, _seed
from tests.unit.test_tenant_ownership import (
    BREAK_GLASS,
    FR,
    MERQI,
    PORTAL_ISSUER,
    W2V,
    W2V_ROLES,
    _as,
    _err,
    _tenant,
    _trust_portal,  # noqa: F401 — autouse fixture: trust the portal realm
)

W2V_DEPS = {
    "workers",
    "kag",
    "inference-router",
    "mlx-backend",
    "mlx-ingestion-backend",
    "evidence-review",
}


@pytest.fixture
def world(db_manager) -> DatabaseManager:
    # evidence-review is not in test_dependency_licences' slice; the kag ->
    # evidence-review edge (migration 062) is seeded only if the app exists.
    app = db_manager.create_app(
        key="evidence-review", name="evidence-review", issuers=("kag", "platform")
    )
    db_manager.create_plan(app_id=UUID(str(app.id)), code="standard", name="Standard")
    _seed(db_manager)
    return db_manager


@pytest.fixture
def w2v_tenant(world) -> str:
    tid = _tenant(world, "w2v", "w2v-offboarded-customer")
    ep = _ensure(world, tid, "w2v")
    assert not ep.refused, [(p.app_key, p.refuse_reason) for p in ep.refused]
    return tid


def _id(db: DatabaseManager, tenant: str, app: str) -> str:
    rows = _licences(db, tenant)[app]
    assert len(rows) == 1, rows
    return rows[0][0]


def _active_deps(db: DatabaseManager, tenant: str) -> set[str]:
    return {k for k, rows in _licences(db, tenant).items() if any(r[1] for r in rows)}


def _row(db: DatabaseManager, lic_id: str) -> dict:
    with db.engine.connect() as conn:
        r = conn.execute(
            text("SELECT status, revoked_at, metadata FROM license WHERE id = :l"), {"l": lic_id}
        ).one()
    return {"status": r[0], "revoked_at": r[1], "metadata": r[2] or {}}


def _revoke(client: TestClient, lic_id: str):
    return client.post("/license/revoke", json={"license_id": lic_id})


def _w2v(db: DatabaseManager, tenant: str) -> AbstractContextManager[TestClient]:
    """w2v-backend as on prod: portal realm, ``license-manager`` only."""
    return _as(db, W2V, iss=PORTAL_ISSUER, roles=W2V_ROLES, tenant=tenant)


def _w2v_admin(db: DatabaseManager, tenant: str) -> AbstractContextManager[TestClient]:
    """w2v-backend holding ``license-admin`` (``DELETE /licenses/user`` requires it)."""
    return _as(db, W2V, iss=PORTAL_ISSUER, tenant=tenant)


def _sub(db: DatabaseManager, lic_id: str) -> str:
    with db.engine.connect() as conn:
        return str(
            conn.execute(
                text(
                    "SELECT s.keycloak_sub FROM license l JOIN subject s ON s.id = l.subject_id "
                    "WHERE l.id = :l"
                ),
                {"l": lic_id},
            ).scalar_one()
        )


class TestBundleRevokeReleasesDependencies:
    def test_setup_has_six_dependency_licences(self, world, w2v_tenant):
        assert _active_deps(world, w2v_tenant) == W2V_DEPS

    @pytest.mark.parametrize("order", [("w2v", "w2v-image"), ("w2v-image", "w2v")])
    def test_revoking_both_bundle_licences_releases_all_six(
        self,
        world,
        w2v_tenant,
        fake,  # noqa: F811
        order,
    ):
        dep_ids = {k: _id(world, w2v_tenant, k) for k in W2V_DEPS}
        released: list[str] = []
        with _w2v(world, w2v_tenant) as w2v:
            for app in order:
                resp = _revoke(w2v, _id(world, w2v_tenant, app))
                assert resp.status_code == 200, resp.text
                assert resp.json()["revoked"] is True
                released += resp.json()["released_dependency_ids"]
        # w2v-image requires nothing: the w2v licence alone carries the closure.
        assert sorted(released) == sorted(dep_ids.values())
        assert _licences(world, w2v_tenant) == {}
        for lic in dep_ids.values():
            assert _row(world, lic)["status"] == "revoked"

    def test_tokens_and_dependency_grants_go_with_the_licences(
        self,
        world,
        w2v_tenant,
        fake,  # noqa: F811
    ):
        kag = _id(world, w2v_tenant, "kag")
        _tok, _raw = world.create_token(UUID(kag))
        assert _dep_grants(world, w2v_tenant)  # (kag, *), (platform, inference-router)
        with _w2v(world, w2v_tenant) as w2v:
            for app in ("w2v", "w2v-image"):
                assert _revoke(w2v, _id(world, w2v_tenant, app)).status_code == 200
        with world.engine.connect() as conn:
            live = conn.execute(
                text("SELECT count(*) FROM token WHERE license_id = :l AND NOT revoked"),
                {"l": kag},
            ).scalar_one()
        assert live == 0
        assert _dep_grants(world, w2v_tenant) == set()

    def test_audit_metadata_and_log_lines(
        self,
        world,
        w2v_tenant,
        fake,  # noqa: F811
        caplog,
    ):
        w2v_lic = _id(world, w2v_tenant, "w2v")
        kag = _id(world, w2v_tenant, "kag")
        with caplog.at_level(logging.INFO), _w2v(world, w2v_tenant) as w2v:
            assert _revoke(w2v, w2v_lic).status_code == 200
        meta = _row(world, kag)["metadata"]
        assert meta["released_by"] == f"service-account-{W2V}"
        assert meta["release_reason"] == DEPENDENCY_RELEASE_REASON
        assert meta["release_reason"] == "dependency released: no remaining dependent licence"
        assert meta["released_by_revoke_of"] == w2v_lic
        assert meta["released_at"]
        # The ensure's provenance is kept, not overwritten.
        assert meta["dependency_of"] == "w2v"
        lines = [r.getMessage() for r in caplog.records if "dependency_released:" in r.getMessage()]
        assert len(lines) == 6, lines
        assert any(f"licence={kag}" in ln and "app=kag" in ln for ln in lines), lines

    def test_re_revoke_is_idempotent(
        self,
        world,
        w2v_tenant,
        fake,  # noqa: F811
    ):
        w2v_lic = _id(world, w2v_tenant, "w2v")
        kag = _id(world, w2v_tenant, "kag")
        with _w2v(world, w2v_tenant) as w2v:
            first = _revoke(w2v, w2v_lic)
            stamp = _row(world, kag)["revoked_at"]
            again = _revoke(w2v, w2v_lic)
        assert first.status_code == again.status_code == 200
        assert len(first.json()["released_dependency_ids"]) == 6
        assert again.json()["revoked"] is True
        assert again.json()["released_dependency_ids"] == []
        assert _row(world, kag)["revoked_at"] == stamp


class TestListExposesDependencyMarkers:
    def test_get_licenses_carries_is_dependency_and_bundle_id(self, world, w2v_tenant):
        with _as(world, BREAK_GLASS, tenant=w2v_tenant) as admin:
            resp = admin.get("/licenses")
        assert resp.status_code == 200, resp.text
        by_app = {row["app"]["key"]: row for row in resp.json()}
        bundle = world.get_bundle_by_key("w2v")
        assert bundle is not None
        for app in ("w2v", "w2v-image"):
            assert by_app[app]["is_dependency"] is False
            assert by_app[app]["bundle_id"] == str(bundle.id)
        for app in W2V_DEPS:
            assert by_app[app]["is_dependency"] is True
            assert by_app[app]["bundle_id"] is None


class TestStillRequiredDependenciesStay:
    def test_a_dependency_another_owners_licence_needs_stays(
        self,
        world,
        w2v_tenant,
        fake,  # noqa: F811
    ):
        """merqi's ``marketing`` requires ``workers`` (-> inference-router): those two
        stay; kag and its own closure go."""
        marketing = world.get_app_by_key("marketing")
        assert marketing is not None
        plan = world.list_plans(UUID(str(marketing.id)))[0]
        with _as(world, BREAK_GLASS, tenant=w2v_tenant) as admin:
            resp = admin.post(
                "/licenses/user/marketing-seat",
                json={"app_id": str(marketing.id), "plan_id": str(plan.id)},
            )
        assert resp.status_code == 201, resp.text
        workers = _id(world, w2v_tenant, "workers")
        with _w2v(world, w2v_tenant) as w2v:
            for app in ("w2v", "w2v-image"):
                assert _revoke(w2v, _id(world, w2v_tenant, app)).status_code == 200
        assert _active_deps(world, w2v_tenant) == {"workers", "inference-router"}
        assert _row(world, workers)["status"] == "active"
        # The platform -> inference-router grant is still walked (workers -> IR).
        assert ("platform", "inference-router") in _dep_grants(world, w2v_tenant)

    def test_a_dependency_shared_with_another_bundle_stays(
        self,
        world,
        fake,  # noqa: F811
    ):
        """A tenant holding conveyor AND w2v: conveyor sells kag/workers and needs the
        same deps — revoking w2v's licences releases nothing."""
        tid = _tenant(world, "fr", "fr-holds-both")
        assert not _ensure(world, tid, "w2v").refused
        assert not _ensure(world, tid, "conveyor").refused
        before = _active_deps(world, tid)
        with _as(world, BREAK_GLASS, tenant=tid) as admin:
            for app in ("w2v", "w2v-image"):
                resp = _revoke(admin, _id(world, tid, app))
                assert resp.status_code == 200, resp.text
                assert resp.json()["released_dependency_ids"] == []
        assert _active_deps(world, tid) == before
        assert before >= {"inference-router", "mlx-backend", "mlx-ingestion-backend"}


class TestExplicitDependencyRevoke:
    def test_owner_revoking_a_required_dependency_is_409(
        self,
        world,
        w2v_tenant,
        fake,  # noqa: F811
    ):
        kag = _id(world, w2v_tenant, "kag")
        with _w2v(world, w2v_tenant) as w2v:
            resp = _revoke(w2v, kag)
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["code"] == "dependency_still_required"
        # kag is required by the w2v licence only (w2v-image requires nothing).
        assert detail["required_by"] == [
            {
                "license_id": _id(world, w2v_tenant, "w2v"),
                "tenant_id": w2v_tenant,
                "app_key": "w2v",
            }
        ]
        assert _row(world, kag)["status"] == "active"
        assert _active_deps(world, w2v_tenant) == W2V_DEPS

    def test_owner_may_revoke_a_dependency_nothing_requires(
        self,
        world,
        w2v_tenant,
        fake,  # noqa: F811
    ):
        """Drop the kag -> evidence-review edge: evidence-review is now orphaned and
        the owner may revoke it explicitly."""
        er = _id(world, w2v_tenant, "evidence-review")
        with world.engine.begin() as conn:
            conn.execute(
                text(
                    "DELETE FROM app_requires WHERE required_app_id = "
                    "(SELECT id FROM app WHERE key = 'evidence-review')"
                )
            )
        with _w2v(world, w2v_tenant) as w2v:
            resp = _revoke(w2v, er)
        assert resp.status_code == 200, resp.text
        assert resp.json()["revoked"] is True
        assert _row(world, er)["status"] == "revoked"
        assert _active_deps(world, w2v_tenant) == W2V_DEPS - {"evidence-review"}

    def test_a_non_owner_non_issuer_is_still_403(
        self,
        world,
        w2v_tenant,
        fake,  # noqa: F811
    ):
        world.put_tenant_grant(w2v_tenant, "merqi", "test")
        kag = _id(world, w2v_tenant, "kag")
        with _as(world, MERQI, tenant=w2v_tenant) as merqi:
            resp = _revoke(merqi, kag)
        assert resp.status_code == 403, resp.text
        assert _err(resp) == "app_not_issuable_by_caller"
        assert _row(world, kag)["status"] == "active"

    def test_the_owner_widening_does_not_reach_the_delete_path(
        self,
        world,
        w2v_tenant,
        fake,  # noqa: F811
    ):
        """``DELETE /licenses/user/{sub}`` keeps the old rule: a dependency row is not
        a bundle row, so the owner is refused there."""
        kag = _id(world, w2v_tenant, "kag")
        with _w2v_admin(world, w2v_tenant) as w2v:
            resp = w2v.delete(f"/licenses/user/{_sub(world, kag)}", params={"confirm_licenses": 1})
        assert resp.status_code == 403, resp.text
        assert _err(resp) == "app_not_issuable_by_caller"
        assert _row(world, kag)["status"] == "active"


class TestSoftDeleteReleases:
    def test_deleting_the_bundle_subjects_releases_the_dependencies(
        self,
        world,
        w2v_tenant,
        fake,  # noqa: F811
    ):
        released: list[str] = []
        with _w2v_admin(world, w2v_tenant) as w2v:
            for app in ("w2v-image", "w2v"):
                sub = _sub(world, _id(world, w2v_tenant, app))
                resp = w2v.delete(f"/licenses/user/{sub}", params={"confirm_licenses": 1})
                assert resp.status_code == 200, resp.text
                assert resp.json()["mode"] == "soft"
                released += resp.json()["released_dependency_ids"]
        assert len(released) == 6
        assert _licences(world, w2v_tenant) == {}


class TestEnsureLock:
    def test_a_held_ensure_lock_defers_the_release_never_the_revoke(
        self,
        world,
        w2v_tenant,
        fake,  # noqa: F811
        monkeypatch,
    ):
        """Revoking is security-critical: an ensure holding the tenant lock must not
        veto it. The licence is revoked, the release deferred; a re-revoke completes it."""
        import models

        monkeypatch.setattr(models, "DEPENDENCY_RELEASE_LOCK_TIMEOUT", "200ms")
        w2v_lic = _id(world, w2v_tenant, "w2v")
        kag = _id(world, w2v_tenant, "kag")
        holder = world.engine.connect()
        try:
            holder.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                {"k": f"bundle_ensure:{w2v_tenant}"},
            )
            with _w2v(world, w2v_tenant) as w2v:
                held = _revoke(w2v, w2v_lic)
                owner_dep = _revoke(w2v, kag)
        finally:
            holder.rollback()
            holder.close()
        assert held.status_code == 200, held.text
        assert held.json()["released_dependency_ids"] == []
        assert _row(world, w2v_lic)["status"] == "revoked"
        assert _active_deps(world, w2v_tenant) == W2V_DEPS
        # The owner's explicit dependency revoke cannot decide "still required"
        # without the lock: refused, nothing written.
        assert owner_dep.status_code == 409, owner_dep.text
        assert _err(owner_dep) == "ensure_in_progress"
        with _w2v(world, w2v_tenant) as w2v:
            again = _revoke(w2v, w2v_lic)
        assert again.status_code == 200, again.text
        assert len(again.json()["released_dependency_ids"]) == 6


class TestExplicitDependencyRevokeIsAReleaseToo:
    """An explicitly revoked dependency is audited and its grants cleaned like a
    released one: the main path revokes it first, so the release step never saw it."""

    def test_explicit_revoke_drops_the_grant_and_stamps_metadata(
        self,
        world,
        w2v_tenant,
        fake,  # noqa: F811
    ):
        er = _id(world, w2v_tenant, "evidence-review")
        with world.engine.begin() as conn:
            conn.execute(
                text(
                    "DELETE FROM app_requires WHERE required_app_id = "
                    "(SELECT id FROM app WHERE key = 'evidence-review')"
                )
            )
        er_grants = {g for g in _dep_grants(world, w2v_tenant) if g[1] == "evidence-review"}
        assert er_grants, _dep_grants(world, w2v_tenant)
        with _w2v(world, w2v_tenant) as w2v:
            resp = _revoke(w2v, er)
        assert resp.status_code == 200, resp.text
        # Only licences released IN ADDITION to the one revoked are listed.
        assert resp.json()["released_dependency_ids"] == []
        assert not {g for g in _dep_grants(world, w2v_tenant) if g[1] == "evidence-review"}
        # Other dependencies' grants are untouched.
        assert ("platform", "inference-router") in _dep_grants(world, w2v_tenant)
        meta = _row(world, er)["metadata"]
        assert meta["released_by"] == f"service-account-{W2V}"
        assert meta["release_reason"] == DEPENDENCY_EXPLICIT_REVOKE_REASON
        assert meta["released_by_revoke_of"] == er
        assert meta["released_at"]


class TestStillRequiredGuardAppliesToEveryCaller:
    def test_an_issuer_revoking_a_required_dependency_is_409(
        self,
        world,
        w2v_tenant,
        fake,  # noqa: F811
    ):
        """fr ISSUES kag, so it is admitted by the issuer rule, not the owner
        widening — and is refused all the same."""
        world.put_tenant_grant(w2v_tenant, "fr", "test")
        kag = _id(world, w2v_tenant, "kag")
        with _as(world, FR, tenant=w2v_tenant) as fr:
            resp = _revoke(fr, kag)
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["code"] == "dependency_still_required"
        assert _row(world, kag)["status"] == "active"

    def test_cross_owner_break_glass_may_force_it_and_is_logged(
        self,
        world,
        w2v_tenant,
        fake,  # noqa: F811
        caplog,
    ):
        kag = _id(world, w2v_tenant, "kag")
        with caplog.at_level(logging.WARNING), _as(world, BREAK_GLASS, tenant=w2v_tenant) as bg:
            resp = _revoke(bg, kag)
        assert resp.status_code == 200, resp.text
        assert _row(world, kag)["status"] == "revoked"
        forced = [
            r.getMessage() for r in caplog.records if "dependency_force_revoked:" in r.getMessage()
        ]
        assert len(forced) == 1, forced
        assert f"licence={kag}" in forced[0]
        assert _id(world, w2v_tenant, "w2v") in forced[0]
        assert _row(world, kag)["metadata"]["release_reason"] == DEPENDENCY_EXPLICIT_REVOKE_REASON


class TestAdvisoryLockTimeoutIsScoped:
    def test_lock_timeout_is_restored_after_the_advisory_lock(self, world, w2v_tenant):
        """The 10s bound is for the advisory-lock wait only; later row locks in the
        same transaction must wait under the session's normal lock_timeout."""
        session = world.get_session()
        try:
            session.execute(text("SET lock_timeout = '0'"))
            assert world._lock_tenant_for_dependency_release(session, w2v_tenant) is True
            assert session.execute(text("SHOW lock_timeout")).scalar_one() == "0"
        finally:
            session.rollback()
            session.close()

    def test_soft_delete_under_a_held_lock_defers_never_500(
        self,
        world,
        w2v_tenant,
        fake,  # noqa: F811
        monkeypatch,
    ):
        import models

        monkeypatch.setattr(models, "DEPENDENCY_RELEASE_LOCK_TIMEOUT", "200ms")
        sub = _sub(world, _id(world, w2v_tenant, "w2v"))
        holder = world.engine.connect()
        try:
            holder.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                {"k": f"bundle_ensure:{w2v_tenant}"},
            )
            with _w2v_admin(world, w2v_tenant) as w2v:
                resp = w2v.delete(f"/licenses/user/{sub}", params={"confirm_licenses": 1})
        finally:
            holder.rollback()
            holder.close()
        assert resp.status_code == 200, resp.text
        assert resp.json()["deleted"] == 1
        assert resp.json()["released_dependency_ids"] == []
        assert _active_deps(world, w2v_tenant) == W2V_DEPS
