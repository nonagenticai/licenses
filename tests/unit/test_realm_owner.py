"""Cross-realm tenant forgery is refused (migration 072, realm-per-app P0 exit gate).

A Keycloak realm with a ``realm_owner`` row belongs to ONE app. Its realm admin
controls every claim its tokens carry (``tenant_id`` AND realm roles), so a token
from that realm may act only on tenants the realm's owner owns or holds an active
``tenant_app_grant`` on. Refusal is 403 ``realm_owner_mismatch``. A realm with no
row (portal, secure-apps, any other) keeps the pre-072 behaviour exactly.

Every refusal has a positive CONTROL (same request shape on an owned/granted
tenant passes), so these cannot pass on a tree that refuses everything.

The probe route is ``GET /licenses/active`` with an app key that has no licence:
an ADMITTED request answers 404 ``no_active_license``, a refused one 403/401 — so
the status alone tells the two apart.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient

import auth.jwt_verify as jv
import main
from models import REALM_OWNERS, DatabaseManager
from tests.support.jwt_keys import TEST_ISSUER, private_key_pem

KC = "https://keycloak.example.com"
CP_ISSUER = f"{KC}/realms/commonpractice"
PORTAL_ISSUER = f"{KC}/realms/portal"
FR_REALM_ISSUER = f"{KC}/realms/fr"  # fr's own realm (077: realm_owner fr -> fr)
NO_ROW_ISSUER = f"{KC}/realms/no-owner-app"  # an app realm with NO realm_owner row
UNTRUSTED_ISSUER = f"{KC}/realms/not-allowlisted"
HEADER = "X-Forwarded-User-Authorization"
NO_LICENCE_APP = "no-such-app-realm-owner-probe"


@pytest.fixture(autouse=True)
def _trust_realms(_trust_test_signing_key, monkeypatch):
    monkeypatch.setenv(
        "KEYCLOAK_TRUSTED_ISSUERS",
        ",".join((TEST_ISSUER, PORTAL_ISSUER, CP_ISSUER, FR_REALM_ISSUER, NO_ROW_ISSUER)),
    )
    monkeypatch.delenv("USER_TRUSTED_ISSUERS", raising=False)


def _token(
    *,
    iss: str,
    roles: list[str],
    tenant_id: str | None = None,
    azp: str = "commonpractice-frontend",
    sub: str | None = None,
) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": sub or f"user-{uuid.uuid4().hex[:8]}",
        "preferred_username": "someone",
        "realm_access": {"roles": roles},
        "resource_access": {},
        "azp": azp,
        "iss": iss,
        "iat": now - 5,
        "exp": now + 3600,
    }
    if tenant_id is not None:
        payload["tenant_id"] = tenant_id
    return jwt.encode(payload, private_key_pem(), algorithm="RS256")


@contextmanager
def _client(db_manager: DatabaseManager, token: str) -> Iterator[TestClient]:
    original = main.db_manager
    main.db_manager = db_manager
    try:
        with TestClient(main.app, raise_server_exceptions=False) as client:
            client.headers["Authorization"] = f"Bearer {token}"
            yield client
    finally:
        main.db_manager = original


def _active(client: TestClient, tenant_id: str):
    return client.get(
        "/licenses/active", params={"tenant_id": tenant_id, "app_key": NO_LICENCE_APP}
    )


def _err(resp) -> str | None:
    body = resp.json()
    detail = body.get("detail") if isinstance(body, dict) else None
    return detail.get("error") if isinstance(detail, dict) else None


def _tenant(db: DatabaseManager, owner: str) -> str:
    return str(db.create_tenant(name=f"t-{uuid.uuid4().hex[:10]}", owner_key=owner).id)


# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------


def test_seed_is_the_app_realms_only():
    assert REALM_OWNERS == (
        ("commonpractice", "commonpractice"),
        ("agent", "agent"),
        ("w2v", "w2v"),
        ("fr", "fr"),
        ("care-bus", "care-bus"),
    )
    realms = {r for r, _ in REALM_OWNERS}
    assert "portal" not in realms and "secure-apps" not in realms


def test_resolver_reads_the_table(db_manager):
    assert db_manager.resolve_realm_owner("commonpractice") == "commonpractice"
    assert db_manager.resolve_realm_owner("agent") == "agent"
    assert db_manager.resolve_realm_owner("w2v") == "w2v"
    assert db_manager.resolve_realm_owner("fr") == "fr"
    assert db_manager.resolve_realm_owner("portal") is None
    assert db_manager.resolve_realm_owner("secure-apps") is None


# ---------------------------------------------------------------------------
# Bearer (middleware) path
# ---------------------------------------------------------------------------


class TestCommonPracticeRealmBearer:
    def test_cp_token_on_cp_tenant_is_admitted(self, db_manager):
        cp_tenant = _tenant(db_manager, "commonpractice")
        with _client(
            db_manager, _token(iss=CP_ISSUER, roles=["license-reader"], tenant_id=cp_tenant)
        ) as c:
            r = _active(c, cp_tenant)
        assert r.status_code == 404, r.text
        assert _err(r) == "no_active_license"

    def test_cp_token_claiming_another_owners_tenant_is_403(self, db_manager):
        other = _tenant(db_manager, "fr")
        with _client(
            db_manager, _token(iss=CP_ISSUER, roles=["license-reader"], tenant_id=other)
        ) as c:
            r = _active(c, other)
        assert r.status_code == 403, r.text
        assert _err(r) == "realm_owner_mismatch"

    def test_cp_token_claiming_a_platform_tenant_is_403(self, db_manager):
        platform = _tenant(db_manager, "platform")
        with _client(
            db_manager, _token(iss=CP_ISSUER, roles=["license-reader"], tenant_id=platform)
        ) as c:
            r = _active(c, platform)
        assert r.status_code == 403, r.text
        assert _err(r) == "realm_owner_mismatch"

    def test_grant_to_commonpractice_admits_the_tenant(self, db_manager):
        other = _tenant(db_manager, "fr")
        db_manager.put_tenant_grant(other, "commonpractice", granted_by="test")
        with _client(
            db_manager, _token(iss=CP_ISSUER, roles=["license-reader"], tenant_id=other)
        ) as c:
            r = _active(c, other)
        assert r.status_code == 404, r.text
        assert _err(r) == "no_active_license"

    def test_revoked_grant_no_longer_admits(self, db_manager):
        other = _tenant(db_manager, "fr")
        db_manager.put_tenant_grant(other, "commonpractice", granted_by="test")
        db_manager.revoke_tenant_grant(other, "commonpractice")
        with _client(
            db_manager, _token(iss=CP_ISSUER, roles=["license-reader"], tenant_id=other)
        ) as c:
            r = _active(c, other)
        assert r.status_code == 403, r.text
        assert _err(r) == "realm_owner_mismatch"

    def test_self_granted_delegation_role_cannot_reach_another_owners_tenant(self, db_manager):
        """The realm admin can also mint ``service-internal`` / ``license-admin`` in
        its OWN realm. That role must not become a cross-owner bypass on the query
        tenant (the delegation shortcut in _enforce_own_tenant_scope)."""
        cp_tenant = _tenant(db_manager, "commonpractice")
        other = _tenant(db_manager, "fr")
        token = _token(iss=CP_ISSUER, roles=["service-internal"], tenant_id=cp_tenant)
        with _client(db_manager, token) as c:
            refused = _active(c, other)
            control = _active(c, cp_tenant)
        assert refused.status_code == 403, refused.text
        assert _err(refused) == "realm_owner_mismatch"
        assert control.status_code == 404, control.text

    def test_retarget_header_to_another_owner_is_refused(self, db_manager):
        cp_tenant = _tenant(db_manager, "commonpractice")
        other = _tenant(db_manager, "fr")
        token = _token(iss=CP_ISSUER, roles=["license-reader"], tenant_id=cp_tenant)
        with _client(db_manager, token) as c:
            r = c.get(
                "/licenses/active",
                params={"tenant_id": other, "app_key": NO_LICENCE_APP},
                headers={"X-Tenant-Id": other},
            )
        assert r.status_code == 403, r.text

    def test_subject_self_tenant_fallback_is_owner_checked(self, db_manager):
        """No tenant claim: get_tenant_id falls back to a sub that IS a tenant id.
        That tenant must still belong to the realm's owner."""
        other = _tenant(db_manager, "fr")
        with _client(db_manager, _token(iss=CP_ISSUER, roles=["license-reader"], sub=other)) as c:
            r = _active(c, other)
        assert r.status_code == 403, r.text
        assert _err(r) == "realm_owner_mismatch"
        cp_tenant = _tenant(db_manager, "commonpractice")
        with _client(
            db_manager, _token(iss=CP_ISSUER, roles=["license-reader"], sub=cp_tenant)
        ) as c:
            r = _active(c, cp_tenant)
        assert r.status_code == 404, r.text


