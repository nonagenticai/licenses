"""Source-level invariant: every signature-verifying ``jwt.decode`` must pin the issuer.

THE DEFECT THIS EXISTS FOR
--------------------------
``auth/`` shipped **two** token verifiers. The live one is ``auth/jwt_verify.py``,
reached from ``auth/middleware.py``: it resolves the signing key from JWKS by ``kid``,
passes ``issuer=`` and sets ``verify_iss: True``, and refuses to guess when the trusted
issuer allowlist is empty.

The other was ``auth/validator.py``. It verified the signature against a single PEM
fetched from ``KeycloakOpenID.public_key()`` — the realm's *active* key, with no ``kid``
matching — and checked **neither ``iss`` nor ``aud``**::

    jwt.decode(token, self.get_public_key(), algorithms=["RS256"],
               options={"verify_signature": True, "verify_aud": False,
                        "verify_exp": True})

This deployment runs **two** Keycloak realms (``portal-realm`` for external users,
``secure-apps`` for the licensing/metering boundary). A verifier that checks neither
``iss`` nor ``aud`` accepts either realm's token as the other's, and accepts any client's
token as any other client's. Nothing in the request path called it — but it was
re-exported from ``auth/__init__.py``'s ``__all__``, so ``from auth import get_validator``
read like the blessed API, one import away from being wired in. The pod does not even set
``KEYCLOAK_CLIENT_ID``/``KEYCLOAK_CLIENT_SECRET``, so it could never have worked; it would
have failed at the Keycloak call, not at the missing issuer check.

Deleting the module closes the instance. This rule closes the shape: the next weak
verifier fails here instead of shipping.

THE RULE
--------
For every call to ``jwt.decode`` in a shipped module:

  * if the call **disables** signature verification (``options={"verify_signature": False}``
    with a literal ``False``), it is an unverified *peek* and is exempt — reading ``iss``
    to choose which JWKS to fetch is legitimate, and ``jwt_verify.verify_jwt`` does exactly
    that before re-checking ``iss`` against the pinned value;
  * otherwise the call verifies a signature, and it MUST pass ``issuer=`` **and** MUST NOT
    set ``verify_iss`` to a literal ``False``.

``verify_aud`` is deliberately NOT part of this rule. ``jwt_verify`` sets it ``False`` on
purpose, with a comment: Keycloak's ``aud`` varies per client (portal-bff, fr-backend,
commonpractice, …) and binding it needs its own audited change. A rule that banned it
would have to be suppressed on the one site that matters, which is how a rule becomes
decoration.

The second arm asserts the dependency cannot creep back: ``python-keycloak`` existed in
this image *solely* for the deleted module, and a resurrected weak verifier would most
naturally reach for it again.

WHAT THIS RULE DOES **NOT** CATCH (stated, not implied)
-------------------------------------------------------
  1. **A verifier that does not go through PyJWT.** A hand-rolled RSA check, or a call
     into a vendored library, is invisible here. The rule keys on ``jwt.decode`` because
     that is what both verifiers used.
  2. **``issuer=`` bound to the wrong value.** The rule proves the argument is *passed*,
     not that it came from an allowlist. ``tests/auth/`` covers ``verify_jwt``'s issuer
     handling behaviourally; this is a source gate, not a substitute for it.
  3. **``options`` built at runtime.** ``options=OPTS`` where ``OPTS`` is a module-level
     dict reads as "no literal ``verify_signature: False``", so the call is treated as
     verifying and must pin the issuer — the safe direction. A runtime-assembled dict that
     *disables* verification would be flagged, not missed.
  4. **Non-shipped code.** ``tests/``, ``scripts/`` and ``migrations/`` are excluded: they
     do not serve requests. A test may legitimately decode without an issuer.

Both arms carry an anti-vacuity floor. A rename of the import, a moved package, or a
corpus that silently collapses to zero files fails the gate instead of passing it.
"""

from __future__ import annotations

import ast
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Directories that ship no request-path code.
_SKIP_DIRS = frozenset(
    {"tests", "scripts", "migrations", ".venv", ".worktrees", "certs", "docs", "__pycache__"}
)

#: The dependency the deleted verifier was the only importer of.
_BANNED_DISTRIBUTION = "python-keycloak"
_BANNED_TOP_LEVEL_MODULE = "keycloak"

#: Modules that MUST be in the corpus. If the walk stops finding these, the corpus
#: collapsed (moved package, changed layout) and every assertion below went vacuous.
_CORPUS_FLOOR = ("main.py", "auth/jwt_verify.py", "auth/middleware.py")

#: At least this many ``jwt.decode`` calls must be found, and at least this many of
#: them must be signature-VERIFYING. Today: 2 calls in ``auth/jwt_verify.py``, one
#: peek and one verifying. A drop to zero means the rule stopped seeing PyJWT.
_MIN_DECODE_CALLS = 2
_MIN_VERIFYING_CALLS = 1


def _shipped_files() -> list[Path]:
    return sorted(
        p
        for p in REPO_ROOT.rglob("*.py")
        if not any(part in _SKIP_DIRS for part in p.relative_to(REPO_ROOT).parts)
    )


@dataclass(frozen=True)
class DecodeCall:
    """One ``jwt.decode`` call site."""

    file: str
    line: int
    verifies_signature: bool
    pins_issuer: bool
    disables_iss_check: bool

    def __str__(self) -> str:  # pragma: no cover - failure formatting only
        return f"{self.file}:{self.line}"


