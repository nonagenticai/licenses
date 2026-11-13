"""``POST /internal/tenants/{tenant_id}/bundle-token`` — the server-side bundle resolver.

WHY. TENANCY_AND_IMPROVEMENT.md §5 (n8n_custom_nodes): every call carries a token resolved
server-side, failing closed. Ruling 2026-09-28: the bundle is the provisioning/billing
object; runtime presents the SPECIFIC per-app licence. So the route takes a REQUIRED
``app`` and mints a short-lived token on the tenant's per-app licence carrying the bundle.

WHAT IS PINNED HERE.
* Happy path: the token introspects to THAT app's licence and tenant, never other apps.
* Fail closed, nothing minted: 404 ``no_bundle`` / ``app_not_in_bundle`` /
  ``no_app_licence`` (distinct codes), 422 for a missing ``app``, 409 for ambiguity.
* The app-in-bundle check is load-bearing: an app REMOVED from the bundle whose licence
  still carries the bundle id must not mint (mutation-checked).
* 403 for any caller without the dedicated ``bundle-token-minter`` realm role — the
  fleet-wide ``service-internal``, ``onboarding-internal`` and ``license-admin`` included
  — and a licence token, so a minted token cannot mint another.
* TTL capped at 60 min, and never past the licence's own expiry.
* Every mint is logged at WARNING as ``bundle_token_minted``.
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from auth.models import KeycloakUser
from models import BundleApp, DatabaseManager
from tests.conftest import SEED_TENANT_ID, _make_authenticated_client
from tests.unit.test_bundle_licence import (
    BUNDLE_KEY,
    _seed_bundle,
    provision_tenant_bundle,
)

TENANT = SEED_TENANT_ID
OTHER_TENANT = "01000000-0000-7000-8000-0000000000b2"
BASE = f"/internal/tenants/{TENANT}/bundle-token"
URL = f"{BASE}?app=kanban-t"


def _kc_user(sub: str, roles: list[str]) -> KeycloakUser:
    return KeycloakUser(
        sub=sub,
        preferred_username=f"svc-{sub}",
        email=f"{sub}@test.com",
        realm_access={"roles": roles},
        resource_access={},
        iss="https://keycloak.example.com/realms/service",
    )


@pytest.fixture
def internal_client(db_manager) -> Generator[TestClient]:
    """The resolver's caller: holds ONLY the dedicated minter role."""
    yield from _make_authenticated_client(
        db_manager, _kc_user("resolver-sa", ["bundle-token-minter"]), TENANT
    )


def _role_client(db_manager, roles: list[str]) -> Generator[TestClient]:
    yield from _make_authenticated_client(db_manager, _kc_user("other-sa", roles), TENANT)


@pytest.fixture
def reader_client(db_manager) -> Generator[TestClient]:
    yield from _make_authenticated_client(
        db_manager, _kc_user("reader-sa", ["license-reader"]), TENANT
    )


def _provision(db: DatabaseManager) -> None:
    """Give TENANT the test bundle via the real provisioning script (one licence per app)."""
    db.ensure_tenant(TENANT, name=f"test-tenant-{TENANT[:8]}", owner_key="platform")
    assert (
        provision_tenant_bundle.main(["--tenant", TENANT, "--bundle", BUNDLE_KEY, "--apply"], db=db)
        == 0
    )


def _token_count(db: DatabaseManager) -> int:
    with db.engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM token")).scalar_one()


def _introspect(client: TestClient, raw: str) -> dict:
    resp = client.post("/license/introspect", json={"token": raw})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _app_licence_id(db: DatabaseManager, app_key: str) -> str:
    with db.engine.connect() as conn:
        return str(
            conn.execute(
                text(
                    "SELECT l.id FROM license l JOIN app a ON a.id = l.app_id "
                    "WHERE l.tenant_id = :t AND a.key = :k AND l.bundle_id IS NOT NULL"
                ),
                {"t": TENANT, "k": app_key},
            ).scalar_one()
        )


