"""Integration tests for licenses health endpoints."""

import pytest

pytestmark = pytest.mark.integration


class TestHealthCheck:
    def test_health_returns_200(self, licenses_client):
        r = licenses_client.get("/health")
        assert r.status_code == 200, r.text

    def test_health_has_status(self, licenses_client):
        data = licenses_client.get("/health").json()
        assert "status" in data

    def test_health_has_service_name(self, licenses_client):
        data = licenses_client.get("/health").json()
        assert "service" in data

    def test_health_reports_not_checked(self, licenses_client):
        """``assert "database" in data`` was vacuous — it passed against the old
        hardcoded ``"connected"``, which is precisely the lie this endpoint used
        to tell. /health opens no session, so the only honest value is
        ``not_checked``; database truth lives on /health/ready."""
        data = licenses_client.get("/health").json()
        assert data["database"] == "not_checked"


class TestLiveness:
    def test_live_returns_200(self, licenses_client):
        r = licenses_client.get("/health/live")
        assert r.status_code == 200, r.text

    def test_live_has_status(self, licenses_client):
        data = licenses_client.get("/health/live").json()
        assert "status" in data


class TestReadiness:
    def test_ready_returns_200(self, licenses_client):
        r = licenses_client.get("/health/ready")
        assert r.status_code == 200, r.text

    def test_ready_has_status(self, licenses_client):
        data = licenses_client.get("/health/ready").json()
        assert "status" in data

    def test_ready_reports_connected(self, licenses_client):
        """The readiness probe target — and since gitops #2553 the only signal
        that pulls a database-less pod out of the EndpointSlice. A membership
        check could not tell ``connected`` from anything else."""
        data = licenses_client.get("/health/ready").json()
        assert data["database"] == "connected"
