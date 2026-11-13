"""``/license/introspect`` exposes the licence subject's Keycloak ``sub`` — only when it IS one.

Why: ``subject_id`` is THIS service's row id. Ingestion records a run's owner as the
Keycloak ``sub`` it was forwarded (fr -> kag -> ingestion), so on the licence path it
compared that owner against ``subject_id`` and a licence holder could never purge their
own run (403). ``subject_keycloak_sub`` is the link that makes the two comparable.

⛔ ``subject.keycloak_sub`` is NOT guaranteed to be a Keycloak sub. ``POST
/licenses/user/{id}`` used to mint a new subject whose keycloak_sub was ANOTHER subject's
row id (2 of 282 on prod, 2026-09-27; one carries fr-test's licence subject id). Emitting
that value would let the minted subject's token claim every run recorded under the other
subject. So the field is None unless the value is UUID-shaped AND names no subject row,
and ``create_or_get_subject`` now resolves a row id instead of registering it as a sub.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import UUID

from fastapi.testclient import TestClient

from models import Subject, keycloak_sub_as_identity
from tests.support.sql_counter import count_statements

KC_SUB = "5b1c7e2a-4f6d-4a8e-9c3b-2d1e0f9a8b7c"  # uuid4, the shape Keycloak issues
KC_SUB_2 = "a3e9d1c0-7b24-4f15-8e6a-9c0d2b4f1e36"
TENANT = "01000000-0000-7000-8000-000000000001"


def _subject(db_manager, keycloak_sub: str) -> Subject:
    return db_manager.create_or_get_subject(keycloak_sub=keycloak_sub, keycloak_realm="secure-apps")


def _raw_subject(db_manager, keycloak_sub: str) -> Subject:
    """Insert a subject row directly — the only way to reproduce the legacy bad shape now
    that create_or_get_subject refuses to mint it."""
    session = db_manager.get_session()
    try:
        s = Subject(keycloak_sub=keycloak_sub, keycloak_realm="secure-apps")
        session.add(s)
        session.commit()
        session.refresh(s)
        return s
    finally:
        session.close()


def _token_for_subject(db_manager, subject, seed_app, seed_plan) -> str:
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


class TestIntrospectExposesSubjectKeycloakSub:
    def test_payload_carries_the_licence_subjects_keycloak_sub(
        self, db_manager, seed_app, seed_plan
    ):
        subject = _subject(db_manager, KC_SUB)
        result = db_manager.introspect_token(
            _token_for_subject(db_manager, subject, seed_app, seed_plan)
        )
        assert result["active"] is True
        assert result["subject_id"] == str(subject.id)
        assert result["subject_keycloak_sub"] == KC_SUB
        # The two ids are genuinely different values — the whole reason the link is needed.
        assert result["subject_keycloak_sub"] != result["subject_id"]

    def test_a_second_seat_gets_its_own_sub_never_the_first(self, db_manager, seed_app, seed_plan):
        """Negative: two seats of one (tenant, app) must not share or swap identities."""
        first_s = _subject(db_manager, KC_SUB)
        second_s = _subject(db_manager, KC_SUB_2)
        first = db_manager.introspect_token(
            _token_for_subject(db_manager, first_s, seed_app, seed_plan)
        )
        second = db_manager.introspect_token(
            _token_for_subject(db_manager, second_s, seed_app, seed_plan)
        )
        assert first["subject_keycloak_sub"] == KC_SUB
        assert second["subject_keycloak_sub"] == KC_SUB_2
        assert second["subject_id"] == str(second_s.id)

    def test_inactive_answer_carries_no_subject_identity(self, db_manager):
        result = db_manager.introspect_token("not-a-real-token")
        assert result["active"] is False
        assert "subject_keycloak_sub" not in result

    def test_subject_join_adds_no_statement(self, db_manager, seed_app, seed_plan):
        """Subject AND the collision EXISTS ride the existing joined SELECT: still 2 SELECTs."""
        raw = _token_for_subject(db_manager, _subject(db_manager, KC_SUB), seed_app, seed_plan)
        with count_statements(db_manager.engine) as rec:
            result = db_manager.introspect_token(raw)
        assert result["subject_keycloak_sub"] == KC_SUB
        assert rec.verbs["select"] == 2, "\n".join(rec.statements)

    def test_http_response_exposes_the_field(
        self, client: TestClient, db_manager, seed_app, seed_plan
    ):
        raw = _token_for_subject(db_manager, _subject(db_manager, KC_SUB), seed_app, seed_plan)
        response = client.post("/license/introspect", json={"token": raw})
        assert response.status_code == 200
        assert response.json()["subject_keycloak_sub"] == KC_SUB


class TestSubjectKeycloakSubIsNeverAnotherSubjectsId:
    """⛔ The attack: a subject whose keycloak_sub equals ANOTHER subject's row id."""

    def test_token_of_a_subject_named_by_another_subjects_id_gets_none(
        self, db_manager, seed_app, seed_plan
    ):
        victim = _subject(db_manager, KC_SUB)  # fr-test's licence subject, in prod terms
        impostor = _raw_subject(db_manager, str(victim.id))  # 019f5237's shape
        result = db_manager.introspect_token(
            _token_for_subject(db_manager, impostor, seed_app, seed_plan)
        )
        assert result["active"] is True
        assert result["subject_id"] == str(impostor.id)
        assert result["subject_keycloak_sub"] is None
        # ...and the victim's own token is unaffected.
        victim_result = db_manager.introspect_token(
            _token_for_subject(db_manager, victim, seed_app, seed_plan)
        )
        assert victim_result["subject_keycloak_sub"] == KC_SUB

    def test_case_variant_of_another_subjects_id_also_gets_none(
        self, db_manager, seed_app, seed_plan
    ):
        victim = _subject(db_manager, KC_SUB)
        impostor = _raw_subject(db_manager, str(victim.id).upper())
        result = db_manager.introspect_token(
            _token_for_subject(db_manager, impostor, seed_app, seed_plan)
        )
        assert result["subject_keycloak_sub"] is None

    def test_over_http_the_attack_yields_null(
        self, client: TestClient, db_manager, seed_app, seed_plan
    ):
        victim = _subject(db_manager, KC_SUB)
        impostor = _raw_subject(db_manager, str(victim.id))
        raw = _token_for_subject(db_manager, impostor, seed_app, seed_plan)
        body = client.post("/license/introspect", json={"token": raw}).json()
        assert body["active"] is True
        assert body["subject_keycloak_sub"] is None
        assert str(victim.id) not in (body["subject_keycloak_sub"] or "")

    def test_non_uuid_sub_is_not_presented_as_a_keycloak_identity(self, db_manager, seed_token):
        """``test-user-sub`` / ``svc-*`` are registry labels, never a Keycloak JWT sub."""
        _token, raw = seed_token
        assert db_manager.introspect_token(raw)["subject_keycloak_sub"] is None

    def test_helper_fails_closed(self):
        assert keycloak_sub_as_identity(KC_SUB, False) == KC_SUB
        assert keycloak_sub_as_identity(KC_SUB, True) is None
        assert keycloak_sub_as_identity(KC_SUB, None) is None  # unknown => collision
        assert keycloak_sub_as_identity("svc-w2v-tenant-dev", False) is None
        assert keycloak_sub_as_identity(None, False) is None
        assert keycloak_sub_as_identity(KC_SUB + "x", False) is None