class TestFrRealmIsScopedToOwnerFr:
    """077: a token from realm `fr` acts only on tenants owner fr owns or holds a grant on."""

    def test_fr_token_on_an_fr_tenant_passes_the_scope(self, db_manager):
        tenant = _tenant(db_manager, "fr")
        with _client(
            db_manager, _token(iss=FR_REALM_ISSUER, roles=["license-reader"], tenant_id=tenant)
        ) as c:
            r = _active(c, tenant)
        assert r.status_code == 404, r.text
        assert _err(r) == "no_active_license"

    def test_fr_token_on_an_ungranted_platform_tenant_is_403(self, db_manager):
        platform = _tenant(db_manager, "platform")
        with _client(
            db_manager,
            _token(iss=FR_REALM_ISSUER, roles=["license-reader"], tenant_id=platform),
        ) as c:
            r = _active(c, platform)
        assert r.status_code == 403, r.text
        assert _err(r) == "realm_owner_mismatch"

    def test_fr_grant_on_a_platform_tenant_admits_it(self, db_manager):
        """The fr fleet's system tenants are platform-owned with an active fr grant."""
        platform = _tenant(db_manager, "platform")
        db_manager.put_tenant_grant(platform, "fr", granted_by="test")
        with _client(
            db_manager,
            _token(iss=FR_REALM_ISSUER, roles=["license-reader"], tenant_id=platform),
        ) as c:
            r = _active(c, platform)
        assert r.status_code == 404, r.text
        assert _err(r) == "no_active_license"


