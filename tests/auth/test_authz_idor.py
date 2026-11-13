"""Cross-tenant authorization (IDOR) regression tests.

Every test in this module targets a hole where a role check existed but
authorized NOTHING about *whose* data was returned: the tenant/subject came
from a caller-controlled path parameter and was never compared to the caller's
own identity. Each of these was a live 200-with-someone-else's-data before
2026-07-27.

Two things are asserted per hole:
  1. the negative — a foreign tenant's data is no longer reachable, and
  2. the positive — the caller's OWN data is still reachable, and a
     ``license-admin`` (the deliberate cross-tenant operator surface, see
     ``GET /licenses/admin/all``) is unaffected.

Without (2) a "fix" that simply broke the endpoints would look green.
"""

import time
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
import pytest
from fastapi.testclient import TestClient

from models import DatabaseManager
from tests.conftest import SEED_OTHER_TENANT_ID, SEED_TENANT_ID
from tests.support.jwt_keys import TEST_ISSUER, private_key_pem

TENANT_A = SEED_TENANT_ID
TENANT_B = SEED_OTHER_TENANT_ID

# The keycloak_sub of the subject that the tenant-A reader legitimately *is*.
READER_A_SUB = "reader-a-subject-sub"
# A different subject, also living on tenant A.
OTHER_A_SUB = "other-a-subject-sub"
# A subject whose licenses live on tenant B.
SUBJECT_B_SUB = "subject-b-sub"

# An `azp` that is NOT in the api_client table (migration 050). The shared
# conftest helper signs its tokens as the break-glass `licenses-admin` client, so
# using it here would have tested the cross-owner carve-out instead of the
# ordinary-caller rule and quietly reported a pass. These tests mint their own
# tokens for that reason.
ORDINARY_AZP = "some-ordinary-client"
# A multi-tenant proxy (this is the w2v license-gate's identity). Since migration
# 050 a proxy is just a client with an OWNER: it may act for a tenant its owner
# owns or holds a grant on. The fixtures below register it (in the test realm)
# as owner `w2v` and grant w2v the tenant it acts for.
PROXY_AZP = "w2v-backend"
# The break-glass client: the only cross-owner operator since migration 050.
BREAK_GLASS_AZP = "licenses-admin"


def _register_proxy(db_manager: DatabaseManager, *granted_tenants: str) -> None:
    from sqlalchemy import text

    with db_manager.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO api_client (realm, client_id, owner_key) "
                "VALUES ('secure-apps', :c, 'w2v') ON CONFLICT DO NOTHING"
            ),
            {"c": PROXY_AZP},
        )
    for tenant in granted_tenants:
        db_manager.put_tenant_grant(tenant, "w2v", "test")


def _client(
    db_manager: DatabaseManager,
    *,
    sub: str,
    roles: list[str],
    tenant_id: str,
    azp: str = ORDINARY_AZP,
    header_tenant_id: str | None = None,
) -> Generator[TestClient]:
    """A TestClient whose token carries an explicit tenant_id claim and `azp`.

    The tenant_id CLAIM (not just the header) matters: for a non-proxy, non-admin
    caller the middleware ignores a contradicting X-Tenant-Id header, so a
    claim-less ordinary token would fall through to the subject fallback and 401.

    ``header_tenant_id`` sends an X-Tenant-Id that DIFFERS from the claim. That is
    the production shape of a trusted multi-tenant proxy: the SA's own token
    carries its service tenant while the acted-on tenant rides the header, and the
    middleware retargets ``user.tenant_id`` onto it only for an allowlisted `azp`.
    For every other caller the differing header is a cross-tenant pivot attempt.
    """
    import main

    original = main.db_manager
    main.db_manager = db_manager
    db_manager.ensure_tenant(tenant_id, name=f"idor-tenant-{tenant_id[:8]}", owner_key="platform")
    token = jwt.encode(
        {
            "sub": sub,
            "preferred_username": sub,
            "realm_access": {"roles": roles},
            "resource_access": {},
            "tenant_id": tenant_id,
            "azp": azp,
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "iss": TEST_ISSUER,
        },
        private_key_pem(),
        algorithm="RS256",
    )
    try:
        with TestClient(main.app, raise_server_exceptions=False) as test_client:
            test_client.headers["Authorization"] = f"Bearer {token}"
            test_client.headers["X-Tenant-Id"] = header_tenant_id or tenant_id
            yield test_client
    finally:
        main.db_manager = original