def _literal_false(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


def _option(call: ast.Call, key: str) -> ast.expr | None:
    """Return the value node for ``options={key: <value>}``, if written as a literal."""
    for kw in call.keywords:
        if kw.arg != "options" or not isinstance(kw.value, ast.Dict):
            continue
        for k, v in zip(kw.value.keys, kw.value.values, strict=True):
            if isinstance(k, ast.Constant) and k.value == key:
                return v
    return None


class _DecodeFinder(ast.NodeVisitor):
    """Collect ``jwt.decode`` calls, following module aliases and direct imports."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.calls: list[DecodeCall] = []
        # `import jwt` / `import jwt as pyjwt` -> the local name bound to the package
        self._module_aliases: set[str] = set()
        # `from jwt import decode as jwt_decode` -> local names bound to the function
        self._direct_names: set[str] = set()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name.split(".")[0] == "jwt":
                self._module_aliases.add(alias.asname or alias.name.split(".")[0])
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module and node.module.split(".")[0] == "jwt":
            for alias in node.names:
                if alias.name == "decode":
                    self._direct_names.add(alias.asname or alias.name)
        self.generic_visit(node)

    def _is_decode(self, func: ast.expr) -> bool:
        if isinstance(func, ast.Attribute) and func.attr == "decode":
            base = func.value
            return isinstance(base, ast.Name) and base.id in self._module_aliases
        return isinstance(func, ast.Name) and func.id in self._direct_names

    def visit_Call(self, node: ast.Call) -> None:
        if self._is_decode(node.func):
            verifies = not _literal_false(_option(node, "verify_signature"))
            self.calls.append(
                DecodeCall(
                    file=self.path,
                    line=node.lineno,
                    verifies_signature=verifies,
                    pins_issuer=any(kw.arg == "issuer" for kw in node.keywords),
                    disables_iss_check=_literal_false(_option(node, "verify_iss")),
                )
            )
        self.generic_visit(node)


def _collect() -> tuple[list[DecodeCall], list[str]]:
    calls: list[DecodeCall] = []
    scanned: list[str] = []
    for path in _shipped_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        scanned.append(rel)
        finder = _DecodeFinder(rel)
        finder.visit(ast.parse(path.read_text(encoding="utf-8")))
        calls.extend(finder.calls)
    return calls, scanned


_CALLS, _SCANNED = _collect()


class TestCorpusIsReal:
    """Anti-vacuity. Every assertion below is worthless if the walk found nothing."""

    def test_corpus_contains_the_modules_this_rule_is_about(self):
        missing = [m for m in _CORPUS_FLOOR if m not in _SCANNED]
        assert not missing, (
            f"the shipped-file walk no longer reaches {missing}; the corpus moved and "
            f"every assertion in this module went vacuous (scanned {len(_SCANNED)} files)"
        )

    def test_the_rule_still_sees_pyjwt(self):
        assert len(_CALLS) >= _MIN_DECODE_CALLS, (
            f"found {len(_CALLS)} jwt.decode call(s), expected at least "
            f"{_MIN_DECODE_CALLS}. Either PyJWT is reached by a spelling this finder "
            f"does not follow, or the verification path was removed."
        )

    def test_at_least_one_call_actually_verifies_a_signature(self):
        verifying = [c for c in _CALLS if c.verifies_signature]
        assert len(verifying) >= _MIN_VERIFYING_CALLS, (
            "no signature-VERIFYING jwt.decode call was found, so the issuer rule below "
            "holds trivially over an empty set"
        )


class TestEveryVerifyingDecodePinsTheIssuer:
    """THE RULE."""

    @pytest.mark.parametrize("call", [c for c in _CALLS if c.verifies_signature], ids=str)
    def test_verifying_decode_passes_issuer(self, call: DecodeCall):
        assert call.pins_issuer, (
            f"{call} verifies a signature but passes no issuer=. A token minted by any "
            f"realm this key signs for is accepted as any other. Pass issuer= (see "
            f"auth/jwt_verify.py:verify_jwt), or set options={{'verify_signature': False}} "
            f"if this is an unverified peek."
        )

    @pytest.mark.parametrize("call", [c for c in _CALLS if c.verifies_signature], ids=str)
    def test_verifying_decode_does_not_disable_the_issuer_check(self, call: DecodeCall):
        assert not call.disables_iss_check, (
            f"{call} passes issuer= but disables verify_iss, which makes the pin inert"
        )


class TestTheDeletedVerifiersDependencyCannotComeBack:
    """python-keycloak was in this image for ``auth/validator.py`` and nothing else."""

    def test_no_shipped_module_imports_keycloak(self):
        offenders: list[str] = []
        for path in _shipped_files():
            rel = path.relative_to(REPO_ROOT).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                # ``ast.walk`` is typed as yielding bare ``AST``; read ``lineno``
                # only inside the narrowed branches, which also have it.
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module] if node.module else []
                else:
                    continue
                if any(n.split(".")[0] == _BANNED_TOP_LEVEL_MODULE for n in names):
                    offenders.append(f"{rel}:{node.lineno}")
        assert not offenders, (
            f"{offenders} import `{_BANNED_TOP_LEVEL_MODULE}`. The only module that ever "
            f"did was the superseded single-PEM verifier deleted with this rule. Token "
            f"verification goes through auth/jwt_verify.py (JWKS, kid-matched, "
            f"issuer-pinned); admin calls to Keycloak go through httpx."
        )

    def test_pyproject_does_not_declare_the_dependency(self):
        pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        deps = pyproject["project"]["dependencies"]
        # Anti-vacuity: a typo'd key would give an empty list and pass for free.
        assert len(deps) > 5, f"dependency list looks wrong ({deps!r})"
        offenders = [
            d
            for d in deps
            if d.split("[")[0].split(">")[0].split("=")[0].strip() == _BANNED_DISTRIBUTION
        ]
        assert not offenders, (
            f"{_BANNED_DISTRIBUTION} is declared ({offenders}) but nothing imports it"
        )
