"""Tenants and licences are OWNED PER APP (migration 050).

Every test here authenticates as a REAL owning client — a signed token whose
verified (iss realm, azp) is a row of ``api_client`` — never as the conftest's
break-glass default, because the cross-owner bypass is exactly what would make
these pass vacuously.

  secure-apps / fr-backend                     -> fr
  secure-apps / commonpractice-licenses-admin  -> commonpractice
  secure-apps / control-service-account        -> merqi
  portal      / w2v-backend                    -> w2v
  secure-apps / licenses-admin                 -> platform, cross_owner
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import main
from auth.models import KeycloakUser
from models import BUNDLE_ENSURER_SEED, BundleApp, DatabaseManager
from tests.conftest import _make_mock_jwt
from tests.support.jwt_keys import TEST_ISSUER
from tests.unit.test_bundle_ensure_route import fake  # noqa: F401 — fixture

PORTAL_ISSUER = "https://keycloak.example.com/realms/portal"
FR = "fr-backend"
CP = "commonpractice-licenses-admin"
MERQI = "control-service-account"
W2V = "w2v-backend"
BREAK_GLASS = "licenses-admin"
UNKNOWN = "some-unregistered-client"

WRITE_ROLES = ["license-admin", "license-manager", "license-reader"]


@pytest.fixture(autouse=True)
def _trust_portal(monkeypatch):
    monkeypatch.setenv("KEYCLOAK_TRUSTED_ISSUERS", f"{TEST_ISSUER},{PORTAL_ISSUER}")


@contextmanager
def _as(
    db_manager: DatabaseManager,
    azp: str,
    *,
    iss: str = TEST_ISSUER,
    roles: list[str] | None = None,
    tenant: str | None = None,
    claim_tenant: str | None = None,
) -> Iterator[TestClient]:
    """A TestClient signed as ``azp`` in ``iss``'s realm, optionally retargeting."""
    main.db_manager = db_manager
    user = KeycloakUser(
        sub=f"service-account-{azp}",
        preferred_username=f"service-account-{azp}",
        realm_access={"roles": roles or WRITE_ROLES},
        resource_access={},
    )
    token = _make_mock_jwt(user, azp=azp, iss=iss)
    if claim_tenant is not None:
        import jwt as pyjwt

        from tests.support.jwt_keys import private_key_pem

        payload = pyjwt.decode(token, options={"verify_signature": False})
        payload["tenant_id"] = claim_tenant
        token = pyjwt.encode(payload, private_key_pem(), algorithm="RS256")
    with TestClient(main.app, raise_server_exceptions=False) as client:
        client.headers["Authorization"] = f"Bearer {token}"
        if tenant is not None:
            client.headers["X-Tenant-Id"] = tenant
        yield client


def _app(db: DatabaseManager, key: str, issuers: tuple[str, ...]) -> tuple[UUID, UUID]:
    app = db.create_app(key=key, name=key, issuers=issuers)
    app_id = UUID(str(app.id))
    plan = db.create_plan(app_id=app_id, code="standard", name="Standard")
    return app_id, UUID(str(plan.id))


def _mint(client: TestClient, app_id: UUID, plan_id: UUID, sub: str | None = None):
    return client.post(
        f"/licenses/user/{sub or 'user-' + uuid.uuid4().hex[:8]}",
        json={"app_id": str(app_id), "plan_id": str(plan_id)},
    )


def _err(resp) -> str | None:
    detail = resp.json().get("detail")
    return detail.get("error") if isinstance(detail, dict) else None


def _tenant(db: DatabaseManager, owner: str, name: str) -> str:
    return str(db.create_tenant(name=name, owner_key=owner).id)


# ---------------------------------------------------------------------------
# Names are unique PER OWNER
# ---------------------------------------------------------------------------


class TestNamesArePerOwner:
    def test_two_owners_create_the_same_name_and_both_201(self, db_manager):
        with _as(db_manager, FR) as fr:
            a = fr.post("/tenants", json={"name": "netcare"})
        cp_id = str(uuid.uuid7())
        with _as(db_manager, CP) as cp:
            b = cp.put(f"/tenants/{cp_id}", json={"name": "netcare"})
        assert a.status_code == 201, a.text
        assert b.status_code == 201, b.text
        assert a.json()["id"] != b.json()["id"] == cp_id
        assert (a.json()["owner_key"], b.json()["owner_key"]) == ("fr", "commonpractice")

    def test_post_with_a_name_another_owner_holds_returns_a_new_uuid(self, db_manager):
        """The cross-tenant merge this migration exists for: fr onboarding `acme`
        after CP registered it used to be handed CP's UUID."""
        cp_tenant = _tenant(db_manager, "commonpractice", "acme")
        with _as(db_manager, FR) as fr:
            resp = fr.post("/tenants", json={"name": "acme"})
        assert resp.status_code == 201, resp.text
        assert resp.json()["id"] != cp_tenant
        assert resp.json()["owner_key"] == "fr"

    def test_same_owner_same_name_is_409(self, db_manager):
        with _as(db_manager, FR) as fr:
            assert fr.post("/tenants", json={"name": "acme"}).status_code == 201
            again = fr.post("/tenants", json={"name": "acme"})
        assert again.status_code == 409, again.text
        assert _err(again) == "tenant_name_taken"

    def test_external_ref_is_idempotent_per_owner(self, db_manager):
        body = {"name": "acme", "external_ref": "acme-slug"}
        with _as(db_manager, FR) as fr:
            first = fr.post("/tenants", json=body)
            second = fr.post("/tenants", json=body)
        with _as(db_manager, CP) as cp:
            other = cp.post("/tenants", json=body)
        assert (first.status_code, second.status_code, other.status_code) == (201, 200, 201)
        assert first.json()["id"] == second.json()["id"]
        assert first.json()["external_ref"] == "acme-slug"
        assert other.json()["id"] != first.json()["id"]

    def test_put_on_an_id_owned_by_another_app_is_409(self, db_manager):
        fr_tenant = _tenant(db_manager, "fr", "fr-cust")
        with _as(db_manager, CP) as cp:
            resp = cp.put(f"/tenants/{fr_tenant}", json={"name": "fr-cust"})
        assert resp.status_code == 409, resp.text
        assert _err(resp) == "tenant_owned_by_other_app"
        assert db_manager.get_tenant(fr_tenant).owner_key == "fr"

    def test_put_same_owner_reregister_is_200_and_name_clash_409(self, db_manager):
        tid = str(uuid.uuid7())
        with _as(db_manager, CP) as cp:
            assert cp.put(f"/tenants/{tid}", json={"name": "cp-x"}).status_code == 201
            assert cp.put(f"/tenants/{tid}", json={"name": "cp-x"}).status_code == 200
            clash = cp.put(f"/tenants/{uuid.uuid7()}", json={"name": "cp-x"})
        assert clash.status_code == 409, clash.text
        assert _err(clash) == "tenant_name_taken"


# ---------------------------------------------------------------------------
# Reads are owner-scoped; grants open read BY ID only
# ---------------------------------------------------------------------------


class TestReads:
    def test_cross_owner_get_by_name_and_id_is_404_until_granted(self, db_manager):
        cp_tenant = _tenant(db_manager, "commonpractice", "cp-only")
        with _as(db_manager, FR) as fr:
            assert fr.get("/tenants/cp-only").status_code == 404
            assert fr.get(f"/tenants/{cp_tenant}").status_code == 404
            db_manager.put_tenant_grant(cp_tenant, "fr", "test")
            by_id = fr.get(f"/tenants/{cp_tenant}")
            by_name = fr.get("/tenants/cp-only")
        assert by_id.status_code == 200, by_id.text
        assert by_id.json()["owner_key"] == "commonpractice"
        assert by_name.status_code == 404, "a grant must never make a tenant resolvable by name"

    def test_a_name_resolves_to_the_callers_own_tenant(self, db_manager):
        fr_t = _tenant(db_manager, "fr", "shared-name")
        cp_t = _tenant(db_manager, "commonpractice", "shared-name")
        with _as(db_manager, FR) as fr:
            assert fr.get("/tenants/shared-name").json()["id"] == fr_t
        with _as(db_manager, CP) as cp:
            assert cp.get("/tenants/shared-name").json()["id"] == cp_t

    def test_list_is_owner_scoped(self, db_manager):
        _tenant(db_manager, "fr", "fr-one")
        _tenant(db_manager, "commonpractice", "cp-one")
        with _as(db_manager, FR) as fr:
            names = {t["name"] for t in fr.get("/tenants").json()}
        assert "fr-one" in names and "cp-one" not in names

    def test_list_honours_an_active_grant_and_still_excludes_ungranted_tenants(self, db_manager):
        """The listing and ``owner_can_access_tenant`` must agree.

        Before this, the retarget check honoured an ACTIVE grant while the
        listing ignored it: fr could act on a granted tenant it could not see
        (live: ``svc-n8n-local-conveyor`` was invisible while fr was granted its
        budgets). The ungranted platform tenants are the control — a fix that
        simply widened the filter would let every platform tenant through.
        """
        fr_own = _tenant(db_manager, "fr", "fr-own-listing")
        conveyed = _tenant(db_manager, "platform", "svc-conveyor-listing")
        ungranted = _tenant(db_manager, "platform", "platform-ungranted-listing")
        db_manager.put_tenant_grant(conveyed, "fr", "test")

        # The two definitions, side by side.
        assert db_manager.owner_can_access_tenant("fr", conveyed)
        assert not db_manager.owner_can_access_tenant("fr", ungranted)
        with _as(db_manager, FR) as fr:
            ids = {t["id"] for t in fr.get("/tenants").json()}

        assert ids == {fr_own, conveyed}
        assert ungranted not in ids

    def test_a_revoked_grant_drops_out_of_the_listing(self, db_manager):
        own = _tenant(db_manager, "fr", "fr-own-revoked")
        conveyed = _tenant(db_manager, "platform", "platform-revoked-listing")
        db_manager.put_tenant_grant(conveyed, "fr", "test")
        db_manager.revoke_tenant_grant(conveyed, "fr")
        with _as(db_manager, FR) as fr:
            ids = {t["id"] for t in fr.get("/tenants").json()}
        assert ids == {own}
        assert not db_manager.owner_can_access_tenant("fr", conveyed)

    def test_internal_tenants_carries_owner_key(self, db_manager):
        _tenant(db_manager, "w2v", "w2v-one")
        with _as(db_manager, BREAK_GLASS) as admin:
            rows = admin.get("/internal/tenants").json()
        by_name = {r["name"]: r for r in rows}
        assert by_name["w2v-one"]["owner_key"] == "w2v"
        assert by_name["w2v-one"]["external_ref"] is None
        assert by_name["seed-tenant-primary"]["owner_key"] == "platform"


