"""New token mints get a bounded expiry or a REGISTERED long-lived class (licenses#244).

THE FINDING. Measured 2026-10-02: 199 active perpetual tokens (127 api, 72 internal)
and no mint path that set an expiry. ``create_token(expires_at=None)`` and
``POST /licenses/{id}/tokens`` without ``expires_days`` both silently meant
"perpetual".

THE RULING (option 2), NEW mints only:
  * an ``api`` mint with no expiry gets ``TOKEN_API_DEFAULT_EXPIRY_DAYS`` (365);
  * an explicit expiry is respected unchanged;
  * a perpetual token must be REGISTERED long-lived (owner + reason): allowed for
    ``internal``/``node``, and for ``api`` only on the n8n licence-token tenants
    (``n8n-dev``; n8n_custom_nodes/CLAUDE.md says those tokens are long-living and
    never regenerated);
  * an unregistered perpetual mint is refused, and nothing is written;
  * EXISTING perpetual tokens are untouched: they stay unregistered and still
    introspect as active.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from models import TOKEN_API_DEFAULT_EXPIRY_DAYS, LongLivedRegistration, Token
from tests.conftest import _make_authenticated_client

TENANT = "01000000-0000-7000-8000-000000000001"
# The production id of the n8n licence-token tenant (the primary, 2026-10-02).
N8N_TENANT_ID = "019de842-c92a-7e37-8fdd-eb0d8c5396f9"
OWNER = "platform-team"
REASON = "service credential; lifecycle decided 2026-12-01 (licenses#244)"


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _tokens(db_manager, license_id: str) -> list[Token]:
    session = db_manager.get_session()
    try:
        return session.query(Token).filter(Token.license_id == UUID(license_id)).all()
    finally:
        session.close()


@pytest.fixture
def n8n_license(db_manager, seed_subject, seed_app, seed_plan):
    """An active licence on the n8n licence-token tenant (`n8n-dev`)."""
    existing = db_manager.get_tenant("n8n-dev")
    tenant_id = str(existing.id) if existing else N8N_TENANT_ID
    db_manager.ensure_tenant(tenant_id, name="n8n-dev", owner_key="platform")
    return db_manager.create_license(
        subject_id=UUID(str(seed_subject.id)),
        app_id=UUID(str(seed_app.id)),
        plan_id=UUID(str(seed_plan.id)),
        tenant_id=tenant_id,
        status="active",
        starts_at=datetime.now(UTC) - timedelta(days=1),
    )


@pytest.fixture
def n8n_client(db_manager, admin_user, n8n_license):
    yield from _make_authenticated_client(
        db_manager, admin_user, tenant_id=str(n8n_license.tenant_id)
    )


class TestApiMintWithoutExpiryGetsTheDefault:
    def test_the_default_is_365_days(self):
        assert TOKEN_API_DEFAULT_EXPIRY_DAYS == 365

    def test_route_api_mint_without_expiry_is_bounded_to_365_days(
        self, auth_client_admin: TestClient, full_seed_data
    ):
        license_id = str(full_seed_data["license"].id)
        response = auth_client_admin.post(f"/licenses/{license_id}/tokens")
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["expires_at"] is not None, "an api mint without expiry must not be perpetual"
        expected = datetime.now(UTC) + timedelta(days=365)
        assert abs((_aware(body["expires_at"]) - expected).total_seconds()) < 120
        assert body["long_lived_owner"] is None

    def test_create_token_api_without_expiry_is_bounded(self, db_manager, seed_license):
        token, _raw = db_manager.create_token(license_id=UUID(str(seed_license.id)))
        assert token.expires_at is not None
        expected = datetime.now(UTC) + timedelta(days=365)
        assert abs((token.expires_at - expected).total_seconds()) < 120
        assert token.long_lived_owner is None


class TestExplicitExpiryIsRespected:
    def test_route_expires_days_is_used_verbatim(
        self, auth_client_admin: TestClient, full_seed_data
    ):
        license_id = str(full_seed_data["license"].id)
        response = auth_client_admin.post(
            f"/licenses/{license_id}/tokens?token_type=internal&expires_days=30"
        )
        assert response.status_code == 201, response.text
        body = response.json()
        expected = datetime.now(UTC) + timedelta(days=30)
        assert abs((_aware(body["expires_at"]) - expected).total_seconds()) < 120
        assert body["long_lived_owner"] is None

    def test_create_token_explicit_expiry_is_kept(self, db_manager, seed_license):
        when = datetime.now(UTC) + timedelta(days=3)
        token, _raw = db_manager.create_token(
            license_id=UUID(str(seed_license.id)), token_type="node", expires_at=when
        )
        assert abs((token.expires_at - when).total_seconds()) < 1
        assert token.long_lived_owner is None


class TestRegisteredLongLivedInternalMintIsAllowed:
    def test_route_registered_internal_mint_is_perpetual_and_recorded(
        self, auth_client_admin: TestClient, db_manager, full_seed_data
    ):
        license_id = str(full_seed_data["license"].id)
        response = auth_client_admin.post(
            f"/licenses/{license_id}/tokens",
            params={
                "token_type": "internal",
                "long_lived": "true",
                "long_lived_owner": OWNER,
                "long_lived_reason": REASON,
            },
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["expires_at"] is None
        assert body["long_lived_owner"] == OWNER
        (row,) = [t for t in _tokens(db_manager, license_id) if str(t.id) == body["token_id"]]
        assert (row.long_lived_owner, row.long_lived_reason) == (OWNER, REASON)

    def test_rotation_keeps_the_registration(self, db_manager, seed_license):
        lid = UUID(str(seed_license.id))
        db_manager.create_token(
            license_id=lid,
            token_type="internal",
            long_lived=LongLivedRegistration(OWNER, REASON),
        )
        _n, new, _raw = db_manager.regenerate_token_for_license(lid, token_type="internal")
        assert new.expires_at is None
        assert (new.long_lived_owner, new.long_lived_reason) == (OWNER, REASON)


class TestTheN8nPathMayStayPerpetual:
    def test_route_n8n_api_mint_without_expiry_is_allowed_when_registered(
        self, n8n_client: TestClient, db_manager, n8n_license
    ):
        license_id = str(n8n_license.id)
        response = n8n_client.post(
            f"/licenses/{license_id}/tokens",
            params={
                "token_type": "api",
                "long_lived": "true",
                "long_lived_owner": "n8n_custom_nodes",
                "long_lived_reason": "n8n licence tokens are long-living by rule",
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["expires_at"] is None
        assert response.json()["long_lived_owner"] == "n8n_custom_nodes"

    def test_create_token_n8n_api_without_expiry_is_allowed(self, db_manager, n8n_license):
        token, _raw = db_manager.create_token(
            license_id=UUID(str(n8n_license.id)),
            token_type="api",
            long_lived=LongLivedRegistration("n8n_custom_nodes", "long-living by rule"),
        )
        assert token.expires_at is None
        assert token.long_lived_owner == "n8n_custom_nodes"
        active = db_manager.introspect_token(_raw)
        assert active is not None and active["active"] is True


class TestUnregisteredPerpetualMintIsRefused:
    def test_route_long_lived_api_off_the_n8n_path_is_refused_and_writes_nothing(
        self, auth_client_admin: TestClient, db_manager, full_seed_data
    ):
        license_id = str(full_seed_data["license"].id)
        before = len(_tokens(db_manager, license_id))
        response = auth_client_admin.post(
            f"/licenses/{license_id}/tokens",
            params={
                "token_type": "api",
                "long_lived": "true",
                "long_lived_owner": OWNER,
                "long_lived_reason": REASON,
            },
        )
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["error"] == "long_lived_not_permitted"
        assert len(_tokens(db_manager, license_id)) == before

    def test_route_internal_without_expiry_or_registration_is_refused(
        self, auth_client_admin: TestClient, db_manager, full_seed_data
    ):
        license_id = str(full_seed_data["license"].id)
        before = len(_tokens(db_manager, license_id))
        response = auth_client_admin.post(f"/licenses/{license_id}/tokens?token_type=internal")
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["error"] == "perpetual_token_not_registered"
        assert len(_tokens(db_manager, license_id)) == before

    def test_route_owner_without_long_lived_is_refused(
        self, auth_client_admin: TestClient, full_seed_data
    ):
        license_id = str(full_seed_data["license"].id)
        response = auth_client_admin.post(
            f"/licenses/{license_id}/tokens", params={"long_lived_owner": OWNER}
        )
        assert response.status_code == 422, response.text

    def test_create_token_long_lived_needs_owner_and_reason(self, db_manager, seed_license):
        from models import TokenExpiryPolicyError

        with pytest.raises(TokenExpiryPolicyError) as exc:
            db_manager.create_token(
                license_id=UUID(str(seed_license.id)),
                token_type="internal",
                long_lived=LongLivedRegistration(" ", REASON),
            )
        assert exc.value.code == "long_lived_requires_owner_and_reason"

    def test_the_database_refuses_a_registered_row_that_expires(self, db_manager, seed_license):
        session = db_manager.get_session()
        try:
            session.add(
                Token(
                    license_id=UUID(str(seed_license.id)),
                    token_hash=Token.hash_token(Token.generate_token()),
                    token_type="internal",
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                    long_lived_owner=OWNER,
                    long_lived_reason=REASON,
                )
            )
            with pytest.raises(IntegrityError, match="ck_token_long_lived_registered"):
                session.commit()
        finally:
            session.rollback()
            session.close()


class TestExistingPerpetualTokensAreUntouched:
    def test_a_legacy_perpetual_row_is_unregistered_and_still_introspects_active(
        self, db_manager, seed_license
    ):
        # A row exactly as the 199 legacy tokens are stored: perpetual, no register entry.
        raw = Token.generate_token()
        session = db_manager.get_session()
        try:
            legacy = Token(
                license_id=UUID(str(seed_license.id)),
                token_hash=Token.hash_token(raw),
                token_type="api",
                expires_at=None,
            )
            session.add(legacy)
            session.commit()
            session.refresh(legacy)
            assert legacy.long_lived_owner is None and legacy.long_lived_reason is None
        finally:
            session.close()
        body = db_manager.introspect_token(raw)
        assert body is not None and body["active"] is True
        # Introspection writes last_used_at: an UPDATE of a legacy row must pass the CHECK.
        session = db_manager.get_session()
        try:
            session.query(Token).filter(Token.token_hash == Token.hash_token(raw)).update(
                {"last_used_at": datetime.now(UTC)}
            )
            session.commit()
        finally:
            session.close()
        body = db_manager.introspect_token(raw)
        assert body is not None and body["active"] is True


class TestLongLivedMintRequiresLicenseAdmin:
    """licenses#244 validator: ``introspect`` never reads ``token_type``, so a long-lived
    ``internal`` token is a perpetual api credential in all but name. The register is a
    license-admin decision; license-manager keeps every BOUNDED mint."""

    def test_license_manager_long_lived_is_403_and_writes_nothing(
        self, auth_client_manager: TestClient, db_manager, full_seed_data
    ):
        license_id = str(full_seed_data["license"].id)
        before = len(_tokens(db_manager, license_id))
        response = auth_client_manager.post(
            f"/licenses/{license_id}/tokens",
            params={
                "token_type": "internal",
                "long_lived": "true",
                "long_lived_owner": OWNER,
                "long_lived_reason": REASON,
            },
        )
        assert response.status_code == 403, response.text
        assert response.json()["detail"]["error"] == "long_lived_requires_license_admin"
        assert len(_tokens(db_manager, license_id)) == before

    def test_license_admin_long_lived_with_owner_and_reason_is_201(
        self, auth_client_admin: TestClient, db_manager, full_seed_data
    ):
        license_id = str(full_seed_data["license"].id)
        response = auth_client_admin.post(
            f"/licenses/{license_id}/tokens",
            params={
                "token_type": "internal",
                "long_lived": "true",
                "long_lived_owner": OWNER,
                "long_lived_reason": REASON,
            },
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["expires_at"] is None
        assert body["long_lived_owner"] == OWNER

    def test_license_manager_without_long_lived_gets_the_bounded_default(
        self, auth_client_manager: TestClient, db_manager, full_seed_data
    ):
        license_id = str(full_seed_data["license"].id)
        response = auth_client_manager.post(f"/licenses/{license_id}/tokens")
        assert response.status_code == 201, response.text
        body = response.json()
        expected = datetime.now(UTC) + timedelta(days=365)
        assert abs((_aware(body["expires_at"]) - expected).total_seconds()) < 120
        assert body["long_lived_owner"] is None

    def test_the_403_code_is_published_in_the_contract(self):
        import json
        from pathlib import Path

        spec = json.loads(
            (Path(__file__).resolve().parents[2] / "docs" / "openapi.json").read_text()
        )
        forbidden = spec["paths"]["/licenses/{license_id}/tokens"]["post"]["responses"]["403"]
        assert "long_lived_requires_license_admin" in forbidden["description"]


# ---------------------------------------------------------------------------
# licenses#244 F2: commonpractice cannot pass an expiry.
# ---------------------------------------------------------------------------

CP_AZP = "commonpractice-licenses-admin"
CP_OWNER = "m4 / commonpractice"
CP_REASON = "caller cannot pass expiry; migrate before removal"
# The secure-apps issuer EXACTLY as Keycloak emits it (measured live 2026-10-03 from
# both the public and the in-cluster well-known endpoint). Literal on purpose: a test
# that imported SECURE_APPS_ISSUER could not catch the constant drifting.
SECURE_APPS_ISS = "https://auth.nonagentic.ai/realms/secure-apps"
PORTAL_ISS = "https://auth.nonagentic.ai/realms/portal"


@pytest.fixture
def _trust_both_realms(monkeypatch):
    """Trust the two real realms' issuers so a token from EITHER verifies; the
    exemption decision must then come from the (iss, azp) pair, not from issuer trust.
    """
    from tests.support.jwt_keys import TEST_ISSUER

    monkeypatch.setenv("KEYCLOAK_TRUSTED_ISSUERS", f"{TEST_ISSUER},{SECURE_APPS_ISS},{PORTAL_ISS}")


def _post_as(
    client: TestClient, user, azp: str, path: str, *, iss: str = SECURE_APPS_ISS, **kwargs
):
    """POST with a REAL signed JWT whose verified ``iss`` / ``azp`` are as given."""
    from tests.conftest import _make_mock_jwt

    headers = {
        **kwargs.pop("headers", {}),
        "Authorization": f"Bearer {_make_mock_jwt(user, azp=azp, iss=iss)}",
    }
    return client.post(path, headers=headers, **kwargs)


def _assert_bounded_default(body: dict) -> None:
    expected = datetime.now(UTC) + timedelta(days=365)
    assert body["expires_at"] is not None
    assert abs((_aware(body["expires_at"]) - expected).total_seconds()) < 120
    assert body["long_lived_owner"] is None


@pytest.fixture
def _owners_may_issue_on_seed_tenant(db_manager, full_seed_data):
    """Migration 050: these tests are about the long-lived register, not ownership.

    They mint as several (iss, azp) pairs on the platform-owned seed tenant, so each
    pair is registered as an owning client (the test-realm twins of the real ones),
    every owner may issue the seed app, and every non-platform owner holds a grant
    on the seed tenant. The ownership rules themselves are pinned in
    tests/unit/test_tenant_ownership.py.
    """
    from sqlalchemy import text

    from tests.conftest import SEED_TENANT_ID

    with db_manager.engine.begin() as conn:
        for realm, client_id, owner in (
            ("portal", CP_AZP, "commonpractice"),
            ("secure-apps", "portal-bff-service", "platform"),
            ("secure-apps", "provisioning-backend", "fr"),
        ):
            conn.execute(
                text(
                    "INSERT INTO api_client (realm, client_id, owner_key) VALUES (:r, :c, :o) "
                    "ON CONFLICT DO NOTHING"
                ),
                {"r": realm, "c": client_id, "o": owner},
            )
    db_manager.add_app_issuers(full_seed_data["app"].id, ("platform", "fr", "commonpractice"))
    for owner in ("fr", "commonpractice"):
        db_manager.put_tenant_grant(SEED_TENANT_ID, owner, "test")


@pytest.mark.usefixtures("_trust_both_realms", "_owners_may_issue_on_seed_tenant")
class TestImplicitLongLivedClient:
    """commonpractice (m4-owned) mints tenant / member-seat ``api`` tokens with NO expiry
    (licenses-admin.ts:461 via license-provisioning.ts:186/:491), stores them, and
    introspects them on every request. A 365-day default there 402s every onboarded
    tenant a year later, so its VERIFIED (iss, azp) pair keeps a registered long-lived
    token. The pair, not ``azp`` alone: a client of the same name can be created in any
    other trusted realm.
    """

    def test_the_commonpractice_client_is_registered_by_default(self):
        from models import LONG_LIVED_IMPLICIT_CLIENTS

        entry = LONG_LIVED_IMPLICIT_CLIENTS[(SECURE_APPS_ISS, CP_AZP)]
        assert (entry.iss, entry.azp) == (SECURE_APPS_ISS, CP_AZP)
        assert entry.owner == CP_OWNER
        assert entry.reason == CP_REASON
        # The registry is keyed on the pair: azp alone is not a key.
        assert CP_AZP not in LONG_LIVED_IMPLICIT_CLIENTS

    def test_same_azp_from_the_portal_realm_is_bounded(
        self, auth_client_admin: TestClient, admin_user, db_manager, full_seed_data
    ):
        license_id = str(full_seed_data["license"].id)
        # Positive control: the SAME azp from secure-apps IS long-lived, so the only
        # discriminator is the verified issuer.
        control = _post_as(auth_client_admin, admin_user, CP_AZP, f"/licenses/{license_id}/tokens")
        assert control.status_code == 201, control.text
        assert control.json()["long_lived_owner"] == CP_OWNER

        response = _post_as(
            auth_client_admin, admin_user, CP_AZP, f"/licenses/{license_id}/tokens", iss=PORTAL_ISS
        )
        assert response.status_code == 201, response.text
        body = response.json()
        _assert_bounded_default(body)
        (row,) = [t for t in _tokens(db_manager, license_id) if str(t.id) == body["token_id"]]
        assert row.expires_at is not None
        assert row.long_lived_owner is None

    def test_the_exact_secure_apps_pair_is_long_lived_and_nothing_near_it_is(self):
        from models import implicit_long_lived_registration

        hit = implicit_long_lived_registration(SECURE_APPS_ISS, CP_AZP, "api", False, False)
        assert hit is not None
        assert (hit.owner, hit.reason) == (CP_OWNER, CP_REASON)
        assert hit.implicit_client == (SECURE_APPS_ISS, CP_AZP)
        # Exact match only: no normalisation of the issuer, no prefix match on azp.
        for iss, azp in (
            (PORTAL_ISS, CP_AZP),
            (SECURE_APPS_ISS + "/", CP_AZP),
            ("http://keycloak.keycloak.svc.cluster.local:8080/realms/secure-apps", CP_AZP),
            (None, CP_AZP),
            (SECURE_APPS_ISS, CP_AZP + "-x"),
            (SECURE_APPS_ISS, "fr-backend"),
            (SECURE_APPS_ISS, None),
        ):
            assert implicit_long_lived_registration(iss, azp, "api", False, False) is None, (
                iss,
                azp,
            )

    def test_wrong_azp_from_the_secure_apps_realm_is_bounded(
        self, auth_client_admin: TestClient, admin_user, full_seed_data
    ):
        from models import implicit_long_lived_registration

        # Precondition on the decision itself: right issuer, wrong client -> no
        # registration, while the right client from the same issuer has one.
        assert implicit_long_lived_registration(SECURE_APPS_ISS, CP_AZP, "api", False, False)
        assert (
            implicit_long_lived_registration(SECURE_APPS_ISS, "fr-backend", "api", False, False)
            is None
        )
        license_id = str(full_seed_data["license"].id)
        response = _post_as(
            auth_client_admin, admin_user, "fr-backend", f"/licenses/{license_id}/tokens"
        )
        assert response.status_code == 201, response.text
        _assert_bounded_default(response.json())

    def test_env_override_carries_iss_and_azp_and_rejects_an_azp_only_entry(self, monkeypatch):
        import json

        import models

        entry = {"iss": PORTAL_ISS, "azp": "x", "owner": "o", "reason": "r"}
        monkeypatch.setenv("LONG_LIVED_IMPLICIT_CLIENTS", json.dumps([entry]))
        parsed = models._long_lived_implicit_clients_from_env()
        assert list(parsed) == [(PORTAL_ISS, "x")]
        monkeypatch.setenv("LONG_LIVED_IMPLICIT_CLIENTS", "[]")
        assert models._long_lived_implicit_clients_from_env() == {}
        for bad in (
            {CP_AZP: {"owner": "o", "reason": "r"}},  # the old azp-keyed shape
            [{"azp": CP_AZP, "owner": "o", "reason": "r"}],  # no iss
            [{"iss": SECURE_APPS_ISS, "owner": "o", "reason": "r"}],  # no azp
        ):
            monkeypatch.setenv("LONG_LIVED_IMPLICIT_CLIENTS", json.dumps(bad))
            with pytest.raises(ValueError):
                models._long_lived_implicit_clients_from_env()

    def test_commonpractice_api_mint_without_expiry_is_registered_long_lived(
        self, auth_client_admin: TestClient, admin_user, db_manager, full_seed_data
    ):
        license_id = str(full_seed_data["license"].id)
        response = _post_as(auth_client_admin, admin_user, CP_AZP, f"/licenses/{license_id}/tokens")
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["expires_at"] is None
        assert body["long_lived_owner"] == CP_OWNER
        (row,) = [t for t in _tokens(db_manager, license_id) if str(t.id) == body["token_id"]]
        assert row.expires_at is None
        assert row.long_lived_owner == CP_OWNER
        assert row.long_lived_reason == CP_REASON

    def test_any_other_client_without_expiry_is_bounded_to_365_days(
        self, auth_client_admin: TestClient, admin_user, full_seed_data
    ):
        license_id = str(full_seed_data["license"].id)
        # Positive control: the SAME request from the registered client is long-lived,
        # so the only discriminator between the two answers is the verified azp.
        control = _post_as(auth_client_admin, admin_user, CP_AZP, f"/licenses/{license_id}/tokens")
        assert control.status_code == 201, control.text
        assert control.json()["expires_at"] is None

        for azp in ("fr-backend", "portal-bff-service", "provisioning-backend"):
            response = _post_as(
                auth_client_admin, admin_user, azp, f"/licenses/{license_id}/tokens"
            )
            assert response.status_code == 201, response.text
            body = response.json()
            expected = datetime.now(UTC) + timedelta(days=365)
            assert body["expires_at"] is not None, azp
            assert abs((_aware(body["expires_at"]) - expected).total_seconds()) < 120
            assert body["long_lived_owner"] is None

    def test_commonpractice_explicit_expiry_is_honoured(
        self, auth_client_admin: TestClient, admin_user, full_seed_data
    ):
        license_id = str(full_seed_data["license"].id)
        control = _post_as(auth_client_admin, admin_user, CP_AZP, f"/licenses/{license_id}/tokens")
        assert control.status_code == 201, control.text
        assert control.json()["long_lived_owner"] == CP_OWNER

        response = _post_as(
            auth_client_admin,
            admin_user,
            CP_AZP,
            f"/licenses/{license_id}/tokens",
            params={"expires_days": 30},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        expected = datetime.now(UTC) + timedelta(days=30)
        assert abs((_aware(body["expires_at"]) - expected).total_seconds()) < 120
        assert body["long_lived_owner"] is None

    def test_a_forged_request_field_or_header_naming_the_client_is_bounded(
        self, auth_client_admin: TestClient, admin_user, db_manager, full_seed_data
    ):
        license_id = str(full_seed_data["license"].id)
        # Precondition: the forged value IS a registered client (otherwise this is vacuous).
        control = _post_as(auth_client_admin, admin_user, CP_AZP, f"/licenses/{license_id}/tokens")
        assert control.status_code == 201, control.text
        assert control.json()["long_lived_owner"] == CP_OWNER

        response = _post_as(
            auth_client_admin,
            admin_user,
            "fr-backend",
            f"/licenses/{license_id}/tokens",
            params={"azp": CP_AZP, "client_id": CP_AZP, "token_type": "api"},
            headers={"X-Client-Id": CP_AZP, "X-Azp": CP_AZP, "X-Forwarded-Client": CP_AZP},
            json={"azp": CP_AZP, "client_id": CP_AZP},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["expires_at"] is not None
        expected = datetime.now(UTC) + timedelta(days=365)
        assert abs((_aware(body["expires_at"]) - expected).total_seconds()) < 120
        assert body["long_lived_owner"] is None
        (row,) = [t for t in _tokens(db_manager, license_id) if str(t.id) == body["token_id"]]
        assert row.long_lived_owner is None
