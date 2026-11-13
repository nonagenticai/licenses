"""No log line and no response body may carry a DSN-carrying value unredacted.

THE DEFECT THIS EXISTS FOR
--------------------------
This service printed the production DSN — driver, user, **password**, host, port,
database — on every pod start. Pod logs are readable by anyone with ``kubectl logs``
on the namespace and are shipped onward to whatever collects them, so the
``licenses_user`` password was published fleet-wide, continuously.

The first guard was vacuous in a way that took an adversarial pass to see.
``test_the_logged_startup_line_carries_no_password`` claimed to "drive the real
logging call the way lifespan does" — and then authored its OWN call::

    main.logger.info("Database initialized: %s", main.redact_dsn(main.DATABASE_URL))

It asserted on a statement the test itself wrote, so it said nothing whatsoever about
``main.py``. Its companion only required the substring ``redact_dsn(DATABASE_URL)`` to
appear *somewhere* in the file and banned one exact f-string spelling. Between them,
**7 of 10 mutations survived**.

THE SECOND DEFECT — WHY THIS FILE WAS REWRITTEN
-----------------------------------------------
The replacement checked the SHAPE OF THE SOURCE: walk every logging call and fail if an
argument reaches a DSN-carrying **name** without passing through ``redact_dsn``. That
closed every spelling that goes through the ``DATABASE_URL`` symbol — and left the
symbol itself optional. ``main.py`` reads its DSN from the environment
(``DATABASE_URL = os.getenv("DATABASE_URL", ...)``), and so can anybody else, at any
point, in one line::

    logger.info("Database URL: %s", os.getenv("DATABASE_URL"))

That is a raw production password on every pod start, and it was **measured green**:
with that exact line committed into ``main.py``'s lifespan, this file reported ``4
passed`` and the whole suite reported ``2 failed / 1386 passed / 58 deselected`` —
byte-identical to a clean baseline. The two pre-existing failures are unrelated
``test_billing_contract`` drift. A guard that cannot fail on a real leak IS the defect;
the rule keyed on a *name* when the harm is carried by a *value*.

THE RULE
--------
For every module that holds a DSN, no **DSN-valued expression** may reach a **sink**
without passing through ``redact_dsn``.

A DSN-valued expression is any of:

  * a name in ``_DSN_NAMES`` (``DATABASE_URL``, ``TEST_DATABASE_URL``, ``database_url``,
    ``dsn``) — the original rule, unchanged;
  * a name in ``_DSN_NAMES_IN_DSN_FUNCTIONS`` (``result``, ``url``) *inside a function
    that takes a DSN parameter*. ``result`` is the local in ``_append_ssl_params``
    holding the URL with SSL params appended — the exact variable whose
    ``.split("@")[0]`` "redaction" printed the password, because everything BEFORE the
    first ``@`` is ``scheme://user:password``. They are scoped because ``result`` and
    ``url`` are ordinary names elsewhere: flagging them globally produced a false
    positive on a ``byo_mode`` handler whose ``result`` is a dict, and a guard that
    cries wolf gets deleted;
  * an **environment read of a DSN key** — ``os.getenv("DATABASE_URL")``,
    ``os.environ["DATABASE_URL"]``, ``os.environ.get("DATABASE_URL")``, the
    ``from os import getenv`` bare form, an aliased module (``import os as _os``), and
    any mapping read of the same key (``cfg["DATABASE_URL"]``, ``cfg.get(...)``). The
    KEY is what is matched, so a value that came from the environment is DSN-valued no
    matter which accessor spelled it;
  * an **attribute** whose final component is a DSN name — ``settings.DATABASE_URL``,
    ``app.state.database_url``. An ``ast.Name`` walk sees ``settings``, never
    ``DATABASE_URL``, so this was a second silent bypass;
  * an **alias** of any of the above. Assignment is tracked in SOURCE ORDER within a
    scope, so ``raw = os.getenv("DATABASE_URL")`` taints ``raw`` for the rest of that
    scope and ``raw = compute()`` clears it again. Module-level taint is seeded into
    nested scopes (minus any parameter that shadows the name), because
    ``_BOOT_DSN = os.getenv("DATABASE_URL")`` at module level and ``logger.info("%s",
    _BOOT_DSN)`` inside ``lifespan`` is one leak written across two scopes;
  * a string built from any of the above — f-string, ``+``, ``%``, ``.format()``,
    ``"".join()``, ``str()``, a ternary, an ``or`` default, or a literal
    tuple/list/dict/set that CONTAINS one.

A sink is:

  * a logging call — ``logger.{debug,info,warning,warn,error,exception,critical,log}``
    — or ``print``;
  * a **response-body sink**. A DSN in a 500 body is the same published credential with
    a worse reader, and ``/health/ready`` is in ``PUBLIC_ENDPOINTS``. The registry is
    IMPORTED from ``test_no_exception_leak`` (``BODY_SINKS``, ``RESPONSE_CLASS_SUFFIX``,
    ``RESPONSE_CONTENT_ARG``) rather than copied, so the two rules cannot drift into
    covering different sets of sinks. ``test_the_shared_sink_registry_is_live`` fails if
    that import ever stops carrying the sinks this rule depends on.

Descent stops at ``redact_dsn`` — ``redact_dsn(os.getenv("DATABASE_URL"))`` is clean —
and stops at an environment read, so the literal ``"DATABASE_URL"`` *inside* the
accessor is never itself mistaken for a leak.

WHAT THIS RULE DOES **NOT** CATCH (stated, not implied)
-------------------------------------------------------
This is a static AST walker over a fixed corpus. It does not catch:

  1. **A DSN read with a non-literal key.** ``os.getenv(key)`` where
     ``key = "DATABASE" + "_URL"``, or a key read from a table at runtime. The literal
     is the whole signal; without one there is nothing to match. ``main._env_int`` does
     exactly ``raw = os.getenv(name)`` and logs ``raw`` on a parse failure — a
     legitimate, non-leaking shape that this rule must stay silent on, so the
     dynamic-key case cannot be closed by banning the shape. See
     ``test_a_dynamic_env_key_is_not_caught``.
  2. **Cross-function flow other than the module-level seed.** ``_stash(DATABASE_URL)``
     in one function and ``logger.info(_LAST)`` in another. Taint crosses module -> scope
     only, never scope -> scope or scope -> module.
  3. **Mutating-method taint.** ``parts = []; parts.append(DATABASE_URL);
     logger.info(" ".join(parts))``. Assignment is tracked; in-place mutation through a
     method call is not.
  4. **A sink not in the registry.** A new helper ``_emit(msg)`` that writes to a log or
     a body is invisible until its name is added. Full interprocedural analysis is the
     only real fix and is out of proportion to a unit-test gate.
  5. **Credential env vars that are not a DSN.** ``os.getenv("POSTGRES_PASSWORD")`` is a
     published credential too, but it is not this rule's subject and adding it here
     would make the failure message lie about what was found.
  6. **``scripts/``.** Twenty CronJob/one-shot scripts read ``DATABASE_URL``. They are
     deliberately outside the corpus (same skip set as ``test_no_exception_leak``), so
     a leak there is uncovered by BOTH rules. This is the largest known hole and it is
     measured, not assumed: ``test_the_uncovered_script_corpus_is_measured`` pins the
     count so the hole cannot grow silently.
  7. **A DSN reconstructed from parts.** ``f"{scheme}://{user}:{pw}@{host}/{db}"``
     assembled from five non-DSN names is a DSN this rule cannot see.

Gaps 1-7 are why ``redact_dsn`` (``models.py``) exists as a runtime layer and fails
CLOSED — see ``tests/unit/test_dsn_redaction.py``, which covers that DIFFERENT layer.
This file never calls it; it only recognises it as the sanitiser that clears a sink.

``_GUARDED_FILES`` is NOT a hand-maintained list you must remember to update:
``test_guarded_files_covers_every_dsn_holding_module`` derives the required set from the
shipped tree and fails if a module that holds a DSN is missing from it, and
``test_the_staleness_guard_would_notice_a_dropped_module`` proves that guard can
actually fail by dropping a module and requiring the guard's own predicate to name it.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard, cast

# The response-body sink registry is SHARED with the exception-leak rule rather than
# copied. Two independent copies drift, and the drift is silent: whichever rule is not
# updated simply stops covering a sink. See the module docstring.
from tests.unit.test_no_exception_leak import (
    BODY_SINKS,
    RESPONSE_CLASS_SUFFIX,
    RESPONSE_CONTENT_ARG,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# Modules that hold a DSN in a local or module-level name.
#
# DERIVED, not hand-listed. It used to be the literal `("main.py", "models.py")` guarded
# by a staleness test — which caught a module someone ADDED, but could not catch the 20
# modules that were already there and already excluded, because the same `scripts` entry
# was missing from the discovery walk as from this tuple. A hole present in both the list
# and the checker of the list is invisible to the checker.
#
# Computing it from the tree removes that failure mode entirely: the corpus is whatever
# the tree holds. `test_the_corpus_is_derived_from_the_tree_not_a_literal` pins the
# derivation, and the floors below pin its size so a glob that matches nothing cannot
# pass as "discovered".
#: Bound after `_shipped_python_files` is defined — see `_GUARDED_FILES` below.
# Names that always carry a DSN wherever they appear in these modules.
_DSN_NAMES = frozenset({"DATABASE_URL", "TEST_DATABASE_URL", "database_url", "dsn"})

# Environment keys whose VALUE is a DSN. Derived from `_DSN_NAMES` so the two cannot
# drift apart — an added DSN symbol brings its env key with it for free.
# `test_the_env_key_set_tracks_the_name_set` pins the derivation.
_DSN_ENV_VARS = frozenset(name for name in _DSN_NAMES if name.isupper())

# Names that carry a DSN *only inside a DSN-handling function* — `result` is the local
# in `_append_ssl_params` holding the URL with SSL params appended, i.e. the exact
# variable whose `.split("@")[0]` "redaction" printed the password. Scoped because
# `result` and `url` are ordinary names elsewhere (see the module docstring).
_DSN_NAMES_IN_DSN_FUNCTIONS = frozenset({"result", "url"})

_LOG_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
)

_SANITISERS = frozenset({"redact_dsn"})

# Accessor names that read a value out of a mapping-like object by key. `getenv` is
# matched on any base (`os.getenv`, `_os.getenv`, a bare `getenv` from
# `from os import getenv`); `get` and subscripting are matched on any base too, because
# `cfg["DATABASE_URL"]` is exactly as much of a DSN as `os.environ["DATABASE_URL"]` and
# the KEY is the whole signal.
_ENV_ACCESSORS = frozenset({"getenv", "get"})

# String operations that PRESERVE DSN-ness: the result still contains the credential.
_STRING_OPS = frozenset(
    {
        "format",
        "join",
        "strip",
        "lstrip",
        "rstrip",
        "lower",
        "upper",
        "casefold",
        "replace",
        "split",
        "rsplit",
        "partition",
        "rpartition",
        "removeprefix",
        "removesuffix",
        "encode",
        "decode",
        "str",
        "repr",
    }
)

#: Node types that open a new binding scope. Alias taint does not cross these, except
#: for the one-way module -> nested seed described in the docstring.
_SCOPE_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)

#: Directories this rule does not read.
#:
#: This USED to be identical to `test_no_exception_leak._DISCOVERY_SKIP_DIRS`, on the
#: stated grounds that "a module the other rule declines to scan is not silently in
#: scope here either". That symmetry was wrong for exactly one entry, `scripts`, and the
#: asymmetry is the point: the other rule is about what reaches an HTTP RESPONSE BODY,
#: and a script serves no responses, so excluding it there is correct. THIS rule is
#: about what reaches a LOG, and scripts log — `scripts/reconcile_metering.py` is the
#: daily CronJob, and every `scripts/provision_*.py` is run under `kubectl exec`, whose
#: stdout is captured by exactly the same collector as pod stdout. A DSN printed there
#: is the production `licenses_user` password in the same place, read by the same people.
#:
#: `migrations` stays out: alembic revisions are generated, hold no logging, and their
#: `sqlalchemy.url` is config, not a call argument.
_DISCOVERY_SKIP_DIRS = frozenset({"tests", "migrations", ".venv", ".worktrees", "certs", "docs"})

#: Substrings that mark a module as holding a DSN.
_DSN_MARKERS = tuple(sorted(_DSN_NAMES))


@dataclass(frozen=True)
class Violation:
    file: str
    lineno: int
    sink: str
    carrier: str

    def __str__(self) -> str:  # pragma: no cover - only rendered on failure
        return f"{self.file}:{self.lineno}: {self.sink} receives {self.carrier} unredacted"


# ---------------------------------------------------------------------------
# Recognising a DSN-valued expression
# ---------------------------------------------------------------------------


def _terminal_name(node: ast.AST) -> str | None:
    """``os.environ`` -> ``environ``; ``environ`` -> ``environ``; else None."""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _dsn_key(node: ast.AST) -> str | None:
    """The DSN env key if ``node`` is a literal ``"DATABASE_URL"``-style constant."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value if node.value in _DSN_ENV_VARS else None
    return None


