"""A subject identifier is refused — never normalised — unless it is exactly what it says.

⛔ The defect (VALIDATE-ownerid2, 2026-09-27): Python's ``$`` matches BEFORE a trailing
``\\n`` (Postgres ``~`` does not), so ``_KEYCLOAK_SUB_UUID_RE.match('<A_sub>\\n')`` passed.
``POST /licenses/user/<A_sub>%0A`` then minted a NEW subject whose introspection emitted
``'<A_sub>\\n'`` as ``subject_keycloak_sub``; ingestion ``.strip()``s it and that licence
holder owned A's runs.

Three layers, each pinned per shape below:
  1. write: ``create_or_get_subject`` (and so ``POST /licenses/user/{id}``) refuses a
     padded / control-char / upper-case-UUID identifier with a 4xx. It does NOT strip:
     a stripped value IS A's sub, i.e. an alias.
  2. read: introspection emits ``subject_keycloak_sub`` only on a ``\\A…\\Z`` lower-case
     UUID ``fullmatch`` — a legacy row in any of these shapes reads None.
  3. no identity regex anywhere in the service is used with ``$`` + ``match``/``search``.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import quote
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from models import (
    MalformedSubjectIdentifierError,
    Subject,
    keycloak_sub_as_identity,
    subject_identifier_problem,
)

A_SUB = "5b1c7e2a-4f6d-4a8e-9c3b-2d1e0f9a8b7c"  # victim A's Keycloak sub
TENANT = "01000000-0000-7000-8000-000000000001"

# Every shape that a strip / case-fold consumer would read as A.
SHAPES = {
    "trailing_newline": A_SUB + "\n",
    "leading_space": " " + A_SUB,
    "carriage_return": A_SUB + "\r",
    "tab": A_SUB + "\t",
    "nul": A_SUB + "\x00",
    "upper_case": A_SUB.upper(),
}
SHAPE_IDS = list(SHAPES)
SHAPE_VALUES = [SHAPES[k] for k in SHAPE_IDS]


def _count(db_manager, sub: str) -> int:
    session = db_manager.get_session()
    try:
        return session.query(Subject).filter(Subject.keycloak_sub == sub).count()
    finally:
        session.close()


def _raw_subject(db_manager, keycloak_sub: str) -> Subject:
    """A legacy row in a bad shape — written directly, as the write path now refuses it."""
    session = db_manager.get_session()
    try:
        s = Subject(keycloak_sub=keycloak_sub, keycloak_realm="secure-apps")
        session.add(s)
        session.commit()
        session.refresh(s)
        return s
    finally:
        session.close()


def _token_for(db_manager, subject, seed_app, seed_plan) -> str:
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


class TestValidatorNamesEveryShape:
    @pytest.mark.parametrize("bad", SHAPE_VALUES, ids=SHAPE_IDS)
    def test_shape_is_a_problem(self, bad):
        assert subject_identifier_problem(bad) is not None

    @pytest.mark.parametrize(
        "good", [A_SUB, "svc-w2v-tenant-dev", "test-user-sub", "fr-test", "a.b_c@d"]
    )
    def test_ordinary_identifiers_pass(self, good):
        assert subject_identifier_problem(good) is None

    @pytest.mark.parametrize("bad", ["", "a b", "x​y", "x ", "x\x7f", "x "])
    def test_other_whitespace_and_format_chars_are_problems(self, bad):
        assert subject_identifier_problem(bad) is not None


class TestWritePathRefuses:
    @pytest.mark.parametrize("bad", SHAPE_VALUES, ids=SHAPE_IDS)
    def test_dal_refuses_and_stores_nothing(self, db_manager, bad):
        a = db_manager.create_or_get_subject(keycloak_sub=A_SUB, keycloak_realm="secure-apps")
        with pytest.raises(MalformedSubjectIdentifierError):
            db_manager.create_or_get_subject(keycloak_sub=bad, keycloak_realm="secure-apps")
        # Not stored raw, and not normalised onto A either (A is untouched, no new row).
        if "\x00" not in bad:  # Postgres text cannot even hold NUL; nothing to query
            assert _count(db_manager, bad) == 0
        assert _count(db_manager, A_SUB) == 1
        session = db_manager.get_session()
        try:
            assert session.query(Subject).count() == 1
            assert str(session.query(Subject).one().id) == str(a.id)
        finally:
            session.close()

    @pytest.mark.parametrize("bad", SHAPE_VALUES, ids=SHAPE_IDS)
    def test_post_route_answers_400(
        self, auth_client_admin: TestClient, db_manager, seed_app, seed_plan, bad
    ):
        """The exact attack: POST /licenses/user/<A_sub>%0A (and the other shapes)."""
        tenant = db_manager.create_tenant(name="strict-sub-tenant", owner_key="platform")
        db_manager.create_or_get_subject(keycloak_sub=A_SUB, keycloak_realm="secure-apps")
        with patch("main._provision_budget", new_callable=AsyncMock):
            response = auth_client_admin.post(
                f"/licenses/user/{quote(bad, safe='')}",
                json={"app_id": str(seed_app.id), "plan_id": str(seed_plan.id)},
                headers={"X-Tenant-Id": str(tenant.id)},
            )
        assert response.status_code == 400, response.text
        session = db_manager.get_session()
        try:
            assert session.query(Subject).count() == 1  # only A
        finally:
            session.close()

    def test_over_255_is_refused_before_the_database(self, db_manager):
        """``keycloak_sub`` is VARCHAR(255): a clear refusal, never a DB truncation error."""
        with pytest.raises(MalformedSubjectIdentifierError, match="at most 255"):
            db_manager.create_or_get_subject(keycloak_sub="s" * 256, keycloak_realm="service")
        assert db_manager.create_or_get_subject(keycloak_sub="s" * 255, keycloak_realm="service")

    def test_over_255_post_route_answers_400(
        self, auth_client_admin: TestClient, db_manager, seed_app, seed_plan
    ):
        tenant = db_manager.create_tenant(name="long-sub-tenant", owner_key="platform")
        with patch("main._provision_budget", new_callable=AsyncMock):
            response = auth_client_admin.post(
                f"/licenses/user/{'s' * 256}",
                json={"app_id": str(seed_app.id), "plan_id": str(seed_plan.id)},
                headers={"X-Tenant-Id": str(tenant.id)},
            )
        assert response.status_code == 400, response.text
        assert "at most 255" in response.text

    def test_upper_case_row_id_still_resolves(self, db_manager):
        """The deferred case check must not break GET's row-id resolution order."""
        a = db_manager.create_or_get_subject(keycloak_sub=A_SUB, keycloak_realm="secure-apps")
        got = db_manager.create_or_get_subject(
            keycloak_sub=str(a.id).upper(), keycloak_realm="secure-apps"
        )
        assert str(got.id) == str(a.id)


