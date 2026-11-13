"""Stage 2 header-trust close — tenant authority reconciliation.

Hole `a`: a client ``X-Tenant-Id`` header overrode the verified JWT tenant and
``auth/tenant.py`` read the header first, so any in-mesh caller holding a
license role could retarget another tenant by forging the header. Stage 2 makes
the verified token identity authoritative: a differing ``X-Tenant-Id`` is
honored ONLY for a ``license-admin`` OR a caller whose verified ``azp``
(client_id) is in the trusted multi-tenant-proxy allowlist (fr-backend /
w2v-backend). Everyone else: the header cannot retarget — a present claim that
contradicts the header is a 403; a claim-less non-proxy caller's forged header
is ignored (tenant resolves from the verified claim/subject, never the header).

⚠️ SUPERSEDED IN PART by migration 050 (tenant ownership per app): the retarget
is honored ONLY to a tenant the caller's OWNER (api_client, keyed on the
verified iss realm + azp) owns or holds a grant on; the break-glass cross_owner
client may retarget any tenant. Neither `license-admin` nor an env allowlist of
proxy azps grants it any more. The probe below installs fake ownership hooks.

Hole `b2`: a no-token + ``X-Tenant-Id`` request minted a roleless user; now 401.

These drive the real ASGI stack (middleware only) via a probe that echoes the
RESOLVED ``request.state.user.tenant_id`` — that value is what every downstream
route acts on, so asserting it proves the pivot is closed at the authority.

RED baseline (origin/main, pre-fix): R1 returns 200 (header overrode claim),
R1b/R2 echo tenant_id == the forged header (pivot), b2 returns 200-with-roleless
user. All must flip after the fix.
"""

import time
import uuid

import jwt
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from auth.middleware import AuthMiddleware
from tests.support.jwt_keys import TEST_ISSUER, private_key_pem, public_key

TRUSTED_PROXY = "fr-backend"
NON_PROXY = "kanban"
BREAK_GLASS = "licenses-admin"
# Tenants the fake api_client says fr holds (owns or is granted).
FR_TENANTS: set[str] = set()


def _tok(*, roles, azp, tenant_id=None, sub=None) -> str:
    payload = {
        "sub": sub or str(uuid.uuid4()),
        "iss": TEST_ISSUER,
        "exp": int(time.time()) + 3600,
        "iat": int(time.time()),
        "preferred_username": f"service-account-{azp}",
        "azp": azp,
        "realm_access": {"roles": roles},
    }
    if tenant_id is not None:
        payload["tenant_id"] = tenant_id
    return jwt.encode(payload, private_key_pem(), algorithm="RS256")


@pytest.fixture
def probe(monkeypatch) -> TestClient:
    """Minimal app carrying ONLY AuthMiddleware; echoes the resolved tenant."""
    monkeypatch.setenv("KEYCLOAK_TRUSTED_ISSUERS", TEST_ISSUER)
    import auth.middleware as mw

    clients = {
        ("secure-apps", TRUSTED_PROXY): ("fr", False),
        ("secure-apps", BREAK_GLASS): ("platform", True),
    }
    FR_TENANTS.clear()
    monkeypatch.setattr(mw, "_owner_resolver", lambda realm, azp: clients.get((realm, azp)))
    monkeypatch.setattr(
        mw, "_tenant_access", lambda owner, tenant: owner == "fr" and tenant in FR_TENANTS
    )

    import auth.jwt_verify as jv

    jv._jwks_client.cache_clear()
    monkeypatch.setattr(jv, "_signing_key", lambda token, issuer: public_key())

    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.get("/licenses/whoami")
    def whoami(request: Request):  # pragma: no cover - trivial probe
        u = request.state.user
        return {
            "tenant_id": getattr(u, "tenant_id", None),
            "roles": u.get_all_roles(),
            "azp": getattr(u, "azp", None),
        }

    return TestClient(app, raise_server_exceptions=False)


def _get(probe, tok=None, tenant=None):
    headers = {}
    if tok is not None:
        headers["Authorization"] = f"Bearer {tok}"
    if tenant is not None:
        headers["X-Tenant-Id"] = tenant
    return probe.get("/licenses/whoami", headers=headers)


# ---------------------------------------------------------------- RED (hole a)


def test_r1_claim_bearing_nonadmin_mismatch_is_403(probe):
    """reader with claim=A + forged header=B → 403 (cross-tenant pivot blocked)."""
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    r = _get(probe, _tok(roles=["license-reader"], azp=NON_PROXY, tenant_id=a), b)
    assert r.status_code == 403, f"pivot not blocked: {r.status_code} {r.text[:200]}"


