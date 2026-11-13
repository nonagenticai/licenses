"""``POST /licenses/{id}/operator-token`` and the ``roles`` field on introspection.

WHY. ingestion_service's operator purge (``DELETE /ingest/documents/{id}?scope=document``)
is gated on ``platform-admin`` read from ``/license/introspect``, and introspection never
returned a role — licences had no role concept — so the route was unreachable and a
co-holder fallback stood in for it (and let one user destroy another's PHI).

WHAT IS PINNED HERE.
* The authority is the caller's OWN verified Keycloak JWT realm role ``platform-admin``;
  the mint records it on ONE token so introspection can answer without calling Keycloak.
* Yourself only: the licence's subject must be the caller.
* Short-lived: the route caps at 60 min and the DB refuses a role-bearing token that is
  perpetual or longer than 65 min, or that carries any role but ``platform-admin``.
* An operator token cannot mint another one (a licence-token caller is only ever
  ``license-reader`` in this service).
* Backward compatible: every ordinary token introspects to ``roles: []``.
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from auth.models import KeycloakUser
from models import DatabaseManager, Token, _granted_roles
from tests.conftest import _make_authenticated_client

TENANT = "01000000-0000-7000-8000-000000000001"
OPERATOR_SUB = "operator-sub-0001"


def _kc_user(sub: str, roles: list[str]) -> KeycloakUser:
    return KeycloakUser(
        sub=sub,
        preferred_username=f"user-{sub}",
        email=f"{sub}@test.com",
        realm_access={"roles": roles},
        resource_access={},
        iss="https://keycloak.example.com/realms/portal",
    )


@pytest.fixture
def operator_client(db_manager) -> Generator[TestClient]:
    yield from _make_authenticated_client(
        db_manager, _kc_user(OPERATOR_SUB, ["platform-admin"]), TENANT
    )


@pytest.fixture
def license_admin_client(db_manager) -> Generator[TestClient]:
    """A license-admin who is NOT a platform admin — the most privileged licences role."""
    yield from _make_authenticated_client(
        db_manager, _kc_user(OPERATOR_SUB, ["license-admin", "license-manager"]), TENANT
    )


def _licence_for(db_manager: DatabaseManager, seed_app, seed_plan, sub: str, **kw):
    db_manager.ensure_tenant(TENANT, name=f"test-tenant-{TENANT[:8]}", owner_key="platform")
    subject = db_manager.create_or_get_subject(keycloak_sub=sub, keycloak_realm="portal")
    return db_manager.create_license(
        subject_id=UUID(str(subject.id)),
        app_id=UUID(str(seed_app.id)),
        plan_id=UUID(str(seed_plan.id)),
        tenant_id=TENANT,
        status=kw.get("status", "active"),
        starts_at=datetime.now(UTC) - timedelta(days=1),
        expires_at=kw.get("expires_at", datetime.now(UTC) + timedelta(days=30)),
    )


@pytest.fixture
def operator_licence(db_manager, seed_app, seed_plan):
    return _licence_for(db_manager, seed_app, seed_plan, OPERATOR_SUB)


def _introspect(client: TestClient, raw: str) -> dict:
    resp = client.post("/license/introspect", json={"token": raw})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _token_rows(db_manager: DatabaseManager, licence_id) -> list[Token]:
    with db_manager.get_session() as s:
        return s.query(Token).filter(Token.license_id == licence_id).all()


class TestAPlatformAdminMintsAShortLivedRoleToken:
    def test_mint_then_introspect_carries_the_role(
        self, operator_client: TestClient, operator_licence, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="main"):
            resp = operator_client.post(
                f"/licenses/{operator_licence.id}/operator-token?ttl_minutes=15"
            )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["roles"] == ["platform-admin"]
        assert body["license_id"] == str(operator_licence.id)
        expires = datetime.fromisoformat(body["expires_at"])
        assert timedelta(minutes=14) < expires - datetime.now(UTC) <= timedelta(minutes=15)

        info = _introspect(operator_client, body["token"])
        assert info["active"] is True
        assert info["roles"] == ["platform-admin"]
        assert info["tenant_id"] == TENANT
        assert info["license_id"] == str(operator_licence.id)

        audit = [
            r.getMessage() for r in caplog.records if "operator_token_minted" in r.getMessage()
        ]
        assert len(audit) == 1, audit
        assert f"actor_sub={OPERATOR_SUB}" in audit[0]
        assert f"license_id={operator_licence.id}" in audit[0]
        assert f"tenant_id={TENANT}" in audit[0]
        assert "roles=platform-admin" in audit[0]

    def test_the_default_ttl_is_fifteen_minutes_and_sixty_is_the_cap(
        self, operator_client: TestClient, operator_licence
    ) -> None:
        resp = operator_client.post(f"/licenses/{operator_licence.id}/operator-token")
        assert resp.status_code == 201, resp.text
        left = datetime.fromisoformat(resp.json()["expires_at"]) - datetime.now(UTC)
        assert timedelta(minutes=14) < left <= timedelta(minutes=15)

        ok = operator_client.post(f"/licenses/{operator_licence.id}/operator-token?ttl_minutes=60")
        assert ok.status_code == 201, ok.text

        for bad in (61, 0, 1440):
            r = operator_client.post(
                f"/licenses/{operator_licence.id}/operator-token?ttl_minutes={bad}"
            )
            assert r.status_code == 422, (bad, r.text)


class TestNobodyElseGetsOne:
    def test_a_license_admin_without_platform_admin_is_refused(
        self, license_admin_client: TestClient, operator_licence, db_manager
    ) -> None:
        resp = license_admin_client.post(f"/licenses/{operator_licence.id}/operator-token")
        assert resp.status_code == 403, resp.text
        assert _token_rows(db_manager, operator_licence.id) == []

    def test_a_platform_admin_cannot_mint_one_on_someone_elses_licence(
        self, operator_client: TestClient, db_manager, seed_app, seed_plan
    ) -> None:
        """⛔ YOURSELF ONLY: an admin credential another person would then hold is refused."""
        victim = _licence_for(db_manager, seed_app, seed_plan, "someone-else-sub")
        resp = operator_client.post(f"/licenses/{victim.id}/operator-token")
        assert resp.status_code == 404, resp.text
        assert _token_rows(db_manager, victim.id) == []

    def test_an_operator_token_cannot_mint_another_one(
        self,
        operator_client: TestClient,
        operator_licence,
        db_manager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A licence-token caller is only ever license-reader here: no self-renewal chain.

        The middleware authenticates an opaque token by calling THIS service's own
        ``/license/introspect`` over loopback. Routed in-process here (ASGITransport onto
        the same app), so the token genuinely AUTHENTICATES — the refusal must then come
        from the role gate, not from a failed introspect (a 401 would prove nothing).
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
        raw = operator_client.post(f"/licenses/{operator_licence.id}/operator-token").json()[
            "token"
        ]
        before = len(_token_rows(db_manager, operator_licence.id))
        resp = operator_client.post(
            f"/licenses/{operator_licence.id}/operator-token",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert resp.status_code == 403, resp.text
        assert "platform-admin" in resp.text
        assert len(_token_rows(db_manager, operator_licence.id)) == before

    def test_an_inactive_licence_is_refused(
        self, operator_client: TestClient, db_manager, seed_app, seed_plan
    ) -> None:
        lapsed = _licence_for(
            db_manager,
            seed_app,
            seed_plan,
            OPERATOR_SUB,
            status="revoked",
        )
        resp = operator_client.post(f"/licenses/{lapsed.id}/operator-token")
        assert resp.status_code == 400, resp.text
        assert _token_rows(db_manager, lapsed.id) == []


OTHER_TENANT = "01000000-0000-7000-8000-000000000002"


class TestTheHeaderTenantMustBeTheLicencesTenant:
    """``X-Tenant-Id`` (when sent) must name the licence's tenant — the plan's rule.

    The fixtures authenticate as ``azp=fr-backend`` (a trusted multi-tenant proxy), so the
    middleware ACCEPTS a retargeting header; the route's own check is the only barrier.
    """

    def test_a_header_naming_another_tenant_is_refused_and_nothing_is_minted(
        self,
        operator_client: TestClient,
        operator_licence,
        db_manager: DatabaseManager,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        db_manager.ensure_tenant(
            OTHER_TENANT, name=f"test-tenant-{OTHER_TENANT[:8]}x", owner_key="platform"
        )
        with caplog.at_level(logging.WARNING, logger="main"):
            resp = operator_client.post(
                f"/licenses/{operator_licence.id}/operator-token",
                headers={"X-Tenant-Id": OTHER_TENANT},
            )
        assert resp.status_code == 403, resp.text
        assert "does not match the licence's tenant" in resp.json()["detail"]
        assert _token_rows(db_manager, operator_licence.id) == []
        assert any(
            "operator_token_refused_tenant_mismatch" in r.getMessage() for r in caplog.records
        )

    def test_a_format_variant_of_the_licence_tenant_is_the_same_tenant(
        self, operator_client: TestClient, operator_licence
    ) -> None:
        """UUID-compared, not string-compared: the hyphen-less form names the same tenant."""
        resp = operator_client.post(
            f"/licenses/{operator_licence.id}/operator-token",
            headers={"X-Tenant-Id": UUID(TENANT).hex},
        )
        assert resp.status_code == 201, resp.text

    def test_no_header_lets_the_licence_decide(
        self, operator_client: TestClient, operator_licence
    ) -> None:
        del operator_client.headers["X-Tenant-Id"]
        resp = operator_client.post(f"/licenses/{operator_licence.id}/operator-token")
        assert resp.status_code == 201, resp.text
        assert resp.json()["license_id"] == str(operator_licence.id)

    def test_same_tenant_id_helper(self) -> None:
        from main import _same_tenant_id

        assert _same_tenant_id(TENANT, UUID(TENANT)) is True
        lettered = "0199abcd-ef00-7000-8000-00000000abcd"
        assert _same_tenant_id(lettered.upper(), lettered) is True
        assert _same_tenant_id(OTHER_TENANT, TENANT) is False
        assert _same_tenant_id("Bearer abc", TENANT) is False
        assert _same_tenant_id("", TENANT) is False


class TestEveryOrdinaryTokenStaysRoleless:
    def test_an_ordinary_token_introspects_to_an_empty_role_list(
        self, client: TestClient, full_seed_data
    ) -> None:
        info = _introspect(client, full_seed_data["raw_token"])
        assert info["active"] is True
        assert info["roles"] == []

    def test_the_ordinary_mint_route_never_grants_a_role(
        self, operator_client: TestClient, license_admin_client: TestClient, operator_licence
    ) -> None:
        resp = license_admin_client.post(f"/licenses/{operator_licence.id}/tokens")
        assert resp.status_code == 201, resp.text
        assert _introspect(license_admin_client, resp.json()["token"])["roles"] == []

    def test_an_inactive_answer_carries_no_roles(self, client: TestClient) -> None:
        info = _introspect(client, "no-such-token")
        assert info["active"] is False
        assert not info.get("roles")


class TestTheDatabaseRefusesAMisMint:
    """The two CHECKs (migration 038 / models.Token) make a bad grant unrepresentable."""

    @pytest.mark.parametrize("roles", [["license-admin"], ["admin"], ["platform-admin", "x"]])
    def test_only_platform_admin_is_grantable(
        self, db_manager: DatabaseManager, operator_licence, roles: list[str]
    ) -> None:
        with pytest.raises(IntegrityError, match="ck_token_granted_roles_allowlist"):
            db_manager.create_token(
                license_id=UUID(str(operator_licence.id)),
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
                granted_roles=roles,
            )

    @pytest.mark.parametrize("expires", [None, timedelta(hours=2), timedelta(days=30)])
    def test_a_role_bearing_token_must_be_short_lived(
        self, db_manager: DatabaseManager, operator_licence, expires
    ) -> None:
        with pytest.raises(IntegrityError, match="ck_token_granted_roles_short_lived"):
            db_manager.create_token(
                license_id=UUID(str(operator_licence.id)),
                expires_at=None if expires is None else datetime.now(UTC) + expires,
                granted_roles=["platform-admin"],
            )

    def test_the_controls_the_checks_must_admit(
        self, db_manager: DatabaseManager, operator_licence
    ) -> None:
        """A check that refuses everything would pass the two tests above; these must land."""
        lid = UUID(str(operator_licence.id))
        db_manager.create_token(license_id=lid, expires_at=None)  # perpetual, roleless
        db_manager.create_token(
            license_id=lid,
            expires_at=datetime.now(UTC) + timedelta(minutes=60),
            granted_roles=["platform-admin"],
        )
        roles = sorted(tuple(_granted_roles(t)) for t in _token_rows(db_manager, lid))
        assert roles == [(), ("platform-admin",)]