class TestCreateOrGetSubjectResolvesARowId:
    """``create_or_get_subject`` never mints a subject whose keycloak_sub is a subject id."""

    def test_row_id_resolves_to_that_subject(self, db_manager):
        victim = _subject(db_manager, KC_SUB)
        got = _subject(db_manager, str(victim.id))
        assert str(got.id) == str(victim.id)
        assert got.keycloak_sub == KC_SUB
        session = db_manager.get_session()
        try:
            assert (
                session.query(Subject).filter(Subject.keycloak_sub == str(victim.id)).count() == 0
            )
        finally:
            session.close()

    def test_uppercase_row_id_resolves_too(self, db_manager):
        victim = _subject(db_manager, KC_SUB)
        assert str(_subject(db_manager, str(victim.id).upper()).id) == str(victim.id)

    def test_unknown_uuid_still_creates_a_new_subject(self, db_manager):
        got = _subject(db_manager, KC_SUB_2)
        assert got.keycloak_sub == KC_SUB_2

    def test_existing_keycloak_sub_match_still_wins(self, db_manager):
        """Order is GET's: keycloak_sub first, then row id — existing rows unchanged."""
        a = _subject(db_manager, KC_SUB)
        assert str(_subject(db_manager, KC_SUB).id) == str(a.id)

    def test_post_route_with_a_row_id_licenses_that_subject(
        self, auth_client_admin: TestClient, db_manager, seed_app, seed_plan
    ):
        tenant = db_manager.create_tenant(name="ownerid-tenant", owner_key="platform")
        victim = _subject(db_manager, KC_SUB)
        with patch("main._provision_budget", new_callable=AsyncMock):
            response = auth_client_admin.post(
                f"/licenses/user/{victim.id}",
                json={"app_id": str(seed_app.id), "plan_id": str(seed_plan.id)},
                headers={"X-Tenant-Id": str(tenant.id)},
            )
        assert response.status_code == 201, response.text
        assert response.json()["subject_id"] == str(victim.id)
        session = db_manager.get_session()
        try:
            assert (
                session.query(Subject).filter(Subject.keycloak_sub == str(victim.id)).count() == 0
            )
        finally:
            session.close()
