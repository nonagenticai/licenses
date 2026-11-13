"""Source-level invariant: a machine-readable error discriminator the code RAISES must
appear in the OpenAPI description of the status it is raised under.

THE DEFECT THIS EXISTS FOR
--------------------------
``POST /licenses/{license_id}/regenerate-token`` refuses with **409** for two genuinely
different reasons, and it tells them apart on the wire::

    detail = {"error": "license_not_active",  "license_id": ..., "status": ...}
    detail = {"error": "all_tokens_expired",  "license_id": ..., "remedy": ...}

The published 409 description covered both in *prose* — "either the license is not
effectively active … or every unrevoked token on it has already expired" — and named
**neither literal**. A client generated from the spec therefore sees one 409 with no
documented way to branch, even though the server has been discriminating all along. The
two cases need opposite handling: `license_not_active` means renew or investigate the
licence; `all_tokens_expired` means mint a token explicitly at
``POST /licenses/{license_id}/tokens``. Guessing wrong strands the caller.

This is the same class as the defect #139 closed — the document under-describing what the
routes honour — but one level down: not the status *code*, the error *identity* inside it.

THE RULE
--------
For every route decorator that declares a status with a ``description``, every
``{"error": "<literal>"}`` raised under that same status **in that route's handler** must
appear verbatim in that description.

Keyed on the literal string, because the literal is exactly what a client branches on. A
prose paraphrase is not a substitute: `grep`ping a generated client for `all_tokens_expired`
must find it.

WHAT THIS RULE DOES **NOT** CATCH (stated, not implied)
-------------------------------------------------------
  1. **Discriminators raised in a helper**, not lexically inside the handler. Taint stops at
     the function boundary; the two sites this exists for are both inline.
  2. **A dict built at runtime** — ``detail={"error": code}`` where ``code`` is a variable.
     Only literal values are read.
  3. **Whether the description is accurate**, only that it mentions the literal. A
     description naming both and explaining neither passes.
  4. **Statuses with no ``description`` key** — nothing is claimed, so nothing is checked.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN = REPO_ROOT / "main.py"


def _const_str(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _joined_str(node: ast.expr | None) -> str:
    """Flatten a description written as an implicitly-concatenated string tuple."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(_const_str(v) or "" for v in node.values)
    return ""


def _status_of(call: ast.Call) -> int | None:
    """`status_code=status.HTTP_409_CONFLICT` / `status_code=409` -> 409."""
    for kw in call.keywords:
        if kw.arg != "status_code":
            continue
        if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, int):
            return kw.value.value
        if isinstance(kw.value, ast.Attribute):  # status.HTTP_409_CONFLICT
            parts = kw.value.attr.split("_")
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1])
    return None


def _raised_discriminators(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[int, set[str]]:
    """status -> {literal values of detail={"error": "..."} raised under it}"""
    out: dict[int, set[str]] = {}
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "HTTPException":
            continue
        code = _status_of(node)
        if code is None:
            continue
        for kw in node.keywords:
            if kw.arg != "detail" or not isinstance(kw.value, ast.Dict):
                continue
            for k, v in zip(kw.value.keys, kw.value.values, strict=True):
                if _const_str(k) == "error" and (lit := _const_str(v)):
                    out.setdefault(code, set()).add(lit)
    return out


def _declared_descriptions(decorators: list[ast.expr]) -> dict[int, str]:
    """status -> description text, from the route decorator's `responses={...}`."""
    out: dict[int, str] = {}
    for dec in decorators:
        if not isinstance(dec, ast.Call):
            continue
        for kw in dec.keywords:
            if kw.arg != "responses" or not isinstance(kw.value, ast.Dict):
                continue
            for k, v in zip(kw.value.keys, kw.value.values, strict=True):
                if not (isinstance(k, ast.Constant) and isinstance(k.value, int)):
                    continue
                if not isinstance(v, ast.Dict):
                    continue
                for kk, vv in zip(v.keys, v.values, strict=True):
                    if _const_str(kk) == "description":
                        out[k.value] = _joined_str(vv)
    return out


def _routes() -> list[tuple[str, dict[int, set[str]], dict[int, str]]]:
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    found = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not node.decorator_list:
            continue
        raised = _raised_discriminators(node)
        declared = _declared_descriptions(node.decorator_list)
        if raised:
            found.append((node.name, raised, declared))
    return found


_ROUTES = _routes()
_PAIRS = [
    (name, code, lit, declared.get(code))
    for name, raised, declared in _ROUTES
    for code, lits in raised.items()
    for lit in sorted(lits)
]


class TestTheCorpusIsReal:
    """Anti-vacuity: an empty pair list would make the rule below hold for free."""

    def test_routes_with_discriminators_were_found(self):
        assert len(_ROUTES) >= 1, "no handler raising a literal error discriminator was found"

    def test_the_defect_route_is_covered(self):
        names = {n for n, _, _ in _ROUTES}
        assert "regenerate_token" in names, (
            "regenerate_token no longer raises literal discriminators — if that is "
            "intentional, update this test; it is the route this rule exists for"
        )

    def test_the_mint_route_publishes_license_not_active(self):
        """#237: POST /licenses/{id}/tokens now refuses a dead licence with the same
        409 discriminator as regenerate-token."""
        lits = {lit for name, _, lit, _ in _PAIRS if name == "create_license_token"}
        assert "license_not_active" in lits, (
            "create_license_token no longer raises license_not_active inline"
        )

    def test_both_known_discriminators_are_seen(self):
        lits = {lit for _, _, lit, _ in _PAIRS}
        for expected in ("license_not_active", "all_tokens_expired"):
            assert expected in lits, f"{expected!r} not detected; the AST walk regressed"


class TestEveryRaisedDiscriminatorIsPublished:
    """THE RULE."""

    @pytest.mark.parametrize(
        ("handler", "code", "literal", "description"),
        _PAIRS,
        ids=[f"{n}-{c}-{lit}" for n, c, lit, _ in _PAIRS],
    )
    def test_discriminator_appears_in_the_published_description(
        self, handler: str, code: int, literal: str, description: str | None
    ):
        assert description is not None, (
            f"{handler} raises {code} with detail.error={literal!r} but the route declares "
            f"no description for {code} — a client cannot learn to branch on it"
        )
        assert literal in description, (
            f"{handler} raises {code} with detail.error={literal!r}, but the published "
            f"{code} description never names that literal. A client generated from the spec "
            f"sees one {code} with no documented discriminator. Description was:\n{description}"
        )
