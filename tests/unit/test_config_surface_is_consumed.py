"""Every public member of ``KeycloakSettings`` must have a consumer, or a reason.

WHY THIS GUARD EXISTS
---------------------
PR #140 deleted ``auth/validator.py``, a superseded JWT verifier that checked
neither ``iss`` nor ``aud``. It was the ONLY reader of ``settings.client_id``,
``settings.client_secret`` and ``settings.ssl_verify``. Deleting it left those
three — and the two ``os.getenv`` reads behind them — as configuration surface
that nothing could ever observe. Dead config is not inert: it is read as a
contract. ``KEYCLOAK_CLIENT_SECRET`` stayed documented in ``README.md`` as
service configuration long after no line of the service read it, which is how a
secret gets provisioned, rotated and audited for nine months for no reason.

The sibling failure was ``jwks_url``: a property deriving a JWKS endpoint from
``KEYCLOAK_REALM``, which production sets to ``nonagentic`` — a realm that
returns 404 (see the ``auth/jwt_verify.py`` module docstring). Nothing called
it, so nothing broke; the next caller to reach for the obvious-looking helper
would have got a URL that 404s in production.

A NAME GREP CANNOT DECIDE THIS. ``grep -rn client_id`` matches the docstring
above, the string ``"client_id"`` in a request body, the env var name, and the
``client_id`` key of an unrelated dict. This walks the AST and counts only
``ast.Attribute`` nodes in **Load** context — i.e. somebody actually reading the
member — plus the ``getattr``/``hasattr`` string forms.

WHAT THIS RULE DOES NOT COVER
-----------------------------
1. **Dynamic access is invisible.** ``getattr(settings, name)`` where ``name``
   is a variable resolves at runtime; only string-literal second arguments are
   seen. A member reached exclusively that way would be reported dead.
2. **Python in the shipped tree only.** ``tests/``, ``scripts/``, ``migrations/``
   are excluded deliberately — a member kept alive solely by its own test is
   dead in production, which is exactly the condition being hunted. A consumer
   living in a shell script, a template, or another repo is likewise invisible;
   that is what ``_ALLOWLIST`` is for.
3. **Name-based, not type-resolved.** ``other_object.realm`` counts as a
   consumer of ``KeycloakSettings.realm``. The rule therefore errs toward
   calling a member LIVE — it under-reports dead code, and cannot raise a
   spurious failure from an unrelated attribute of the same name.
4. **Intra-file references count, so a dead *cycle* survives.** ``token_url`` is
   read only by ``service_token_url``, which ``service_auth.py`` reads — a real
   chain, rooted outside the file, so intra-file reads must count. The price is
   that two dead members referring only to each other would both look live.
   Nothing in the file is currently in that shape.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "auth" / "config.py"

#: Directories that are not shipped in the runtime image. A member consumed only
#: from here is dead in production.
_EXCLUDED_DIRS = frozenset(
    {
        "tests",
        "scripts",
        "migrations",
        "docs",
        ".venv",
        ".worktrees",
        ".git",
        ".hypothesis",
        "__pycache__",
    }
)

#: Members with no shipped-tree consumer that are nevertheless kept, each with a
#: stated reason. An entry here is a claim that a consumer exists OUTSIDE the
#: Python the sweep can see — not a parking space for code nobody uses.
#:
#: Empty by construction as of the change that introduced this guard: every
#: unconsumed member was removed rather than excused.
_ALLOWLIST: dict[str, str] = {}


def _shipped_python_files() -> list[Path]:
    """Every ``.py`` file that reaches the runtime image."""
    files = []
    for path in sorted(REPO_ROOT.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT)
        if any(part in _EXCLUDED_DIRS for part in rel.parts):
            continue
        files.append(path)
    return files


def _public_members(source: str) -> dict[str, str]:
    """Public surface of ``KeycloakSettings``: ``{name: "attribute"|"property"}``.

    Attributes are ``self.x = ...`` (plain or annotated) inside the class body's
    methods; properties are ``@property`` / ``@cached_property`` defs.
    """
    tree = ast.parse(source)
    class_def = next(
        (n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "KeycloakSettings"),
        None,
    )
    assert class_def is not None, "KeycloakSettings class not found in auth/config.py"

    members: dict[str, str] = {}
    for node in ast.walk(class_def):
        # @property / @cached_property defs
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
            for dec in node.decorator_list:
                dec_name = dec.attr if isinstance(dec, ast.Attribute) else getattr(dec, "id", "")
                if dec_name in {"property", "cached_property"}:
                    members[node.name] = "property"
                    break
        # self.x = ... assignments
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
                and not target.attr.startswith("_")
            ):
                members.setdefault(target.attr, "attribute")
    return members


def _consumers(files: list[Path], names: set[str]) -> dict[str, list[str]]:
    """Map each name to the shipped-tree sites that READ it.

    Only ``ast.Attribute`` nodes in Load context count. The ``self.x = ...``
    definition site is a Store and is therefore not its own consumer — which is
    the whole point: an attribute assigned once and never read scores zero.
    """
    found: dict[str, list[str]] = {name: [] for name in names}
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(REPO_ROOT)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.ctx, ast.Load)
                and node.attr in names
            ):
                found[node.attr].append(f"{rel}:{node.lineno}: {ast.unparse(node)}")
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"getattr", "hasattr", "setattr"}
                and len(node.args) >= 2
            ):
                attr_arg = node.args[1]
                if isinstance(attr_arg, ast.Constant):
                    attr_name = attr_arg.value
                    if isinstance(attr_name, str) and attr_name in names:
                        found[attr_name].append(f"{rel}:{node.lineno}: {ast.unparse(node)}")
    return found


@pytest.fixture(scope="module")
def shipped_files() -> list[Path]:
    return _shipped_python_files()


@pytest.fixture(scope="module")
def members() -> dict[str, str]:
    return _public_members(CONFIG_PATH.read_text(encoding="utf-8"))


class TestCorpusIsNotEmpty:
    """Anti-vacuity. A walk that finds nothing must fail, not pass."""

    def test_shipped_corpus_is_populated(self, shipped_files):
        """The corpus walk must find real files, including the known entrypoints."""
        assert shipped_files, (
            f"ANTI-VACUITY: the shipped-tree walk under {REPO_ROOT} found zero .py "
            "files. The exclusion list or the repo layout changed; this guard "
            "would otherwise report every member dead."
        )
        rels = {str(p.relative_to(REPO_ROOT)) for p in shipped_files}
        for expected in ("auth/config.py", "auth/middleware.py", "main.py", "service_auth.py"):
            assert expected in rels, (
                f"ANTI-VACUITY: {expected} is missing from the shipped corpus "
                f"({len(rels)} files found). The walk is not seeing the service."
            )

    def test_excluded_dirs_are_actually_excluded(self, shipped_files):
        """`tests/` must NOT be in the corpus, or test-only use would look live."""
        rels = {str(p.relative_to(REPO_ROOT)) for p in shipped_files}
        assert not any(r.startswith("tests/") for r in rels), (
            "tests/ leaked into the shipped corpus; a member kept alive only by "
            "its own unit test would then be scored as consumed."
        )

    def test_member_discovery_is_populated(self, members):
        """The class-surface parse must find real members, including known-live ones."""
        assert members, (
            "ANTI-VACUITY: zero public members parsed out of KeycloakSettings. "
            "The class was renamed or the AST shape changed; the guard would "
            "otherwise pass by having nothing to check."
        )
        for expected in ("keycloak_url", "realm_name", "token_url", "service_token_url"):
            assert expected in members, (
                f"ANTI-VACUITY: known member {expected!r} was not discovered. "
                f"Discovered: {sorted(members)}"
            )

    def test_the_consumer_index_can_find_hits(self, shipped_files, members):
        """The counter must be able to score a member LIVE.

        If every member came back with zero consumers the guard would "fail
        loudly" for the wrong reason — a broken walk, not dead code.
        """
        found = _consumers(shipped_files, set(members))
        live = {name for name, sites in found.items() if sites}
        assert live, (
            "ANTI-VACUITY: the consumer index found zero reads of ANY member "
            "across the whole shipped tree. The AST sweep is broken."
        )


class TestDetectorFires:
    """The rule must be able to fail. A guard that cannot fire proves nothing."""

    def test_a_dead_member_is_detected(self):
        """A synthetic write-only attribute + unread property score zero."""
        source = (
            "class KeycloakSettings:\n"
            "    def __init__(self):\n"
            "        self.live_one = 1\n"
            "        self.dead_attr = 2\n"
            "    @property\n"
            "    def dead_prop(self):\n"
            "        return self.live_one\n"
        )
        members = _public_members(source)
        assert members == {
            "live_one": "attribute",
            "dead_attr": "attribute",
            "dead_prop": "property",
        }
        # `self.live_one` is Load-read inside dead_prop; the other two never are.
        tmp_tree = ast.parse(source)
        loads = {
            n.attr
            for n in ast.walk(tmp_tree)
            if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load)
        }
        assert loads == {"live_one"}, f"detector mis-scored Load context: {loads}"

    def test_store_context_is_not_a_consumer(self):
        """`self.x = ...` must not count as a read of `x`."""
        tree = ast.parse("class C:\n    def __init__(self):\n        self.x = 1\n")
        stores = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store)
        ]
        assert len(stores) == 1 and stores[0].attr == "x"
        loads = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load)
        ]
        assert loads == [], "an assignment target was mis-read as a consumer"


class TestEveryPublicMemberIsConsumed:
    """The rule itself."""

    def test_no_unconsumed_public_member(self, shipped_files, members):
        """Every public member of KeycloakSettings is read somewhere shipped."""
        found = _consumers(shipped_files, set(members))
        dead = {
            name: members[name]
            for name, sites in found.items()
            if not sites and name not in _ALLOWLIST
        }
        assert not dead, (
            "auth/config.py exposes configuration surface that NOTHING in the "
            "shipped tree reads. Delete it, or add it to _ALLOWLIST with the "
            "off-tree consumer that justifies it.\n"
            + "\n".join(f"  - {name} ({kind}) — 0 consumers" for name, kind in sorted(dead.items()))
        )

    def test_allowlist_has_no_stale_entries(self, shipped_files, members):
        """An allowlisted member that gained a consumer must leave the allowlist."""
        found = _consumers(shipped_files, set(members))
        stale = sorted(n for n in _ALLOWLIST if found.get(n))
        assert not stale, (
            f"_ALLOWLIST excuses members that now HAVE consumers: {stale}. "
            "Remove the entries so the exemption cannot outlive its reason."
        )

    def test_allowlist_entries_state_a_reason(self):
        """An allowlist entry without a real reason is an unexplained exemption."""
        unexplained = sorted(n for n, reason in _ALLOWLIST.items() if len(reason.strip()) < 20)
        assert not unexplained, (
            f"_ALLOWLIST entries with no stated reason: {unexplained}. "
            "State the off-tree consumer that keeps the member alive."
        )

    def test_allowlist_names_only_real_members(self, members):
        """An allowlist entry for a deleted member is dead weight that hides drift."""
        unknown = sorted(n for n in _ALLOWLIST if n not in members)
        assert not unknown, (
            f"_ALLOWLIST names members that KeycloakSettings no longer exposes: {unknown}."
        )
