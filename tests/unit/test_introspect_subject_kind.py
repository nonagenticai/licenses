"""``/license/introspect`` says whether the licence holder is the TENANT or a MEMBER.

Why: MCP grants care-bus tool scope to the tenant's bundle licence
(``svc:<tenant>:care-bus``) only, never to a person's per-login care-bus licence. The
bundle licence's sub is not UUID-shaped, so its ``subject_keycloak_sub`` is null — and so
is a member's whose stored sub does not qualify as a Keycloak identity. ``subject_kind``
(``models.is_tenant_level_subject`` on the RAW sub + realm) tells them apart.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from models import Subject
from tests.support.sql_counter import count_statements

TENANT = "01000000-0000-7000-8000-000000000001"
KC_SUB = "5b1c7e2a-4f6d-4a8e-9c3b-2d1e0f9a8b7c"


def _raw_subject(db_manager, keycloak_sub: str, realm: str) -> Subject:
    session = db_manager.get_session()
    try:
        s = Subject(keycloak_sub=keycloak_sub, keycloak_realm=realm)
        session.add(s)
        session.commit()
        session.refresh(s)
        return s
    finally:
        session.close()


def _token(db_manager, subject, seed_app, seed_plan) -> str:
    lic = db_manager.create_license(
        subject_id=UUID(str(subject.id)),
        app_id=UUID(str(seed_app.id)),
        plan_id=UUID(str(seed_plan.id)),
        tenant_id=TENANT,
        status="active",
        starts_at=datetime.now(UTC) - timedelta(days=1),
        expires_at=datetime.now(UTC) + timedelta(days=30),
    )
    _tok, raw = db_manager.create_token(
        license_id=UUID(str(lic.id)),
        token_type="api",
        expires_at=datetime.now(UTC) + timedelta(days=30),
    )
    return raw


def _introspect(db_manager, sub: str, realm: str, seed_app, seed_plan) -> dict:
    subject = _raw_subject(db_manager, sub, realm)
    result = db_manager.introspect_token(_token(db_manager, subject, seed_app, seed_plan))
    assert result["active"] is True
    return result


class TestSubjectKind:
    def test_tenant_level_bundle_licence_is_tenant(self, db_manager, seed_app, seed_plan):
        result = _introspect(db_manager, f"svc:{TENANT}:care-bus", "service", seed_app, seed_plan)
        assert result["subject_kind"] == "tenant"
        # The bundle licence's sub is no Keycloak identity: this field alone was null.
        assert result["subject_keycloak_sub"] is None

    def test_per_login_member_licence_is_member(self, db_manager, seed_app, seed_plan):
        result = _introspect(db_manager, KC_SUB, "care-bus", seed_app, seed_plan)
        assert result["subject_kind"] == "member"
        assert result["subject_keycloak_sub"] == KC_SUB

    @pytest.mark.parametrize("sub", ["not-a-uuid", "user@example.com", "svc-lookalike"])
    def test_member_whose_sub_is_not_an_identity_is_still_member(
        self, db_manager, seed_app, seed_plan, sub
    ):
        """The case the field exists for: subject_keycloak_sub null, yet a PERSON."""
        result = _introspect(db_manager, sub, "care-bus", seed_app, seed_plan)
        assert result["subject_keycloak_sub"] is None
        assert result["subject_kind"] == "member"

    def test_member_named_by_another_subjects_id_is_member(self, db_manager, seed_app, seed_plan):
        victim = _raw_subject(db_manager, KC_SUB, "care-bus")
        result = _introspect(db_manager, str(victim.id), "care-bus", seed_app, seed_plan)
        assert result["subject_keycloak_sub"] is None
        assert result["subject_kind"] == "member"

    def test_inactive_answer_carries_no_kind(self, db_manager):
        result = db_manager.introspect_token("not-a-real-token")
        assert result["active"] is False
        assert "subject_kind" not in result

    def test_no_extra_statement(self, db_manager, seed_app, seed_plan):
        subject = _raw_subject(db_manager, f"svc:{TENANT}:care-bus", "service")
        raw = _token(db_manager, subject, seed_app, seed_plan)
        with count_statements(db_manager.engine) as rec:
            result = db_manager.introspect_token(raw)
        assert result["subject_kind"] == "tenant"
        assert rec.verbs["select"] == 2, "\n".join(rec.statements)

    @pytest.mark.parametrize(
        ("sub", "realm", "kind"),
        [(f"svc:{TENANT}:care-bus", "service", "tenant"), ("not-a-uuid", "care-bus", "member")],
    )
    def test_http_response_exposes_the_field(
        self, client: TestClient, db_manager, seed_app, seed_plan, sub, realm, kind
    ):
        raw = _token(db_manager, _raw_subject(db_manager, sub, realm), seed_app, seed_plan)
        response = client.post("/license/introspect", json={"token": raw})
        assert response.status_code == 200
        assert response.json()["subject_kind"] == kind