# ---------------------------------------------------------------------------
# Issuance: app_issuer AND (owner OR grant)
# ---------------------------------------------------------------------------


class TestIssuance:
    def test_fr_minting_commonpractice_is_403(self, db_manager):
        cp_app, cp_plan = _app(db_manager, "commonpractice", ("commonpractice",))
        fr_tenant = _tenant(db_manager, "fr", "netcare")
        with _as(db_manager, FR, tenant=fr_tenant) as fr:
            resp = _mint(fr, cp_app, cp_plan)
        assert resp.status_code == 403, resp.text
        assert _err(resp) == "app_not_issuable_by_caller"
        assert db_manager.list_licenses(tenant_id=fr_tenant) == []

    def test_cp_minting_on_an_fr_tenant_needs_a_grant(self, db_manager):
        cp_app, cp_plan = _app(db_manager, "commonpractice", ("commonpractice",))
        fr_tenant = _tenant(db_manager, "fr", "fr-cust")
        with _as(db_manager, CP, tenant=fr_tenant) as cp:
            refused = _mint(cp, cp_app, cp_plan)
        assert refused.status_code == 403, refused.text
        assert _err(refused) == "tenant_not_accessible"

        with _as(db_manager, FR) as fr:
            grant = fr.put(f"/tenants/{fr_tenant}/grants/commonpractice")
        assert grant.status_code == 201, grant.text
        with _as(db_manager, CP, tenant=fr_tenant) as cp:
            ok = _mint(cp, cp_app, cp_plan)
        assert ok.status_code == 201, ok.text
        assert ok.json()["tenant_id"] == fr_tenant

    def test_claim_tenant_without_ownership_is_tenant_not_accessible(self, db_manager):
        """No header at all: a token whose own tenant claim names someone else's
        tenant still cannot mint there."""
        cp_app, cp_plan = _app(db_manager, "commonpractice", ("commonpractice",))
        fr_tenant = _tenant(db_manager, "fr", "fr-claim")
        with _as(db_manager, CP, claim_tenant=fr_tenant) as cp:
            resp = _mint(cp, cp_app, cp_plan)
        assert resp.status_code == 403, resp.text
        assert _err(resp) == "tenant_not_accessible"

    def test_revoke_of_an_app_the_caller_does_not_issue_is_403(self, db_manager):
        cp_app, cp_plan = _app(db_manager, "commonpractice", ("commonpractice",))
        fr_tenant = _tenant(db_manager, "fr", "fr-rev")
        with _as(db_manager, BREAK_GLASS, tenant=fr_tenant) as admin:
            lic = _mint(admin, cp_app, cp_plan)
        assert lic.status_code == 201, lic.text
        with _as(db_manager, FR, tenant=fr_tenant) as fr:
            resp = fr.post("/license/revoke", json={"license_id": lic.json()["id"]})
            tok = fr.post(f"/licenses/{lic.json()['id']}/tokens", json={"expires_days": 1})
        assert resp.status_code == 403, resp.text
        assert _err(resp) == "app_not_issuable_by_caller"
        assert tok.status_code == 403, tok.text
        assert db_manager.get_license(UUID(lic.json()["id"])).status == "active"

    def test_unknown_client_write_is_403_and_reads_still_work(self, db_manager, seed_token):
        with _as(db_manager, UNKNOWN) as unknown:
            write = unknown.post("/tenants", json={"name": "nope"})
        assert write.status_code == 403, write.text
        assert _err(write) == "unknown_api_client"
        _tok, raw = seed_token
        with TestClient(main.app, raise_server_exceptions=False) as anon:
            introspect = anon.post("/license/introspect", json={"token": raw})
        assert introspect.status_code == 200, introspect.text
        assert introspect.json()["active"] is True

    def test_unknown_client_cannot_mint_even_with_license_admin(self, db_manager):
        app_id, plan_id = _app(db_manager, "workers", ("platform", "fr"))
        with _as(db_manager, UNKNOWN, claim_tenant="01000000-0000-7000-8000-000000000001") as u:
            resp = _mint(u, app_id, plan_id)
        assert resp.status_code == 403, resp.text
        assert _err(resp) == "unknown_api_client"


# ---------------------------------------------------------------------------
# Owner-only operations; break-glass audited
# ---------------------------------------------------------------------------


class TestOwnerOnly:
    def test_grantee_cannot_rename_archive_byo_or_grant_onward(self, db_manager):
        fr_tenant = _tenant(db_manager, "fr", "fr-owned")
        db_manager.put_tenant_grant(fr_tenant, "commonpractice", "test")
        with _as(db_manager, CP) as cp:
            calls = {
                "rename": cp.patch(f"/tenants/{fr_tenant}", json={"name": "stolen"}),
                "archive": cp.patch(f"/tenants/{fr_tenant}/archive", json={"archived": True}),
                "byo": cp.patch(
                    f"/tenants/{fr_tenant}/byo-mode", json={"byo_mode": "platform_only"}
                ),
                "grant": cp.put(f"/tenants/{fr_tenant}/grants/w2v"),
            }
        for what, resp in calls.items():
            assert resp.status_code == 403, (what, resp.text)
            assert _err(resp) == "tenant_owner_only", what
        t = db_manager.get_tenant(fr_tenant)
        assert (t.name, t.archived_at) == ("fr-owned", None)

    def test_another_owners_tenant_is_404_for_writes(self, db_manager):
        fr_tenant = _tenant(db_manager, "fr", "fr-hidden")
        with _as(db_manager, CP) as cp:
            assert cp.patch(f"/tenants/{fr_tenant}", json={"name": "x"}).status_code == 404
            assert cp.patch("/tenants/fr-hidden", json={"name": "x"}).status_code == 404

    def test_owner_can_grant_list_and_revoke(self, db_manager):
        fr_tenant = _tenant(db_manager, "fr", "fr-g")
        with _as(db_manager, FR) as fr:
            put = fr.put(f"/tenants/{fr_tenant}/grants/w2v")
            again = fr.put(f"/tenants/{fr_tenant}/grants/w2v")
            listed = fr.get(f"/tenants/{fr_tenant}/grants")
            self_grant = fr.put(f"/tenants/{fr_tenant}/grants/fr")
            ghost = fr.put(f"/tenants/{fr_tenant}/grants/no-such-owner")
            deleted = fr.delete(f"/tenants/{fr_tenant}/grants/w2v")
            gone = fr.delete(f"/tenants/{fr_tenant}/grants/w2v")
        assert (put.status_code, again.status_code) == (201, 200)
        # platform holds the infrastructure grant every non-platform tenant is born with
        assert sorted(g["grantee_owner_key"] for g in listed.json()) == ["platform", "w2v"]
        assert self_grant.status_code == 422 and _err(self_grant) == "grant_to_owner"
        assert ghost.status_code == 404 and _err(ghost) == "owner_not_found"
        assert deleted.status_code == 204, deleted.text
        assert deleted.content == b""
        assert gone.status_code == 404 and _err(gone) == "grant_not_found"
        assert db_manager.owner_can_access_tenant("w2v", fr_tenant) is False

    def test_break_glass_acts_across_owners_and_is_audited(self, db_manager, caplog):
        fr_tenant = _tenant(db_manager, "fr", "fr-bg")
        with caplog.at_level(logging.WARNING), _as(db_manager, BREAK_GLASS) as admin:
            resp = admin.patch(f"/tenants/{fr_tenant}", json={"name": "fr-bg-renamed"})
        assert resp.status_code == 200, resp.text
        assert db_manager.get_tenant(fr_tenant).name == "fr-bg-renamed"
        audit = [r.getMessage() for r in caplog.records if "cross_owner_action" in r.getMessage()]
        assert any("action=tenant_rename" in m and fr_tenant in m for m in audit), audit
        assert any("tenant_owner=fr" in m and "caller_owner=platform" in m for m in audit)