def _dsn_env_read(node: ast.AST) -> str | None:
    """The env key read by ``node``, if ``node`` reads a DSN out of a mapping.

    Covers ``os.getenv(K)``, ``getenv(K)``, ``_os.getenv(K)``, ``os.environ[K]``,
    ``environ[K]``, ``os.environ.get(K)`` and any other ``obj[K]`` / ``obj.get(K)``.
    Matched on the literal KEY, never on the accessor alone — banning ``os.getenv``
    outright would flag ``os.getenv("LOG_LEVEL")``, which is why
    ``test_an_unrelated_getenv_is_not_a_violation`` exists.
    """
    if isinstance(node, ast.Subscript):
        return _dsn_key(node.slice)
    if isinstance(node, ast.Call) and node.args:
        func = node.func
        name = func.id if isinstance(func, ast.Name) else _terminal_name(func)
        if name in _ENV_ACCESSORS:
            return _dsn_key(node.args[0])
    return None


def _receiver_chain(node: ast.AST) -> list[str]:
    """``self.engine.url.password`` -> ``["self", "engine", "url", "password"]``."""
    parts: list[str] = []
    cur: ast.AST | None = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return list(reversed(parts))


def _is_url_password_read(node: ast.AST) -> bool:
    """``engine.url.password`` — the plaintext password off a SQLAlchemy ``URL``.

    MEASURED on the installed SQLAlchemy (2.0.51), not assumed:

        str(url)                              -> …//licenses_user:***@host/licenses
        url.render_as_string(hide_password=0) -> …//licenses_user:SuperSecret@host/…
        url.password                          -> SuperSecret

    So ``str(engine.url)`` is SAFE — ``URL.__str__`` renders with
    ``hide_password=True`` — and is deliberately NOT flagged here. The two spellings
    that do return the credential are. Getting this backwards would have added a rule
    that fires on the harmless spelling while the harmful ones stayed invisible.

    Keyed on ``.url.password`` rather than on ``.password`` alone: a bare ``.password``
    is `user.password` and `settings.password` all over an auth service, and a rule that
    cries wolf gets deleted. ``models.py`` is where the engine lives, so the receiver is
    an engine in every reachable spelling.
    """
    if not isinstance(node, ast.Attribute) or node.attr != "password":
        return False
    return "url" in _receiver_chain(node)[:-1]


