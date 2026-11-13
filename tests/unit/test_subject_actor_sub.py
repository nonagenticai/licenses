"""``subject.actor_sub`` + introspection's ``subject_actor_sub`` (migration 047).

Why: license-authz stamps metering events with ``actor_id`` = the licence subject's
Keycloak sub. Most service subjects are registry LABELS (``svc-kag``), so
``subject_keycloak_sub`` is None for them and the events carried no actor. Owner rule
2026-10-07: a service's actor is its OWN Keycloak service-account sub. 047 states it in
``subject.actor_sub`` WITHOUT touching ``keycloak_sub`` (the runtime lookup key), and
introspection returns ``subject_actor_sub``: keycloak_sub when it is a trusted sub, else
actor_sub, else None.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient

from models import Subject, actor_identity
from scripts.apply_migrations import _run_file, split_statements
from tests.conftest import TEST_DATABASE_URL
from tests.support.sql_counter import count_statements

REPO = Path(__file__).resolve().parents[2]
UP = REPO / "migrations" / "047_subject_actor_sub.sql"
DOWN = REPO / "migrations" / "downgrades" / "047_subject_actor_sub.sql"

KC_SUB = "5b1c7e2a-4f6d-4a8e-9c3b-2d1e0f9a8b7c"  # a human's Keycloak sub
SA_SUB = "a52d3c9a-11ea-46a3-aa49-8651ae166391"  # svc-kag's SA sub, as 047 states it
OTHER_SA = "0e4c0bde-3f1c-4d6b-9a52-7c1d2e3f4a5b"
TENANT = "01000000-0000-7000-8000-000000000001"

#: ``('<keycloak_sub>', '<uuid>'::uuid)`` — the VALUES rows of 047.
_ROW = re.compile(r"\(\s*'([^']+)'\s*,\s*'([0-9a-f-]{36})'::uuid\s*\)")


def _subject(db_manager, keycloak_sub: str, actor_sub: str | None = None) -> Subject:
    session = db_manager.get_session()
    try:
        s = Subject(
            keycloak_sub=keycloak_sub,
            keycloak_realm="service",
            actor_sub=UUID(actor_sub) if actor_sub else None,
        )
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


class TestIntrospectReturnsTheActor:
    def test_label_subject_with_a_stated_actor_returns_the_sa_sub(
        self, db_manager, seed_app, seed_plan
    ):
        subject = _subject(db_manager, "svc-kag", SA_SUB)
        result = db_manager.introspect_token(_token(db_manager, subject, seed_app, seed_plan))
        assert result["active"] is True
        assert result["subject_actor_sub"] == SA_SUB
        # The identity field is UNCHANGED: a label is still not a Keycloak sub.
        assert result["subject_keycloak_sub"] is None

    def test_label_subject_without_an_actor_returns_none(self, db_manager, seed_app, seed_plan):
        subject = _subject(db_manager, "svc-coding-ai")
        result = db_manager.introspect_token(_token(db_manager, subject, seed_app, seed_plan))
        assert result["subject_actor_sub"] is None

    def test_a_real_keycloak_sub_wins_over_a_stated_actor(self, db_manager, seed_app, seed_plan):
        subject = _subject(db_manager, KC_SUB, OTHER_SA)
        result = db_manager.introspect_token(_token(db_manager, subject, seed_app, seed_plan))
        assert result["subject_keycloak_sub"] == KC_SUB
        assert result["subject_actor_sub"] == KC_SUB

    def test_a_human_subject_without_actor_sub_still_has_an_actor(
        self, db_manager, seed_app, seed_plan
    ):
        subject = _subject(db_manager, KC_SUB)
        result = db_manager.introspect_token(_token(db_manager, subject, seed_app, seed_plan))
        assert result["subject_actor_sub"] == KC_SUB

    def test_a_subject_named_by_another_subjects_id_falls_back_to_actor_sub_only(
        self, db_manager, seed_app, seed_plan
    ):
        """The collision guard still holds: the borrowed row id is never the actor."""
        victim = _subject(db_manager, KC_SUB)
        impostor = _subject(db_manager, str(victim.id))
        result = db_manager.introspect_token(_token(db_manager, impostor, seed_app, seed_plan))
        assert result["subject_actor_sub"] is None
        assert str(victim.id) != result["subject_actor_sub"]

    def test_inactive_answer_carries_no_actor(self, db_manager):
        assert "subject_actor_sub" not in db_manager.introspect_token("not-a-real-token")

    def test_actor_adds_no_statement(self, db_manager, seed_app, seed_plan):
        raw = _token(db_manager, _subject(db_manager, "svc-kag", SA_SUB), seed_app, seed_plan)
        with count_statements(db_manager.engine) as rec:
            result = db_manager.introspect_token(raw)
        assert result["subject_actor_sub"] == SA_SUB
        assert rec.verbs["select"] == 2, "\n".join(rec.statements)

    def test_http_response_exposes_the_field(
        self, client: TestClient, db_manager, seed_app, seed_plan
    ):
        raw = _token(db_manager, _subject(db_manager, "svc-kag", SA_SUB), seed_app, seed_plan)
        body = client.post("/license/introspect", json={"token": raw}).json()
        assert body["subject_actor_sub"] == SA_SUB
        assert body["subject_keycloak_sub"] is None

    def test_helper(self):
        assert actor_identity(KC_SUB, UUID(OTHER_SA)) == KC_SUB
        assert actor_identity(None, UUID(SA_SUB)) == SA_SUB
        assert actor_identity(None, None) is None


# --------------------------------------------------------------------------- 047 itself


def _rows(path: Path) -> list[tuple[str, str]]:
    return _ROW.findall(path.read_text(encoding="utf-8"))


@pytest.fixture
def conn(db_manager):
    c = psycopg.connect(TEST_DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1))
    try:
        yield c
    finally:
        c.rollback()
        c.close()


def _insert(conn, keycloak_sub: str, actor_sub: str | None = None) -> None:
    conn.execute(
        "INSERT INTO subject (keycloak_sub, keycloak_realm, actor_sub) VALUES (%s, 'service', %s)",
        (keycloak_sub, actor_sub),
    )


def _actor(conn, keycloak_sub: str) -> str | None:
    row = conn.execute(
        "SELECT actor_sub FROM subject WHERE keycloak_sub = %s", (keycloak_sub,)
    ).fetchone()
    assert row is not None
    return None if row[0] is None else str(row[0])


class TestMigrationText:
    def test_the_mapping_is_the_reviewed_set(self):
        """Positive control for every test that reads ``_rows`` (an empty match is vacuous)."""
        rows = _rows(UP)
        assert len(rows) == 31
        assert len({k for k, _ in rows}) == 31, "a subject is mapped twice"

    def test_no_tenant_level_or_per_tenant_app_licence_is_mapped(self):
        """Those licences are the TENANT's for an app; the app's SA is not who acts."""
        names = {k for k, _ in _rows(UP)}
        assert not {n for n in names if n.startswith("svc:")}
        assert not {n for n in names if n.startswith("svc-inference-router-")}
        assert not {n for n in names if n.startswith("svc-coding-")}, "coding has no SA"

    def test_keycloak_sub_is_never_written(self):
        body = "\n".join(
            ln
            for ln in UP.read_text(encoding="utf-8").splitlines()
            if not ln.lstrip().startswith("--")
        )
        assert not re.search(r"SET\s+keycloak_sub", body, re.IGNORECASE)

    def test_downgrade_is_committed_and_outside_the_runner_glob(self):
        assert DOWN.exists()
        assert DOWN.resolve() not in {p.resolve() for p in UP.parent.glob("*.sql")}
        assert len(split_statements(DOWN.read_text(encoding="utf-8"))) == 1


class TestMigrationRunForReal:
    def test_fills_mapped_subjects_only_and_never_overwrites(self, conn):
        _insert(conn, "svc-kag")
        _insert(conn, "svc-fr-backend-kag")
        _insert(conn, "svc-coding-ai")  # no SA: must stay NULL
        _insert(conn, "svc-inference-router-merqi")  # per-tenant licence: must stay NULL
        _insert(conn, "svc-karma", OTHER_SA)  # set by hand: must survive
        assert _actor(conn, "svc-kag") is None, "positive control"

        _run_file(conn, UP)

        assert _actor(conn, "svc-kag") == SA_SUB
        assert _actor(conn, "svc-fr-backend-kag") == "31f62c60-bef6-46a0-b0be-9a8f823fd987"
        assert _actor(conn, "svc-coding-ai") is None
        assert _actor(conn, "svc-inference-router-merqi") is None
        assert _actor(conn, "svc-karma") == OTHER_SA
        keys = {r[0] for r in conn.execute("SELECT keycloak_sub FROM subject").fetchall()}
        assert "svc-kag" in keys, "keycloak_sub must be untouched"

    def test_rerun_is_a_noop(self, conn):
        _insert(conn, "svc-kag")
        _run_file(conn, UP)
        _run_file(conn, UP)
        assert _actor(conn, "svc-kag") == SA_SUB
