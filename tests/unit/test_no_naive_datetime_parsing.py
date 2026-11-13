"""Source-level invariant: shipped code may not call ``datetime.fromisoformat``.

THE DEFECT THIS EXISTS FOR
--------------------------
``datetime.fromisoformat("2027-01-01")`` and ``fromisoformat("2027-01-01T00:00:00")``
both succeed and both return a **NAIVE** datetime. Both are valid ISO 8601, and a field
or flag documented as "(ISO 8601)" invites exactly those spellings.

A naive datetime written to a ``timestamptz`` column is resolved by PostgreSQL against
the **session** ``TimeZone`` GUC, so the same input denotes a different instant
depending on a server setting. For ``license.starts_at`` / ``.expires_at`` that decides
when an entitlement begins and dies: ``License.effective_status`` compares ``expires_at``
against ``datetime.now(UTC)``, so a licence reads live or expired off by the UTC offset —
and that verdict is what ``provisioning`` and fr's licence gate consume. The failure is
not a crash, it is a silently wrong answer.

Two independent instances shipped:

* the HTTP boundary — ``LicenseCreate.starts_at`` / ``.expires_at`` were ``str`` parsed
  in the handler. Closed by #152 by typing them ``AwareDatetime``.
* the CLI boundary — ``scripts/provision_{kag,fr,w2v}_tenant.py`` parsed ``--expires-at``
  the same way, and never cross the API boundary at all. Closed by
  ``models.parse_aware_datetime_arg``, which REFUSES an offset-less value instead of
  guessing.

Fixing both instances does not stop the third. This rule does.

THE RULE
--------
No module in the shipped corpus — top-level modules, ``auth/``, ``services/`` **and
``scripts/``** — may call ``datetime.fromisoformat`` or ``date.fromisoformat``, under any
spelling: attribute (``datetime.fromisoformat``), bare name after
``from datetime import ...``, or through an aliased import.

Use instead:
  * ``AwareDatetime`` on a pydantic model, at an HTTP boundary;
  * ``models.parse_aware_datetime_arg`` for an operator-supplied argument;
  * ``models._as_utc`` when normalising a value already in hand.

``scripts/`` is deliberately IN the corpus. It is excluded from
``test_no_exception_leak`` and was the largest known hole in the DSN rules, and it is
where two of the three offending call sites lived.

WHAT THIS RULE DOES **NOT** CATCH (stated, not implied)
-------------------------------------------------------
  1. **Other naive-datetime producers.** ``datetime.strptime`` without ``%z``,
     ``datetime(...)`` constructed positionally, ``dateutil.parser.parse``. They have the
     same consequence; ``fromisoformat`` is banned by name because it is the one that
     shipped twice and the one that *looks* safe. Widening to a semantic "is this naive"
     analysis is out of proportion to a unit-test gate.
  2. **A naive value arriving from elsewhere** — a JSON body, another service, a DB read
     through a driver configured without tzinfo. ``models._as_utc`` is the runtime
     backstop for the DAL; this is a source rule.
  3. **``tests/``.** Test code parsing a response body it just received is not writing to
     a ``timestamptz`` column, and banning it there would produce noise that gets the
     rule deleted. The corpus is shipped code only — stated so the exemption is a
     decision, not an oversight.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Shipped code. NOTE `scripts/` IS included — see the docstring.
_SKIP_DIRS = frozenset(
    {"tests", "migrations", ".venv", ".worktrees", "certs", "docs", "__pycache__"}
)

_BANNED = "fromisoformat"
#: Modules that MUST be in the corpus, or the walk has silently collapsed.
_CORPUS_FLOOR = ("main.py", "models.py", "scripts/provision_w2v_tenant.py")


def _shipped_files() -> list[Path]:
    return sorted(
        p
        for p in REPO_ROOT.rglob("*.py")
        if not any(part in _SKIP_DIRS for part in p.relative_to(REPO_ROOT).parts)
    )


def _violations() -> list[str]:
    out: list[str] = []
    for path in _shipped_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # Names bound to fromisoformat by `from datetime import ...` are matched too.
        direct: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in {"datetime"}:
                for a in node.names:
                    if a.name == _BANNED:
                        direct.add(a.asname or a.name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            hit = (isinstance(f, ast.Attribute) and f.attr == _BANNED) or (
                isinstance(f, ast.Name) and f.id in direct
            )
            if hit:
                out.append(f"{rel}:{node.lineno}")
    return out


_FILES = _shipped_files()
_VIOLATIONS = _violations()


class TestTheCorpusIsReal:
    """Anti-vacuity: an empty corpus would make the rule below hold for free."""

    def test_shipped_files_were_found(self):
        assert len(_FILES) >= 10, f"only {len(_FILES)} shipped files found; the walk is broken"

    def test_corpus_contains_the_modules_this_rule_is_about(self):
        rels = {p.relative_to(REPO_ROOT).as_posix() for p in _FILES}
        missing = [m for m in _CORPUS_FLOOR if m not in rels]
        assert not missing, f"corpus no longer reaches {missing}; every assertion here is vacuous"

    def test_scripts_are_in_the_corpus(self):
        rels = {p.relative_to(REPO_ROOT).as_posix() for p in _FILES}
        scripts = [r for r in rels if r.startswith("scripts/")]
        assert len(scripts) >= 5, (
            f"only {len(scripts)} scripts/ modules in the corpus. scripts/ is deliberately "
            f"covered by this rule — two of the three original call sites were there."
        )

    def test_the_detector_can_actually_fire(self):
        """A rule that has never matched anything is not known to work."""
        sample = ast.parse("import datetime\nx = datetime.datetime.fromisoformat('2027-01-01')\n")
        found = [
            n
            for n in ast.walk(sample)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == _BANNED
        ]
        assert found, "the AST matcher does not recognise its own target spelling"


class TestNoShippedModuleParsesIsoformat:
    """THE RULE."""

    def test_no_fromisoformat_in_shipped_code(self):
        assert not _VIOLATIONS, (
            f"`datetime.fromisoformat` is called in shipped code at {_VIOLATIONS}. It returns a "
            f"NAIVE datetime for a date-only or offset-less string, and a naive value in a "
            f"timestamptz column is resolved against the database session's TimeZone — the same "
            f"input then means different instants. Use AwareDatetime on a pydantic model at an "
            f"HTTP boundary, models.parse_aware_datetime_arg for an operator-supplied argument, "
            f"or models._as_utc to normalise a value already in hand."
        )


class TestTheReplacementRefusesNaiveInput:
    """The rule is only worth having if the sanctioned replacement is strict."""

    @pytest.mark.parametrize("raw", ["2027-01-01", "2027-01-01T00:00:00", "2027-01-01 00:00:00"])
    def test_offset_less_values_are_refused(self, raw: str):
        from models import DatetimeArgError, parse_aware_datetime_arg

        with pytest.raises(DatetimeArgError) as exc:
            parse_aware_datetime_arg(raw, arg="--expires-at")
        assert "--expires-at" in str(exc.value), "the refusal must name the argument"

    @pytest.mark.parametrize(
        "raw", ["2027-01-01T00:00:00+00:00", "2027-01-01T00:00:00Z", "2027-01-01T02:00:00+02:00"]
    )
    def test_offset_bearing_values_are_accepted_and_normalised(self, raw: str):
        from datetime import UTC

        from models import parse_aware_datetime_arg

        got = parse_aware_datetime_arg(raw, arg="--expires-at")
        assert got.tzinfo is not None, "result must be aware"
        offset = got.utcoffset()
        assert offset is not None and offset.total_seconds() == 0, "must be normalised to UTC"
        assert got.tzinfo is UTC

    def test_garbage_is_refused(self):
        from models import DatetimeArgError, parse_aware_datetime_arg

        with pytest.raises(DatetimeArgError):
            parse_aware_datetime_arg("not-a-date", arg="--expires-at")