# ---------------------------------------------------------------------------
# X-Tenant-Id retarget is limited to owned / granted tenants
# ---------------------------------------------------------------------------


class TestRetarget:
    def test_retarget_to_an_unheld_tenant_is_403_and_to_an_owned_one_works(self, db_manager):
        app_id, plan_id = _app(db_manager, "workers", ("platform", "fr"))
        cp_tenant = _tenant(db_manager, "commonpractice", "cp-target")
        fr_tenant = _tenant(db_manager, "fr", "fr-target")
        with _as(db_manager, FR, tenant=cp_tenant) as fr:
            refused = fr.get("/licenses")
        assert refused.status_code == 403, refused.text
        assert _err(refused) == "tenant_not_accessible"
        with _as(db_manager, FR, tenant=fr_tenant) as fr:
            ok = _mint(fr, app_id, plan_id)
        assert ok.status_code == 201, ok.text
        assert ok.json()["tenant_id"] == fr_tenant


# ---------------------------------------------------------------------------
# Bundle ensure: owner only, and it grants the bundled apps' issuers
# ---------------------------------------------------------------------------


def _bundle(db: DatabaseManager, key: str, app_id: UUID) -> None:
    """A bundle with migration 052's ensurers for ``key`` (created through the DAL,
    which refuses a bundle without one)."""
    bundle_id = db.create_bundle(key, key, ensurers=BUNDLE_ENSURER_SEED[key]).id
    session = db.get_session()
    try:
        session.add(BundleApp(bundle_id=bundle_id, app_id=app_id))
        session.commit()
    finally:
        session.close()


class TestBundleEnsure:
    BODY = {"plans": ["standard"], "quota_override": None, "dry_run": False}

    def test_owner_ensure_grants_w2v_which_can_then_mint(self, db_manager, fake):  # noqa: F811
        w2v_app, w2v_plan = _app(db_manager, "w2v", ("w2v",))
        _bundle(db_manager, "w2v", w2v_app)
        merqi_tenant = str(uuid.uuid7())
        roles = ["bundle-provisioner", "license-manager", "license-admin"]
        with _as(db_manager, MERQI, roles=roles) as merqi:
            reg = merqi.put(f"/tenants/{merqi_tenant}", json={"name": "merqi-cust"})
            assert reg.status_code == 201, reg.text

        with _as(db_manager, W2V, iss=PORTAL_ISSUER, tenant=merqi_tenant) as w2v:
            before = _mint(w2v, w2v_app, w2v_plan)
        assert before.status_code == 403, before.text
        assert _err(before) == "tenant_not_accessible"

        with _as(db_manager, MERQI, roles=roles) as merqi:
            ensured = merqi.post(
                f"/internal/tenants/{merqi_tenant}/bundles/w2v/ensure", json=self.BODY
            )
        assert ensured.status_code == 200, ensured.text
        grants = db_manager.list_tenant_grants(merqi_tenant)
        by_owner = {g.grantee_owner_key: g.granted_by for g in grants}
        assert set(by_owner) == {"platform", "w2v"}  # platform: born with it
        assert by_owner["w2v"].startswith("bundle:w2v")

        with _as(db_manager, W2V, iss=PORTAL_ISSUER, tenant=merqi_tenant) as w2v:
            after = _mint(w2v, w2v_app, w2v_plan, sub="w2v-member")
        assert after.status_code == 201, after.text

    def test_non_owner_ensure_reads_as_absent(self, db_manager, fake):  # noqa: F811
        app_id, _plan = _app(db_manager, "w2v", ("w2v",))
        _bundle(db_manager, "w2v", app_id)
        fr_tenant = _tenant(db_manager, "fr", "fr-bundle")
        with _as(db_manager, MERQI, roles=["bundle-provisioner"]) as merqi:
            resp = merqi.post(f"/internal/tenants/{fr_tenant}/bundles/w2v/ensure", json=self.BODY)
        assert resp.status_code == 404, resp.text
        assert _err(resp) == "tenant_not_found"
        # only the infrastructure grant the tenant was born with
        assert [g.grantee_owner_key for g in db_manager.list_tenant_grants(fr_tenant)] == [
            "platform"
        ]

    def test_dry_run_writes_no_grant(self, db_manager, fake):  # noqa: F811
        app_id, _plan = _app(db_manager, "w2v", ("w2v",))
        _bundle(db_manager, "w2v", app_id)
        tid = _tenant(db_manager, "merqi", "merqi-dry")
        with _as(db_manager, MERQI, roles=["bundle-provisioner"]) as merqi:
            resp = merqi.post(
                f"/internal/tenants/{tid}/bundles/w2v/ensure",
                json={**self.BODY, "dry_run": True},
            )
        assert resp.status_code == 200, resp.text
        # only the infrastructure grant the tenant was born with
        assert [g.grantee_owner_key for g in db_manager.list_tenant_grants(tid)] == ["platform"]


class TestCommonPracticeEnsuresItsOwnBundles:
    """George 2026-10-14: netcare is a FRESH CommonPractice tenant whose apps arrive
    through bundles CP itself ensures — with its real client, which holds ONLY
    ``license-admin`` (no ``bundle-provisioner``, no break-glass). The bundled apps
    are issued by platform/fr, not CP: the bundle path is the owner's sanctioned
    route and licences mints, so no ``app_issuer`` membership is asked of CP."""

    BODY = {"plans": ["tenant-pooled"], "quota_override": None, "dry_run": False}
    N8N_NODES = (
        "kaizen_core_actions",
        "kaizen_discover_actions",
        "meta_prompt_core_actions",
        "meta_prompt_discover_actions",
        "metering",
        "observability",
    )
    # A slice of non-agentic-ai: fr-issued apps (single issuer -> fr is granted) and
    # the platform+fr long tail (multi-issuer -> nobody new).
    NON_AGENTIC = {"fr": ("fr",), "kag": ("fr",), "kanban": ("platform", "fr")}

    def _seed(self, db: DatabaseManager) -> None:
        def bundle(key: str, apps: dict[str, tuple[str, ...]]) -> None:
            bundle_id = db.create_bundle(key, key, ensurers=BUNDLE_ENSURER_SEED[key]).id
            session = db.get_session()
            try:
                for app_key, issuers in apps.items():
                    app = db.create_app(key=app_key, name=app_key, issuers=issuers)
                    db.create_plan(app_id=UUID(str(app.id)), code="tenant-pooled", name="TP")
                    session.add(BundleApp(bundle_id=bundle_id, app_id=app.id))
                session.commit()
            finally:
                session.close()

        bundle("n8n-nodes", {k: ("platform", "fr") for k in self.N8N_NODES})
        bundle("non-agentic-ai", self.NON_AGENTIC)

    def _register(self, db: DatabaseManager) -> str:
        tid = str(uuid.uuid7())
        with _as(db, CP, roles=["license-admin"]) as cp:
            reg = cp.put(f"/tenants/{tid}", json={"name": "netcare"})
        assert reg.status_code == 201, reg.text
        assert reg.json()["owner_key"] == "commonpractice"
        return tid

    def _live(self, db: DatabaseManager, tid: str) -> dict[str, tuple[str, str, str]]:
        with db.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT a.key, s.keycloak_sub, s.keycloak_realm, p.code "
                    "FROM license l JOIN app a ON a.id = l.app_id "
                    "JOIN subject s ON s.id = l.subject_id JOIN plan p ON p.id = l.plan_id "
                    "WHERE l.tenant_id = :t AND l.status = 'active' AND l.deleted_at IS NULL"
                ),
                {"t": tid},
            ).fetchall()
        return {r[0]: (r[1], r[2], r[3]) for r in rows}

    def test_cp_ensures_n8n_nodes_and_non_agentic_ai_idempotently(
        self,
        db_manager,
        fake,  # noqa: F811
    ):
        self._seed(db_manager)
        tid = self._register(db_manager)
        first: dict[str, dict] = {}
        with _as(db_manager, CP, roles=["license-admin"]) as cp:
            for key in ("n8n-nodes", "non-agentic-ai"):
                resp = cp.post(f"/internal/tenants/{tid}/bundles/{key}/ensure", json=self.BODY)
                assert resp.status_code == 200, resp.text
                assert resp.json()["refused"] == []
                assert {e["decision"] for e in resp.json()["apps"].values()} == {"insert"}
                first[key] = resp.json()["apps"]
        live = self._live(db_manager, tid)
        assert set(live) == {*self.N8N_NODES, *self.NON_AGENTIC}
        for app_key, (sub, realm, plan) in live.items():
            assert (sub, realm, plan) == (f"svc:{tid}:{app_key}", "service", "tenant-pooled")
        # least-privilege grants: fr for its single-issuer apps; the platform+fr
        # long tail grants nobody new (platform: born with it).
        grants = {g.grantee_owner_key for g in db_manager.list_tenant_grants(tid)}
        assert grants == {"platform", "fr"}

        with _as(db_manager, CP, roles=["license-admin"]) as cp:
            for key in ("n8n-nodes", "non-agentic-ai"):
                again = cp.post(f"/internal/tenants/{tid}/bundles/{key}/ensure", json=self.BODY)
                assert again.status_code == 200, again.text
                apps = again.json()["apps"]
                assert {e["decision"] for e in apps.values()} == {"exists"}
                assert {a: e["license_id"] for a, e in apps.items()} == {
                    a: e["license_id"] for a, e in first[key].items()
                }
        assert len(self._live(db_manager, tid)) == len(live)

    def test_a_non_owner_is_refused_and_nothing_is_written(
        self,
        db_manager,
        fake,  # noqa: F811
    ):
        self._seed(db_manager)
        tid = self._register(db_manager)
        with _as(db_manager, FR, roles=["license-admin"]) as fr:
            resp = fr.post(f"/internal/tenants/{tid}/bundles/n8n-nodes/ensure", json=self.BODY)
        assert resp.status_code == 404, resp.text
        assert _err(resp) == "tenant_not_found"
        assert self._live(db_manager, tid) == {}

    def test_a_grantee_is_refused_and_nothing_is_written(
        self,
        db_manager,
        fake,  # noqa: F811
    ):
        self._seed(db_manager)
        tid = self._register(db_manager)
        db_manager.put_tenant_grant(tid, "fr", "test")
        with _as(db_manager, FR, roles=["license-admin", "bundle-provisioner"]) as fr:
            resp = fr.post(f"/internal/tenants/{tid}/bundles/n8n-nodes/ensure", json=self.BODY)
        assert resp.status_code == 403, resp.text
        assert _err(resp) == "tenant_owner_only"
        assert self._live(db_manager, tid) == {}