def _is_unhidden_render_call(node: ast.AST) -> bool:
    """``url.render_as_string(hide_password=False)`` — the DSN with the password in it.

    The default is ``hide_password=True``, so only an explicit falsy override leaks;
    a bare ``render_as_string()`` is as safe as ``str(url)`` and is not flagged.
    """
    if not isinstance(node, ast.Call):
        return False
    if getattr(node.func, "attr", None) != "render_as_string":
        return False
    for kw in node.keywords:
        if kw.arg == "hide_password" and isinstance(kw.value, ast.Constant):
            return not kw.value.value
    return False


def _is_dsn_attribute(node: ast.AST) -> bool:
    """``settings.DATABASE_URL`` / ``app.state.database_url`` — an attribute, not a Name.

    An ``ast.Name`` walk sees ``settings`` and never ``DATABASE_URL``, so without this
    an attribute read of the settings symbol walked straight past the rule.
    """
    if _is_url_password_read(node):
        return True
    return isinstance(node, ast.Attribute) and node.attr in _DSN_NAMES


def _is_sanitiser_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    return name in _SANITISERS


def _unsanitised_dsn_carriers(
    node: ast.AST, extra_names: frozenset[str] = frozenset()
) -> list[str]:
    """DSN-carrying expressions reachable in ``node`` without passing a sanitiser.

    Descent stops at a sanitiser call, so ``redact_dsn(DATABASE_URL)`` is clean while
    ``"x" + DATABASE_URL``, ``f"{DATABASE_URL}"``, ``result.split("@")[0]`` and
    ``os.getenv("DATABASE_URL")`` are not. Descent also stops at an environment read, so
    the literal ``"DATABASE_URL"`` *inside* the accessor is not double-counted as a leak.
    """
    found: list[str] = []

    def walk(n: ast.AST) -> None:
        if _is_sanitiser_call(n):
            return  # sanitised — do not descend
        if (key := _dsn_env_read(n)) is not None:
            found.append(f"an environment read of ${key}")
            return  # the key literal below is the NAME, not the value
        if _is_unhidden_render_call(n):
            found.append(f"the unhidden render {ast.unparse(n)!r}")
            return  # the receiver below is the URL object, not a second finding
        if _is_dsn_attribute(n):
            assert isinstance(n, ast.Attribute)
            found.append(f"the attribute {ast.unparse(n)!r}")
            return
        if isinstance(n, ast.Name) and (n.id in _DSN_NAMES or n.id in extra_names):
            found.append(repr(n.id))
        for child in ast.iter_child_nodes(n):
            walk(child)

    walk(node)
    return found


def _is_dsn_valued(expr: ast.AST | None, tainted: frozenset[str]) -> bool:
    """True when the VALUE of ``expr`` is (or contains) a DSN.

    Deliberately narrower than ``_unsanitised_dsn_carriers``: this decides whether an
    assignment propagates taint, and "contains a DSN name somewhere" is far too wide for
    that. ``db_manager = DatabaseManager(DATABASE_URL)`` mentions the DSN but evaluates
    to a manager, and tainting ``db_manager`` would make every log line that names it a
    false positive. So an arbitrary call is NOT DSN-valued; only string operations,
    container literals and the reads above are.
    """
    if expr is None:
        return False
    if _is_sanitiser_call(expr):
        return False
    if _dsn_env_read(expr) is not None:
        return True
    if _is_dsn_attribute(expr):
        return True
    if isinstance(expr, ast.Name):
        return expr.id in _DSN_NAMES or expr.id in tainted
    if isinstance(expr, ast.JoinedStr):
        return any(
            _is_dsn_valued(part.value, tainted)
            for part in expr.values
            if isinstance(part, ast.FormattedValue)
        )
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add | ast.Mod):
        return _is_dsn_valued(expr.left, tainted) or _is_dsn_valued(expr.right, tainted)
    if isinstance(expr, ast.IfExp):
        return _is_dsn_valued(expr.body, tainted) or _is_dsn_valued(expr.orelse, tainted)
    if isinstance(expr, ast.BoolOp):
        return any(_is_dsn_valued(value, tainted) for value in expr.values)
    if isinstance(expr, ast.Tuple | ast.List | ast.Set):
        return any(_is_dsn_valued(elt, tainted) for elt in expr.elts)
    if isinstance(expr, ast.Dict):
        return any(_is_dsn_valued(value, tainted) for value in expr.values if value is not None)
    if isinstance(expr, ast.Starred):
        return _is_dsn_valued(expr.value, tainted)
    if isinstance(expr, ast.Call):
        func = expr.func
        name = func.id if isinstance(func, ast.Name) else _terminal_name(func)
        if name not in _STRING_OPS:
            return False
        base = func.value if isinstance(func, ast.Attribute) else None
        return _is_dsn_valued(base, tainted) or any(
            _is_dsn_valued(arg, tainted) for arg in expr.args
        )
    return False


# ---------------------------------------------------------------------------
# Sinks
# ---------------------------------------------------------------------------


def _is_logging_call(node: ast.AST) -> TypeGuard[ast.Call]:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name) and func.id == "print":
        return True
    return isinstance(func, ast.Attribute) and func.attr in _LOG_METHODS


def _body_sink_argument(node: ast.Call) -> ast.expr | None:
    """The caller-visible argument of ``node``, if ``node`` builds a response body.

    Uses the registry imported from ``test_no_exception_leak`` so the two rules cover
    the same sinks. ``content`` is starlette's FIRST parameter — reading position 1
    grabs ``status_code`` instead and misses every positional leak.
    """
    func = node.func
    if isinstance(func, ast.Name):
        name: str | None = func.id
    elif isinstance(func, ast.Attribute):
        name = func.attr
    else:
        return None

    spec = BODY_SINKS.get(name)
    if spec is None and name.endswith(RESPONSE_CLASS_SUFFIX):
        spec = RESPONSE_CONTENT_ARG
    if spec is None:
        return None

    keyword, position = spec
    for kw in node.keywords:
        if kw.arg == keyword:
            return kw.value
    if len(node.args) > position:
        return node.args[position]
    return None