class TestRealmsWithoutARowAreUnchanged:
    def test_portal_token_on_existing_tenant_is_not_refused(self, db_manager):
        for owner in ("platform", "fr", "commonpractice"):
            tenant = _tenant(db_manager, owner)
            with _client(
                db_manager,
                _token(iss=PORTAL_ISSUER, roles=["license-reader"], tenant_id=tenant),
            ) as c:
                r = _active(c, tenant)
            assert r.status_code == 404, (owner, r.text)
            assert _err(r) == "no_active_license"

    def test_secure_apps_delegation_role_still_reads_any_tenant(self, db_manager):
        cp_tenant = _tenant(db_manager, "commonpractice")
        with _client(
            db_manager, _token(iss=TEST_ISSUER, roles=["service-internal"], azp="svc")
        ) as c:
            r = _active(c, cp_tenant)
        assert r.status_code == 404, r.text

    def test_an_app_realm_without_a_row_keeps_pre_072_behaviour(self, db_manager):
        tenant = _tenant(db_manager, "platform")
        with _client(
            db_manager,
            _token(iss=NO_ROW_ISSUER, roles=["license-reader"], tenant_id=tenant),
        ) as c:
            r = _active(c, tenant)
        assert r.status_code == 404, r.text


class TestIssuerAllowlist:
    def test_issuer_not_in_allowlist_is_401(self, db_manager):
        tenant = _tenant(db_manager, "commonpractice")
        with _client(
            db_manager,
            _token(iss=UNTRUSTED_ISSUER, roles=["license-reader"], tenant_id=tenant),
        ) as c:
            r = _active(c, tenant)
        assert r.status_code == 401, r.text