class TestBundleEnsurerAllowlist:
    """Migration 052: the caller's owner must be a ``bundle_ensurer`` of the bundle.

    The hole it closes: before 052 any owner could ensure ANY bundle on its own
    tenant, and licences mints every bundled app — so fr could ensure
    ``commonpractice`` on an fr tenant and have a CommonPractice licence minted.
    """

    BODY = {"plans": ["standard"], "quota_override": None, "dry_run": False}
    BUNDLES: dict[str, dict[str, tuple[str, ...]]] = {
        "commonpractice": {"commonpractice": ("commonpractice",)},
        "non-agentic-ai": {"fr": ("fr",), "kanban": ("platform", "fr")},
        "n8n-nodes": {"metering": ("platform", "fr")},
        "w2v": {"w2v": ("w2v",)},
    }

    def _seed(self, db: DatabaseManager) -> None:
        for key, apps in self.BUNDLES.items():
            bundle_id = db.create_bundle(key, key, ensurers=BUNDLE_ENSURER_SEED[key]).id
            session = db.get_session()
            try:
                for app_key, issuers in apps.items():
                    app_id = db.get_app_by_key(app_key)
                    app_id = app_id.id if app_id is not None else _app(db, app_key, issuers)[0]
                    session.add(BundleApp(bundle_id=bundle_id, app_id=app_id))
                session.commit()
            finally:
                session.close()

    def _licences(self, db: DatabaseManager, tid: str) -> set[str]:
        with db.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT a.key FROM license l JOIN app a ON a.id = l.app_id "
                    "WHERE l.tenant_id = :t AND l.deleted_at IS NULL"
                ),
                {"t": tid},
            ).fetchall()
        return {r[0] for r in rows}

    def _holds(self, db: DatabaseManager, tid: str) -> int:
        with db.engine.connect() as conn:
            return conn.execute(
                text("SELECT count(*) FROM tenant_bundle WHERE tenant_id = :t"), {"t": tid}
            ).scalar_one()

    def test_fr_cannot_ensure_commonpractice_on_its_own_tenant(self, db_manager, fake):  # noqa: F811
        self._seed(db_manager)
        tid = _tenant(db_manager, "fr", "fr-own")
        roles = ["license-admin", "license-manager", "bundle-provisioner"]
        with _as(db_manager, FR, roles=roles) as fr:
            resp = fr.post(f"/internal/tenants/{tid}/bundles/commonpractice/ensure", json=self.BODY)
        assert resp.status_code == 403, resp.text
        assert _err(resp) == "bundle_not_ensurable_by_caller"
        assert self._licences(db_manager, tid) == set()
        assert self._holds(db_manager, tid) == 0
        # only the infrastructure grant the tenant was born with; commonpractice none
        assert [g.grantee_owner_key for g in db_manager.list_tenant_grants(tid)] == ["platform"]

    def test_cp_ensures_its_bundles_on_its_own_tenant(self, db_manager, fake):  # noqa: F811
        self._seed(db_manager)
        tid = _tenant(db_manager, "commonpractice", "cp-own")
        with _as(db_manager, CP, roles=["license-admin"]) as cp:
            for key in ("commonpractice", "non-agentic-ai", "n8n-nodes"):
                resp = cp.post(f"/internal/tenants/{tid}/bundles/{key}/ensure", json=self.BODY)
                assert resp.status_code == 200, (key, resp.text)
                assert resp.json()["refused"] == []
        assert self._licences(db_manager, tid) == {"commonpractice", "fr", "kanban", "metering"}

    def test_merqi_ensures_w2v(self, db_manager, fake):  # noqa: F811
        self._seed(db_manager)
        tid = _tenant(db_manager, "merqi", "merqi-own")
        with _as(db_manager, MERQI, roles=["bundle-provisioner", "license-manager"]) as merqi:
            resp = merqi.post(f"/internal/tenants/{tid}/bundles/w2v/ensure", json=self.BODY)
        assert resp.status_code == 200, resp.text
        assert self._licences(db_manager, tid) == {"w2v"}

    def test_an_unlisted_owner_is_refused(self, db_manager, fake):  # noqa: F811
        self._seed(db_manager)
        tid = _tenant(db_manager, "w2v", "w2v-own")
        assert "w2v" not in BUNDLE_ENSURER_SEED["non-agentic-ai"]
        with _as(db_manager, W2V, iss=PORTAL_ISSUER, roles=["license-manager"]) as w2v:
            resp = w2v.post(
                f"/internal/tenants/{tid}/bundles/non-agentic-ai/ensure", json=self.BODY
            )
        assert resp.status_code == 403, resp.text
        assert _err(resp) == "bundle_not_ensurable_by_caller"
        assert self._licences(db_manager, tid) == set()
        assert self._holds(db_manager, tid) == 0

    def test_a_bundle_with_no_ensurer_is_refused_to_every_owner(self, db_manager, fake):  # noqa: F811
        self._seed(db_manager)
        with db_manager.engine.begin() as conn:
            conn.execute(
                text(
                    "DELETE FROM bundle_ensurer WHERE bundle_id = "
                    "(SELECT id FROM bundle WHERE key = 'w2v')"
                )
            )
        tid = _tenant(db_manager, "w2v", "w2v-none")
        with _as(db_manager, W2V, iss=PORTAL_ISSUER, roles=["license-manager"]) as w2v:
            resp = w2v.post(f"/internal/tenants/{tid}/bundles/w2v/ensure", json=self.BODY)
        assert resp.status_code == 403, resp.text
        assert _err(resp) == "bundle_not_ensurable_by_caller"

    def test_break_glass_ensures_an_unlisted_bundle_and_is_audited(
        self,
        db_manager,
        fake,  # noqa: F811
        caplog,
    ):
        self._seed(db_manager)
        tid = _tenant(db_manager, "platform", "platform-own")
        assert "platform" not in BUNDLE_ENSURER_SEED["commonpractice"]
        with caplog.at_level(logging.WARNING), _as(db_manager, BREAK_GLASS) as admin:
            resp = admin.post(
                f"/internal/tenants/{tid}/bundles/commonpractice/ensure", json=self.BODY
            )
        assert resp.status_code == 200, resp.text
        assert self._licences(db_manager, tid) == {"commonpractice"}
        audit = [r.getMessage() for r in caplog.records if "cross_owner_action" in r.getMessage()]
        assert any("action=bundle_ensure_unlisted:commonpractice" in m for m in audit), audit
        assert any(f"tenant={tid}" in m for m in audit), audit

    def test_an_unknown_bundle_is_still_404(self, db_manager, fake):  # noqa: F811
        tid = _tenant(db_manager, "commonpractice", "cp-unknown")
        with _as(db_manager, CP, roles=["license-admin"]) as cp:
            resp = cp.post(f"/internal/tenants/{tid}/bundles/no-such/ensure", json=self.BODY)
        assert resp.status_code == 404, resp.text

    def test_create_bundle_requires_an_ensurer(self, db_manager):
        from models import ClientSafeError

        for ensurers in ((), [], "commonpractice"):
            with pytest.raises(ClientSafeError, match="at least one ensurer"):
                db_manager.create_bundle("lonely", "Lonely", ensurers=ensurers)
        assert db_manager.get_bundle_by_key("lonely") is None
        made = db_manager.create_bundle("paired", "Paired", ensurers=("merqi", "merqi"))
        assert db_manager.bundle_ensurers(made.id) == {"merqi"}

    def test_the_commonpractice_bundle_is_commonpractices_alone(self):
        assert BUNDLE_ENSURER_SEED["commonpractice"] == ("commonpractice",)
        assert set(BUNDLE_ENSURER_SEED["n8n-nodes"]) == {"commonpractice", "platform", "fr"}
        assert "fr" not in BUNDLE_ENSURER_SEED["w2v"]