# ---------------------------------------------------------------------------
# Scope walking and source-ordered alias taint
# ---------------------------------------------------------------------------


def _iter_own_nodes(node: ast.AST) -> Iterator[ast.AST]:
    """Every node belonging to this scope, NOT descending into nested scopes."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _SCOPE_TYPES):
            continue
        yield child
        yield from _iter_own_nodes(child)


def _immediate_scopes(node: ast.AST) -> Iterator[ast.AST]:
    """The scopes nested directly inside ``node``, at any statement depth."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _SCOPE_TYPES):
            yield child
        else:
            yield from _immediate_scopes(child)


def _is_dsn_function(fn: ast.AST) -> bool:
    """A function is DSN-handling when it takes a DSN-carrying parameter."""
    if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
        return False
    args = fn.args
    names = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    return bool(names & _DSN_NAMES)


def _parameter_names(scope: ast.AST) -> set[str]:
    if not isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
        return set()
    args = scope.args
    names = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    for extra in (args.vararg, args.kwarg):
        if extra is not None:
            names.add(extra.arg)
    return names


def _position(node: ast.AST) -> tuple[int, int]:
    return (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))


def _assignment_parts(node: ast.AST) -> tuple[ast.expr | None, Sequence[ast.expr], bool] | None:
    """``(value, targets, is_augmented)`` for any binding form, else None."""
    if isinstance(node, ast.Assign):
        return node.value, list(node.targets), False
    if isinstance(node, ast.AnnAssign):
        return node.value, [node.target], False
    if isinstance(node, ast.AugAssign):
        # `x += DATABASE_URL` never CLEARS x, it only ever adds to it.
        return node.value, [node.target], True
    if isinstance(node, ast.NamedExpr):
        return node.value, [node.target], False
    if isinstance(node, ast.withitem) and node.optional_vars is not None:
        return node.context_expr, [node.optional_vars], False
    return None


def _root_name(node: ast.expr) -> str | None:
    while isinstance(node, ast.Attribute | ast.Subscript):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _bound_root_names(targets: Sequence[ast.expr]) -> set[str]:
    names: set[str] = set()
    for target in targets:
        for sub in ast.walk(target):
            if isinstance(sub, ast.Name | ast.Attribute | ast.Subscript):
                root = _root_name(sub)
                if root:
                    names.add(root)
    return names


class _Scanner:
    """Source-ordered alias taint over one module.

    Events (assignments and sink calls) are replayed in SOURCE POSITION order within a
    scope, so ``raw = os.getenv("DATABASE_URL")`` taints ``raw`` for the statements
    below it and a later ``raw = compute()`` clears it again.
    """

    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.violations: list[Violation] = []
        self.inspected = 0

    def scan(self, scope: ast.AST, extra: frozenset[str]) -> frozenset[str]:
        events: list[tuple[tuple[int, int], int, str, object]] = []
        for order, node in enumerate(_iter_own_nodes(scope)):
            if (parts := _assignment_parts(node)) is not None:
                anchor = node.context_expr if isinstance(node, ast.withitem) else node
                events.append((_position(anchor), order, "assign", parts))
            elif isinstance(node, ast.Call):
                if _is_logging_call(node):
                    for arg in [*node.args, *(kw.value for kw in node.keywords)]:
                        events.append((_position(node), order, "log call", arg))
                elif (detail := _body_sink_argument(node)) is not None:
                    events.append((_position(node), order, "response body", detail))

        tainted = set(extra)
        for _pos, _order, kind, payload in sorted(events, key=lambda e: (e[0], e[1])):
            if kind == "assign":
                value, targets, is_augmented = cast(
                    tuple[ast.expr | None, Sequence[ast.expr], bool], payload
                )
                bound = _bound_root_names(targets)
                if _is_dsn_valued(value, frozenset(tainted)):
                    tainted |= bound
                elif not is_augmented:
                    tainted -= bound
                continue
            self.inspected += 1
            expr = cast(ast.expr, payload)
            for carrier in _unsanitised_dsn_carriers(expr, frozenset(tainted)):
                self.violations.append(
                    Violation(self.filename, getattr(expr, "lineno", 0), kind, carrier)
                )
        return frozenset(tainted)


def scan_source(source: str, filename: str = "<synthetic>") -> tuple[list[Violation], int]:
    """Scan one module. Returns ``(violations, sink_arguments_inspected)``."""
    tree = ast.parse(source)
    scanner = _Scanner(filename)

    # Pass 1: the module scope. Its final taint set is the seed for nested scopes —
    # `_BOOT_DSN = os.getenv("DATABASE_URL")` at module level and `logger.info(_BOOT_DSN)`
    # inside `lifespan` is one leak written across two scopes.
    module_tainted = scanner.scan(tree, frozenset())

    def descend(scope: ast.AST, inherited: frozenset[str]) -> None:
        for sub in _immediate_scopes(scope):
            extra = inherited
            if _is_dsn_function(sub):
                extra = extra | _DSN_NAMES_IN_DSN_FUNCTIONS
            # A parameter SHADOWS a module-level name; it is not the same binding.
            scanner.scan(sub, extra - _parameter_names(sub))
            descend(sub, extra)

    descend(tree, module_tainted)
    return scanner.violations, scanner.inspected


def scan_repo() -> tuple[list[Violation], int, dict[str, int]]:
    violations: list[Violation] = []
    total = 0
    per_file: dict[str, int] = {}
    for rel in _GUARDED_FILES:
        path = REPO_ROOT / rel
        assert path.is_file(), f"{rel} is missing — _GUARDED_FILES is stale"
        found, inspected = scan_source(path.read_text(encoding="utf-8"), rel)
        violations.extend(found)
        per_file[rel] = inspected
        total += inspected
    return violations, total, per_file


def _shipped_python_files() -> list[Path]:
    return [
        path
        for path in sorted(REPO_ROOT.rglob("*.py"))
        if not (set(path.relative_to(REPO_ROOT).parts) & _DISCOVERY_SKIP_DIRS)
        and not path.relative_to(REPO_ROOT).parts[0].startswith(".")
    ]


def _dsn_holding_modules() -> tuple[str, ...]:
    return tuple(
        str(path.relative_to(REPO_ROOT))
        for path in _shipped_python_files()
        if any(marker in path.read_text(encoding="utf-8") for marker in _DSN_MARKERS)
    )


#: The scanned corpus. Bound at import so `scan_repo` and every floor see one value.
_GUARDED_FILES: tuple[str, ...] = _dsn_holding_modules()


