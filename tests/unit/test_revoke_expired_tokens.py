"""The estate-wide token-revocation sweep, and the guards that stop it being green about nothing.

WHY THIS JOB EXISTS
-------------------
`revoked` is a flag this service sets; `expires_at` is a clock. Nothing moved a token from
the second state to the first, so expired-but-unrevoked rows accumulate — the population
#135 had to defend against, where `max()` over unrevoked tokens returned a PAST expiry and
a rotation minted a dead credential while destroying the working ones. Prod held 2 such
tokens on 2026-08-17.

The n8n workflow meant to do this reports 12/12 `success` while revoking nothing: it lists
licences through a TENANT-SCOPED endpoint with a single-tenant credential, so no credential
change can make it estate-wide.

⚠️ NO TEST HERE CAN PASS ON AN EMPTY TABLE. Every one seeds real rows, and each seeds at
least one row that must NOT be touched — so a sweep that ignored its predicate entirely
would be caught by the same test that proves it works.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from models import DatabaseManager, Token
from scripts import revoke_expired_tokens
from tests.conftest import SEED_TENANT_ID

_ENV = ("REVOKE_DRY_RUN", "REVOKE_LIMIT", "REVOKE_ACTOR")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x/y")  # presence check only


@pytest.fixture
def cli_db(db_manager, monkeypatch):
    monkeypatch.setattr(revoke_expired_tokens, "_db_manager", lambda: db_manager)
    return db_manager


def _mk_token(
    db: DatabaseManager, app, plan, *, key: str, expires_days: float | None, revoked: bool = False
) -> Token:
    """Seed ONE token on a real licence. `expires_days=None` is PERPETUAL — a different
    thing from long-expired, and conflating them would revoke the whole estate."""
    subject = db.create_or_get_subject(keycloak_sub=key, keycloak_realm="secure-apps")
    now = datetime.now(UTC)
    lic = db.create_license(
        subject_id=UUID(str(subject.id)),
        app_id=UUID(str(app.id)),
        plan_id=UUID(str(plan.id)),
        tenant_id=SEED_TENANT_ID,
        starts_at=now - timedelta(days=1),
        expires_at=None,
    )
    s = db.get_session()
    try:
        t = Token(
            id=uuid.uuid4(),
            license_id=UUID(str(lic.id)),
            token_hash=uuid.uuid4().hex,
            token_type="api",
            revoked=revoked,
            created_at=now,
            expires_at=None if expires_days is None else now + timedelta(days=expires_days),
        )
        s.add(t)
        s.commit()
        s.refresh(t)
        return t
    finally:
        s.close()


def _revoked(db: DatabaseManager, token_id) -> bool:
    s = db.get_session()
    try:
        row = s.get(Token, token_id)
        assert row is not None, f"token {token_id} vanished from the estate"
        return bool(row.revoked)
    finally:
        s.close()


def _one_json_line(capsys) -> dict[str, Any]:
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected exactly one JSON line, got {lines!r}"
    return json.loads(lines[0])


@pytest.fixture
def estate(db_manager, seed_app, seed_plan):
    """Four shapes. Only `expired` may ever be touched."""
    return {
        "expired": _mk_token(db_manager, seed_app, seed_plan, key="s-exp", expires_days=-10),
        "live": _mk_token(db_manager, seed_app, seed_plan, key="s-live", expires_days=+30),
        "perpetual": _mk_token(db_manager, seed_app, seed_plan, key="s-perp", expires_days=None),
        "already": _mk_token(
            db_manager, seed_app, seed_plan, key="s-rev", expires_days=-5, revoked=True
        ),
    }


class TestTheSweepSelectsOnlyDeadTokens:
    def test_armed_sweep_revokes_only_the_expired_one(self, cli_db, estate, monkeypatch, capsys):
        monkeypatch.setenv("REVOKE_DRY_RUN", "false")
        rc = revoke_expired_tokens.main_cli([])
        out = _one_json_line(capsys)
        assert _revoked(cli_db, estate["expired"].id) is True
        # the three that must survive — this is what makes the test non-vacuous
        assert _revoked(cli_db, estate["live"].id) is False, "revoked a LIVE token"
        assert _revoked(cli_db, estate["perpetual"].id) is False, "revoked a PERPETUAL token"
        assert out["acted"] == 1 and out["expired_after"] == 0 and out["converged"] is True
        assert rc == 0

    def test_a_perpetual_token_is_not_expired(self, cli_db, estate):
        """`expires_at IS NULL` must not satisfy `expires_at < now`."""
        assert cli_db.count_expired_unrevoked_tokens(datetime.now(UTC)) == 1

    def test_an_already_revoked_expired_token_is_not_counted(self, cli_db, estate):
        """Otherwise the sweep never converges: it would re-count rows it cannot change."""
        cli_db.revoke_all_expired_tokens(dry_run=False, now=datetime.now(UTC))
        assert cli_db.count_expired_unrevoked_tokens(datetime.now(UTC)) == 0


class TestDryRunIsNotGreenAboutNothing:
    def test_dry_run_writes_nothing_and_reports_not_converged(
        self, cli_db, estate, monkeypatch, capsys
    ):
        monkeypatch.setenv("REVOKE_DRY_RUN", "true")
        rc = revoke_expired_tokens.main_cli([])
        out = _one_json_line(capsys)
        assert _revoked(cli_db, estate["expired"].id) is False, "a DRY RUN wrote to the estate"
        assert out["dry_run"] is True and out["expired_before"] == 1
        assert out["converged"] is False, "a dry run that FOUND rows is not converged"
        assert rc == 1, "a dry run over a rotten estate must not exit 0"

    def test_dry_run_is_the_default(self, cli_db, estate, monkeypatch, capsys):
        """A missing knob must not arm a NON-REVERSIBLE sweep."""
        revoke_expired_tokens.main_cli([])
        assert _one_json_line(capsys)["dry_run"] is True
        assert _revoked(cli_db, estate["expired"].id) is False

    def test_a_blank_value_reads_as_the_default(self, cli_db, estate, monkeypatch, capsys):
        """`REVOKE_DRY_RUN: ""` in a manifest must not arm it by accident."""
        monkeypatch.setenv("REVOKE_DRY_RUN", "")
        revoke_expired_tokens.main_cli([])
        assert _one_json_line(capsys)["dry_run"] is True


class TestTruncationIsReported:
    def test_limit_leaves_rows_behind_and_says_so(
        self, cli_db, db_manager, seed_app, seed_plan, monkeypatch, capsys
    ):
        for i in range(3):
            _mk_token(db_manager, seed_app, seed_plan, key=f"trunc-{i}", expires_days=-(i + 2))
        monkeypatch.setenv("REVOKE_DRY_RUN", "false")
        monkeypatch.setenv("REVOKE_LIMIT", "1")
        rc = revoke_expired_tokens.main_cli([])
        out = _one_json_line(capsys)
        assert out["acted"] == 1 and out["expired_after"] == 2
        assert out["converged"] is False and rc == 1, (
            "an estate that outgrew LIMIT must not be green"
        )
        assert any("left_behind" in p for p in out["problems"])


class TestConfigFailuresCannotBeMistakenForSuccess:
    def test_missing_database_url_exits_2(self, monkeypatch, capsys):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        rc = revoke_expired_tokens.main_cli([])
        assert _one_json_line(capsys)["status"] == "config_error"
        assert rc == 2, "config failure must be distinguishable from a converged estate"

    @pytest.mark.parametrize("bad", ["0", "5001", "-1"])
    def test_out_of_range_limit_exits_2(self, monkeypatch, capsys, bad):
        monkeypatch.setenv("REVOKE_LIMIT", bad)
        rc = revoke_expired_tokens.main_cli([])
        assert _one_json_line(capsys)["status"] == "config_error" and rc == 2


class TestTheCountIsAnIndependentOpinion:
    def test_count_is_hand_written_sql_not_the_sweep_query(self):
        """If the count reused the sweep's ORM query the guards could not fail — any
        drift would move both numbers together and every comparison is a tautology."""
        import inspect

        src = inspect.getsource(DatabaseManager.count_expired_unrevoked_tokens)
        assert "_EXPIRED_TOKEN_COUNT_SQL" in src
        assert "session.query(Token)" not in src, "the count must not reuse the ORM selection"


class TestTheJobActuallyReadsDatabaseUrl:
    """The defect this class exists for.

    `_db_manager` originally called `DatabaseManager()` with NO argument. That does
    not read the environment — `models.DatabaseManager.__init__` hard-defaults to
    `postgresql+psycopg://localhost:5432/licenses`. Every unit test passed, because
    they monkeypatch `_db_manager` wholesale and never exercise it. The first
    production dry run returned `status=db_error`, exit 2, having tried
    `127.0.0.1:5432` inside a pod whose database is elsewhere.

    So these tests deliberately do NOT use the `cli_db` fixture: they exercise the
    real `_db_manager`, which the rest of the file replaces.
    """

    def test_the_configured_url_reaches_the_manager(self, monkeypatch):
        seen: dict[str, str | None] = {}

        class _Spy:
            def __init__(self, url: str | None = None):
                seen["url"] = url

        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db.example:5432/licenses")
        monkeypatch.setattr(revoke_expired_tokens, "DatabaseManager", _Spy)
        revoke_expired_tokens._db_manager()
        assert seen["url"] == "postgresql+psycopg://u:p@db.example:5432/licenses", (
            "the DSN must be passed EXPLICITLY; DatabaseManager() with no argument "
            "silently uses a localhost default and can never work in a pod"
        )

    def test_no_argument_form_is_not_used(self, monkeypatch):
        """A positive control on the above: constructing with no URL must be impossible
        here, so a future refactor back to `DatabaseManager()` fails by name."""
        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db.example:5432/licenses")
        calls: list[tuple] = []

        class _Spy:
            def __init__(self, *a, **kw):
                calls.append((a, kw))

        monkeypatch.setattr(revoke_expired_tokens, "DatabaseManager", _Spy)
        revoke_expired_tokens._db_manager()
        assert calls and (calls[0][0] or calls[0][1]), "DatabaseManager was called with NO url"

    @pytest.mark.parametrize("value", ["", "   "])
    def test_blank_database_url_raises_rather_than_defaulting(self, monkeypatch, value):
        monkeypatch.setenv("DATABASE_URL", value)
        with pytest.raises(ValueError, match="DATABASE_URL"):
            revoke_expired_tokens._db_manager()