# ---------------------------------------------------------------------------
# Migration 057: kag owns its tenants and ensures `kag-research`
# ---------------------------------------------------------------------------


class TestKagOwnsKagResearch:
    """kag-frontend separation (decision 5, R1, R8): the separated kag app signs in
    as ``secure-apps/kag-licenses-admin`` (owner kag, ``license-admin`` only, no
    break-glass), registers its own tenants, ensures ``kag-research`` on them and
    mints its own apps there. It may not mint fr / commonpractice apps, nor ensure
    ``knowledge``; fr keeps minting ``kag`` for the transition."""

    KAG_CLIENT = "kag-licenses-admin"
    ROLES = ["license-admin"]
    BODY = {"plans": ["tenant-pooled", "standard"], "quota_override": None, "dry_run": False}
    # The live issuers after 057 come from the same map the boot seed reads.
    KAG_RESEARCH = ("kag", "kag-graph", "kag-orchestrator", "inference-router")
    KNOWLEDGE = ("fr", "kag", "kag-graph", "kag-orchestrator")

    def _seed(self, db: DatabaseManager) -> dict[str, dict[str, UUID]]:
        """Apps with the post-057 issuers and the live plan codes (the primary 2026-10-15:
        tenant-pooled on the three KAG apps, ONLY standard on inference-router)."""
        from models import issuers_for_app_key

        plans: dict[str, dict[str, UUID]] = {}
        for key in (*self.KAG_RESEARCH, "fr", "commonpractice"):
            app = db.create_app(key=key, name=key, issuers=issuers_for_app_key(key))
            app_id = UUID(str(app.id))
            codes = ("standard",) if key == "inference-router" else ("tenant-pooled", "standard")
            plans[key] = {
                "app": app_id,
                **{c: UUID(str(db.create_plan(app_id=app_id, code=c, name=c).id)) for c in codes},
            }
        for bundle_key, members in (
            ("kag-research", self.KAG_RESEARCH),
            ("knowledge", self.KNOWLEDGE),
        ):
            bundle_id = db.create_bundle(
                bundle_key, bundle_key, ensurers=BUNDLE_ENSURER_SEED[bundle_key]
            ).id
            session = db.get_session()
            try:
                for key in members:
                    session.add(BundleApp(bundle_id=bundle_id, app_id=plans[key]["app"]))
                session.commit()
            finally:
                session.close()
        return plans

    def _register(self, db: DatabaseManager) -> str:
        tid = str(uuid.uuid7())
        with _as(db, self.KAG_CLIENT, roles=self.ROLES) as kag:
            reg = kag.put(f"/tenants/{tid}", json={"name": "kag-customer"})
        assert reg.status_code == 201, reg.text
        assert reg.json()["owner_key"] == "kag"
        return tid

    def _live(self, db: DatabaseManager, tid: str) -> dict[str, str]:
        with db.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT a.key, p.code FROM license l JOIN app a ON a.id = l.app_id "
                    "JOIN plan p ON p.id = l.plan_id "
                    "WHERE l.tenant_id = :t AND l.status = 'active' AND l.deleted_at IS NULL"
                ),
                {"t": tid},
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    def test_kag_ensures_kag_research_on_its_own_tenant(self, db_manager, fake):  # noqa: F811
        self._seed(db_manager)
        tid = self._register(db_manager)
        with _as(db_manager, self.KAG_CLIENT, roles=self.ROLES) as kag:
            resp = kag.post(f"/internal/tenants/{tid}/bundles/kag-research/ensure", json=self.BODY)
        assert resp.status_code == 200, resp.text
        assert resp.json()["refused"] == []
        assert self._live(db_manager, tid) == {
            "kag": "tenant-pooled",
            "kag-graph": "tenant-pooled",
            "kag-orchestrator": "tenant-pooled",
            "inference-router": "standard",
        }
        # kag owns the tenant and issues every app: nobody new is granted.
        assert {g.grantee_owner_key for g in db_manager.list_tenant_grants(tid)} == {"platform"}

    def test_kag_mints_kag_on_its_own_tenant(self, db_manager):
        plans = self._seed(db_manager)
        tid = self._register(db_manager)
        with _as(db_manager, self.KAG_CLIENT, roles=self.ROLES, tenant=tid) as kag:
            resp = _mint(kag, plans["kag"]["app"], plans["kag"]["standard"])
        assert resp.status_code == 201, resp.text
        assert resp.json()["tenant_id"] == tid

    def test_kag_cannot_mint_fr_or_commonpractice(self, db_manager):
        plans = self._seed(db_manager)
        tid = self._register(db_manager)
        for key in ("fr", "commonpractice"):
            with _as(db_manager, self.KAG_CLIENT, roles=self.ROLES, tenant=tid) as kag:
                resp = _mint(kag, plans[key]["app"], plans[key]["standard"])
            assert resp.status_code == 403, (key, resp.text)
            assert _err(resp) == "app_not_issuable_by_caller", key
        assert self._live(db_manager, tid) == {}

    def test_fr_still_mints_kag_during_the_transition(self, db_manager):
        plans = self._seed(db_manager)
        fr_tenant = _tenant(db_manager, "fr", "fr-kag-user")
        with _as(db_manager, FR, tenant=fr_tenant) as fr:
            resp = _mint(fr, plans["kag"]["app"], plans["kag"]["standard"])
        assert resp.status_code == 201, resp.text
        assert resp.json()["tenant_id"] == fr_tenant

    def test_kag_cannot_ensure_knowledge(self, db_manager, fake):  # noqa: F811
        self._seed(db_manager)
        tid = self._register(db_manager)
        assert "kag" not in BUNDLE_ENSURER_SEED["knowledge"]
        with _as(db_manager, self.KAG_CLIENT, roles=self.ROLES) as kag:
            resp = kag.post(f"/internal/tenants/{tid}/bundles/knowledge/ensure", json=self.BODY)
        assert resp.status_code == 403, resp.text
        assert _err(resp) == "bundle_not_ensurable_by_caller"
        assert self._live(db_manager, tid) == {}

    def test_kag_research_is_kags_alone(self):
        assert BUNDLE_ENSURER_SEED["kag-research"] == ("kag",)
        assert "kag" not in BUNDLE_ENSURER_SEED["research"]


# ---------------------------------------------------------------------------
# DAL: a bare name is no longer a key
# ---------------------------------------------------------------------------


class TestAmbiguousNames:
    def test_unscoped_name_lookup_refuses_to_pick(self, db_manager, caplog):
        _tenant(db_manager, "fr", "twice")
        _tenant(db_manager, "commonpractice", "twice")
        with caplog.at_level(logging.WARNING):
            assert db_manager.get_tenant("twice") is None
        assert any("tenant_name_ambiguous" in r.getMessage() for r in caplog.records)

    def test_break_glass_name_lookup_of_an_ambiguous_name_is_409(self, db_manager):
        _tenant(db_manager, "fr", "twice")
        _tenant(db_manager, "commonpractice", "twice")
        with _as(db_manager, BREAK_GLASS) as admin:
            resp = admin.get("/tenants/twice")
        assert resp.status_code == 409, resp.text
        assert _err(resp) == "tenant_name_ambiguous"

    def test_expired_grant_is_not_access(self, db_manager):
        tid = _tenant(db_manager, "fr", "fr-exp")
        db_manager.put_tenant_grant(tid, "w2v", "test")
        assert db_manager.owner_can_access_tenant("w2v", tid)
        db_manager.revoke_tenant_grant(tid, "w2v")
        assert not db_manager.owner_can_access_tenant("w2v", tid)
        # history kept, and a new grant may be issued after a revoke
        db_manager.put_tenant_grant(tid, "w2v", "test")
        w2v_rows = [
            g
            for g in db_manager.list_tenant_grants(tid, active_only=False)
            if g.grantee_owner_key == "w2v"
        ]
        assert len(w2v_rows) == 2


class TestSeedAppsDeclaresIssuers:
    def test_every_seeded_app_declares_an_issuer(self):
        from scripts.seed_apps import APPS, check_issuers

        check_issuers(APPS)
        assert all(spec["issuers"] for spec in APPS)

    def test_an_app_without_an_issuer_fails_seeding(self):
        from scripts.seed_apps import MissingIssuerError, check_issuers

        with pytest.raises(MissingIssuerError, match="no-issuer-app"):
            check_issuers([{"key": "no-issuer-app"}])

    def test_seed_writes_the_issuers(self, db_manager):
        from scripts.seed_apps import APPS, upsert_app, upsert_issuers

        spec = next(s for s in APPS if s["key"] == "commonpractice")
        app = upsert_app(db_manager, spec)
        upsert_issuers(db_manager, app, spec)
        assert db_manager.app_issuers(app.id) == {"commonpractice"}


# ---------------------------------------------------------------------------
# A GRANTEE re-registering (PUT) an id it was granted: 200, row unchanged
# ---------------------------------------------------------------------------