def _dsn_holding_modules_missing_from(guarded: Sequence[str]) -> list[str]:
    """Shipped modules that hold a DSN but are absent from ``guarded``.

    Extracted so the staleness guard and its mutation proof call the SAME predicate.
    Two copy-pasted loops asserting the same proposition is how the sibling rule's
    "proof" came to prove nothing.
    """
    guarded_set = set(guarded)
    missing: list[str] = []
    for path in _shipped_python_files():
        rel = str(path.relative_to(REPO_ROOT))
        text = path.read_text(encoding="utf-8")
        if not any(marker in text for marker in _DSN_MARKERS):
            continue
        if rel not in guarded_set:
            missing.append(rel)
    return missing


# ---------------------------------------------------------------------------
# The invariant, over the real source tree
# ---------------------------------------------------------------------------


class TestNoDsnReachesASink:
    def test_no_logging_call_receives_an_unredacted_dsn(self) -> None:
        """The whole class, not one spelling of it.

        This is the test a leak in ``main.py``/``models.py`` fails. It fires on the
        symbol (``DATABASE_URL``), on the environment read (``os.getenv("DATABASE_URL")``
        — the shape that was measured GREEN under the previous rule), on an attribute
        (``settings.DATABASE_URL``), and on any alias of the three.
        """
        violations, _, _ = scan_repo()
        offenders = [v for v in violations if v.sink == "log call"]
        assert not offenders, (
            "a DSN-carrying value reaches a logging call without redact_dsn — this is "
            "how the production database password was printed on every pod start:\n  "
            + "\n  ".join(str(v) for v in offenders)
        )

    def test_no_response_sink_receives_an_unredacted_dsn(self) -> None:
        """A DSN in a 500 body is the same credential with a worse reader.

        ``/health/ready`` is listed in ``PUBLIC_ENDPOINTS`` (``auth/middleware.py``), so
        any pod in the cluster can read an unauthenticated error body off this service.
        """
        violations, _, _ = scan_repo()
        offenders = [v for v in violations if v.sink == "response body"]
        assert not offenders, (
            "a DSN-carrying value reaches a response body without redact_dsn:\n  "
            + "\n  ".join(str(v) for v in offenders)
        )

    def test_the_startup_line_still_exists(self) -> None:
        """A deleted log line passes every leak check while removing the evidence.

        Not a security defect, but it silently destroys the operator's only answer to
        "which database did this pod attach to?" — and the original suite could not tell
        the difference between "redacted" and "gone".
        """
        main_src = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        models_src = (REPO_ROOT / "models.py").read_text(encoding="utf-8")
        assert "Database initialized" in main_src, "the startup DSN line was removed"
        assert "Database URL with SSL" in models_src, "the SSL-branch DSN line was removed"

    def test_the_startup_line_is_sanitised_in_the_real_source(self) -> None:
        """Pin the one line the whole workstream exists for, by name.

        A regression here should not merely be one entry in a list of violations.
        """
        tree = ast.parse((REPO_ROOT / "main.py").read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if _is_logging_call(node)
            and any(
                isinstance(arg, ast.Constant) and "Database initialized" in str(arg.value)
                for arg in node.args
            )
        ]
        assert len(calls) == 1, f"expected exactly one startup DSN log line, found {len(calls)}"
        assert any(_is_sanitiser_call(sub) for sub in ast.walk(calls[0])), (
            "the startup line no longer passes the DSN through redact_dsn"
        )


class TestWalkerIsNotVacuous:
    """A walker that finds nothing must not read as coverage."""

    def test_the_guarded_files_exist_and_parse(self) -> None:
        """Anti-vacuity: every assertion here is empty if the walker sees nothing."""
        for rel in _GUARDED_FILES:
            path = REPO_ROOT / rel
            assert path.is_file(), f"{rel} is missing — _GUARDED_FILES is stale"
            ast.parse(path.read_text(encoding="utf-8"))

    def test_inspected_enough_sink_arguments(self) -> None:
        """Per-file floors, not just a total.

        A total alone lets one file's coverage collapse while the other's growth hides
        it — and a corpus that collapses to zero is exactly how a source-level gate goes
        green forever without anyone noticing.
        """
        violations, total, per_file = scan_repo()
        # Measured 685 / 298 / 39 / 348 with the corpus derived from the tree (it was
        # 337 across two files when only main.py and models.py were read). The floors
        # sit ~12% below so ordinary refactoring does not trip them, while a corpus that
        # collapses — the failure this class exists for — does.
        assert total >= 600, f"walker inspected only {total} sink arguments: {per_file}"
        assert per_file["main.py"] >= 260, (
            f"main.py contributed only {per_file['main.py']} sink arguments — the walker "
            "is no longer seeing the file the rule exists for"
        )
        assert per_file["models.py"] >= 34, (
            f"models.py contributed only {per_file['models.py']} sink arguments — the "
            "SSL branch that printed the password lives there"
        )
        # A per-GROUP floor for scripts, for the same reason main.py and models.py have
        # their own: a total alone lets the whole script corpus drop out while main.py's
        # growth hides it, and the script corpus is 51% of the sink arguments this rule
        # inspects. Without this line, deleting `scripts` from the discovery walk again
        # would still clear `total >= 600` for a while.
        script_args = sum(n for rel, n in per_file.items() if rel.startswith("scripts/"))
        assert script_args >= 300, (
            f"the scripts corpus contributed only {script_args} sink arguments — these "
            "run under `kubectl exec` and as the metering CronJob, and their stdout is "
            "collected exactly like pod stdout"
        )
        assert not violations

    def test_walker_still_detects_a_planted_env_leak(self) -> None:
        """Positive control, in the same class as the count.

        A counter proves the walker *looked*; it does not prove the walker can still
        *see*. A detector gutted to ``return []`` satisfies the floor above and reports
        a clean tree — the exact "green means nothing" failure this class prevents.
        """
        planted, inspected = scan_source('logger.info("db %s", os.getenv("DATABASE_URL"))\n')
        assert inspected == 2
        assert len(planted) == 1, "the walker no longer detects the headline evasion"

    def test_guarded_files_covers_every_dsn_holding_module(self) -> None:
        """Positive control against a stale ``_GUARDED_FILES``.

        Derived from the corpus, never trusted: any shipped module whose text names a
        DSN symbol must be scanned. A hand-maintained tuple silently under-covers the
        moment someone adds a module — and under-covering is invisible, because a
        smaller corpus produces fewer violations, not more.
        """
        missing = _dsn_holding_modules_missing_from(_GUARDED_FILES)
        assert not missing, f"modules hold a DSN but are not scanned: {missing}"

    def test_the_staleness_guard_would_notice_a_dropped_module(self) -> None:
        """Mutation-proof for the guard above, which passes vacuously on a clean tree.

        Actually drops a module and requires the guard's OWN predicate to name it, so
        the two cannot drift into asserting the same trivially-true proposition.
        """
        assert _dsn_holding_modules_missing_from(_GUARDED_FILES) == []
        for victim in _GUARDED_FILES:
            survivors = [f for f in _GUARDED_FILES if f != victim]
            named = _dsn_holding_modules_missing_from(survivors)
            assert named == [victim], f"dropping {victim} must make the guard name it; got {named}"

    def test_the_env_key_set_tracks_the_name_set(self) -> None:
        """``_DSN_ENV_VARS`` is derived, not hand-listed — pin the derivation.

        If someone replaces the comprehension with a literal that omits
        ``TEST_DATABASE_URL``, the env-read half of this rule silently narrows.
        """
        assert _DSN_ENV_VARS == {"DATABASE_URL", "TEST_DATABASE_URL"}
        assert _DSN_ENV_VARS <= _DSN_NAMES

    def test_the_shared_sink_registry_is_live(self) -> None:
        """The response half of this rule is only as good as the imported registry."""
        assert {"HTTPException", "_internal_error", "_client_error", "_error_response"} <= set(
            BODY_SINKS
        )
        assert RESPONSE_CLASS_SUFFIX == "Response"
        assert RESPONSE_CONTENT_ARG == ("content", 0)