def test_r1b_claimless_nonproxy_forged_header_is_ignored(probe):
    """claim-less non-proxy reader + forged header=B → header ignored (tenant != B)."""
    b = str(uuid.uuid4())
    r = _get(probe, _tok(roles=["license-reader"], azp=NON_PROXY), b)
    # Not a 403 (nothing to contradict), but the forged header must NOT become
    # the operative tenant — it is ignored; the resolver later fails closed.
    assert r.status_code == 200, r.text[:200]
    assert r.json()["tenant_id"] != b, "forged header still became operative tenant"


def test_r2_claimless_nonproxy_manager_write_header_ignored(probe):
    """claim-less non-proxy manager + forged header=B → header ignored."""
    b = str(uuid.uuid4())
    r = _get(probe, _tok(roles=["license-manager"], azp=NON_PROXY), b)
    assert r.status_code == 200, r.text[:200]
    assert r.json()["tenant_id"] != b


# -------------------------------------------------------------- GREEN (legit)


def test_g1_trusted_proxy_header_is_honored(probe):
    """fr-backend (owner fr), claim-less, header=B that fr holds → serves B."""
    b = str(uuid.uuid4())
    FR_TENANTS.add(b)
    r = _get(probe, _tok(roles=["license-reader"], azp=TRUSTED_PROXY), b)
    assert r.status_code == 200, r.text[:200]
    assert r.json()["tenant_id"] == b, "legit proxy tenant targeting broke"


def test_g1b_proxy_header_for_a_tenant_it_does_not_hold_is_403(probe):
    """Migration 050: fr-backend (a KNOWN owning app), claim-less, header=B that fr
    neither owns nor holds a grant on → 403 tenant_not_accessible, so the caller
    can tell "not onboarded to this app" from a broken token."""
    b = str(uuid.uuid4())
    r = _get(probe, _tok(roles=["license-reader"], azp=TRUSTED_PROXY), b)
    assert r.status_code == 403, r.text[:200]
    assert r.json()["detail"]["error"] == "tenant_not_accessible"


def test_g1c_proxy_with_claim_and_unheld_header_is_403(probe):
    """fr-backend with claim=A and header=B it does not hold → 403 pivot."""
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    r = _get(probe, _tok(roles=["license-reader"], azp=TRUSTED_PROXY, tenant_id=a), b)
    assert r.status_code == 403, r.text[:200]


def test_g2_admin_role_alone_no_longer_retargets(probe):
    """Migration 050: license-admin on a client with no owner + header=B → ignored.
    Holding the role no longer implies cross-app reach."""
    b = str(uuid.uuid4())
    r = _get(probe, _tok(roles=["license-admin"], azp=NON_PROXY), b)
    assert r.status_code == 200, r.text[:200]
    assert r.json()["tenant_id"] != b


def test_g2b_break_glass_header_is_honored(probe, caplog):
    """The cross_owner client + header=B → serves B, and the retarget is audited."""
    b = str(uuid.uuid4())
    with caplog.at_level("WARNING"):
        r = _get(probe, _tok(roles=["license-admin"], azp=BREAK_GLASS), b)
    assert r.status_code == 200, r.text[:200]
    assert r.json()["tenant_id"] == b
    assert any(
        "cross_owner_action: action=retarget" in rec.getMessage() and b in rec.getMessage()
        for rec in caplog.records
    )


def test_g3_header_equals_claim_is_allowed(probe):
    """non-proxy reader, claim=A, header=A → allowed, scoped to A."""
    a = str(uuid.uuid4())
    r = _get(probe, _tok(roles=["license-reader"], azp=NON_PROXY, tenant_id=a), a)
    assert r.status_code == 200, r.text[:200]
    assert r.json()["tenant_id"] == a


def test_g3b_claim_only_no_header_is_allowed(probe):
    """claim=A, no header → scoped to A (gateway-stripped traffic path)."""
    a = str(uuid.uuid4())
    r = _get(probe, _tok(roles=["license-reader"], azp=NON_PROXY, tenant_id=a))
    assert r.status_code == 200, r.text[:200]
    assert r.json()["tenant_id"] == a


# ------------------------------------------------------------- GREEN (hole b2)


def test_g4_no_token_with_header_is_401(probe):
    """no Authorization + X-Tenant-Id → 401 (roleless-user fallback removed)."""
    r = _get(probe, tenant=str(uuid.uuid4()))
    assert r.status_code == 401, f"no-token header path still admits: {r.status_code}"


# Migration 072: these drive AuthMiddleware on a DB-less minimal app; no realm is owned.
pytestmark = pytest.mark.usefixtures("no_realm_owner")
