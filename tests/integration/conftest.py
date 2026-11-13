"""Integration test fixtures for licenses endpoints.

Supports two modes:
1. LOCAL (default): Uses FastAPI TestClient + the DATABASE_URL tests/conftest.py
   resolved (explicit TEST_DATABASE_URL/DATABASE_URL, else an ephemeral postgres:18).
2. REMOTE: Set LICENSES_URL=https://licenses.example.com to hit live cluster services.

Run with:  uv run pytest tests/integration/ -v -m integration
Skip with: uv run pytest tests/ (excluded by default via addopts)
"""

import os
import sys
import time
import uuid

import httpx
import jwt
import pytest

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.support.jwt_keys import TEST_ISSUER, private_key_pem  # noqa: E402

LICENSES_URL = os.environ.get("LICENSES_URL", "")
# NO DEFAULT, deliberately. This used to fall back to a hardcoded literal that
# was a LIVE, UNREVOKED, PERPETUAL production api token for the `kag` app on
# tenant `n8n-dev` — created 2026-05-16 and still in active use when this was
# found on 2026-08-11. It sat in the repo, and in every clone's git history,
# for nearly three months. detect-secrets did not catch it: the value is a bare
# high-entropy string in the second argument of `os.environ.get`, which its
# keyword plugin does not treat as an assignment (it DID flag the DATABASE_URL
# five lines below).
#
# Remote mode now requires the token to come from the environment and fails
# loudly without it — see `licenses_client`. An empty default cannot
# accidentally authenticate against a live cluster.
LICENSE_TOKEN = os.environ.get("LICENSE_TOKEN", "")
TENANT_ID = os.environ.get("TENANT_ID", "0195f48b-d000-7000-8000-000000000001")

# Set by tests/conftest.py (explicit env, else an ephemeral postgres:18) — never a
# host default.
LOCAL_DB_URL = os.environ["DATABASE_URL"]


def _is_local_mode() -> bool:
    """True when no remote LICENSES_URL is configured."""
    return not LICENSES_URL or LICENSES_URL.startswith("http://localhost")


def _make_admin_jwt() -> str:
    """Create a REAL RS256-signed JWT with admin roles.

    The middleware verifies signatures (auth/jwt_verify.py), so this must be
    genuinely signed by the throwaway test keypair. Only meaningful in local
    mode, where `_trust_test_signing_key` in tests/conftest.py points the JWKS
    seam at that key; against a REMOTE LICENSES_URL this token will (correctly)
    be rejected, because that deployment trusts only real Keycloak issuers.
    """
    payload = {
        "sub": "integration-test-admin",
        "preferred_username": "integration-admin",
        "email": "admin@integration-test.local",
        "realm_access": {"roles": ["license-admin", "license-manager", "license-reader"]},
        "resource_access": {},
        "exp": int(time.time()) + 3600,
        "iat": int(time.time()),
        "iss": TEST_ISSUER,
        # Migration 050: the break-glass owning client (secure-apps realm), so the
        # X-Tenant-Id below may target the platform-owned integration tenant and
        # issue any app — these tests exercise the catalogue, not ownership.
        "azp": "licenses-admin",
    }
    return jwt.encode(payload, private_key_pem(), algorithm="RS256")


class _TestClientAdapter:
    """Wraps FastAPI TestClient to match httpx.Client API used by tests."""

    def __init__(self, test_client):
        self._tc = test_client

    def get(self, url, **kwargs):
        return self._tc.get(url, **kwargs)

    def post(self, url, **kwargs):
        return self._tc.post(url, **kwargs)

    def put(self, url, **kwargs):
        return self._tc.put(url, **kwargs)

    def patch(self, url, **kwargs):
        return self._tc.patch(url, **kwargs)

    def delete(self, url, **kwargs):
        return self._tc.delete(url, **kwargs)


def delete_app_confirmed(licenses_client, app_id):
    """Delete an app, acknowledging the cross-tenant cascade when one is required.

    ``DELETE /apps/{id}`` refuses with 409 when the delete would destroy licences,
    and reports the exact live count. Teardown genuinely does intend to destroy
    everything the test created, so behave the way a real caller must: read the
    count out of the refusal and re-issue with it. This goes THROUGH the guard —
    it never bypasses it.

    Without this, an unchecked teardown ``delete`` silently 409s and leaks a
    GLOBAL catalogue row per run: several tests create licences directly on the
    ``test_app`` fixture and never remove them.
    """
    r = licenses_client.delete(f"/apps/{app_id}")
    if r.status_code == 409:
        detail = r.json()["detail"]
        r = licenses_client.delete(f"/apps/{app_id}?confirm_licenses={detail['licenses']}")
    return r