@pytest.fixture
def two_tenant_data(db_manager: DatabaseManager) -> dict:
    """One shared app/plan; three subjects; licenses split across tenants A and B."""
    app = db_manager.create_app(key="shared-app", name="Shared App", issuers=("platform",))
    plan = db_manager.create_plan(
        app_id=UUID(str(app.id)), code="std", name="Standard", duration_days=30
    )
    subjects = {
        sub: db_manager.create_or_get_subject(keycloak_sub=sub, keycloak_realm="secure-apps")
        for sub in (READER_A_SUB, OTHER_A_SUB, SUBJECT_B_SUB)
    }
    now = datetime.now(UTC)
    licenses = {}
    for sub, tenant in (
        (READER_A_SUB, TENANT_A),
        (OTHER_A_SUB, TENANT_A),
        (SUBJECT_B_SUB, TENANT_B),
    ):
        licenses[sub] = db_manager.create_license(
            subject_id=UUID(str(subjects[sub].id)),
            app_id=UUID(str(app.id)),
            plan_id=UUID(str(plan.id)),
            tenant_id=tenant,
            status="active",
            starts_at=now - timedelta(days=1),
            expires_at=now + timedelta(days=30),
        )
    return {"app": app, "plan": plan, "subjects": subjects, "licenses": licenses}


@pytest.fixture
def reader_a(db_manager: DatabaseManager) -> Generator[TestClient]:
    """Ordinary license-reader on tenant A, identity == READER_A_SUB."""
    yield from _client(db_manager, sub=READER_A_SUB, roles=["license-reader"], tenant_id=TENANT_A)


@pytest.fixture
def admin_a(db_manager: DatabaseManager) -> Generator[TestClient]:
    """license-admin on tenant A (the deliberate cross-tenant operator).

    Migration 050: holding `license-admin` no longer implies cross-app reach — the
    operator surface is the break-glass client, so this signs as it.
    """
    yield from _client(
        db_manager,
        sub="admin-sub",
        roles=["license-admin"],
        tenant_id=TENANT_A,
        azp=BREAK_GLASS_AZP,
    )


@pytest.fixture
def proxy_a(db_manager: DatabaseManager) -> Generator[TestClient]:
    """Trusted multi-tenant proxy SA (w2v-backend) acting for tenant A.

    Holds license-reader + license-manager and NOT license-admin — the exact
    role set w2v_orchestrator's license gate uses.
    """
    _register_proxy(db_manager, TENANT_A)
    yield from _client(
        db_manager,
        sub="w2v-backend-sa",
        roles=["license-reader", "license-manager"],
        tenant_id=TENANT_A,
        azp=PROXY_AZP,
    )


@pytest.fixture
def proxy_acting_for_b(db_manager: DatabaseManager) -> Generator[TestClient]:
    """Trusted proxy SA whose own claim is tenant A but which ACTS for tenant B.

    This is the production shape the ``proxy_a`` fixture does not cover: there the
    claim and the header agree, so the middleware's retarget branch never runs. A
    real w2v pod presents a ``w2v-backend`` client-credentials token (its own
    service tenant, or no tenant claim at all) and names the tenant it is serving
    in X-Tenant-Id. Only an owning client granted (or owning) tenant B makes that
    retarget legal (migration 050).
    """
    _register_proxy(db_manager, TENANT_B)
    yield from _client(
        db_manager,
        sub="w2v-backend-sa",
        roles=["license-reader", "license-manager"],
        tenant_id=TENANT_A,
        azp=PROXY_AZP,
        header_tenant_id=TENANT_B,
    )