# ---------------------------------------------------------------------------
# Discriminating power — synthetic, because a clean corpus proves nothing
# ---------------------------------------------------------------------------


class TestDetectorDiscriminatingPower:
    """The gate must fire on every leak spelling and stay quiet on legitimate code.

    A gate cannot prove its own discriminating power against a corpus that is already
    clean, so every case here is synthetic source parsed on the spot.
    """

    @staticmethod
    def _violations(body: str) -> list[Violation]:
        found, inspected = scan_source(body)
        assert inspected >= 1, f"walker inspected NO sink argument in:\n{body}"
        return found

    # -- the ORIGINAL rule: every shape it caught before must still be caught -----
    #    (a rewrite that closes a hole while quietly opening an old one is not a fix)

    def test_fstring_of_the_symbol_is_a_violation(self) -> None:
        assert self._violations('logger.info(f"Database initialized: {DATABASE_URL}")\n')

    def test_concatenation_of_the_symbol_is_a_violation(self) -> None:
        assert self._violations('logger.info("Database initialized: " + DATABASE_URL)\n')

    def test_percent_format_of_the_symbol_is_a_violation(self) -> None:
        assert self._violations('logger.info("db %s" % DATABASE_URL)\n')

    def test_lazy_logging_argument_is_a_violation(self) -> None:
        assert self._violations('logger.info("db %s", DATABASE_URL)\n')

    def test_an_added_debug_call_is_a_violation(self) -> None:
        assert self._violations('logger.debug("Full DSN: %s", DATABASE_URL)\n')

    def test_the_ssl_disable_branch_is_a_violation(self) -> None:
        assert self._violations('logger.info("PostgreSQL SSL disabled for %s", database_url)\n')

    def test_the_split_at_redaction_is_a_violation(self) -> None:
        """``result.split("@")[0]`` printed the password and hid the hostname."""
        assert self._violations(
            "def _append_ssl_params(self, database_url):\n"
            '    logger.info("url: %s", result.split("@")[0] + "@...")\n'
        )

    def test_a_ternary_escape_is_a_violation(self) -> None:
        assert self._violations(
            'logger.info("db %s", redact_dsn(DATABASE_URL) if prod else DATABASE_URL)\n'
        )

    def test_print_is_a_violation(self) -> None:
        assert self._violations('print("db", DATABASE_URL)\n')

    def test_the_test_dsn_symbol_is_a_violation(self) -> None:
        assert self._violations('logger.info("db %s", TEST_DATABASE_URL)\n')

    def test_the_lowercase_dsn_symbol_is_a_violation(self) -> None:
        assert self._violations('logger.info("db %s", dsn)\n')

    # -- THE HOLE THIS PR CLOSES: the value, reached without the symbol ----------

    def test_os_getenv_of_the_dsn_is_a_violation(self) -> None:
        """The headline evasion — measured GREEN under the previous rule.

        With this exact line committed into ``main.py``'s lifespan, the old guard
        reported ``4 passed`` and the full suite reported ``2 failed / 1386 passed``,
        while the pod logged a raw production password on every start.
        """
        assert self._violations('logger.info("Database URL: %s", os.getenv("DATABASE_URL"))\n')

    def test_os_getenv_with_a_default_is_a_violation(self) -> None:
        assert self._violations('logger.info("db %s", os.getenv("DATABASE_URL", ""))\n')

    def test_os_environ_subscript_is_a_violation(self) -> None:
        assert self._violations('logger.info("db %s", os.environ["DATABASE_URL"])\n')

    def test_os_environ_get_is_a_violation(self) -> None:
        assert self._violations('logger.info("db %s", os.environ.get("DATABASE_URL"))\n')

    def test_bare_imported_getenv_is_a_violation(self) -> None:
        """``from os import getenv`` produces a BARE call with no ``os.`` to grep for."""
        assert self._violations(
            'from os import getenv\nlogger.info("db %s", getenv("DATABASE_URL"))\n'
        )

    def test_bare_imported_environ_is_a_violation(self) -> None:
        assert self._violations(
            'from os import environ\nlogger.info("db %s", environ["DATABASE_URL"])\n'
        )

    def test_aliased_os_module_is_a_violation(self) -> None:
        assert self._violations(
            'import os as _os\nlogger.info("db %s", _os.getenv("DATABASE_URL"))\n'
        )

    def test_a_config_mapping_read_is_a_violation(self) -> None:
        """The key is the signal, not the accessor — ``cfg["DATABASE_URL"]`` is a DSN."""
        assert self._violations('logger.info("db %s", cfg["DATABASE_URL"])\n')

    def test_the_test_dsn_env_key_is_a_violation(self) -> None:
        assert self._violations('logger.info("db %s", os.getenv("TEST_DATABASE_URL"))\n')

    def test_an_fstring_around_the_env_read_is_a_violation(self) -> None:
        assert self._violations("logger.info(f\"db {os.getenv('DATABASE_URL')}\")\n")

    def test_an_attribute_read_of_the_settings_symbol_is_a_violation(self) -> None:
        """An ``ast.Name`` walk sees ``settings``, never ``DATABASE_URL``."""
        assert self._violations('logger.info("db %s", settings.DATABASE_URL)\n')

    def test_a_nested_attribute_read_is_a_violation(self) -> None:
        assert self._violations('logger.info("db %s", app.state.database_url)\n')

    # -- aliasing: the same value under a different name -------------------------

    def test_an_alias_of_the_env_read_is_a_violation(self) -> None:
        """``raw = os.getenv("DATABASE_URL")`` then ``logger.info(raw)``."""
        assert self._violations('raw = os.getenv("DATABASE_URL")\nlogger.info("db %s", raw)\n')

    def test_an_alias_of_the_symbol_is_a_violation(self) -> None:
        assert self._violations('shown = DATABASE_URL\nlogger.info("db %s", shown)\n')

    def test_a_chained_alias_is_a_violation(self) -> None:
        assert self._violations('a = os.environ["DATABASE_URL"]\nb = a\nlogger.info("db %s", b)\n')

    def test_an_alias_built_by_string_formatting_is_a_violation(self) -> None:
        assert self._violations(
            'pretty = "dsn={}".format(os.getenv("DATABASE_URL"))\nlogger.info(pretty)\n'
        )

    def test_an_alias_inside_a_container_literal_is_a_violation(self) -> None:
        assert self._violations(
            'ctx = {"dsn": os.getenv("DATABASE_URL")}\nlogger.info("ctx %s", ctx)\n'
        )

    def test_an_alias_from_an_or_default_is_a_violation(self) -> None:
        assert self._violations(
            'u = os.getenv("DATABASE_URL") or "postgresql://x"\nlogger.info("db %s", u)\n'
        )

    def test_an_augmented_assignment_never_clears_taint(self) -> None:
        assert self._violations('msg = os.getenv("DATABASE_URL")\nmsg += "!"\nlogger.info(msg)\n')

    def test_a_module_level_alias_leaked_inside_a_function_is_a_violation(self) -> None:
        """One leak written across two scopes — the module-level seed exists for this."""
        assert self._violations(
            '_BOOT_DSN = os.getenv("DATABASE_URL")\n'
            'def lifespan():\n    logger.info("db %s", _BOOT_DSN)\n'
        )

    def test_a_leak_through_logging_extra_is_a_violation(self) -> None:
        """``extra={...}`` reaches every structured-log sink just as the message does."""
        assert self._violations(
            'logger.info("started", extra={"dsn": os.getenv("DATABASE_URL")})\n'
        )

    # -- response-body sinks -----------------------------------------------------

    def test_the_dsn_in_an_http_exception_detail_is_a_violation(self) -> None:
        assert self._violations(
            'raise HTTPException(status_code=500, detail=os.getenv("DATABASE_URL"))\n'
        )

    def test_the_dsn_in_a_json_response_body_is_a_violation(self) -> None:
        assert self._violations(
            'return JSONResponse({"dsn": os.environ["DATABASE_URL"]}, status_code=500)\n'
        )

    def test_the_dsn_through_the_internal_error_helper_is_a_violation(self) -> None:
        assert self._violations('raise _internal_error(500, f"db {DATABASE_URL}", exc, "ctx")\n')

    # -- must STAY QUIET: the negative controls ----------------------------------

    def test_an_unrelated_getenv_is_not_a_violation(self) -> None:
        """THE NEGATIVE CONTROL. The rule must ban a VALUE, not a function name.

        ``main.py`` calls ``os.getenv`` for ``LOG_LEVEL``, ``SERVICE_NAME``, ``VERSION``,
        ``ENVIRONMENT``, ``REALM_NAME``, ``METERING_SERVICE_URL`` and
        ``CORS_ALLOWED_ORIGINS``; ``models.py`` for ``POSTGRES_SSLMODE``, ``PGSSLCERT``,
        ``PGSSLKEY``, ``PGSSLROOTCERT`` and two timeouts. If any of those, logged, were
        a violation, this rule would be a ban on ``os.getenv`` — it would cry wolf on
        the first legitimate line and be deleted, and the DSN would go back to being
        printed. Every one of these MUST be silent.
        """
        for src in (
            'logger.info("log level is %s", os.getenv("LOG_LEVEL", "INFO"))\n',
            'logger.info("env %s", os.getenv("ENVIRONMENT", "production"))\n',
            'logger.info("service %s v%s", os.getenv("SERVICE_NAME"), os.getenv("VERSION"))\n',
            'logger.info("sslmode %s", os.environ["POSTGRES_SSLMODE"])\n',
            'logger.info("ca %s", os.environ.get("PGSSLROOTCERT"))\n',
            'logger.info("origins %s", os.getenv("CORS_ALLOWED_ORIGINS", ""))\n',
        ):
            found, inspected = scan_source(src)
            assert inspected >= 1, f"walker inspected NO sink argument in:\n{src}"
            assert not found, f"the rule FALSELY flags a legitimate env read: {src} -> {found}"

    def test_an_alias_of_an_unrelated_getenv_is_not_a_violation(self) -> None:
        """The negative control, one indirection deep — taint must not over-spread."""
        assert not self._violations(
            'level = os.getenv("LOG_LEVEL", "INFO")\nlogger.info("level %s", level)\n'
        )

    def test_the_env_var_name_as_a_literal_is_not_a_violation(self) -> None:
        """Logging the NAME of the variable is how you tell an operator it is unset.

        ``_validate_production_config`` names ``DATABASE_URL`` in its failure message.
        A rule that cannot tell ``"DATABASE_URL"`` from ``os.getenv("DATABASE_URL")``
        forbids the diagnostic and permits nothing extra.
        """
        assert not self._violations(
            'logger.error("required env var %s must be set", "DATABASE_URL")\n'
        )

    def test_a_sanitised_env_read_is_not_a_violation(self) -> None:
        assert not self._violations('logger.info("db %s", redact_dsn(os.getenv("DATABASE_URL")))\n')

    def test_a_sanitised_symbol_is_not_a_violation(self) -> None:
        assert not self._violations(
            'logger.info("Database initialized: %s", redact_dsn(DATABASE_URL))\n'
        )

    def test_a_log_line_with_no_dsn_is_not_a_violation(self) -> None:
        assert not self._violations('logger.info("ready")\nlogger.info("tenant %s", tenant_id)\n')

    def test_the_env_int_warning_shape_is_not_a_violation(self) -> None:
        """The real ``main._env_int`` shape: a dynamic key, logged on a parse failure.

        ``raw = os.getenv(name)`` has no literal key, so it is not a DSN read. This is
        also gap 1 in the docstring — the price of not banning the accessor.
        """
        assert not self._violations(
            "def _env_int(name, default):\n"
            "    raw = os.getenv(name)\n"
            '    logger.warning("%s=%r is not an integer; using %d", name, raw, default)\n'
        )

    def test_result_outside_a_dsn_function_is_not_a_violation(self) -> None:
        """The false positive that forced ``result``/``url`` to be scoped."""
        assert not self._violations(
            "def set_byo_mode(tenant_id, mode):\n"
            '    result = {"tenant_id": tenant_id}\n'
            '    logger.info("byo result %s", result)\n'
        )

    def test_a_parameter_shadowing_a_tainted_global_is_not_a_violation(self) -> None:
        """A parameter is a different binding; module taint must not leak onto it."""
        assert not self._violations(
            '_BOOT = os.getenv("DATABASE_URL")\n'
            'def render(_BOOT):\n    logger.info("value %s", _BOOT)\n'
        )

    def test_a_rebinding_clears_taint(self) -> None:
        assert not self._violations(
            'v = os.getenv("DATABASE_URL")\nv = "static"\nlogger.info("v %s", v)\n'
        )

    def test_a_manager_built_from_the_dsn_is_not_a_violation(self) -> None:
        """``db_manager = DatabaseManager(DATABASE_URL)`` evaluates to a MANAGER.

        Tainting it would make every log line naming ``db_manager`` a false positive,
        which is why ``_is_dsn_valued`` refuses to propagate through an arbitrary call.
        """
        assert not self._violations(
            "db_manager = DatabaseManager(DATABASE_URL)\n"
            'logger.info("pool %s", db_manager.pool_size)\n'
        )