class TestResolverFault:
    def test_resolver_fault_refuses_with_503(self, db_manager, monkeypatch):
        tenant = _tenant(db_manager, "platform")

        def boom(realm):
            raise RuntimeError("db down")

        monkeypatch.setattr(db_manager, "resolve_realm_owner", boom)
        with _client(
            db_manager, _token(iss=PORTAL_ISSUER, roles=["license-reader"], tenant_id=tenant)
        ) as c:
            r = _active(c, tenant)
        assert r.status_code == 503, r.text
        assert _err(r) == "realm_owner_unresolved"


# ---------------------------------------------------------------------------
# verify_user_jwt / USER_TRUSTED_ISSUERS
# ---------------------------------------------------------------------------


class TestUserTrustedIssuers:
    def test_default_is_the_portal_subset_of_trusted_issuers(self):
        assert jv.user_trusted_issuers() == (PORTAL_ISSUER,)

    def test_explicit_accepts_realm_names_and_urls(self, monkeypatch):
        monkeypatch.setenv("KEYCLOAK_URL", KC)
        monkeypatch.setenv("USER_TRUSTED_ISSUERS", f"portal, {CP_ISSUER}/")
        assert jv.user_trusted_issuers() == (PORTAL_ISSUER, CP_ISSUER)

    def test_verify_user_jwt_returns_the_realm(self, monkeypatch):
        monkeypatch.setenv("USER_TRUSTED_ISSUERS", f"{PORTAL_ISSUER},{CP_ISSUER}")
        claims, realm = jv.verify_user_jwt(_token(iss=CP_ISSUER, roles=[]))
        assert realm == "commonpractice" and claims["iss"] == CP_ISSUER

    def test_verify_user_jwt_refuses_an_issuer_outside_the_list(self):
        with pytest.raises(jwt.InvalidTokenError):
            jv.verify_user_jwt(_token(iss=CP_ISSUER, roles=[]))  # default: portal only

    def test_user_allowlist_does_not_need_keycloak_trusted_issuers(self, monkeypatch):
        """Independent lists: the forwarded-user list alone admits the issuer."""
        monkeypatch.setenv("KEYCLOAK_TRUSTED_ISSUERS", TEST_ISSUER)
        monkeypatch.setenv("USER_TRUSTED_ISSUERS", CP_ISSUER)
        _, realm = jv.verify_user_jwt(_token(iss=CP_ISSUER, roles=[]))
        assert realm == "commonpractice"


# ---------------------------------------------------------------------------
# Forwarded user token (PUT /tenants/{id}/budget-currency)
# ---------------------------------------------------------------------------


class TestForwardedUserRealmOwner:
    """The relay (provisioning-backend, portal realm, license-admin, owner fr)
    forwards an end-user token. With USER_TRUSTED_ISSUERS including the CP realm,
    a CP user may be forwarded only for tenants commonpractice owns or is granted."""

    def _put(self, db_manager, tenant: str, forwarded: str):
        sa = _token(
            iss=PORTAL_ISSUER,
            roles=["license-admin", "license-manager", "license-reader"],
            azp="provisioning-backend",
            sub="service-account-provisioning-backend",
        )
        with _client(db_manager, sa) as c:
            return c.put(
                f"/tenants/{tenant}/budget-currency",
                json={"budget_currency": "EUR"},
                headers={"X-Tenant-Id": tenant, HEADER: f"Bearer {forwarded}"},
            )

    def test_cp_user_not_in_allowlist_is_401(self, db_manager):
        tenant = _tenant(db_manager, "fr")
        r = self._put(db_manager, tenant, _token(iss=CP_ISSUER, roles=["super_admin"]))
        assert r.status_code == 401, r.text
        assert _err(r) == "forwarded_user_token_invalid"

    def test_cp_user_on_a_tenant_commonpractice_does_not_own_is_403(self, db_manager, monkeypatch):
        monkeypatch.setenv("USER_TRUSTED_ISSUERS", f"{PORTAL_ISSUER},{CP_ISSUER}")
        tenant = _tenant(db_manager, "fr")
        r = self._put(db_manager, tenant, _token(iss=CP_ISSUER, roles=["super_admin"]))
        assert r.status_code == 403, r.text
        assert _err(r) == "realm_owner_mismatch"

    def test_control_cp_user_on_a_granted_tenant_passes(self, db_manager, monkeypatch):
        monkeypatch.setenv("USER_TRUSTED_ISSUERS", f"{PORTAL_ISSUER},{CP_ISSUER}")
        tenant = _tenant(db_manager, "fr")
        db_manager.put_tenant_grant(tenant, "commonpractice", granted_by="test")
        r = self._put(db_manager, tenant, _token(iss=CP_ISSUER, roles=["super_admin"]))
        assert r.status_code == 200, r.text

    def test_portal_user_unchanged(self, db_manager, monkeypatch):
        monkeypatch.setenv("USER_TRUSTED_ISSUERS", f"{PORTAL_ISSUER},{CP_ISSUER}")
        tenant = _tenant(db_manager, "fr")
        r = self._put(db_manager, tenant, _token(iss=PORTAL_ISSUER, roles=["super_admin"]))
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# S2S ``?realm=`` on GET /licenses/active (license-authz#78)
# ---------------------------------------------------------------------------


