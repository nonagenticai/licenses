"""Integration tests for licenses token endpoints."""

import uuid

import pytest

pytestmark = pytest.mark.integration


class TestTokenCreate:
    def test_create_api_token(self, licenses_client, test_license):
        r = licenses_client.post(f"/licenses/{test_license['id']}/tokens")
        assert r.status_code == 201, r.text
        data = r.json()
        assert "token" in data
        assert data["license_id"] == test_license["id"]
        assert data["token_type"] == "api"

    def test_create_node_token(self, licenses_client, test_license):
        r = licenses_client.post(
            f"/licenses/{test_license['id']}/tokens",
            # licenses#244: a node token needs an explicit expiry (or a registered
            # long-lived mint); without either it is refused with 422.
            params={"token_type": "node", "expires_days": 30},
        )
        assert r.status_code == 201, r.text
        assert r.json()["token_type"] == "node"

    def test_create_internal_token(self, licenses_client, test_license):
        r = licenses_client.post(
            f"/licenses/{test_license['id']}/tokens",
            # licenses#244: a internal token needs an explicit expiry (or a registered
            # long-lived mint); without either it is refused with 422.
            params={"token_type": "internal", "expires_days": 30},
        )
        assert r.status_code == 201, r.text
        assert r.json()["token_type"] == "internal"

    def test_create_token_with_expiration(self, licenses_client, test_license):
        r = licenses_client.post(
            f"/licenses/{test_license['id']}/tokens",
            params={"expires_days": 30},
        )
        assert r.status_code == 201, r.text
        assert r.json()["expires_at"] is not None

    def test_create_token_nonexistent_license(self, licenses_client):
        r = licenses_client.post(f"/licenses/{uuid.uuid4()}/tokens")
        assert r.status_code in (404, 500), r.text


class TestTokenIntrospect:
    def test_introspect_valid_token(self, licenses_client, test_license):
        # First create a token
        tr = licenses_client.post(f"/licenses/{test_license['id']}/tokens")
        assert tr.status_code == 201, tr.text
        token_str = tr.json()["token"]

        # Introspect it
        r = licenses_client.post("/license/introspect", json={"token": token_str})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["active"] is True
        assert data["license_id"] == test_license["id"]

    def test_introspect_invalid_token(self, licenses_client):
        r = licenses_client.post("/license/introspect", json={"token": "invalid-token-value"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["active"] is False

    def test_introspect_empty_token(self, licenses_client):
        r = licenses_client.post("/license/introspect", json={"token": ""})
        assert r.status_code == 200, r.text
        assert r.json()["active"] is False

    def test_introspect_missing_token_field(self, licenses_client):
        r = licenses_client.post("/license/introspect", json={})
        assert r.status_code == 422, r.text


class TestTokenRevoke:
    def test_revoke_by_token(self, licenses_client, test_license):
        # Create a token
        tr = licenses_client.post(f"/licenses/{test_license['id']}/tokens")
        assert tr.status_code == 201, tr.text
        token_str = tr.json()["token"]

        # Revoke it
        r = licenses_client.post("/license/revoke", json={"token": token_str})
        assert r.status_code == 200, r.text
        assert r.json()["revoked"] is True

    def test_revoke_by_license_id(self, licenses_client, test_app, test_plan):
        # Create a fresh license with tokens
        subject_id = f"revoke-test-{uuid.uuid4().hex[:8]}"
        lr = licenses_client.post(
            f"/licenses/user/{subject_id}",
            json={"app_id": test_app["id"], "plan_id": test_plan["id"]},
        )
        assert lr.status_code == 201, lr.text
        lic_id = lr.json()["id"]

        # Create a token
        tr = licenses_client.post(f"/licenses/{lic_id}/tokens")
        assert tr.status_code == 201, tr.text

        # Revoke all tokens for this license
        r = licenses_client.post("/license/revoke", json={"license_id": lic_id})
        assert r.status_code == 200, r.text
        assert r.json()["revoked"] is True

    def test_revoke_nonexistent_token(self, licenses_client):
        r = licenses_client.post("/license/revoke", json={"token": "nonexistent-token"})
        assert r.status_code == 200, r.text
        assert r.json()["revoked"] is False