@pytest.fixture(scope="session")
def licenses_client():
    """Client for licenses service -- TestClient locally, httpx.Client remotely."""
    if _is_local_mode():
        # Set env vars BEFORE importing main (module-level reads them)
        os.environ["DATABASE_URL"] = LOCAL_DB_URL
        # No DEFAULT_TENANT_ID: main.py never read it for request handling — it
        # only decorated the published X-Tenant-Id parameter, and that concrete
        # value is gone. Setting it here implied a server-side default tenant
        # that never existed. Tests carry the tenant in the header, as callers do.
        os.environ["ENVIRONMENT"] = "development"
        os.environ["ENABLE_DOCS"] = "true"
        os.environ["POSTGRES_SSLMODE"] = "disable"

        from fastapi.testclient import TestClient

        import main

        # Create tables using sync engine
        from models import Base

        Base.metadata.create_all(bind=main.db_manager.engine)

        # Register the client's tenant so POST /licenses/user/{sub} passes the
        # "tenant must be a registered tenant" mint guard (the license.tenant_id
        # -> tenants.id FK invariant). Idempotent. In REMOTE mode the target
        # deployment is expected to already have this tenant registered.
        main.db_manager.seed_ownership_reference()  # migration 050 owners/clients
        main.db_manager.ensure_tenant(
            TENANT_ID, name="integration-test-tenant", owner_key="platform"
        )

        mock_jwt = _make_admin_jwt()

        with TestClient(main.app, raise_server_exceptions=False) as tc:
            tc.headers["Authorization"] = f"Bearer {mock_jwt}"
            tc.headers["X-Tenant-Id"] = TENANT_ID
            yield _TestClientAdapter(tc)
    else:
        if not LICENSE_TOKEN:
            raise RuntimeError(
                "LICENSE_TOKEN must be set to run the integration tests in REMOTE mode "
                f"(LICENSES_URL={LICENSES_URL!r}). There is deliberately no default: the "
                "previous one was a live production token committed to the repo. Export a "
                "token for the target environment, or unset LICENSES_URL to run locally."
            )
        with httpx.Client(
            base_url=LICENSES_URL,
            headers={
                "Authorization": f"Bearer {LICENSE_TOKEN}",
                "X-Tenant-Id": TENANT_ID,
            },
            verify=False,
            timeout=30.0,
        ) as client:
            yield client


@pytest.fixture
def test_region(licenses_client):
    """Create a test region and clean up after."""
    code = f"test-{uuid.uuid4().hex[:8]}"
    r = licenses_client.post("/regions", json={"code": code, "name": f"Test Region {code}"})
    assert r.status_code == 201, r.text
    region = r.json()
    yield region
    # No delete endpoint for regions -- leave it (test data is harmless)


@pytest.fixture
def test_app(licenses_client):
    """Create a test app and clean up after."""
    key = f"inttest-{uuid.uuid4().hex[:8]}"
    r = licenses_client.post("/apps", json={"key": key, "name": f"Integration Test App {key}"})
    assert r.status_code == 201, r.text
    app = r.json()
    yield app
    delete_app_confirmed(licenses_client, app["id"])


@pytest.fixture
def test_plan(licenses_client, test_app):
    """Create a test plan linked to test_app."""
    code = f"plan-{uuid.uuid4().hex[:8]}"
    r = licenses_client.post(
        "/plans",
        json={
            "app_id": test_app["id"],
            "code": code,
            "name": f"Test Plan {code}",
        },
    )
    assert r.status_code == 201, r.text
    plan = r.json()
    yield plan
    # Plan cascades when app is deleted; explicit delete may 404 if app fixture already ran


@pytest.fixture
def test_license(licenses_client, test_app, test_plan):
    """Create a test license for a generated subject."""
    subject_id = f"test-subject-{uuid.uuid4().hex[:8]}"
    r = licenses_client.post(
        f"/licenses/user/{subject_id}",
        json={"app_id": test_app["id"], "plan_id": test_plan["id"]},
    )
    assert r.status_code == 201, r.text
    lic = r.json()
    yield lic
    # Clean up all licences for this subject (cascades tokens via FK).
    # ⛔ MUST CONFIRM. Soft delete is the default as of this branch, so an
    # unconfirmed DELETE is refused with 409 and this teardown would silently
    # clean up NOTHING — leaking rows into every later test in the session.
    # The radius comes from the refusal itself, so the fixture never has to
    # know how many licences the test happened to mint.
    radius = licenses_client.delete(f"/licenses/user/{subject_id}")
    if radius.status_code == 409:
        count = radius.json()["detail"]["licenses"]
        licenses_client.delete(f"/licenses/user/{subject_id}?confirm_licenses={count}")