class TestTheEngineUrlIsNotAWayOut:
    """``engine.url`` reaches the same credential by a route no DSN NAME appears on.

    ``models.py`` holds the engine, so this is not a hypothetical spelling. Every claim
    below was measured against the INSTALLED SQLAlchemy (2.0.51) rather than assumed,
    because the intuitive guess is wrong in the dangerous direction:

        str(url)                                 -> …//licenses_user:***@host/licenses
        url.render_as_string()                   -> …//licenses_user:***@host/licenses
        url.render_as_string(hide_password=False) -> …//licenses_user:<PLAINTEXT>@host/…
        url.password                             -> <PLAINTEXT>

    A rule written from the guess would flag ``str(engine.url)`` — which is masked and
    harmless — and let the two that actually return the credential through.
    """

    @staticmethod
    def _violations(body: str) -> list[Violation]:
        found, inspected = scan_source(body)
        assert inspected >= 1, f"walker inspected NO sink argument in:\n{body}"
        return found

    def test_the_password_attribute_is_a_violation(self) -> None:
        assert self._violations('logger.info("db %s", engine.url.password)\n')

    def test_it_is_found_through_a_longer_receiver_chain(self) -> None:
        # `self.engine.url.password` inside a method whose signature never mentions a
        # DSN — the shape a scoped, parameter-driven rule cannot see.
        assert self._violations('logger.info("db %s", self.engine.url.password)\n')

    def test_an_unhidden_render_is_a_violation(self) -> None:
        assert self._violations(
            'logger.info("db %s", engine.url.render_as_string(hide_password=False))\n'
        )

    # -- the other half: these are SAFE and must stay unflagged -------------------
    #    A guard that fires on the masked spellings is a guard someone deletes.

    def test_str_of_the_url_is_not_flagged(self) -> None:
        """``URL.__str__`` renders with ``hide_password=True``. Measured, not assumed."""
        assert not self._violations('logger.info("db %s", str(engine.url))\n')

    def test_a_default_render_is_not_flagged(self) -> None:
        assert not self._violations('logger.info("db %s", engine.url.render_as_string())\n')

    def test_a_hidden_render_is_not_flagged(self) -> None:
        assert not self._violations(
            'logger.info("db %s", engine.url.render_as_string(hide_password=True))\n'
        )

    def test_a_bare_password_attribute_is_not_flagged(self) -> None:
        """``user.password`` / ``settings.password`` are everywhere in an auth service.

        The rule keys on ``.url.password``, not on ``.password``. Without this
        discrimination the rule fires across the auth path and gets deleted, taking the
        three real detections above with it.
        """
        assert not self._violations('logger.info("user %s", user.password)\n')
        assert not self._violations('logger.info("cfg %s", settings.password)\n')

    def test_redaction_still_clears_it(self) -> None:
        assert not self._violations('logger.info("db %s", redact_dsn(engine.url.password))\n')