class TestGranteeReregister:
    """commonpractice re-registers (PUT /tenants/{id}) before EVERY mint, including
    on the shared platform tenants it holds a grant on. That must be 200 with the
    row unchanged — never a rename, never an owner change."""

    def test_grantee_put_is_200_and_unchanged(self, db_manager):
        tid = _tenant(db_manager, "platform", "shared-system")
        db_manager.put_tenant_grant(tid, "commonpractice", "test")
        with _as(db_manager, CP) as cp:
            resp = cp.put(f"/tenants/{tid}", json={"name": "shared-system"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["owner_key"] == "platform"
        assert db_manager.get_tenant(tid).owner_key == "platform"

    def test_grantee_put_with_another_name_is_200_and_never_renames(self, db_manager, caplog):
        tid = _tenant(db_manager, "platform", "system-x")
        db_manager.put_tenant_grant(tid, "commonpractice", "test")
        with caplog.at_level(logging.WARNING), _as(db_manager, CP) as cp:
            resp = cp.put(f"/tenants/{tid}", json={"name": "common-practice-demo"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == "system-x"
        stored = db_manager.get_tenant(tid)
        assert (stored.name, stored.owner_key) == ("system-x", "platform")
        assert "tenant_register_by_grantee_name_ignored" in caplog.text

    def test_non_grantee_put_is_409(self, db_manager):
        tid = _tenant(db_manager, "platform", "ungranted")
        with _as(db_manager, CP) as cp:
            resp = cp.put(f"/tenants/{tid}", json={"name": "ungranted"})
        assert resp.status_code == 409, resp.text
        assert _err(resp) == "tenant_owned_by_other_app"

    def test_a_revoked_grant_is_409_again(self, db_manager):
        tid = _tenant(db_manager, "platform", "was-granted")
        db_manager.put_tenant_grant(tid, "commonpractice", "test")
        db_manager.revoke_tenant_grant(tid, "commonpractice")
        with _as(db_manager, CP) as cp:
            resp = cp.put(f"/tenants/{tid}", json={"name": "was-granted"})
        assert resp.status_code == 409, resp.text


# ---------------------------------------------------------------------------
# Platform is infrastructure: every non-platform tenant carries a platform grant
# ---------------------------------------------------------------------------


def _active_grantees(db: DatabaseManager, tid: str) -> dict[str, str]:
    return {str(g.grantee_owner_key): str(g.granted_by) for g in db.list_tenant_grants(tid)}


class TestPlatformGrant:
    def test_post_by_another_app_grants_platform(self, db_manager):
        with _as(db_manager, FR) as fr:
            resp = fr.post("/tenants", json={"name": "fr-made"})
        assert resp.status_code == 201, resp.text
        assert _active_grantees(db_manager, resp.json()["id"]) == {
            "platform": "platform-infrastructure"
        }

    def test_put_create_by_another_app_grants_platform(self, db_manager):
        tid = str(uuid.uuid7())
        with _as(db_manager, CP) as cp:
            resp = cp.put(f"/tenants/{tid}", json={"name": "cp-made"})
        assert resp.status_code == 201, resp.text
        assert _active_grantees(db_manager, tid) == {"platform": "platform-infrastructure"}

    def test_a_platform_tenant_gets_no_self_grant(self, db_manager):
        with _as(db_manager, BREAK_GLASS) as admin:
            resp = admin.post("/tenants", json={"name": "platform-made"})
        assert resp.status_code == 201, resp.text
        assert resp.json()["owner_key"] == "platform"
        assert _active_grantees(db_manager, resp.json()["id"]) == {}

    def test_portal_bff_reads_a_cp_created_tenant(self, db_manager):
        tid = str(uuid.uuid7())
        with _as(db_manager, CP) as cp:
            assert cp.put(f"/tenants/{tid}", json={"name": "cp-portal"}).status_code == 201
        with _as(db_manager, "portal-bff-service", iss=PORTAL_ISSUER) as bff:
            resp = bff.get(f"/tenants/{tid}")
        assert resp.status_code == 200, resp.text

    def test_grant_and_tenant_commit_together(self, db_manager):
        """An unknown owner refuses BEFORE anything is written: no tenant, no grant."""
        from models import ClientSafeError

        tid = str(uuid.uuid7())
        with pytest.raises(ClientSafeError, match="unknown tenant owner"):
            db_manager.ensure_tenant(tid, name="no-such-owner", owner_key="nobody")
        assert db_manager.get_tenant(tid) is None


# ---------------------------------------------------------------------------
# No silent owner default; no app without an issuer
# ---------------------------------------------------------------------------


class TestNoSilentDefaults:
    def test_tenant_primitives_require_an_owner(self, db_manager):
        with pytest.raises(TypeError):
            db_manager.create_tenant(name="ownerless")  # type: ignore[call-arg]
        with pytest.raises(TypeError):
            db_manager.ensure_tenant(str(uuid.uuid7()), name="ownerless")  # type: ignore[call-arg]

    def test_the_db_has_no_owner_default(self, db_manager):
        with db_manager.engine.begin() as conn:
            default = conn.execute(
                text(
                    "SELECT column_default FROM information_schema.columns "
                    "WHERE table_name = 'tenants' AND column_name = 'owner_key'"
                )
            ).scalar()
        assert default is None

    def test_create_app_requires_an_issuer(self, db_manager):
        from models import ClientSafeError

        with pytest.raises(TypeError):
            db_manager.create_app(key="no-issuer", name="x")  # type: ignore[call-arg]
        with pytest.raises(ClientSafeError, match="at least one issuer"):
            db_manager.create_app(key="no-issuer", name="x", issuers=())
        assert db_manager.get_app_by_key("no-issuer") is None
        app = db_manager.create_app(key="two-issuers", name="x", issuers=("fr", "w2v"))
        assert db_manager.app_issuers(app.id) == {"fr", "w2v"}

    def test_post_apps_defaults_issuer_to_the_callers_owner(self, db_manager):
        with _as(db_manager, FR) as fr:
            mine = fr.post("/apps", json={"key": "fr-new-app", "name": "x"})
            own = fr.post("/apps", json={"key": "own-issuer", "name": "x", "issuers": ["fr"]})
        with _as(db_manager, BREAK_GLASS) as admin:
            named = admin.post(
                "/apps", json={"key": "named-issuers", "name": "x", "issuers": ["w2v"]}
            )
            bad = admin.post("/apps", json={"key": "bad-issuer", "name": "x", "issuers": ["nope"]})
        assert mine.status_code == 201, mine.text
        assert db_manager.app_issuers(UUID(mine.json()["id"])) == {"fr"}
        assert own.status_code == 201, own.text
        assert db_manager.app_issuers(UUID(own.json()["id"])) == {"fr"}
        assert named.status_code == 201, named.text
        assert db_manager.app_issuers(UUID(named.json()["id"])) == {"w2v"}
        assert bad.status_code == 400, bad.text
        assert db_manager.get_app_by_key("bad-issuer") is None

    @pytest.mark.parametrize("issuers", [["w2v"], ["fr", "w2v"], ["nope"]])
    def test_post_apps_refuses_issuer_squatting(self, db_manager, issuers):
        """A non-cross_owner caller may name only its OWN owner as issuer."""
        with _as(db_manager, FR) as fr:
            resp = fr.post("/apps", json={"key": "squat", "name": "x", "issuers": issuers})
        assert resp.status_code == 403, resp.text
        assert _err(resp) == "issuer_not_caller_owner"
        assert db_manager.get_app_by_key("squat") is None

    def test_post_apps_foreign_issuer_by_break_glass_is_audited(self, db_manager, caplog):
        with caplog.at_level(logging.WARNING), _as(db_manager, BREAK_GLASS) as admin:
            resp = admin.post("/apps", json={"key": "bg-app", "name": "x", "issuers": ["merqi"]})
        assert resp.status_code == 201, resp.text
        audit = [r.getMessage() for r in caplog.records if "cross_owner_action" in r.getMessage()]
        assert any("action=app_create_foreign_issuers" in m and "bg-app" in m for m in audit)

    def test_unknown_client_naming_an_issuer_is_403(self, db_manager):
        with _as(db_manager, UNKNOWN) as unknown:
            resp = unknown.post("/apps", json={"key": "u-app", "name": "x", "issuers": ["fr"]})
        assert resp.status_code == 403, resp.text
        assert _err(resp) == "unknown_api_client"

    @pytest.mark.parametrize(
        "module,argv",
        [
            (
                "scripts.provision_fr_tenant",
                ["--tenant-id", "x", "--tenant-name", "x", "--owner-sub", "s"],
            ),
            (
                "scripts.provision_w2v_tenant",
                ["--tenant-id", "x", "--tenant-name", "x", "--owner-sub", "s"],
            ),
            (
                "scripts.provision_kag_tenant",
                ["--tenant-id", "x", "--tenant-name", "x", "--owner-sub", "s"],
            ),
            ("scripts.provision_service_token", ["--subject", "svc-x"]),
            ("scripts.provision_demo_tenant", ["--owner-sub", "s", "--apps", "kag"]),
        ],
    )
    def test_operator_scripts_require_tenant_owner(self, module, argv, capsys):
        import importlib

        mod = importlib.import_module(module)
        with pytest.raises(SystemExit) as exc:
            mod._parse_args(argv)
        assert exc.value.code == 2
        assert "--tenant-owner" in capsys.readouterr().err

    def test_resolve_orphans_refuses_to_register_without_an_owner(self, monkeypatch, capsys):
        from scripts import resolve_orphan_tenants as rot

        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://unused@localhost:1/x")
        assert rot.main(["--register", f"x={uuid.uuid4()}"]) == 2
        assert "--tenant-owner is required" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Per-APP tenant settings: a grantee may set them for an app it ISSUES
# ---------------------------------------------------------------------------


class TestPerAppSettings:
    """commonpractice sets its OWN app's BYO posture (``setTenantByoMode`` ->
    ``PUT /tenants/{id}/apps/commonpractice/byo-mode``) on shared platform
    tenants it only holds a grant on. Tenant-WIDE settings stay owner-only
    (``TestOwnerOnly``)."""

    BYO = {"byo_mode": "platform_only"}

    def _setup(self, db: DatabaseManager) -> str:
        _app(db, "commonpractice", ("commonpractice",))
        _app(db, "w2v", ("w2v",))
        tid = _tenant(db, "platform", "shared-per-app")
        db.put_tenant_grant(tid, "commonpractice", "test")
        return tid

    def test_grantee_issuer_sets_and_clears_its_apps_byo(self, db_manager):
        tid = self._setup(db_manager)
        with _as(db_manager, CP) as cp:
            put = cp.put(f"/tenants/{tid}/apps/commonpractice/byo-mode", json=self.BYO)
            cleared = cp.delete(f"/tenants/{tid}/apps/commonpractice/byo-mode")
            unprov = cp.delete(f"/tenants/{tid}/offerings/commonpractice/standard")
        assert put.status_code == 200, put.text
        assert put.json()["byo_mode"] == "platform_only"
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["cleared"] is True
        assert unprov.status_code == 200, unprov.text

    def test_grantee_non_issuer_is_403(self, db_manager):
        tid = self._setup(db_manager)
        with _as(db_manager, CP) as cp:
            put = cp.put(f"/tenants/{tid}/apps/w2v/byo-mode", json=self.BYO)
            clear = cp.delete(f"/tenants/{tid}/apps/w2v/byo-mode")
            ghost = cp.put(f"/tenants/{tid}/apps/no-such-app/byo-mode", json=self.BYO)
            prov = cp.post(f"/tenants/{tid}/offerings/w2v/standard")
        for what, resp in {"put": put, "clear": clear, "ghost": ghost, "prov": prov}.items():
            assert resp.status_code == 403, (what, resp.text)
            assert _err(resp) == "app_not_issuable_by_caller", what
        assert db_manager.get_tenant_app_byo_mode(tid, "w2v") is None

    def test_non_grantee_is_403(self, db_manager):
        tid = self._setup(db_manager)
        with _as(db_manager, W2V, iss=PORTAL_ISSUER) as w2v:
            put = w2v.put(f"/tenants/{tid}/apps/w2v/byo-mode", json=self.BYO)
            unprov = w2v.delete(f"/tenants/{tid}/offerings/w2v/standard")
        for what, resp in {"put": put, "unprov": unprov}.items():
            assert resp.status_code == 403, (what, resp.text)
            assert _err(resp) == "tenant_not_accessible", what
        assert db_manager.get_tenant_app_byo_mode(tid, "w2v") is None

    def test_grantee_by_name_is_404_and_owner_sets_an_app_it_issues(self, db_manager):
        _app(db_manager, "w2v", ("w2v",))
        _app(db_manager, "kag", ("fr",))
        tid = _tenant(db_manager, "fr", "fr-per-app")
        db_manager.put_tenant_grant(tid, "w2v", "test")
        with _as(db_manager, W2V, iss=PORTAL_ISSUER) as w2v:
            by_name = w2v.put("/tenants/fr-per-app/apps/w2v/byo-mode", json=self.BYO)
        assert by_name.status_code == 404, by_name.text
        with _as(db_manager, FR) as fr:
            own = fr.put("/tenants/fr-per-app/apps/kag/byo-mode", json=self.BYO)
            # Owning the tenant is not owning the app (test_app_setting_ownership.py).
            foreign = fr.put("/tenants/fr-per-app/apps/w2v/byo-mode", json=self.BYO)
        assert own.status_code == 200, own.text
        assert foreign.status_code == 403, foreign.text
        assert _err(foreign) == "app_not_administrable_by_caller"

    def test_tenant_wide_byo_stays_owner_only_for_an_issuing_grantee(self, db_manager):
        tid = self._setup(db_manager)
        with _as(db_manager, CP) as cp:
            resp = cp.patch(f"/tenants/{tid}/byo-mode", json=self.BYO)
        assert resp.status_code == 403, resp.text
        assert _err(resp) == "tenant_owner_only"


# ---------------------------------------------------------------------------
# Bundle ensure grants LEAST privilege
# ---------------------------------------------------------------------------


class TestBundleGrantees:
    @pytest.mark.parametrize(
        "owner,issuers,expected",
        [
            ("merqi", {"w2v"}, {"w2v"}),  # single issuer
            ("w2v", {"w2v"}, set()),  # owner issues it
            ("merqi", {"fr", "commonpractice"}, set()),  # multi, no platform
            ("merqi", {"platform", "fr"}, {"platform"}),  # multi -> platform only
            ("fr", {"platform", "fr"}, set()),  # owner is one of several issuers
            ("platform", {"platform", "fr"}, set()),
        ],
    )
    def test_rule(self, owner, issuers, expected):
        from models import bundle_grantees

        assert bundle_grantees(owner, issuers) == expected

    def _bundle_of(self, db: DatabaseManager, key: str, apps: dict[str, tuple[str, ...]]) -> UUID:
        with db.engine.begin() as conn:
            bundle_id = conn.execute(
                text("INSERT INTO bundle (key, name) VALUES (:k, :k) RETURNING id"), {"k": key}
            ).scalar_one()
        session = db.get_session()
        try:
            for app_key, issuers in apps.items():
                app_id, _plan = _app(db, app_key, issuers)
                session.add(BundleApp(bundle_id=bundle_id, app_id=app_id))
            session.commit()
        finally:
            session.close()
        return UUID(str(bundle_id))

    def test_inference_router_grants_neither_fr_nor_commonpractice(self, db_manager):
        bundle = self._bundle_of(
            db_manager,
            "ir-bundle",
            {"inference-router": ("fr", "commonpractice"), "w2v": ("w2v",)},
        )
        tid = _tenant(db_manager, "merqi", "merqi-ir")
        created = db_manager.ensure_bundle_grants(tid, bundle, "test", hold=True)
        assert created == ["w2v"]
        assert set(_active_grantees(db_manager, tid)) == {"platform", "w2v"}

    def test_owner_issuer_of_a_bundled_app_grants_nobody_for_it(self, db_manager):
        bundle = self._bundle_of(
            db_manager,
            "fr-bundle",
            {"kag": ("fr",), "workers": ("platform", "fr")},
        )
        tid = _tenant(db_manager, "fr", "fr-own-bundle")
        created = db_manager.ensure_bundle_grants(tid, bundle, "test", hold=True)
        assert created == []
        assert set(_active_grantees(db_manager, tid)) == {"platform"}  # born with it

    def test_multi_issuer_with_platform_on_a_platform_free_tenant(self, db_manager):
        bundle = self._bundle_of(db_manager, "long-tail", {"workers": ("platform", "fr")})
        tid = _tenant(db_manager, "commonpractice", "cp-long-tail")
        db_manager.revoke_tenant_grant(tid, "platform")
        created = db_manager.ensure_bundle_grants(tid, bundle, "test", hold=True)
        assert created == ["platform"]
        assert set(_active_grantees(db_manager, tid)) == {"platform"}


# ---------------------------------------------------------------------------
# The owner-scoped writes admit license-manager-only owners
# ---------------------------------------------------------------------------

#: The REAL realm-role sets from the Keycloak inventory (plan, step 0). The rest of
#: this file signs every client with WRITE_ROLES (all three), which is exactly what
#: hid the 403 "Role 'license-admin' required" merqi and w2v hit on every registration.
MERQI_ROLES = ["bundle-provisioner", "license-manager"]
W2V_ROLES = ["license-manager"]
FR_PORTAL_ROLES = ["license-manager"]


class TestLicenseManagerOwners:
    def test_manager_only_owners_register_their_own_tenants(self, db_manager):
        merqi_tid = str(uuid.uuid4())
        w2v_tid = str(uuid.uuid4())
        with _as(db_manager, MERQI, roles=MERQI_ROLES) as merqi:
            put = merqi.put(f"/tenants/{merqi_tid}", json={"name": "merqi-own"})
            post = merqi.post("/tenants", json={"name": "merqi-posted"})
        with _as(db_manager, W2V, iss=PORTAL_ISSUER, roles=W2V_ROLES) as w2v:
            w_put = w2v.put(f"/tenants/{w2v_tid}", json={"name": "w2v-own"})
            w_post = w2v.post("/tenants", json={"name": "w2v-posted"})
        for what, resp in {"put": put, "post": post, "w_put": w_put, "w_post": w_post}.items():
            assert resp.status_code == 201, (what, resp.text)
        assert db_manager.get_tenant(merqi_tid).owner_key == "merqi"
        assert db_manager.get_tenant(w2v_tid).owner_key == "w2v"
        assert post.json()["owner_key"] == "merqi"
        assert w_post.json()["owner_key"] == "w2v"

    def test_cross_owner_is_still_refused_for_a_manager(self, db_manager):
        tid = str(uuid.uuid4())
        with _as(db_manager, MERQI, roles=MERQI_ROLES) as merqi:
            assert merqi.put(f"/tenants/{tid}", json={"name": "merqi-held"}).status_code == 201
        with _as(db_manager, W2V, iss=PORTAL_ISSUER, roles=W2V_ROLES) as w2v:
            put = w2v.put(f"/tenants/{tid}", json={"name": "w2v-steal"})
            grant = w2v.put(f"/tenants/{tid}/grants/w2v")
            byo = w2v.put(f"/tenants/{tid}/apps/w2v/byo-mode", json={"byo_mode": "platform_only"})
        assert put.status_code == 409, put.text
        assert _err(put) == "tenant_owned_by_other_app"
        assert grant.status_code == 404, grant.text
        assert byo.status_code == 403, byo.text
        assert db_manager.get_tenant(tid).name == "merqi-held"

    def test_manager_only_owner_grants_lists_and_revokes(self, db_manager):
        tid = str(uuid.uuid4())
        with _as(db_manager, MERQI, roles=MERQI_ROLES) as merqi:
            merqi.put(f"/tenants/{tid}", json={"name": "merqi-granting"})
            put = merqi.put(f"/tenants/{tid}/grants/w2v")
            listed = merqi.get(f"/tenants/{tid}/grants")
            deleted = merqi.delete(f"/tenants/{tid}/grants/w2v")
        assert put.status_code == 201, put.text
        assert listed.status_code == 200, listed.text
        assert "w2v" in {g["grantee_owner_key"] for g in listed.json()}
        assert deleted.status_code == 204, deleted.text

    def test_manager_only_issuing_grantee_sets_its_apps_byo(self, db_manager):
        _app(db_manager, "w2v", ("w2v",))
        tid = _tenant(db_manager, "merqi", "merqi-w2v-byo")
        db_manager.put_tenant_grant(tid, "w2v", "test")
        with _as(db_manager, W2V, iss=PORTAL_ISSUER, roles=W2V_ROLES) as w2v:
            put = w2v.put(f"/tenants/{tid}/apps/w2v/byo-mode", json={"byo_mode": "platform_only"})
            clear = w2v.delete(f"/tenants/{tid}/apps/w2v/byo-mode")
        assert put.status_code == 200, put.text
        assert clear.status_code == 200, clear.text

    def test_reader_only_is_still_missing_role(self, db_manager):
        with _as(db_manager, FR, iss=PORTAL_ISSUER, roles=["license-reader"]) as fr:
            resp = fr.post("/tenants", json={"name": "reader-cannot-create"})
        assert resp.status_code == 403, resp.text

    def test_manager_only_owner_runs_every_tenant_wide_write(self, db_manager):
        """Rename, archive/unarchive, tenant BYO, cost centre, budget currency: an
        owner whose client holds ONLY license-manager manages its own tenant."""
        with _as(db_manager, FR, iss=PORTAL_ISSUER, roles=FR_PORTAL_ROLES) as fr:
            created = fr.post("/tenants", json={"name": "fr-portal-own"})
            assert created.status_code == 201, created.text
            tid = created.json()["id"]
            calls = {
                "rename": fr.patch(f"/tenants/{tid}", json={"name": "fr-renamed"}),
                "byo": fr.patch(f"/tenants/{tid}/byo-mode", json={"byo_mode": "platform_only"}),
                "cost_centre": fr.patch(
                    f"/tenants/{tid}/cost-centre", json={"cost_centre": "service"}
                ),
                "currency": fr.put(
                    f"/tenants/{tid}/budget-currency", json={"budget_currency": "EUR"}
                ),
                "archive": fr.patch(f"/tenants/{tid}/archive", json={"archived": True}),
            }
            archived = db_manager.get_tenant(tid).archived_at
            unarchive = fr.patch(f"/tenants/{tid}/archive", json={"archived": False})
        for what, resp in {**calls, "unarchive": unarchive}.items():
            assert resp.status_code == 200, (what, resp.text)
        assert archived is not None
        t = db_manager.get_tenant(tid)
        assert (t.name, t.byo_mode, t.cost_centre, t.budget_currency, t.archived_at) == (
            "fr-renamed",
            "platform_only",
            "service",
            "EUR",
            None,
        )

    def test_merqi_and_w2v_managers_rename_and_archive_their_own(self, db_manager):
        merqi_tid = _tenant(db_manager, "merqi", "merqi-wide")
        w2v_tid = _tenant(db_manager, "w2v", "w2v-wide")
        with _as(db_manager, MERQI, roles=MERQI_ROLES) as merqi:
            m_ren = merqi.patch(f"/tenants/{merqi_tid}", json={"name": "merqi-wide-2"})
            m_arc = merqi.patch(f"/tenants/{merqi_tid}/archive", json={"archived": True})
        with _as(db_manager, W2V, iss=PORTAL_ISSUER, roles=W2V_ROLES) as w2v:
            w_ren = w2v.patch(f"/tenants/{w2v_tid}", json={"name": "w2v-wide-2"})
            w_arc = w2v.patch(f"/tenants/{w2v_tid}/archive", json={"archived": True})
        for what, resp in {"m_ren": m_ren, "m_arc": m_arc, "w_ren": w_ren, "w_arc": w_arc}.items():
            assert resp.status_code == 200, (what, resp.text)
        assert db_manager.get_tenant(merqi_tid).name == "merqi-wide-2"
        assert db_manager.get_tenant(w2v_tid).archived_at is not None

    def test_manager_only_grantee_is_tenant_owner_only(self, db_manager):
        tid = _tenant(db_manager, "merqi", "merqi-granted")
        db_manager.put_tenant_grant(tid, "w2v", "test")
        with _as(db_manager, W2V, iss=PORTAL_ISSUER, roles=W2V_ROLES) as w2v:
            calls = {
                "rename": w2v.patch(f"/tenants/{tid}", json={"name": "w2v-stole"}),
                "archive": w2v.patch(f"/tenants/{tid}/archive", json={"archived": True}),
                "byo": w2v.patch(f"/tenants/{tid}/byo-mode", json={"byo_mode": "platform_only"}),
                "cost_centre": w2v.patch(
                    f"/tenants/{tid}/cost-centre", json={"cost_centre": "service"}
                ),
                "currency": w2v.put(
                    f"/tenants/{tid}/budget-currency", json={"budget_currency": "EUR"}
                ),
            }
        for what, resp in calls.items():
            assert resp.status_code == 403, (what, resp.text)
            assert _err(resp) == "tenant_owner_only", (what, resp.text)
        t = db_manager.get_tenant(tid)
        assert (t.name, t.archived_at, t.budget_currency) == ("merqi-granted", None, None)

    def test_manager_only_non_owner_is_refused(self, db_manager):
        tid = _tenant(db_manager, "merqi", "merqi-private")
        with _as(db_manager, W2V, iss=PORTAL_ISSUER, roles=W2V_ROLES) as w2v:
            rename = w2v.patch(f"/tenants/{tid}", json={"name": "w2v-stole"})
            by_name = w2v.patch("/tenants/merqi-private", json={"name": "w2v-stole"})
            archive = w2v.patch(f"/tenants/{tid}/archive", json={"archived": True})
        for what, resp in {"rename": rename, "by_name": by_name, "archive": archive}.items():
            assert resp.status_code == 404, (what, resp.text)
        t = db_manager.get_tenant(tid)
        assert (t.name, t.archived_at) == ("merqi-private", None)

    def test_reader_only_owner_cannot_run_tenant_wide_writes(self, db_manager):
        tid = _tenant(db_manager, "fr", "fr-reader-held")
        with _as(db_manager, FR, iss=PORTAL_ISSUER, roles=["license-reader"]) as fr:
            calls = {
                "rename": fr.patch(f"/tenants/{tid}", json={"name": "fr-reader-renamed"}),
                "archive": fr.patch(f"/tenants/{tid}/archive", json={"archived": True}),
                "byo": fr.patch(f"/tenants/{tid}/byo-mode", json={"byo_mode": "platform_only"}),
                "cost_centre": fr.patch(
                    f"/tenants/{tid}/cost-centre", json={"cost_centre": "service"}
                ),
                "currency": fr.put(
                    f"/tenants/{tid}/budget-currency", json={"budget_currency": "EUR"}
                ),
            }
        for what, resp in calls.items():
            assert resp.status_code == 403, (what, resp.text)
            assert _err(resp) != "tenant_owner_only", what
        t = db_manager.get_tenant(tid)
        assert (t.name, t.archived_at) == ("fr-reader-held", None)

    def test_cross_owner_manager_wide_write_still_works(self, db_manager):
        tid = _tenant(db_manager, "fr", "fr-bg-manager")
        with _as(db_manager, BREAK_GLASS, roles=["license-manager"]) as admin:
            resp = admin.patch(f"/tenants/{tid}/archive", json={"archived": True})
        assert resp.status_code == 200, resp.text
        assert db_manager.get_tenant(tid).archived_at is not None