class TestHappyPath:
    def test_mint_introspects_to_that_apps_licence_and_tenant(
        self, db_manager, internal_client, caplog
    ) -> None:
        _seed_bundle(db_manager)
        _provision(db_manager)
        before = _token_count(db_manager)

        with caplog.at_level(logging.WARNING, logger="main"):
            resp = internal_client.post(URL)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["tenant_id"] == TENANT
        assert body["bundle"] == BUNDLE_KEY
        assert body["app"] == "kanban-t"
        assert body["license_id"] == _app_licence_id(db_manager, "kanban-t")
        assert "apps" not in body
        assert _token_count(db_manager) == before + 1

        info = _introspect(internal_client, body["token"])
        assert info["active"] is True
        assert info["tenant_id"] == TENANT
        assert info["license_id"] == body["license_id"]
        assert info["app"]["key"] == "kanban-t"
        assert "apps" not in info, "introspection never returns the bundle's other apps"
        assert info["roles"] == []

        audit = [r for r in caplog.records if "bundle_token_minted" in r.getMessage()]
        assert len(audit) == 1, [r.getMessage() for r in caplog.records]
        assert audit[0].levelno == logging.WARNING
        msg = audit[0].getMessage()
        assert "actor_sub=resolver-sa" in msg
        assert f"tenant_id={TENANT}" in msg
        assert "app=kanban-t" in msg
        assert f"token_id={body['token_id']}" in msg
        assert body["token"] not in msg, "the raw token must never reach a log line"

    def test_each_app_gets_its_own_licence(self, db_manager, internal_client) -> None:
        _seed_bundle(db_manager)
        _provision(db_manager)
        a = internal_client.post(f"{BASE}?app=alpha-app").json()
        z = internal_client.post(f"{BASE}?app=zeta-app").json()
        assert a["license_id"] != z["license_id"]
        assert _introspect(internal_client, a["token"])["app"]["key"] == "alpha-app"
        assert _introspect(internal_client, z["token"])["app"]["key"] == "zeta-app"


class TestTtl:
    def test_default_is_15_minutes(self, db_manager, internal_client) -> None:
        _seed_bundle(db_manager)
        _provision(db_manager)
        body = internal_client.post(URL).json()
        left = datetime.fromisoformat(body["expires_at"]) - datetime.now(UTC)
        assert timedelta(minutes=14) < left <= timedelta(minutes=15)

    def test_60_is_the_ceiling(self, db_manager, internal_client) -> None:
        _seed_bundle(db_manager)
        _provision(db_manager)
        ok = internal_client.post(f"{URL}&ttl_minutes=60")
        assert ok.status_code == 201, ok.text
        left = datetime.fromisoformat(ok.json()["expires_at"]) - datetime.now(UTC)
        assert timedelta(minutes=59) < left <= timedelta(minutes=60)
        before = _token_count(db_manager)
        assert internal_client.post(f"{URL}&ttl_minutes=61").status_code == 422
        assert internal_client.post(f"{URL}&ttl_minutes=0").status_code == 422
        assert _token_count(db_manager) == before

    def test_never_outlives_the_licence(self, db_manager, internal_client) -> None:
        bundle_id, _ = _seed_bundle(db_manager)
        _provision(db_manager)
        lic_end = datetime.now(UTC) + timedelta(minutes=5)
        with db_manager.engine.begin() as conn:
            conn.execute(
                text("UPDATE license SET expires_at = :e WHERE bundle_id = :b"),
                {"e": lic_end, "b": bundle_id},
            )
        body = internal_client.post(f"{URL}&ttl_minutes=60").json()
        assert datetime.fromisoformat(body["expires_at"]) <= lic_end