class TestActiveLicenseRealmParam:
    """license-authz resolves a non-portal realm user's entitlement S2S (secure-apps
    token, delegation role) and names the user's realm in ``?realm=``."""

    @staticmethod
    def _s2s(db_manager):
        return _client(
            db_manager,
            _token(iss=TEST_ISSUER, roles=["service-internal"], azp="license-authz"),
        )

    @staticmethod
    def _get(c, tenant: str, realm: str | None):
        params = {"tenant_id": tenant, "app_key": NO_LICENCE_APP}
        if realm is not None:
            params["realm"] = realm
        return c.get("/licenses/active", params=params)

    def test_owned_realm_on_another_owners_tenant_is_403(self, db_manager):
        other = _tenant(db_manager, "fr")
        with self._s2s(db_manager) as c:
            refused = self._get(c, other, "commonpractice")
            control = self._get(c, other, None)  # no param: today's behaviour
        assert refused.status_code == 403, refused.text
        assert _err(refused) == "realm_owner_mismatch"
        assert control.status_code == 404, control.text

    def test_owned_realm_on_own_tenant_ok(self, db_manager):
        cp = _tenant(db_manager, "commonpractice")
        with self._s2s(db_manager) as c:
            r = self._get(c, cp, "commonpractice")
        assert r.status_code == 404, r.text
        assert _err(r) == "no_active_license"

    def test_owned_realm_on_granted_tenant_ok(self, db_manager):
        other = _tenant(db_manager, "fr")
        db_manager.put_tenant_grant(other, "commonpractice", granted_by="test")
        with self._s2s(db_manager) as c:
            r = self._get(c, other, "commonpractice")
        assert r.status_code == 404, r.text

    def test_realm_without_a_row_is_unchanged(self, db_manager):
        other = _tenant(db_manager, "fr")
        with self._s2s(db_manager) as c:
            for realm in ("portal", "fr", "not-a-realm"):
                r = self._get(c, other, realm)
                assert r.status_code == 404, (realm, r.text)

    def test_param_never_widens_a_non_s2s_caller(self, db_manager):
        """A bare license-reader keeps its own-tenant scope whatever ``realm`` says."""
        own = _tenant(db_manager, "platform")
        other = _tenant(db_manager, "fr")
        token = _token(iss=PORTAL_ISSUER, roles=["license-reader"], tenant_id=own)
        with _client(db_manager, token) as c:
            r = self._get(c, other, "portal")
        assert r.status_code == 403, r.text
        assert _err(r) != "realm_owner_mismatch"

    def test_resolver_fault_is_503(self, db_manager, monkeypatch):
        other = _tenant(db_manager, "fr")

        real = db_manager.resolve_realm_owner

        def boom(realm):  # fault ONLY on the param's realm, not the caller's own
            if realm == "commonpractice":
                raise RuntimeError("db down")
            return real(realm)

        with self._s2s(db_manager) as c:
            monkeypatch.setattr(db_manager, "resolve_realm_owner", boom)
            r = self._get(c, other, "commonpractice")
        assert r.status_code == 503, r.text
        assert _err(r) == "realm_owner_unresolved"
