"""The OWNER switches budget currency through provisioning (PR #243, round 4).

The real call path is fr -> provisioning ``inference/apply`` -> licences
``PUT /tenants/{id}/budget-currency``, made as SA ``provisioning-backend``, which
holds ``license-admin`` and NOT ``platform-admin``. Without a forwarded owner
identity every switch was a 409 and the audit line named the SA. The relay now
forwards the owner's portal JWT in ``X-Forwarded-User-Authorization``; licences
verifies it with the service's one JWKS/trusted-issuer verifier, narrowed to the
``portal`` realm, and grants the switch only for realm ``super_admin`` /
``platform-admin``. An invalid forwarded token is a 401, never ignored.

Each 409 case carries a positive CONTROL (the same request with a valid owner
token succeeds), so the test cannot pass on a tree that simply ignores the header.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Generator
from functools import lru_cache
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from auth.models import KeycloakUser
from tests.conftest import _make_authenticated_client, _make_mock_jwt
from tests.support.jwt_keys import TEST_ISSUER, private_key_pem

PORTAL_ISSUER = "https://keycloak.example.com/realms/portal"
HEADER = "X-Forwarded-User-Authorization"
OWNER_SUB = "owner-portal-sub-0001"
SA_AZP = "provisioning-backend"
SA_SUB = "service-account-provisioning-backend"


@lru_cache(maxsize=1)
def _foreign_key_pem() -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _portal_jwt(
    roles: list[str],
    *,
    sub: str = OWNER_SUB,
    iss: str = PORTAL_ISSUER,
    exp_offset: int = 3600,
    key: bytes | None = None,
) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": sub,
        "preferred_username": "owner",
        "realm_access": {"roles": roles},
        "azp": "fr-frontend",
        "iss": iss,
        "iat": now - 10,
        "exp": now + exp_offset,
    }
    return jwt.encode(payload, key or private_key_pem(), algorithm="RS256")


@pytest.fixture
def _trust_portal_issuer(_trust_test_signing_key, monkeypatch):
    # Both realms trusted, as in prod (DEFAULT_TRUSTED_REALMS = portal, secure-apps).
    monkeypatch.setenv("KEYCLOAK_TRUSTED_ISSUERS", f"{TEST_ISSUER},{PORTAL_ISSUER}")


@pytest.fixture
def sa_client(db_manager, _trust_portal_issuer) -> Generator[TestClient]:
    """SA provisioning-backend: license-admin/manager/reader, NOT platform-admin."""
    sa = KeycloakUser(
        sub=SA_SUB,
        preferred_username=SA_SUB,
        realm_access={"roles": ["license-admin", "license-manager", "license-reader"]},
        resource_access={},
        iss=TEST_ISSUER,
    )
    for client in _make_authenticated_client(db_manager, sa):
        # Migration 050: the real client is portal/provisioning-backend -> owner
        # `fr`, so the SA token is minted in the portal realm and the tenants these
        # tests change are fr's.
        client.headers["Authorization"] = (
            f"Bearer {_make_mock_jwt(sa, azp=SA_AZP, iss=PORTAL_ISSUER)}"
        )
        # The route addresses the tenant by path; the harness's default X-Tenant-Id
        # (a platform seed tenant fr does not hold) would now be a 403.
        del client.headers["X-Tenant-Id"]
        yield client


def _url(tenant) -> str:
    return f"/tenants/{tenant.id}/budget-currency"


def _audit(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "budget_currency_changed" in r.getMessage()]


@pytest.mark.parametrize("owner_role", ["super_admin", "platform-admin"])
def test_sa_with_forwarded_owner_token_switches_and_audits_the_owner(
    sa_client: TestClient, db_manager, caplog, owner_role
):
    t = db_manager.create_tenant(
        owner_key="fr", name=f"fwd-ok-{owner_role}".replace("_", "-"), budget_currency="EUR"
    )
    token = _portal_jwt([owner_role, "default-roles-portal"])
    with caplog.at_level(logging.DEBUG):
        resp = sa_client.put(
            _url(t), json={"budget_currency": "USD"}, headers={HEADER: f"Bearer {token}"}
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["budget_currency"] == "USD"
    assert db_manager.get_tenant(str(t.id)).budget_currency == "USD"
    audit = _audit(caplog)
    assert len(audit) == 1, audit
    for part in (f"actor={OWNER_SUB}", f"via={SA_AZP}", "from=EUR", "to=USD", f"tenant={t.id}"):
        assert part in audit[0], (part, audit[0])
    assert f"actor={SA_SUB}" not in audit[0]
    # Never a token, in any log line or the response.
    sa_token = sa_client.headers["Authorization"][7:]
    for rec in caplog.records:
        msg = rec.getMessage()
        assert token not in msg and sa_token not in msg, msg
    assert token not in resp.text


@pytest.mark.parametrize("roles", [["tenant_admin"], ["member"], []])
def test_sa_with_forwarded_non_owner_token_is_409(sa_client: TestClient, db_manager, roles):
    t = db_manager.create_tenant(
        owner_key="fr",
        name=f"fwd-no-{len(roles)}-{'-'.join(roles) or 'none'}".replace("_", "-"),
        budget_currency="EUR",
    )
    resp = sa_client.put(
        _url(t), json={"budget_currency": "CAD"}, headers={HEADER: f"Bearer {_portal_jwt(roles)}"}
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "budget_currency_immutable"
    assert db_manager.get_tenant(str(t.id)).budget_currency == "EUR"
    # CONTROL: the only difference is the owner role on the forwarded token.
    ok = sa_client.put(
        _url(t),
        json={"budget_currency": "CAD"},
        headers={HEADER: f"Bearer {_portal_jwt(['super_admin'])}"},
    )
    assert ok.status_code == 200, ok.text
    assert db_manager.get_tenant(str(t.id)).budget_currency == "CAD"


def test_sa_without_forwarded_token_is_409(sa_client: TestClient, db_manager):
    t = db_manager.create_tenant(owner_key="fr", name="fwd-absent", budget_currency="USD")
    resp = sa_client.put(_url(t), json={"budget_currency": "EUR"})
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "budget_currency_immutable"
    assert db_manager.get_tenant(str(t.id)).budget_currency == "USD"
    # CONTROL: the same SA, plus the owner's forwarded token, may switch.
    ok = sa_client.put(
        _url(t),
        json={"budget_currency": "EUR"},
        headers={HEADER: f"Bearer {_portal_jwt(['super_admin'])}"},
    )
    assert ok.status_code == 200, ok.text


@pytest.mark.parametrize(
    "header_value",
    [
        pytest.param(
            f"Bearer {_portal_jwt(['super_admin'], key=_foreign_key_pem())}", id="forged-signature"
        ),
        pytest.param(f"Bearer {_portal_jwt(['super_admin'], exp_offset=-3600)}", id="expired"),
        pytest.param(
            f"Bearer {_portal_jwt(['super_admin'], iss=TEST_ISSUER)}", id="secure-apps-realm"
        ),
        pytest.param(
            f"Bearer {_portal_jwt(['super_admin'], iss='https://evil.example/realms/portal')}",
            id="untrusted-issuer",
        ),
        pytest.param(f"Bearer {_portal_jwt(['super_admin'], sub='')}", id="no-sub"),
        pytest.param(_portal_jwt(["super_admin"]), id="no-bearer-scheme"),
        pytest.param("Bearer not-a-jwt", id="garbage"),
        pytest.param("", id="empty"),
    ],
)
@pytest.mark.parametrize(("held", "to"), [("EUR", "USD"), (None, "EUR")])
def test_invalid_forwarded_token_is_refused_outright(
    sa_client: TestClient, db_manager, caplog, header_value, held, to
):
    """Refused even where the SA's OWN authority would suffice (null -> EUR): a bad
    forwarded token never falls through to a weaker path."""
    t = db_manager.create_tenant(
        owner_key="fr",
        name=f"fwd-bad-{abs(hash((header_value, held))) % 10**8}",
        budget_currency=held,
    )
    with caplog.at_level(logging.DEBUG):
        resp = sa_client.put(_url(t), json={"budget_currency": to}, headers={HEADER: header_value})
    assert resp.status_code == 401, resp.text
    assert resp.json()["detail"]["error"] == "forwarded_user_token_invalid"
    assert db_manager.get_tenant(str(t.id)).budget_currency == held
    assert not _audit(caplog)
    if header_value:
        secret = header_value.removeprefix("Bearer ")
        assert all(secret not in r.getMessage() for r in caplog.records)


def test_same_currency_is_idempotent_200_and_audited_once(
    sa_client: TestClient, db_manager, caplog
):
    t = db_manager.create_tenant(owner_key="fr", name="fwd-idem", budget_currency="EUR")
    hdr = {HEADER: f"Bearer {_portal_jwt(['super_admin'])}"}
    with caplog.at_level(logging.WARNING):
        first = sa_client.put(_url(t), json={"budget_currency": "CAD"}, headers=hdr)
        again = sa_client.put(_url(t), json={"budget_currency": "CAD"}, headers=hdr)
        bare = sa_client.put(_url(t), json={"budget_currency": "CAD"})
    for r in (first, again, bare):
        assert r.status_code == 200, r.text
        assert r.json()["budget_currency"] == "CAD"
    assert len(_audit(caplog)) == 1, _audit(caplog)


def test_budget_currency_is_on_every_tenant_read_model(auth_client_admin: TestClient, db_manager):
    t = db_manager.create_tenant(owner_key="fr", name="read-ccy", budget_currency="CAD")
    one = auth_client_admin.get(f"/tenants/{t.id}")
    assert one.status_code == 200, one.text
    assert one.json()["budget_currency"] == "CAD"
    listing = auth_client_admin.get("/tenants")
    assert listing.status_code == 200, listing.text
    assert {r["name"]: r["budget_currency"] for r in listing.json()}["read-ccy"] == "CAD"
    internal = auth_client_admin.get("/internal/tenants")
    assert internal.status_code == 200, internal.text
    assert {r["name"]: r.get("budget_currency") for r in internal.json()}["read-ccy"] == "CAD"
