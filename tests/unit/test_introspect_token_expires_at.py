"""``/license/introspect`` reports the presented token's OWN expiry (licenses #240).

Why: a caller holding a licence token could see the LICENCE's ``expires_at`` but not
the token's, so it had no way to refresh a token before it lapsed — and the two are
independent (a 30-day token on a perpetual licence, or a perpetual token on a licence
that ends next week). ``token_expires_at`` is that missing field.

``null`` means the token never expires. ``GET /licenses/active`` resolves an
entitlement with NO token, so the field is ``null`` there too ("not applicable").
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi.testclient import TestClient

from models import Token
from tests.conftest import insert_legacy_perpetual_token
from tests.support.sql_counter import count_statements

TENANT = "01000000-0000-7000-8000-000000000001"


def _licence(db_manager, seed_subject, seed_app, seed_plan, *, expires_at):
    return db_manager.create_license(
        subject_id=UUID(str(seed_subject.id)),
        app_id=UUID(str(seed_app.id)),
        plan_id=UUID(str(seed_plan.id)),
        tenant_id=TENANT,
        status="active",
        starts_at=datetime.now(UTC) - timedelta(days=1),
        expires_at=expires_at,
    )


def _token(db_manager, lic, *, expires_at) -> tuple[Token, str]:
    return db_manager.create_token(
        license_id=UUID(str(lic.id)), token_type="api", expires_at=expires_at
    )


class TestIntrospectTokenExpiresAt:
    def test_expiring_token_reports_its_own_expiry(
        self, db_manager, seed_subject, seed_app, seed_plan
    ):
        lic_exp = datetime.now(UTC) + timedelta(days=365)
        tok_exp = datetime.now(UTC) + timedelta(days=7)
        lic = _licence(db_manager, seed_subject, seed_app, seed_plan, expires_at=lic_exp)
        tok, raw = _token(db_manager, lic, expires_at=tok_exp)

        result = db_manager.introspect_token(raw)

        assert result["active"] is True
        assert result["token_expires_at"] == tok.expires_at.isoformat()
        assert datetime.fromisoformat(result["token_expires_at"]) == tok_exp
        # Independent of the licence-level expiry, which is unchanged.
        assert result["token_expires_at"] != result["expires_at"]
        assert datetime.fromisoformat(result["expires_at"]) == lic_exp

    def test_perpetual_token_reports_null(self, db_manager, seed_subject, seed_app, seed_plan):
        lic_exp = datetime.now(UTC) + timedelta(days=30)
        lic = _licence(db_manager, seed_subject, seed_app, seed_plan, expires_at=lic_exp)
        # A legacy (pre-licenses#244) perpetual row: new mints can no longer be one.
        _tok, raw = insert_legacy_perpetual_token(db_manager, lic.id)

        result = db_manager.introspect_token(raw)

        assert result["active"] is True
        assert result["token_expires_at"] is None
        # A perpetual TOKEN does not make the licence perpetual.
        assert datetime.fromisoformat(result["expires_at"]) == lic_exp

    def test_expiring_token_on_a_perpetual_licence(
        self, db_manager, seed_subject, seed_app, seed_plan
    ):
        tok_exp = datetime.now(UTC) + timedelta(days=3)
        lic = _licence(db_manager, seed_subject, seed_app, seed_plan, expires_at=None)
        _tok, raw = _token(db_manager, lic, expires_at=tok_exp)

        result = db_manager.introspect_token(raw)

        assert result["expires_at"] is None
        assert datetime.fromisoformat(result["token_expires_at"]) == tok_exp

    def test_field_adds_no_statement(self, db_manager, seed_subject, seed_app, seed_plan):
        lic = _licence(db_manager, seed_subject, seed_app, seed_plan, expires_at=None)
        _tok, raw = _token(db_manager, lic, expires_at=datetime.now(UTC) + timedelta(days=1))
        with count_statements(db_manager.engine) as rec:
            result = db_manager.introspect_token(raw)
        assert result["token_expires_at"] is not None
        assert rec.verbs["select"] == 2, "\n".join(rec.statements)

    def test_inactive_answer_carries_no_token_expiry(self, db_manager):
        result = db_manager.introspect_token("not-a-real-token")
        assert result["active"] is False
        assert "token_expires_at" not in result

    def test_http_introspect_exposes_the_field(
        self, client: TestClient, db_manager, seed_subject, seed_app, seed_plan
    ):
        tok_exp = datetime.now(UTC) + timedelta(days=7)
        lic = _licence(db_manager, seed_subject, seed_app, seed_plan, expires_at=None)
        _tok, raw = _token(db_manager, lic, expires_at=tok_exp)

        body = client.post("/license/introspect", json={"token": raw}).json()

        assert body["active"] is True
        assert datetime.fromisoformat(body["token_expires_at"]) == tok_exp
        assert body["expires_at"] is None


class TestLicensesActiveHasNoToken:
    def test_licenses_active_reports_null(self, auth_client_admin: TestClient, full_seed_data):
        response = auth_client_admin.get(
            "/licenses/active", params={"tenant_id": TENANT, "app_key": "test-app"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["active"] is True
        assert "token_expires_at" in body
        assert body["token_expires_at"] is None