class TestFailsClosed:
    def _refused(self, db, resp, code: int, error: str) -> dict:
        assert resp.status_code == code, resp.text
        detail = resp.json()["detail"]
        assert detail["error"] == error, detail
        return detail

    def test_missing_app_is_422(self, db_manager, internal_client) -> None:
        _seed_bundle(db_manager)
        _provision(db_manager)
        before = _token_count(db_manager)
        assert internal_client.post(BASE).status_code == 422
        assert internal_client.post(f"{BASE}?app=").status_code == 422
        assert _token_count(db_manager) == before

    def test_app_not_in_the_bundle_is_404(self, db_manager, internal_client) -> None:
        _seed_bundle(db_manager)
        _provision(db_manager)
        db_manager.create_app(key="outsider-app", name="outsider-app", issuers=("platform",))
        before = _token_count(db_manager)
        detail = self._refused(
            db_manager,
            internal_client.post(f"{BASE}?app=outsider-app"),
            404,
            "app_not_in_bundle",
        )
        assert detail["bundles"] == [BUNDLE_KEY]
        assert _token_count(db_manager) == before

    def test_app_removed_from_the_bundle_does_not_mint(self, db_manager, internal_client) -> None:
        """The licence still carries bundle_id, but the app left the bundle.

        ONLY the app-in-bundle check stops this: the per-app licence lookup alone
        would find the (still bundle-tagged) licence and mint. Mutation target.
        """
        bundle_id, apps = _seed_bundle(db_manager)
        _provision(db_manager)
        with db_manager.engine.begin() as conn:
            conn.execute(
                text("DELETE FROM bundle_app WHERE bundle_id = :b AND app_id = :a"),
                {"b": bundle_id, "a": apps["kanban-t"]},
            )
        before = _token_count(db_manager)
        self._refused(db_manager, internal_client.post(URL), 404, "app_not_in_bundle")
        assert _token_count(db_manager) == before

    def test_app_in_bundle_without_a_licence_is_404(self, db_manager, internal_client) -> None:
        bundle_id, _ = _seed_bundle(db_manager)
        _provision(db_manager)
        with db_manager.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE license SET status = 'revoked' WHERE bundle_id = :b AND app_id = "
                    "(SELECT id FROM app WHERE key = 'kanban-t')"
                ),
                {"b": bundle_id},
            )
        before = _token_count(db_manager)
        detail = self._refused(db_manager, internal_client.post(URL), 404, "no_app_licence")
        assert detail["bundle"] == BUNDLE_KEY
        assert _token_count(db_manager) == before
        assert internal_client.post(f"{BASE}?app=alpha-app").status_code == 201, "siblings mint"

    def test_tenant_without_a_bundle_is_404(self, db_manager, internal_client) -> None:
        _seed_bundle(db_manager)  # the bundle exists; the tenant just does not hold it
        before = _token_count(db_manager)
        detail = self._refused(db_manager, internal_client.post(URL), 404, "no_bundle")
        assert detail["reason"] == "no_bundle_held"
        assert _token_count(db_manager) == before

    def test_unknown_tenant_is_404(self, db_manager, internal_client) -> None:
        detail = self._refused(
            db_manager,
            internal_client.post(
                "/internal/tenants/01000000-0000-7000-8000-00000000dead/bundle-token?app=kanban-t"
            ),
            404,
            "no_bundle",
        )
        assert detail["reason"] == "tenant_not_found"

    def test_non_uuid_tenant_is_400(self, internal_client) -> None:
        resp = internal_client.post("/internal/tenants/not-a-uuid/bundle-token?app=kanban-t")
        assert resp.status_code == 400

    def test_an_app_key_too_long_for_a_service_subject_is_400(
        self, db_manager, internal_client
    ) -> None:
        from models import SERVICE_SUBJECT_APP_KEY_MAX_LEN

        _seed_bundle(db_manager)
        _provision(db_manager)
        before = _token_count(db_manager)
        resp = internal_client.post(f"{BASE}?app={'z' * (SERVICE_SUBJECT_APP_KEY_MAX_LEN + 1)}")
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"]["error"] == "app_key_too_long"
        assert (
            f"allows at most {SERVICE_SUBJECT_APP_KEY_MAX_LEN}" in resp.json()["detail"]["reason"]
        )
        assert _token_count(db_manager) == before

    def test_a_member_licence_beside_the_bundle_does_not_make_it_ambiguous(
        self, db_manager, internal_client
    ) -> None:
        """provision_tenant_bundle never adopts a member's licence, so the bundle holds one."""
        _bundle_id, apps = _seed_bundle(db_manager)
        subj = db_manager.create_or_get_subject(
            keycloak_sub="44444444-dddd-4ddd-8ddd-444444444444", keycloak_realm="portal"
        )
        plan = [p for p in db_manager.list_plans(apps["kanban-t"]) if p.code == "standard"][0]
        member = db_manager.create_license(
            subject_id=UUID(str(subj.id)),
            app_id=apps["kanban-t"],
            plan_id=UUID(str(plan.id)),
            tenant_id=TENANT,
        )
        _provision(db_manager)
        resp = internal_client.post(URL)
        assert resp.status_code == 201, resp.text
        assert resp.json()["license_id"] != str(member.id)
        assert resp.json()["license_id"] == _app_licence_id(db_manager, "kanban-t")

    def test_archived_tenant_is_404(self, db_manager, internal_client) -> None:
        _seed_bundle(db_manager)
        _provision(db_manager)
        with db_manager.engine.begin() as conn:
            conn.execute(
                text("UPDATE tenants SET archived_at = now() WHERE id = :t"), {"t": TENANT}
            )
        before = _token_count(db_manager)
        detail = self._refused(db_manager, internal_client.post(URL), 404, "no_bundle")
        assert detail["reason"] == "tenant_archived"
        assert _token_count(db_manager) == before

    def test_a_licence_without_the_holding_row_is_404(self, db_manager, internal_client) -> None:
        _seed_bundle(db_manager)
        _provision(db_manager)
        with db_manager.engine.begin() as conn:
            conn.execute(text("DELETE FROM tenant_bundle WHERE tenant_id = :t"), {"t": TENANT})
        self._refused(db_manager, internal_client.post(URL), 404, "no_bundle")

    def test_an_unheld_bundle_key_is_404(self, db_manager, internal_client) -> None:
        _seed_bundle(db_manager)
        _provision(db_manager)
        detail = self._refused(
            db_manager, internal_client.post(f"{URL}&bundle=not-held"), 404, "no_bundle"
        )
        assert detail["reason"] == "bundle_not_held"

    def test_an_expired_app_licence_is_404(self, db_manager, internal_client) -> None:
        """status='active' but past its expiry: only ``lic.is_active()`` catches it."""
        bundle_id, _ = _seed_bundle(db_manager)
        _provision(db_manager)
        with db_manager.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE license SET starts_at = now() - interval '2 days', "
                    "expires_at = now() - interval '1 day' WHERE bundle_id = :b"
                ),
                {"b": bundle_id},
            )
        before = _token_count(db_manager)
        self._refused(db_manager, internal_client.post(URL), 404, "no_app_licence")
        assert _token_count(db_manager) == before

    def test_a_revoked_bundle_is_404(self, db_manager, internal_client) -> None:
        """The licence is active; the BUNDLE itself is revoked (``Bundle.revoked``)."""
        bundle_id, _ = _seed_bundle(db_manager)
        _provision(db_manager)
        with db_manager.engine.begin() as conn:
            conn.execute(text("UPDATE bundle SET revoked = true WHERE id = :b"), {"b": bundle_id})
        before = _token_count(db_manager)
        detail = self._refused(db_manager, internal_client.post(URL), 404, "no_bundle")
        assert detail["reason"] == "no_bundle_held"
        assert _token_count(db_manager) == before

    def test_two_licences_for_the_app_in_the_bundle_is_409(
        self, db_manager, internal_client
    ) -> None:
        bundle_id, apps = _seed_bundle(db_manager)
        _provision(db_manager)
        subject = db_manager.create_or_get_subject(keycloak_sub="svc-dup", keycloak_realm="service")
        plan = [p for p in db_manager.list_plans(apps["kanban-t"]) if p.code == "standard"][0]
        db_manager.create_license(
            subject_id=UUID(str(subject.id)),
            app_id=apps["kanban-t"],
            plan_id=UUID(str(plan.id)),
            tenant_id=TENANT,
            bundle_id=bundle_id,
        )
        before = _token_count(db_manager)
        detail = self._refused(db_manager, internal_client.post(URL), 409, "ambiguous_app_licence")
        assert len(detail["license_ids"]) == 2
        assert _token_count(db_manager) == before

    @pytest.mark.parametrize(
        "roles",
        [
            ["service-internal"],
            ["onboarding-internal"],
            ["license-admin"],
            ["service-internal", "onboarding-internal", "license-admin"],
        ],
        ids=["service-internal", "onboarding-internal", "license-admin", "all-three"],
    )
    def test_internal_service_roles_alone_are_403(self, db_manager, roles: list[str]) -> None:
        """PR #226 audit: service-internal is held fleet-wide; it must not mint for ANY tenant."""
        _seed_bundle(db_manager)
        _provision(db_manager)
        before = _token_count(db_manager)
        for client in _role_client(db_manager, roles):
            resp = client.post(URL)
            assert resp.status_code == 403, resp.text
            assert "bundle-token-minter" in resp.text
        assert _token_count(db_manager) == before

    def test_minter_role_as_a_client_role_only_is_403(self, db_manager) -> None:
        """A client role of the same name on some other client does not satisfy the gate."""
        _seed_bundle(db_manager)
        _provision(db_manager)
        user = KeycloakUser(
            sub="client-role-sa",
            preferred_username="svc-client-role-sa",
            email="client-role-sa@test.com",
            realm_access={"roles": ["service-internal"]},
            resource_access={"some-client": {"roles": ["bundle-token-minter"]}},
            iss="https://keycloak.example.com/realms/service",
        )
        for client in _make_authenticated_client(db_manager, user, TENANT):
            assert client.post(URL).status_code == 403

    def test_a_non_internal_caller_is_403(self, db_manager, reader_client) -> None:
        _seed_bundle(db_manager)
        _provision(db_manager)
        before = _token_count(db_manager)
        assert reader_client.post(URL).status_code == 403
        assert _token_count(db_manager) == before

    def test_a_minted_token_cannot_mint(
        self, db_manager, internal_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A licence-token caller is only ever license-reader: no self-renewal chain.

        The middleware authenticates an opaque token by POSTing it to this service's own
        ``/license/introspect`` over loopback; routed in-process here (the pattern from
        test_operator_token.py) so the token genuinely AUTHENTICATES and the refusal comes
        from the role gate — a 401 would prove nothing.
        """
        import httpx

        import auth.middleware as mw
        import main

        real_async_client = httpx.AsyncClient

        def _loopback_client(*_a, **_kw):
            return real_async_client(
                transport=httpx.ASGITransport(app=main.app), base_url="http://127.0.0.1"
            )

        monkeypatch.setattr(mw.httpx, "AsyncClient", _loopback_client)
        _seed_bundle(db_manager)
        _provision(db_manager)
        minted = internal_client.post(URL).json()["token"]
        before = _token_count(db_manager)
        resp = internal_client.post(URL, headers={"Authorization": f"Bearer {minted}"})
        assert resp.status_code == 403, resp.text
        assert "bundle-token-minter" in resp.text
        assert _token_count(db_manager) == before


class TestSeveralBundles:
    def _second_bundle(self, db: DatabaseManager, *, with_alpha: bool = False) -> int:
        app = db.create_app(key="beta-app", name="beta-app", issuers=("platform",))
        db.create_plan(app_id=UUID(str(app.id)), code="standard", name="Standard")
        with db.engine.begin() as conn:
            bid = conn.execute(
                text("INSERT INTO bundle (key, name) VALUES ('second-bundle', 'S') RETURNING id")
            ).scalar_one()
            if with_alpha:
                conn.execute(
                    text(
                        "INSERT INTO bundle_app (bundle_id, app_id) "
                        "SELECT :b, id FROM app WHERE key = 'alpha-app'"
                    ),
                    {"b": bid},
                )
        session = db.get_session()
        try:
            session.add(BundleApp(bundle_id=bid, app_id=UUID(str(app.id))))
            session.commit()
        finally:
            session.close()
        return provision_tenant_bundle.main(
            ["--tenant", TENANT, "--bundle", "second-bundle", "--apply"], db=db
        )

    def test_an_app_in_one_held_bundle_resolves_without_bundle(
        self, db_manager, internal_client
    ) -> None:
        _seed_bundle(db_manager)
        _provision(db_manager)
        assert self._second_bundle(db_manager) == 0
        ok = internal_client.post(f"{BASE}?app=beta-app")
        assert ok.status_code == 201, ok.text
        assert ok.json()["bundle"] == "second-bundle"
        info = _introspect(internal_client, ok.json()["token"])
        assert info["app"]["key"] == "beta-app" and info["tenant_id"] == TENANT

    def test_an_app_in_two_held_bundles_with_one_licence_resolves_to_it(
        self, db_manager, internal_client
    ) -> None:
        """Migration 059 DEDUPE: two held bundles reach alpha-app but the tenant has ONE
        licence for it, so there is nothing ambiguous — it mints on that licence
        without `bundle` (this was a 409 `ambiguous_bundle` before 059)."""
        _seed_bundle(db_manager)
        _provision(db_manager)
        # alpha-app is already bound to the first bundle, so provisioning the second one
        # REFUSES it (exit 3) rather than minting a second (tenant, alpha-app) licence.
        assert self._second_bundle(db_manager, with_alpha=True) == 3
        resp = internal_client.post(f"{BASE}?app=alpha-app")
        assert resp.status_code == 201, resp.text
        assert resp.json()["bundle"] == BUNDLE_KEY
        ok = internal_client.post(f"{BASE}?app=alpha-app&bundle={BUNDLE_KEY}")
        assert ok.status_code == 201, ok.text
        assert ok.json()["license_id"] == resp.json()["license_id"]
        miss = internal_client.post(f"{BASE}?app=alpha-app&bundle=second-bundle")
        assert miss.status_code == 404 and miss.json()["detail"]["error"] == "no_app_licence"

    def test_two_held_bundles_with_different_licences_stay_ambiguous(
        self, db_manager, internal_client
    ) -> None:
        """Two DIFFERENT licences (here: a member licence carrying the second bundle)
        are still a 409 `ambiguous_bundle` without `bundle`."""
        _seed_bundle(db_manager)
        _provision(db_manager)
        assert self._second_bundle(db_manager, with_alpha=True) == 3
        with db_manager.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO license (subject_id, app_id, plan_id, tenant_id, status, "
                    "bundle_id) SELECT s.id, l.app_id, l.plan_id, l.tenant_id, 'active', "
                    "(SELECT id FROM bundle WHERE key = 'second-bundle') "
                    "FROM license l JOIN app a ON a.id = l.app_id, subject s "
                    "WHERE a.key = 'alpha-app' AND l.tenant_id = :t "
                    "AND s.id <> l.subject_id LIMIT 1"
                ),
                {"t": TENANT},
            )
        resp = internal_client.post(f"{BASE}?app=alpha-app")
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["error"] == "ambiguous_bundle"
        assert detail["bundles"] == ["second-bundle", BUNDLE_KEY]

    def test_another_tenants_bundle_is_not_visible(self, db_manager, internal_client) -> None:
        _seed_bundle(db_manager)
        _provision(db_manager)
        db_manager.ensure_tenant(OTHER_TENANT, name="test-tenant-other", owner_key="platform")
        resp = internal_client.post(f"/internal/tenants/{OTHER_TENANT}/bundle-token?app=kanban-t")
        assert resp.status_code == 404, resp.text