class TestKnownUncoveredShapes:
    """The gate's holes, asserted rather than assumed.

    Each test pins a shape this rule is KNOWN not to catch, so the limit is measured and
    reviewable. If someone later strengthens the walker, these fail and get promoted
    into ``TestDetectorDiscriminatingPower``. Treating an unstated hole as coverage is
    the failure mode this class prevents.
    """

    @staticmethod
    def _violations(body: str) -> list[Violation]:
        found, inspected = scan_source(body)
        assert inspected >= 1, f"walker inspected NO sink argument in:\n{body}"
        return found

    def test_a_dynamic_env_key_is_not_caught(self) -> None:
        """Gap 1: no literal key, nothing to match."""
        assert not self._violations(
            'key = "DATABASE" + "_URL"\nlogger.info("db %s", os.getenv(key))\n'
        )

    def test_scope_to_scope_flow_is_not_caught(self) -> None:
        """Gap 2: taint crosses module -> nested only, never nested -> nested."""
        assert not self._violations(
            'def a():\n    _stash(os.getenv("DATABASE_URL"))\n'
            'def b():\n    logger.info("db %s", LAST)\n'
        )

    def test_mutating_method_taint_is_not_caught(self) -> None:
        """Gap 3: laundered through ``list.append`` rather than assignment."""
        assert not self._violations(
            'parts = []\nparts.append(DATABASE_URL)\nlogger.info("db %s", " ".join(parts))\n'
        )

    def test_an_unregistered_sink_is_not_caught(self) -> None:
        """Gap 4: an emitter whose name is in neither registry.

        Opts out of the ``inspected >= 1`` floor on purpose — an unregistered sink is
        invisible by definition, so zero inspections IS the finding here.
        """
        found, inspected = scan_source('_emit("db %s", os.getenv("DATABASE_URL"))\n')
        assert not found
        assert inspected == 0, "_emit is now a registered sink — promote this test"
        assert "_emit" not in BODY_SINKS

    def test_a_non_dsn_credential_env_var_is_not_caught(self) -> None:
        """Gap 5: a published credential, but not this rule's subject."""
        assert not self._violations('logger.info("pw %s", os.getenv("POSTGRES_PASSWORD"))\n')

    def test_a_dsn_reassembled_from_parts_is_not_caught(self) -> None:
        """Gap 7: five non-DSN names that add up to a DSN."""
        assert not self._violations('logger.info(f"{scheme}://{user}:{pw}@{host}/{db}")\n')

    def test_every_dsn_holding_script_is_actually_scanned(self) -> None:
        """Gap 6, closed rather than measured.

        This test replaces a ceiling that counted the DSN-holding scripts NOT scanned by
        this rule and asserted the count stayed between 15 and 25. That is an honest way
        to record a hole, but it is still a hole: it went green while 19 modules holding
        the production DSN were never read, and it would have gone green if one of them
        started printing that DSN tomorrow.

        The corpus is now derived, so the assertion is the direct one — every script the
        tree ships that names a DSN is in the scanned set, compared as SETS so a count
        that happens to match cannot pass for the right files.
        """
        on_disk = {
            f"scripts/{path.name}"
            for path in (REPO_ROOT / "scripts").glob("*.py")
            if any(marker in path.read_text(encoding="utf-8") for marker in _DSN_MARKERS)
        }
        assert on_disk, "positive control: no DSN-holding script found at all"
        scanned = {rel for rel in _GUARDED_FILES if rel.startswith("scripts/")}
        assert on_disk <= scanned, f"DSN-holding scripts not scanned: {sorted(on_disk - scanned)}"

    def test_the_corpus_is_derived_from_the_tree_not_a_literal(self) -> None:
        """The derivation itself, and that it discriminates.

        A derived corpus can regress into a constant without anyone noticing, so pin
        both halves: the real tree yields the files we expect, AND a tree missing one of
        them yields a smaller set. The second half is what a frozen literal fails.
        """
        assert "main.py" in _GUARDED_FILES and "models.py" in _GUARDED_FILES
        assert len(_GUARDED_FILES) >= 20, (
            f"corpus collapsed to {len(_GUARDED_FILES)} files: {_GUARDED_FILES}"
        )
        # Discriminator: the SAME predicate over a corpus with one file removed must
        # name that file. If `_GUARDED_FILES` were a literal, this would pass while the
        # derivation was dead.
        for victim in ("main.py", "models.py"):
            survivors = [f for f in _GUARDED_FILES if f != victim]
            assert _dsn_holding_modules_missing_from(survivors) == [victim]