class TestIntrospectionRefusesLegacyRows:
    @pytest.mark.parametrize("bad", SHAPE_VALUES, ids=SHAPE_IDS)
    def test_helper_returns_none(self, bad):
        assert keycloak_sub_as_identity(bad, False) is None

    @pytest.mark.parametrize(
        "bad",
        [v for v in SHAPE_VALUES if "\x00" not in v],  # Postgres text cannot store NUL
        ids=[k for k in SHAPE_IDS if k != "nul"],
    )
    def test_legacy_row_introspects_as_none(self, db_manager, seed_app, seed_plan, bad):
        victim = db_manager.create_or_get_subject(keycloak_sub=A_SUB, keycloak_realm="secure-apps")
        legacy = _raw_subject(db_manager, bad)
        result = db_manager.introspect_token(_token_for(db_manager, legacy, seed_app, seed_plan))
        assert result["active"] is True
        assert result["subject_keycloak_sub"] is None
        # A's own token is unaffected.
        own = db_manager.introspect_token(_token_for(db_manager, victim, seed_app, seed_plan))
        assert own["subject_keycloak_sub"] == A_SUB


# --- no identity regex may pair a `$` with match/search --------------------------------

_ROOT = Path(__file__).resolve().parents[2]
_SCANNED = [
    _ROOT / "main.py",
    _ROOT / "models.py",
    _ROOT / "service_auth.py",
    *sorted((_ROOT / "auth").rglob("*.py")),
    *sorted((_ROOT / "services").rglob("*.py")),
    *sorted((_ROOT / "scripts").rglob("*.py")),
]


def _has_dollar_anchor(pattern: str) -> bool:
    """True if ``pattern`` has an unescaped ``$`` outside a character class."""
    escaped = in_class = False
    for ch in pattern:
        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = True
        elif in_class:
            in_class = ch != "]"
        elif ch == "[":
            in_class = True
        elif ch == "$":
            return True
    return False


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    dollar_names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "compile"
            and node.value.args
            and isinstance(node.value.args[0], ast.Constant)
            and isinstance(node.value.args[0].value, str)
            and _has_dollar_anchor(node.value.args[0].value)
        ):
            dollar_names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    out = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("match", "search")
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in dollar_names
        ):
            out.append(f"{path.name}:{node.lineno} {node.func.value.id}")
    return out


def test_no_dollar_anchored_regex_is_used_with_match_or_search():
    """Python ``$`` accepts a trailing newline: use ``fullmatch`` or ``\\A…\\Z``."""
    found = [v for p in _SCANNED for v in _violations(p)]
    assert not found, found


def test_the_scanner_can_fail(tmp_path):
    """Control: the scanner flags the exact pre-fix shape, and not its fullmatch fix."""
    bad = tmp_path / "bad.py"
    bad.write_text('import re\nR = re.compile(r"^[0-9a-f]+$")\nR.match(x)\n')
    assert _violations(bad)
    good = tmp_path / "good.py"
    good.write_text('import re\nR = re.compile(r"^[0-9a-f]+$")\nR.fullmatch(x)\n')
    assert not _violations(good)