class TestGetActiveLicenseIdor:
    """GET /licenses/active?tenant_id=&app_key= — ``tenant_id`` is a QUERY param.

    ``require_internal_or_reader`` admits any ``license-reader``, and every opaque
    licence token self-introspects to exactly that role, so the role check alone
    authorizes NOTHING about *whose* entitlement is returned. The guard at
    main.py (``Not authorized to read another tenant's active license``) is what
    does. It was the one IDOR hole in this file with no regression test.

    These tests also pin the mechanism the w2v entitlement gate must use: a
    trusted multi-tenant-proxy `azp` (w2v-backend) naming the served tenant in
    X-Tenant-Id, which is DELEGATION — scoped to the one tenant it acts for — and
    not the cross-tenant operator surface that ``license-admin`` grants.
    """

    def test_reader_cannot_read_foreign_tenant_active_license(self, reader_a, two_tenant_data):
        """THE required negative: an ordinary tenant caller asking about another
        tenant must stay 403. Nothing below may widen this."""
        resp = reader_a.get(
            "/licenses/active", params={"tenant_id": TENANT_B, "app_key": "shared-app"}
        )
        assert resp.status_code == 403, resp.text
        assert "another tenant" in resp.text
        assert TENANT_B not in resp.json().get("detail", "")

    def test_reader_cannot_pivot_with_a_forged_header(self, reader_a, two_tenant_data):
        """Sending X-Tenant-Id: B is not a way around the query-param guard — the
        middleware ignores/rejects a header contradicting a non-proxy's claim."""
        resp = reader_a.get(
            "/licenses/active",
            params={"tenant_id": TENANT_B, "app_key": "shared-app"},
            headers={"X-Tenant-Id": TENANT_B},
        )
        assert resp.status_code == 403, resp.text

    def test_reader_can_still_read_own_tenant_active_license(self, reader_a, two_tenant_data):
        """Positive control: without this a fix that simply broke the route is green."""
        resp = reader_a.get(
            "/licenses/active", params={"tenant_id": TENANT_A, "app_key": "shared-app"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["tenant_id"] == TENANT_A

    def test_trusted_proxy_may_resolve_the_tenant_it_acts_for(
        self, proxy_acting_for_b, two_tenant_data
    ):
        """The w2v license gate's path: w2v-backend holds NO internal-service role
        and no license-admin, yet must resolve entitlement for the tenant it is
        serving. If this 403s, the gate can only ever answer for its own tenant and
        every cross-tenant render resolves ``unknown`` -> 503."""
        resp = proxy_acting_for_b.get(
            "/licenses/active", params={"tenant_id": TENANT_B, "app_key": "shared-app"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["tenant_id"] == TENANT_B

    def test_trusted_proxy_is_still_scoped_to_the_tenant_it_acts_for(
        self, proxy_acting_for_b, two_tenant_data
    ):
        """Delegation, not cross-tenant access: acting for B it may not read A.

        This is what keeps the proxy carve-out narrower than ``license-admin``."""
        resp = proxy_acting_for_b.get(
            "/licenses/active", params={"tenant_id": TENANT_A, "app_key": "shared-app"}
        )
        assert resp.status_code == 403, resp.text

    def test_admin_can_still_read_any_tenant_active_license(self, admin_a, two_tenant_data):
        """Positive control for the deliberate operator surface."""
        resp = admin_a.get(
            "/licenses/active", params={"tenant_id": TENANT_B, "app_key": "shared-app"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["tenant_id"] == TENANT_B


class TestListUserLicensesIdor:
    """GET /licenses/user/{subject_id} — subject_id is caller-controlled."""

    def test_reader_cannot_read_foreign_tenant_subject(self, reader_a, two_tenant_data):
        """Reader on tenant A must not see a tenant-B subject's licenses.

        Before the fix this returned 200 with tenant B's license body.
        """
        resp = reader_a.get(f"/licenses/user/{SUBJECT_B_SUB}")
        assert resp.status_code == 403, resp.text
        assert TENANT_B not in resp.text

    def test_reader_cannot_read_another_subject_on_same_tenant(self, reader_a, two_tenant_data):
        """Same tenant is not enough — the caller must BE the subject.

        Before the fix this returned 200 with the other user's licenses.
        """
        resp = reader_a.get(f"/licenses/user/{OTHER_A_SUB}")
        assert resp.status_code == 403, resp.text

    def test_reader_cannot_read_another_subject_by_internal_uuid(self, reader_a, two_tenant_data):
        """The check must survive the alternate id form (internal UUID, not sub)."""
        other_id = two_tenant_data["subjects"][OTHER_A_SUB].id
        resp = reader_a.get(f"/licenses/user/{other_id}")
        assert resp.status_code == 403, resp.text

    def test_reader_can_still_read_own_subject(self, reader_a, two_tenant_data):
        """Positive control: the endpoint still works for its legitimate caller."""
        resp = reader_a.get(f"/licenses/user/{READER_A_SUB}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body) == 1
        assert body[0]["tenant_id"] == TENANT_A

    def test_admin_can_still_read_any_subject_cross_tenant(self, admin_a, two_tenant_data):
        """Regression guard for portal-bff / provisioning: license-admin is
        deliberately cross-tenant (same contract as GET /licenses/admin/all)."""
        resp = admin_a.get(f"/licenses/user/{SUBJECT_B_SUB}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body) == 1
        assert body[0]["tenant_id"] == TENANT_B

    def test_trusted_proxy_may_read_another_subject_on_its_own_tenant(
        self, proxy_a, two_tenant_data
    ):
        """Regression guard for the w2v license gate.

        w2v-backend holds only license-reader/license-manager yet must resolve
        an END USER's licenses (subject != the SA's own sub). A flat
        caller==subject rule would 403 this and take the gate down.
        """
        resp = proxy_a.get(f"/licenses/user/{OTHER_A_SUB}")
        assert resp.status_code == 200, resp.text
        assert [lic["tenant_id"] for lic in resp.json()] == [TENANT_A]

    def test_trusted_proxy_is_still_tenant_scoped(self, proxy_a, two_tenant_data):
        """The proxy carve-out is DELEGATION, not cross-tenant access.

        Acting for tenant A, w2v-backend must not receive tenant B's licenses
        even though it may name a foreign subject. This is the half of the hole
        that actually mattered.
        """
        resp = proxy_a.get(f"/licenses/user/{SUBJECT_B_SUB}")
        assert resp.status_code == 200, resp.text
        assert resp.json() == []
        assert TENANT_B not in resp.text


class TestListAppLicensesIdor:
    """GET /apps/{app_id}/licenses — apps are a GLOBAL catalogue, so the app id
    authorizes nothing about tenancy."""

    def test_reader_sees_only_own_tenant_licenses(self, reader_a, two_tenant_data):
        """Before the fix this returned all three licenses across both tenants."""
        app_id = two_tenant_data["app"].id
        resp = reader_a.get(f"/apps/{app_id}/licenses")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        tenants = {lic["tenant_id"] for lic in body}
        assert tenants == {TENANT_A}
        assert len(body) == 2

    def test_admin_still_sees_all_tenants(self, admin_a, two_tenant_data):
        """Positive control: the operator surface is unchanged."""
        app_id = two_tenant_data["app"].id
        resp = admin_a.get(f"/apps/{app_id}/licenses")
        assert resp.status_code == 200, resp.text
        assert {lic["tenant_id"] for lic in resp.json()} == {TENANT_A, TENANT_B}


class TestGetTenantIdor:
    """GET /tenants/{name_or_id} — a foreign tenant must be indistinguishable
    from a non-existent one."""

    def test_reader_gets_404_not_403_for_foreign_tenant(self, reader_a, db_manager):
        """404, not 403: a 403 confirms the id exists, which IS the leak.

        Before the fix this returned 200 with the whole tenant dict.
        """
        resp = reader_a.get(f"/tenants/{TENANT_B}")
        assert resp.status_code == 404, resp.text

    def test_reader_gets_404_for_foreign_tenant_by_name(self, reader_a, db_manager):
        """The name form must not be a bypass of the uuid form."""
        resp = reader_a.get("/tenants/seed-tenant-other")
        assert resp.status_code == 404, resp.text

    def test_reader_can_still_read_own_tenant(self, reader_a):
        """Positive control."""
        resp = reader_a.get(f"/tenants/{TENANT_A}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["id"] == TENANT_A

    def test_admin_can_still_read_any_tenant(self, admin_a):
        """Positive control for the operator surface."""
        resp = admin_a.get(f"/tenants/{TENANT_B}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["id"] == TENANT_B


class TestListTenantsIdor:
    """GET /tenants — used to hand the entire customer roster to any reader."""

    def test_reader_sees_only_own_tenant(self, reader_a, db_manager):
        """Before the fix this returned every tenants row."""
        db_manager.create_tenant(name="unrelated-customer", owner_key="platform")
        resp = reader_a.get("/tenants")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert [t["id"] for t in body] == [TENANT_A]
        assert "unrelated-customer" not in resp.text

    def test_admin_still_sees_all_tenants(self, admin_a, db_manager):
        """Positive control: operator listing unchanged."""
        db_manager.create_tenant(name="unrelated-customer", owner_key="platform")
        resp = admin_a.get("/tenants")
        assert resp.status_code == 200, resp.text
        names = {t["name"] for t in resp.json()}
        assert "unrelated-customer" in names
        assert {"seed-tenant-primary", "seed-tenant-other"} <= names
