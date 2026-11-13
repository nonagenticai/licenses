"""Source-level invariant: a caught exception may never reach the caller.

THE DEFECT THIS EXISTS FOR
--------------------------
``main.py`` used to interpolate caught exceptions into ``HTTPException(detail=...)``
at 36 sites. A SQLAlchemy/psycopg exception stringifies to the failing SQL statement
*plus its bound parameters*; a connection failure stringifies to the DSN (host, port,
user, database). Nothing downstream sanitised any of it — ``http_exception_handler``
scrubs only 5xx, and a route-raised 4xx passes through verbatim. The worst site was
``/health/ready``, which is in ``PUBLIC_ENDPOINTS`` (``auth/middleware.py``) — any
pod in the cluster could read the database topology unauthenticated.

(This paragraph used to justify that with "``global_exception_handler`` re-raises
``HTTPException`` unchanged". That claim was FALSE — a route-raised ``HTTPException``
never reaches ``global_exception_handler`` at all, because ``ExceptionMiddleware``
catches it first, and the re-raise it referred to could not have produced a response
anyway. The rule below is unaffected; only the stated reason was wrong. See
``main.global_exception_handler`` and
``tests/auth/test_middleware.py::TestMiddlewareRaisedHTTPExceptionBecomesAResponse``.)

PR #94 fixed exactly one endpoint by hand and built no helper, which is why the other
36 survived. Hand-fixing 36 sites does not stop the 37th, so the fix is this rule.

THE RULE
--------
Once an ``except ... as <name>:`` handler has bound ``<name>``, no error-detail
expression in the REST OF THE ENCLOSING FUNCTION may reference it — directly, or
through a local assigned from it.

Scoping the taint to the enclosing function rather than the handler body is not
pedantry. A second adversarial review proved this shape defeats BOTH layers of this
PR, end to end, on the wire::

    except Exception as e:
        msg = str(e)
    raise HTTPException(500, detail=f"Failed: {msg}")

Handler-scoped tracking is silent on it, and the runtime scrubber cannot save it
either, because once the handler exits ``__cause__`` and ``__context__`` are both
``None`` — there is no cause left to compare the detail against. ``finally:`` and
``else:`` are the same hole. Events are replayed in SOURCE ORDER so that a genuine
rebinding (``e = compute()`` after the handler) still clears the taint.

"Error-detail expression" means any argument that ends up in a response body. Keying
only on ``HTTPException(detail=...)`` is escapable — an adversarial review of this rule
found ``auth/middleware.py`` returning ``self._error_response(exc.status_code,
str(exc.detail))``, which reaches the body through a *helper*, with no ``HTTPException(``
call and no ``detail=`` keyword anywhere. So the sinks are a registry (``BODY_SINKS``):

  * ``HTTPException(..., detail=<expr>)`` — or its second POSITIONAL argument. No site
    in the repo uses the positional form today; it is covered so that the first one to
    appear does not walk straight through.
  * ``_internal_error(status, <expr>, exc, ctx)`` — the sanitising helper's *message*.
    Without this the helper would be a one-line bypass for the rule it exists to satisfy.
  * ``_client_error(status, <expr>, exc, ctx)`` — its 4xx sibling, same reasoning.
  * ``_error_response(status, <expr>)`` — the middleware's response builder.
  * ``*Response(<expr>, ...)`` — including bare ``Response``, and including the
    POSITIONAL form. ``content`` is starlette's FIRST parameter; reading position 1
    grabbed ``status_code`` instead, which missed every positional leak and quietly
    inflated the anti-vacuity counter with an integer.

Two escapes from the rule, both deliberate and both keyed on the CAUGHT TYPE — never on
a line allowlist, which silently re-permits whatever later moves onto that line:

  * ``except ClientSafeError as e:`` MAY interpolate ``e``. ``ClientSafeError``
    (``models.py``) marks errors the DAL authored itself out of caller-supplied
    input — an invalid ``byo_mode``, a tenant-name conflict. Those messages are
    written in our own source, contain only what the caller already sent, and are
    the caller's only actionable signal. The unsafe thing was never the message, it
    was the *catch*: a bare ``except ValueError`` also catches whatever psycopg,
    SQLAlchemy or pydantic raises.
  * ``except HTTPException as exc:`` MAY interpolate ``exc``. Every ``HTTPException``
    in this service is constructed by this service, and its ``detail`` has already been
    checked by *this very rule* — so the rule is closed under re-raising one. That is
    what makes ``auth/middleware.py``'s ``str(exc.detail)`` provably safe rather than
    merely untested: it can only ever carry a detail this rule already cleared.

The exception variable may also be passed to ``_internal_error`` as its ``exc``
argument — that is the argument whose whole purpose is to be logged.

WHAT THIS RULE DOES **NOT** CATCH (stated, not implied)
-------------------------------------------------------
The walker is function-scoped and source-ordered, tracks whole-name assignment, and
recognises sinks BY NAME. It does not catch:

  1. **A response builder not in ``BODY_SINKS``.** A new helper — ``_fail(msg)`` — that
     builds a response is invisible until its name is added here. This is the same
     escape a verifier found at ``auth/middleware.py``; naming the sinks closes the
     instances that exist, not the shape. Full interprocedural "does this call
     transitively build a Response" analysis is the only real fix and is out of
     proportion to a unit-test gate.
  2. **Cross-function flow.** ``except Exception as e: _stash(e)`` in one function,
     and a ``detail=`` reading that stashed state in another. Taint stops at function,
     lambda and class boundaries.
  3. **Mutating-method taint.** ``parts = []; parts.append(str(e)); detail=" ".join(parts)``.
     Assignment (including ``d[k] = ...`` / ``o.a = ...``, whose base name IS tainted)
     is tracked; in-place mutation through a method call is not.
  4. **Runtime-dynamic details.** ``detail=MESSAGES[key]`` where the table was
     populated with exception text at runtime.
  5. **Loop-carried ordering.** Replay is by source position, so a sink written ABOVE a
     handler but executed after it on a later iteration reads as untainted. Straight-line
     and ``finally``/``else`` flow — the shapes that actually occur — are ordered
     correctly.

``SCANNED_FILES`` is no longer in this list: ``test_scanned_files_covers_every_body_building_module``
derives the required set from the corpus across ALL four sink families and fails if a
module that builds a response body is missing from it.

Gaps 1-5 are part of why ``main.http_exception_handler`` exists: for ``status >= 500``
it drops any ``detail`` — string, or string nested in a dict/list — that literally
contains its own ``__cause__``/``__context__`` text, whatever route produced it. Note
the two layers do NOT overlap completely: the runtime layer is blind to anything raised
after its handler exited (no cause survives), which is exactly why gap-free static
scoping had to be the function, not the handler. 4xx is left alone on purpose —
``tests/auth/test_rbac.py`` asserts role names in 403 bodies and the 409 from
``POST /tenants/{tenant_id}`` is a contract. Every one of these gaps has a test in
``TestKnownUncoveredShapes`` asserting the gate is SILENT on it, so the hole is
measured, not assumed.

Bare ``except:`` / ``except Exception:`` handlers bind no name, but can still reach the
active exception through ``traceback.format_exc()`` / ``sys.exc_info()`` /
``sys.exception()`` — in dotted form, via an aliased module (``import traceback as tb``),
or as a bare name after ``from traceback import format_exc``. All three are flagged
wherever they appear in a body sink.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Every module that constructs an HTTP error response for a caller.
SCANNED_FILES: list[str] = [
    "main.py",
    "models.py",
    "service_auth.py",
    "auth/dependencies.py",
    "auth/jwt_verify.py",
    "auth/middleware.py",
    "auth/models.py",
    "auth/tenant.py",
    "auth/config.py",
]

#: Callables whose argument reaches the caller's response body, mapped to the
#: (keyword name, positional index) of that argument. Keyed by name — see the
#: module docstring on why this registry is the rule's known limit.
BODY_SINKS: dict[str, tuple[str, int]] = {
    "HTTPException": ("detail", 1),
    "_internal_error": ("message", 1),
    "_client_error": ("message", 1),
    "_error_response": ("message", 1),
}

#: Any call whose callee name ends with this also has its ``content`` inspected, which
#: covers ``JSONResponse``/``ORJSONResponse``/``PlainTextResponse``/bare ``Response``
#: without enumerating starlette's class list. Bare ``Response`` is INCLUDED — this
#: module imports it, and ``Response(content=str(e), status_code=500)`` is a real sink.
RESPONSE_CLASS_SUFFIX = "Response"
#: ``content`` is starlette's FIRST positional parameter (``Response(content, status_code,
#: ...)``). Position 1 is ``status_code`` — reading it would miss every positional leak
#: and inflate the anti-vacuity counter with an integer.
RESPONSE_CONTENT_ARG = ("content", 0)

#: Directories that ship no response bodies (or are not shipped at all).
_DISCOVERY_SKIP_DIRS = frozenset(
    {"tests", "scripts", "migrations", ".venv", ".worktrees", "certs", "docs"}
)


def _body_building_modules_missing_from(scanned: list[str]) -> list[str]:
    """Shipped modules that name a body sink but are absent from ``scanned``.

    Extracted so the staleness guard and its mutation proof call the SAME predicate.
    They used to be two copy-pasted loops asserting the same proposition, which is why
    the "proof" proved nothing: it could not drop a module from a list it re-derived.
    """
    markers = (*BODY_SINKS, RESPONSE_CLASS_SUFFIX)
    scanned_set = set(scanned)
    missing: list[str] = []
    for path in sorted(REPO_ROOT.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT)
        if set(rel.parts) & _DISCOVERY_SKIP_DIRS or rel.parts[0].startswith("."):
            continue
        if not any(marker in path.read_text(encoding="utf-8") for marker in markers):
            continue
        if str(rel) not in scanned_set:
            missing.append(str(rel))
    return missing


#: Caught types whose message may be echoed to the caller (see module docstring).
#: ``HTTPException`` is safe because its detail has already passed THIS rule.
SAFE_CAUGHT_TYPES = frozenset({"ClientSafeError", "HTTPException"})
CLIENT_SAFE_EXCEPTION = "ClientSafeError"

#: Zero-argument escapes to the *active* exception — reachable even from a handler
#: that binds no name.
AMBIENT_EXCEPTION_ACCESSORS: set[tuple[str, str]] = {
    ("traceback", "format_exc"),
    ("sys", "exc_info"),
    ("sys", "exception"),
}


@dataclass(frozen=True)
class Violation:
    file: str
    lineno: int
    reason: str
    source: str

    def __str__(self) -> str:  # pragma: no cover - only rendered on failure
        return f"{self.file}:{self.lineno}: {self.reason} -> {self.source}"


def _root_name(node: ast.expr) -> str | None:
    """``d["k"]`` -> ``d``; ``o.a.b`` -> ``o``; ``x`` -> ``x``; else None."""
    while isinstance(node, ast.Attribute | ast.Subscript):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _names_used(expr: ast.expr) -> set[str]:
    """Every bare name read anywhere inside ``expr``.

    ``ast.walk`` descends into f-strings (``JoinedStr``/``FormattedValue``), nested
    f-strings, call arguments, attribute bases and subscripts alike, so
    ``f"{e}"``, ``str(e)``, ``repr(e)``, ``f"{e.args}"``, ``f"{e.__cause__!r}"`` and
    ``f"{'|'.join(str(a) for a in e.args)}"`` are all one code path.
    """
    return {n.id for n in ast.walk(expr) if isinstance(n, ast.Name)}


def _caught_type_name(handler: ast.ExceptHandler) -> str | None:
    """The single type this handler catches, or None for a bare/tuple handler."""
    t = handler.type
    if isinstance(t, ast.Name):
        return t.id
    if isinstance(t, ast.Attribute):
        return t.attr
    return None


def _is_client_safe(handler: ast.ExceptHandler) -> bool:
    """True only when the handler catches exactly one type from ``SAFE_CAUGHT_TYPES``.

    A tuple such as ``except (ClientSafeError, OSError) as e`` is NOT safe: the
    ``OSError`` arm would be echoed too.
    """
    return _caught_type_name(handler) in SAFE_CAUGHT_TYPES


def _uses_ambient_accessor(
    expr: ast.expr, ambient_names: set[str], module_aliases: dict[str, str]
) -> bool:
    """True if ``expr`` reaches the *active* exception without naming a bound variable.

    Covers the dotted form (``traceback.format_exc()``, including ``import traceback
    as tb``) and the bare form produced by ``from traceback import format_exc``.
    Flagged wherever it appears in a body sink, not only inside a handler: there is no
    legitimate reason to put a traceback in a response body, and outside a handler the
    call just yields ``"NoneType: None"``.
    """
    for node in ast.walk(expr):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            base = func.value
            if isinstance(base, ast.Name):
                module = module_aliases.get(base.id, base.id)
                if (module, func.attr) in AMBIENT_EXCEPTION_ACCESSORS:
                    return True
        elif isinstance(func, ast.Name) and func.id in ambient_names:
            return True
    return False


#: Node types that open a new binding scope. Taint does not cross these.
_SCOPE_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _iter_scopes(tree: ast.AST):
    """The module, plus every nested function/lambda/class, as separate scopes."""
    yield tree
    for node in ast.walk(tree):
        if isinstance(node, _SCOPE_TYPES):
            yield node


def _iter_own_nodes(node: ast.AST):
    """Every node belonging to this scope, NOT descending into nested scopes."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _SCOPE_TYPES):
            continue
        yield child
        yield from _iter_own_nodes(child)


def _position(node: ast.AST) -> tuple[int, int]:
    lineno = getattr(node, "lineno", 0)
    col = getattr(node, "col_offset", 0)
    return (lineno, col)


def _assignment_parts(node: ast.AST) -> tuple[ast.expr | None, Sequence[ast.expr], bool] | None:
    """``(value, targets, is_augmented)`` for any binding form, else None."""
    if isinstance(node, ast.Assign):
        return node.value, list(node.targets), False
    if isinstance(node, ast.AnnAssign):
        return node.value, [node.target], False
    if isinstance(node, ast.AugAssign):
        # `x += str(e)` never CLEARS x, it only ever adds to it.
        return node.value, [node.target], True
    if isinstance(node, ast.NamedExpr):
        return node.value, [node.target], False
    if isinstance(node, ast.withitem) and node.optional_vars is not None:
        return node.context_expr, [node.optional_vars], False
    return None


def _bound_root_names(targets: Sequence[ast.expr]) -> set[str]:
    names: set[str] = set()
    for target in targets:
        for sub in ast.walk(target):
            if isinstance(sub, ast.Name | ast.Attribute | ast.Subscript):
                root = _root_name(sub)
                if root:
                    names.add(root)
    return names


def _ambient_alias_names(tree: ast.AST) -> set[str]:
    """Local names bound to ``traceback.format_exc`` / ``sys.exc_info`` / ``sys.exception``.

    ``from traceback import format_exc`` (or ``as tb_fmt``) produces a BARE call that
    the dotted-attribute check cannot see. Resolving the import makes the bare form
    detectable without flagging every unrelated function that happens to be called
    ``exception``.
    """
    aliases: set[str] = set()
    wanted = {attr for _module, attr in AMBIENT_EXCEPTION_ACCESSORS}
    modules = {module for module, _attr in AMBIENT_EXCEPTION_ACCESSORS}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in modules:
            for alias in node.names:
                if alias.name in wanted:
                    aliases.add(alias.asname or alias.name)
    return aliases


def _module_aliases(tree: ast.AST) -> dict[str, str]:
    """``import traceback as tb`` -> ``{"tb": "traceback"}`` (plus identity imports)."""
    found: dict[str, str] = {}
    modules = {module for module, _attr in AMBIENT_EXCEPTION_ACCESSORS}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in modules:
                    found[alias.asname or alias.name] = alias.name
    return found


def _sink_detail(node: ast.Call) -> ast.expr | None:
    """The caller-visible argument of ``node``, if ``node`` is a body sink."""
    func = node.func
    if isinstance(func, ast.Name):
        name: str | None = func.id
    elif isinstance(func, ast.Attribute):
        name = func.attr
    else:
        name = None
    if name is None:
        return None

    spec = BODY_SINKS.get(name)
    if spec is None and name.endswith(RESPONSE_CLASS_SUFFIX):
        # Starlette/FastAPI Response classes take `content` FIRST. Reading position 1
        # here used to grab `status_code` instead — missing every positional leak AND
        # inflating the anti-vacuity counter by "inspecting" an integer.
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


class _ScopeScanner:
    """Function-scoped, source-ordered taint.

    Taint deliberately OUTLIVES the handler body. This is silent under handler-scoped
    tracking and defeats the runtime scrubber too, because once the handler has exited
    both ``__cause__`` and ``__context__`` are ``None``::

        except Exception as e:
            msg = str(e)
        raise HTTPException(500, detail=f"Failed: {msg}")

    ``finally:`` and ``else:`` are the same shape. So a handler binding taints its name
    for the REST of the enclosing function, and events are replayed in source order so
    that a genuine rebinding still clears it.
    """

    def __init__(
        self, filename: str, source: str, ambient_names: set[str], module_aliases: dict[str, str]
    ) -> None:
        self.filename = filename
        self.source = source
        self.ambient_names = ambient_names
        self.module_aliases = module_aliases
        self.violations: list[Violation] = []
        self.inspected = 0

    def scan(self, scope: ast.AST) -> None:
        events: list[tuple[tuple[int, int], int, str, object]] = []
        for order, node in enumerate(_iter_own_nodes(scope)):
            if isinstance(node, ast.ExceptHandler) and node.name:
                events.append((_position(node), order, "handler", node))
            elif (parts := _assignment_parts(node)) is not None:
                events.append(
                    (
                        _position(node)
                        if not isinstance(node, ast.withitem)
                        else _position(node.context_expr),
                        order,
                        "assign",
                        parts,
                    )
                )
            elif isinstance(node, ast.Call) and (detail := _sink_detail(node)) is not None:
                events.append((_position(node), order, "check", detail))

        tainted: set[str] = set()
        for _pos, _order, kind, payload in sorted(events, key=lambda e: (e[0], e[1])):
            if kind == "handler":
                handler = cast(ast.ExceptHandler, payload)
                name = cast(str, handler.name)
                if _is_client_safe(handler):
                    # Rebinding to a client-safe type CLEARS any earlier taint on the
                    # name, so ordering an unsafe handler before a safe one that reuses
                    # the same variable does not produce a false positive.
                    tainted.discard(name)
                else:
                    tainted.add(name)
            elif kind == "assign":
                value, targets, is_augmented = cast(
                    tuple[ast.expr | None, Sequence[ast.expr], bool], payload
                )
                bound = _bound_root_names(targets)
                if value is not None and (_names_used(value) & tainted):
                    tainted |= bound
                elif not is_augmented:
                    tainted -= bound
            else:
                self.inspected += 1
                self._check(cast(ast.expr, payload), tainted)

    def _check(self, detail: ast.expr, tainted: set[str]) -> None:
        segment = ast.get_source_segment(self.source, detail) or ast.dump(detail)
        hits = sorted(_names_used(detail) & tainted)
        if hits:
            self.violations.append(
                Violation(
                    self.filename,
                    detail.lineno,
                    f"detail interpolates caught exception {hits!r}",
                    segment,
                )
            )
            return
        if _uses_ambient_accessor(detail, self.ambient_names, self.module_aliases):
            self.violations.append(
                Violation(
                    self.filename,
                    detail.lineno,
                    "detail reads the active exception via traceback/sys",
                    segment,
                )
            )


def scan_source(source: str, filename: str = "<synthetic>") -> tuple[list[Violation], int]:
    """Scan one module. Returns ``(violations, detail_expressions_inspected)``."""
    tree = ast.parse(source)
    scanner = _ScopeScanner(filename, source, _ambient_alias_names(tree), _module_aliases(tree))
    for scope in _iter_scopes(tree):
        scanner.scan(scope)
    return scanner.violations, scanner.inspected


def scan_repo() -> tuple[list[Violation], int, dict[str, int]]:
    violations: list[Violation] = []
    total = 0
    per_file: dict[str, int] = {}
    for rel in SCANNED_FILES:
        path = REPO_ROOT / rel
        assert path.is_file(), f"{rel} is missing — SCANNED_FILES is stale"
        found, inspected = scan_source(path.read_text(encoding="utf-8"), rel)
        violations.extend(found)
        per_file[rel] = inspected
        total += inspected
    return violations, total, per_file


class TestNoExceptionLeak:
    """The invariant itself, over the real source tree."""

    def test_no_detail_interpolates_a_caught_exception(self) -> None:
        violations, _, _ = scan_repo()
        assert not violations, "HTTPException detail leaks a caught exception:\n" + "\n".join(
            str(v) for v in violations
        )

    def test_health_ready_detail_is_static(self) -> None:
        """The worst site specifically: ``/health/ready`` is PUBLIC.

        ``auth/middleware.py`` lists it in ``PUBLIC_ENDPOINTS``, so its 503 body is
        readable by anything that can open a socket to the pod. Pinned by name so a
        regression here is not merely one entry in a list of violations.

        Matched as ``FunctionDef | AsyncFunctionDef``: whether the handler is a
        coroutine is an event-loop concern (it is a plain ``def`` since the
        blocking handlers were moved into the threadpool) and says nothing about
        whether its ``detail=`` can leak an exception. Narrowing to
        ``AsyncFunctionDef`` made the ``len(handlers) == 1`` precondition — not
        the leak assertion — the thing that failed. The precondition is kept, so
        renaming or deleting the handler still fails loudly.
        """
        source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        handlers = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == "health_ready"
        ]
        assert len(handlers) == 1, "health_ready endpoint not found in main.py"
        violations, inspected = scan_source(ast.unparse(handlers[0]), "main.py::health_ready")
        assert inspected >= 1, "walker inspected no detail expression inside health_ready"
        assert not violations, [str(v) for v in violations]

    def test_client_safe_error_is_the_only_permitted_catch(self) -> None:
        """The two sites that DO echo a message must catch ``ClientSafeError``.

        If someone widens either back to ``except ValueError``, the echoed message
        is once again whatever psycopg/pydantic raised, and this test fails.
        """
        source = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        echoing = [
            handler
            for handler in ast.walk(tree)
            if isinstance(handler, ast.ExceptHandler)
            and handler.name
            and any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id in BODY_SINKS
                and any(
                    handler.name in _names_used(kw.value)
                    for kw in call.keywords
                    if kw.arg == BODY_SINKS[call.func.id][0]
                )
                for call in ast.walk(handler)
            )
        ]
        # Exact pin, not a floor: exactly five sites in main.py are allowed to
        # echo — the 422 from POST /offerings, the 409 from PUT
        # /tenants/{tenant_id} (register_tenant), the 422 from PATCH
        # /tenants/{name_or_id}/cost-centre (set_tenant_cost_centre, migration
        # 034), and the 422 from GET /internal/tenants/{id}/seat-count for a
        # malformed period (models.count_tenant_seats raises only fixed
        # ClientSafeError messages), and the 404/409/422 from POST
        # /internal/tenants/{id}/bundles/{bundle}/ensure (services.bundle_ensure
        # EnsureError, authored from the caller's own tenant id / bundle key).
        # A floor of ">= 1" would stay green if one of
        # them silently lost its narrowing and stopped echoing, which is the drop
        # this test exists for.
        assert len(echoing) == 5, (
            f"expected exactly 5 echoing handlers in main.py, found {len(echoing)} "
            f"at lines {[h.lineno for h in echoing]}"
        )
        for handler in echoing:
            assert _caught_type_name(handler) == CLIENT_SAFE_EXCEPTION, (
                f"main.py:{handler.lineno} echoes its caught exception but catches "
                f"{_caught_type_name(handler)!r}, not {CLIENT_SAFE_EXCEPTION} — that "
                "catch also swallows psycopg/pydantic errors, whose text carries the "
                "failing statement and its bound parameters"
            )

    def test_scanned_files_covers_every_body_building_module(self) -> None:
        """Positive control against a stale ``SCANNED_FILES``.

        Derived from the corpus, never hand-listed: any shipped module that names a
        body sink must be scanned. A hand-maintained list silently under-covers the
        moment someone adds a module.

        The predicate must cover EVERY sink family, not just ``BODY_SINKS``. Discovering
        on ``BODY_SINKS`` alone left a module that leaks solely through
        ``JSONResponse(content={"detail": str(e)})`` — never naming ``HTTPException`` —
        invisible to the walker AND to this guard, so the docstring's "derived from the
        corpus" was true of only three of the four families.
        """
        missing = _body_building_modules_missing_from(SCANNED_FILES)
        assert not missing, f"modules build response bodies but are not scanned: {missing}"

    def test_the_staleness_guard_would_notice_a_dropped_module(self) -> None:
        """Mutation-proof for the guard above, since it passes vacuously on a clean tree.

        This test used to promise, in this docstring, to "drop a scanned module from the
        list in memory" — and then not do it. It re-derived ``discovered`` with a
        copy-pasted loop and asserted ``discovered <= set(SCANNED_FILES)``, which is
        logically the SAME proposition as the guard it claims to prove. A predicate that
        silently under-discovers (say ``markers`` losing ``RESPONSE_CLASS_SUFFIX``) passes
        both, because a shrunken ``discovered`` is still a subset. It proved nothing the
        guard did not already assert.

        Now it actually drops a module and requires the guard's own predicate to name it,
        by calling the SAME helper the guard calls — so the two cannot drift.
        """
        # Sanity: the predicate finds something at all before we mutate anything.
        assert _body_building_modules_missing_from(SCANNED_FILES) == []

        for victim in ("main.py", "auth/middleware.py"):
            assert victim in SCANNED_FILES, f"{victim} is the subject of this proof"
            survivors = [f for f in SCANNED_FILES if f != victim]
            named = _body_building_modules_missing_from(survivors)
            assert named == [victim], (
                f"dropping {victim} must make the guard name it; got {named}. "
                "`auth/middleware.py` is the important half — it reaches the body only "
                "through `_error_response`/`JSONResponse`, so it is invisible to a "
                "predicate that discovers on BODY_SINKS alone."
            )

    def test_client_safe_error_exists_and_subclasses_value_error(self) -> None:
        from models import ClientSafeError

        assert issubclass(ClientSafeError, ValueError)


class TestWalkerIsNotVacuous:
    """A walker that finds nothing must not read as coverage."""

    def test_inspected_enough_detail_expressions(self) -> None:
        """Anti-vacuity floor.

        ``main.py`` held 78 ``HTTPException`` calls when this rule was written; 34 of
        them became ``_internal_error``/``_client_error`` calls, which this walker also
        inspects. If a refactor drops the walker below this floor it has stopped looking
        at the surface it was written for, and a green result means nothing.

        Per-file floors, not just a total: a total alone lets one file's coverage
        collapse while another's growth hides it. ``auth/middleware.py`` is pinned
        separately because it is reachable ONLY through the ``_error_response`` and
        ``Response`` sink families — if those two regress, its count is what drops.
        """
        violations, total, per_file = scan_repo()
        assert total >= 100, f"walker inspected only {total} detail expressions: {per_file}"
        assert per_file["main.py"] >= 90, (
            f"main.py contributed only {per_file['main.py']} detail expressions — "
            "the walker is no longer seeing the file the rule exists for"
        )
        assert per_file["auth/middleware.py"] >= 5, (
            f"auth/middleware.py contributed only {per_file['auth/middleware.py']} — "
            "the _error_response / Response sink families have regressed"
        )
        assert not violations

    def test_walker_still_detects_a_planted_leak(self) -> None:
        """Positive control, in the same test class as the count.

        A counter proves the walker *looked*; it does not prove the walker can still
        *see*. A detector gutted to ``return []`` would satisfy the floor above and
        report a clean tree — the exact "green means nothing" failure this class
        exists to prevent. So plant a leak and require it to be found.
        """
        planted, inspected = scan_source(
            "try:\n    f()\nexcept Exception as e:\n"
            '    raise HTTPException(500, detail=f"boom {e}")\n'
        )
        assert inspected == 1
        assert len(planted) == 1, "the walker no longer detects an obvious leak"

    def test_every_scanned_file_parses(self) -> None:
        """A file that fails to parse must fail loudly, not be skipped silently."""
        for rel in SCANNED_FILES:
            ast.parse((REPO_ROOT / rel).read_text(encoding="utf-8"))


class TestDetectorDiscriminatingPower:
    """The gate must fire on real leaks and stay quiet on legitimate details.

    A gate cannot prove its own discriminating power against a corpus that is
    already clean, so every case here is synthetic source parsed on the spot.
    """

    @staticmethod
    def _violations(body: str) -> list[Violation]:
        found, inspected = scan_source(body)
        assert inspected >= 1, f"walker inspected NO detail expression in:\n{body}"
        return found

    # -- must FIRE ---------------------------------------------------------
    def test_fstring_with_str_of_exception_is_a_violation(self) -> None:
        assert self._violations(
            "try:\n    f()\nexcept Exception as e:\n"
            '    raise HTTPException(500, detail=f"x {str(e)}")\n'
        )

    def test_bare_str_of_exception_is_a_violation(self) -> None:
        assert self._violations(
            "try:\n    f()\nexcept Exception as e:\n    raise HTTPException(500, detail=str(e))\n"
        )

    def test_exception_args_is_a_violation(self) -> None:
        assert self._violations(
            "try:\n    f()\nexcept Exception as e:\n"
            '    raise HTTPException(500, detail=f"{e.args}")\n'
        )

    def test_repr_of_exception_is_a_violation(self) -> None:
        assert self._violations(
            "try:\n    f()\nexcept Exception as e:\n    raise HTTPException(500, detail=repr(e))\n"
        )

    def test_str_of_exception_args_is_a_violation(self) -> None:
        assert self._violations(
            "try:\n    f()\nexcept Exception as e:\n"
            "    raise HTTPException(500, detail=str(e.args))\n"
        )

    def test_cause_chain_is_a_violation(self) -> None:
        assert self._violations(
            "try:\n    f()\nexcept Exception as e:\n"
            '    raise HTTPException(500, detail=f"{e.__cause__}")\n'
        )

    def test_indirection_through_a_local_is_a_violation(self) -> None:
        """``msg = str(e)`` then ``detail=msg`` — the obvious way around a naive rule."""
        assert self._violations(
            "try:\n    f()\nexcept Exception as e:\n    msg = str(e)\n"
            "    raise HTTPException(500, detail=msg)\n"
        )

    def test_chained_indirection_is_a_violation(self) -> None:
        assert self._violations(
            "try:\n    f()\nexcept Exception as e:\n    a = str(e)\n    b = a\n"
            '    raise HTTPException(500, detail=f"boom {b}")\n'
        )

    def test_indirection_through_a_container_is_a_violation(self) -> None:
        assert self._violations(
            "try:\n    f()\nexcept Exception as e:\n    d = {}\n    d['m'] = str(e)\n"
            "    raise HTTPException(500, detail=d['m'])\n"
        )

    def test_nested_fstring_is_a_violation(self) -> None:
        assert self._violations(
            "try:\n    f()\nexcept Exception as e:\n"
            '    raise HTTPException(500, detail=f"outer {f"inner {e}"}")\n'
        )

    def test_exception_through_a_helper_call_is_a_violation(self) -> None:
        assert self._violations(
            "try:\n    f()\nexcept Exception as e:\n"
            "    raise HTTPException(500, detail=_describe(e))\n"
        )

    def test_positional_detail_is_a_violation(self) -> None:
        """``HTTPException(500, str(e))`` — no ``detail=`` keyword to grep for."""
        assert self._violations(
            "try:\n    f()\nexcept Exception as e:\n    raise HTTPException(500, str(e))\n"
        )

    def test_leak_through_the_sanitising_helper_is_a_violation(self) -> None:
        """The helper's *message* is caller-visible; it must not be a bypass."""
        assert self._violations(
            "try:\n    f()\nexcept Exception as e:\n"
            '    raise _internal_error(500, f"boom {e}", e, "ctx")\n'
        )

    def test_helper_exc_argument_is_not_a_violation(self) -> None:
        """...but the argument that exists to BE logged is exempt."""
        assert not self._violations(
            "try:\n    f()\nexcept Exception as e:\n"
            '    raise _internal_error(500, "Boom", e, "ctx")\n'
        )

    def test_leak_through_the_middleware_response_helper_is_a_violation(self) -> None:
        """The escape an adversarial review of this rule actually found.

        ``auth/middleware.py`` reaches the response body via
        ``self._error_response(status, str(exc.detail))`` — no ``HTTPException(``
        call, no ``detail=`` keyword. A rule keyed on ``detail=`` scores it zero.
        """
        assert self._violations(
            "try:\n    f()\nexcept Exception as e:\n    return self._error_response(500, str(e))\n"
        )

    def test_leak_through_a_json_response_body_is_a_violation(self) -> None:
        """``JSONResponse`` bypasses ``HTTPException`` entirely."""
        assert self._violations(
            "try:\n    f()\nexcept Exception as e:\n"
            '    return JSONResponse(status_code=500, content={"detail": str(e)})\n'
        )

    def test_leak_after_the_handler_exits_is_a_violation(self) -> None:
        """F1, the headline escape — proven on the wire before it was closed.

        A verifier rewrote ``list_all_licenses_admin`` into exactly this shape: the full
        suite passed, this gate stayed green, anti-vacuity stayed green, and the response
        carried the DSN. Handler-scoped taint cannot see it, and the runtime scrubber
        cannot either — after the handler exits there is no ``__cause__`` left to match.
        """
        assert self._violations(
            "def f():\n    try:\n        g()\n    except Exception as e:\n"
            "        msg = str(e)\n"
            '    raise HTTPException(500, detail=f"Failed: {msg}")\n'
        )

    def test_leak_from_a_finally_block_is_a_violation(self) -> None:
        assert self._violations(
            "def f():\n    try:\n        g()\n    except Exception as e:\n"
            "        msg = str(e)\n"
            "    finally:\n        raise HTTPException(500, detail=msg)\n"
        )

    def test_leak_from_an_else_block_is_a_violation(self) -> None:
        assert self._violations(
            "def f():\n    try:\n        g()\n    except Exception as e:\n"
            "        msg = str(e)\n"
            "    else:\n        raise HTTPException(500, detail=msg)\n"
        )

    def test_exception_variable_itself_after_the_handler_is_a_violation(self) -> None:
        """No intermediate local — the bound name used directly, post-handler."""
        assert self._violations(
            "def f():\n    try:\n        g()\n    except Exception as e:\n        pass\n"
            '    raise HTTPException(500, detail=f"{e}")\n'
        )

    def test_positional_json_response_content_is_a_violation(self) -> None:
        """F2: ``content`` is starlette's FIRST parameter, not its second."""
        assert self._violations(
            "def f():\n    try:\n        g()\n    except Exception as e:\n"
            '        return JSONResponse({"detail": str(e)}, status_code=500)\n'
        )

    def test_positional_plain_text_response_is_a_violation(self) -> None:
        assert self._violations(
            "def f():\n    try:\n        g()\n    except Exception as e:\n"
            "        return PlainTextResponse(str(e), 500)\n"
        )

    def test_bare_response_class_is_a_violation(self) -> None:
        """F3: bare ``Response`` is a real sink, and main.py now imports it."""
        assert self._violations(
            "def f():\n    try:\n        g()\n    except Exception as e:\n"
            "        return Response(content=str(e), status_code=500)\n"
        )

    def test_leak_through_the_client_error_helper_is_a_violation(self) -> None:
        """The 4xx sibling helper must not be a bypass either."""
        assert self._violations(
            "def f():\n    try:\n        g()\n    except Exception as e:\n"
            '        raise _client_error(400, f"bad {e}", e, "ctx")\n'
        )

    def test_bare_imported_format_exc_is_a_violation(self) -> None:
        """F5: ``from traceback import format_exc`` produces a BARE call."""
        assert self._violations(
            "from traceback import format_exc\n"
            "def f():\n    try:\n        g()\n    except Exception:\n"
            "        raise HTTPException(500, detail=format_exc())\n"
        )

    def test_aliased_traceback_module_is_a_violation(self) -> None:
        assert self._violations(
            "import traceback as tb\n"
            "def f():\n    try:\n        g()\n    except Exception:\n"
            "        raise HTTPException(500, detail=tb.format_exc())\n"
        )

    def test_traceback_format_exc_in_a_bare_handler_is_a_violation(self) -> None:
        """No ``as e`` to reference, yet the whole traceback still reaches the caller."""
        assert self._violations(
            "try:\n    f()\nexcept Exception:\n"
            "    raise HTTPException(500, detail=traceback.format_exc())\n"
        )

    def test_sys_exc_info_is_a_violation(self) -> None:
        assert self._violations(
            "try:\n    f()\nexcept Exception:\n"
            '    raise HTTPException(500, detail=f"{sys.exc_info()[1]}")\n'
        )

    def test_outer_exception_is_still_forbidden_inside_a_client_safe_handler(self) -> None:
        """A ``ClientSafeError`` frame permits ITS OWN variable, not an outer one."""
        assert self._violations(
            "try:\n    f()\nexcept Exception as outer:\n    try:\n        g()\n"
            "    except ClientSafeError as inner:\n"
            "        raise HTTPException(422, detail=str(outer))\n"
        )

    def test_client_safe_in_a_tuple_is_a_violation(self) -> None:
        """``except (ClientSafeError, OSError)`` would echo the OSError arm too."""
        assert self._violations(
            "try:\n    f()\nexcept (ClientSafeError, OSError) as e:\n"
            "    raise HTTPException(422, detail=str(e))\n"
        )

    # -- must STAY QUIET ---------------------------------------------------
    def test_request_data_interpolation_is_not_a_violation(self) -> None:
        """The false-positive guard.

        ``main.py`` legitimately interpolates the caller's OWN request data into
        404/409 details (``App not found: {body.app_key}``). That is data the caller
        just sent us; a rule that flags it would be abandoned within a week.
        """
        assert not self._violations(
            "try:\n    f()\nexcept Exception as e:\n"
            '    raise HTTPException(404, detail=f"App not found: {body.app_key}")\n'
        )

    def test_static_detail_is_not_a_violation(self) -> None:
        assert not self._violations(
            "try:\n    f()\nexcept Exception as e:\n"
            '    raise HTTPException(500, detail="Internal server error")\n'
        )

    def test_client_safe_error_may_be_interpolated(self) -> None:
        """The permitted escape — narrowed catch, message authored by our own DAL."""
        assert not self._violations(
            "try:\n    f()\nexcept ClientSafeError as e:\n"
            "    raise HTTPException(422, detail=str(e))\n"
        )

    def test_local_not_derived_from_the_exception_is_not_a_violation(self) -> None:
        assert not self._violations(
            'try:\n    f()\nexcept Exception as e:\n    msg = f"app {body.app_key}"\n'
            "    raise HTTPException(404, detail=msg)\n"
        )

    def test_detail_outside_any_handler_is_not_a_violation(self) -> None:
        assert not self._violations('raise HTTPException(400, detail=f"bad {e}")\n')

    def test_shadowed_name_after_the_handler_is_not_a_violation(self) -> None:
        """``e`` used outside the handler body is a different binding entirely."""
        found, _ = scan_source(
            "try:\n    f()\nexcept Exception as e:\n    pass\n"
            'e = compute()\nraise HTTPException(400, detail=f"{e}")\n'
        )
        assert not found

    def test_reraising_an_http_exceptions_own_detail_is_not_a_violation(self) -> None:
        """``except HTTPException as exc: ... str(exc.detail)`` — the middleware shape.

        Safe by construction, not by inspection: every ``HTTPException`` in this
        service is built by this service, and its ``detail`` has already been
        cleared by this same rule. The rule is closed under re-raising one.
        """
        assert not self._violations(
            "try:\n    f()\nexcept HTTPException as exc:\n"
            "    return self._error_response(exc.status_code, str(exc.detail))\n"
        )


class TestKnownUncoveredShapes:
    """The gate's holes, asserted rather than assumed.

    Each test below pins a shape this rule is KNOWN not to catch. They exist so the
    limit is measured and reviewable: if someone later strengthens the walker, these
    fail and get promoted into ``TestDetectorDiscriminatingPower``. Treating an
    unstated hole as coverage is the failure mode this class prevents — none of these
    shapes exists in the repo today (verified by an independent taint-flow scan), and
    ``main.http_exception_handler`` scrubs all of them at runtime on 5xx.
    """

    @staticmethod
    def _violations(body: str) -> list[Violation]:
        """Same contract as the discriminating-power helper, INCLUDING the floor.

        Without ``inspected >= 1`` a "no violations" result cannot be told apart from
        "the walker never looked" — and that is precisely the distinction the
        post-handler escape turned on: it read as a documented gap while actually being
        a walker that stopped tracking. Gap 1 is the one exception (an unregistered
        sink is BY DEFINITION not inspected), so it opts out explicitly below.
        """
        found, inspected = scan_source(body)
        assert inspected >= 1, f"walker inspected NO detail expression in:\n{body}"
        return found

    def test_unregistered_response_helper_is_not_caught(self) -> None:
        """Gap 1: a body-building helper whose name is not in ``BODY_SINKS``.

        Opts out of the ``inspected >= 1`` floor on purpose: an unregistered sink is
        invisible by definition, so zero inspections IS the finding here.
        """
        found, inspected = scan_source(
            "try:\n    f()\nexcept Exception as e:\n    return _fail(500, str(e))\n"
        )
        assert not found
        assert inspected == 0, "_fail is now a registered sink — promote this test"
        assert "_fail" not in BODY_SINKS

    def test_cross_function_flow_is_not_caught(self) -> None:
        """Gap 2: the exception escapes the handler into module state."""
        assert not self._violations(
            "try:\n    f()\nexcept Exception as e:\n    _stash(e)\n"
            "def g():\n    raise HTTPException(500, detail=LAST_ERROR)\n"
        )

    def test_mutating_method_taint_is_not_caught(self) -> None:
        """Gap 3: taint laundered through ``list.append`` rather than assignment."""
        assert not self._violations(
            "try:\n    f()\nexcept Exception as e:\n    parts = []\n"
            "    parts.append(str(e))\n"
            '    raise HTTPException(500, detail=" ".join(parts))\n'
        )

    def test_runtime_populated_table_is_not_caught(self) -> None:
        """Gap 4: the detail is a lookup whose table was filled at runtime."""
        assert not self._violations(
            "try:\n    f()\nexcept Exception as e:\n"
            "    raise HTTPException(500, detail=MESSAGES[key])\n"
        )

    def test_loop_carried_ordering_is_not_caught(self) -> None:
        """Gap 5: replay is by SOURCE position, not by execution order.

        A sink written above the handler but reached again on a later iteration is
        untainted at its source position. Ordering is what makes the post-handler
        escape catchable at all, so this is the price of that fix — and unlike the
        post-handler shape, the runtime scrubber DOES cover this one, because the
        handler is still on the stack when the sink runs.
        """
        assert not self._violations(
            "def f():\n    msg = ''\n    for _ in range(2):\n"
            "        raise HTTPException(500, detail=msg)\n"
            "        try:\n            g()\n        except Exception as e:\n"
            "            msg = str(e)\n"
        )

    def test_taint_does_not_cross_a_nested_function(self) -> None:
        """Gap 2, the intra-file half: a nested ``def`` is its own scope."""
        assert not self._violations(
            "def outer():\n    try:\n        g()\n    except Exception as e:\n"
            "        def inner():\n"
            '            raise HTTPException(500, detail=f"{e}")\n'
            "        return inner\n"
        )


# --------------------------------------------------------------------------
# Behavioural proof. The AST rule above is a source-level invariant; these drive
# the real app and assert on the bytes a caller actually receives.
# --------------------------------------------------------------------------

# A FAKE DSN, shaped exactly like the real one this endpoint used to leak.
#
# Assembled from parts on purpose. Written as ONE literal, a URL carrying
# embedded credentials matches detect-secrets' "Basic Auth Credentials" plugin —
# correctly, on shape alone, since the scanner cannot know the value is invented.
# Do NOT re-inline it, and do NOT reach for a `pragma: allowlist` marker or a
# `.secrets.baseline` entry: both blunt a gate that is doing its job, and a
# baselined fake teaches the next reader that fakes belong there. Splitting the
# literal keeps the scanner fully sharp while the runtime value stays
# byte-identical to what a real psycopg connection error carries.
# (Prose here must avoid the same shape, or the comment trips the plugin too.)
_LEAK_CREDENTIALS = "licenses:hunter2"
_LEAK_NETLOC = "10.42.0.9:5432"
LEAKY_DSN = "postgresql://" + _LEAK_CREDENTIALS + "@" + _LEAK_NETLOC + "/licenses"


class TestHealthReadyDoesNotLeakTheDsn:
    """``GET /health/ready`` is PUBLIC — its 503 body is unauthenticated output.

    ``auth/middleware.py`` lists ``/health/ready`` in ``PUBLIC_ENDPOINTS``, so before
    this fix any pod in the cluster could force a DB error and read the DSN, host and
    port out of the response body. This is the site the whole workstream exists for.
    """

    def test_dsn_never_reaches_the_caller(self, client, monkeypatch, caplog) -> None:
        import logging

        import main

        def _boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError(f"could not connect: dsn={LEAKY_DSN}")

        monkeypatch.setattr(main.db_manager, "get_session", _boom)

        with caplog.at_level(logging.ERROR):
            response = client.get("/health/ready")

        assert response.status_code == 503
        assert response.json()["detail"] == "Database not ready"

        # The whole response, headers included — not just the parsed detail. A leak
        # that lands anywhere in the payload is still a leak.
        assert "hunter2" not in response.text
        assert "10.42.0.9" not in response.text
        assert "dsn=" not in response.text

        # ...and the real cause MUST still be diagnosable. A fix that silences the
        # operator as well as the attacker is not a fix.
        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert "hunter2" in logged, "the real cause was not logged — operators are now blind"
        assert "Readiness check failed" in logged

    def test_healthy_ready_is_untouched(self, client) -> None:
        """Positive control: the sanitising path must not have broken the happy path."""
        response = client.get("/health/ready")
        assert response.status_code == 200
        assert response.json() == {"status": "ready", "database": "connected"}


class TestRuntimeScrubberIsScopedTo5xx:
    """``main.http_exception_handler`` — the defence-in-depth layer, measured.

    It rewrites a 5xx ``detail`` ONLY when that detail literally contains its own
    ``__cause__``/``__context__`` text. That containment test is what lets it scrub a
    future leak without flattening the static per-endpoint messages callers rely on.
    """

    @staticmethod
    def _handle(status_code: int, detail: object, cause: BaseException | None):
        import asyncio

        from fastapi import HTTPException

        import main

        exc = HTTPException(status_code=status_code, detail=detail)
        if cause is not None:
            exc.__cause__ = cause
        return asyncio.run(main.http_exception_handler(None, exc))  # ty: ignore

    def test_5xx_detail_containing_its_cause_is_scrubbed(self) -> None:
        cause = RuntimeError(f"could not connect: dsn={LEAKY_DSN}")
        response = self._handle(500, f"Failed: {cause}", cause)
        assert response.status_code == 500
        assert b"hunter2" not in response.body
        assert b"Internal server error" in response.body

    def test_5xx_static_detail_is_preserved(self) -> None:
        """The scrubber must NOT flatten every 5xx into one message.

        ``/health/ready`` returns 503 "Database not ready" with a DB error as its
        cause; that detail is static, so it passes through. Without this test the
        scrubber could be a blanket replace and still look correct.
        """
        cause = RuntimeError(f"could not connect: dsn={LEAKY_DSN}")
        response = self._handle(503, "Database not ready", cause)
        assert response.status_code == 503
        assert b"Database not ready" in response.body

    def test_4xx_is_never_scrubbed(self) -> None:
        """4xx details are contract.

        ``tests/auth/test_rbac.py`` asserts role names in 403 bodies, and the 409
        from ``POST /tenants/{tenant_id}`` carries the caller's only actionable
        signal. Even a 4xx detail that DOES contain its cause is left alone.
        """
        cause = ValueError("name 'acme' already belongs to tenant 0195")
        response = self._handle(409, str(cause), cause)
        assert response.status_code == 409
        assert b"already belongs to tenant" in response.body

    def test_5xx_dict_detail_containing_its_cause_is_scrubbed(self) -> None:
        """F6: ``detail`` is not always a string.

        ``GET /licenses/active`` already returns ``detail={"error": ...}``, so a 5xx
        built the same way — ``detail={"error": str(e)}`` — used to sail past an
        ``isinstance(detail, str)`` guard completely untouched.
        """
        cause = RuntimeError(f"could not connect: dsn={LEAKY_DSN}")
        response = self._handle(500, {"error": f"boom {cause}"}, cause)
        assert response.status_code == 500
        assert b"hunter2" not in response.body
        assert b"Internal server error" in response.body

    def test_5xx_nested_list_detail_is_scrubbed(self) -> None:
        cause = RuntimeError(f"could not connect: dsn={LEAKY_DSN}")
        response = self._handle(500, {"errors": [{"msg": str(cause)}]}, cause)
        assert b"hunter2" not in response.body

    def test_5xx_dict_detail_key_containing_its_cause_is_scrubbed(self) -> None:
        """A leak can be KEYED by the exception text as easily as valued by it."""
        cause = RuntimeError(f"could not connect: dsn={LEAKY_DSN}")
        response = self._handle(500, {str(cause): "failed"}, cause)
        assert b"hunter2" not in response.body

    def test_5xx_clean_dict_detail_is_preserved(self) -> None:
        """Discriminating control: a structured 5xx body that leaks nothing survives."""
        cause = RuntimeError(f"could not connect: dsn={LEAKY_DSN}")
        response = self._handle(500, {"error": "upstream_unavailable"}, cause)
        assert b"upstream_unavailable" in response.body

    def test_4xx_structured_detail_is_never_scrubbed(self) -> None:
        """``GET /licenses/active``'s 404 shape — a real contract, verified unchanged."""
        cause = RuntimeError(f"could not connect: dsn={LEAKY_DSN}")
        response = self._handle(404, {"error": "no_active_license"}, cause)
        assert response.status_code == 404
        assert b"no_active_license" in response.body

    def test_short_cause_does_not_false_positive(self) -> None:
        """``"" in detail`` is always True — the length floor is load-bearing.

        A 1-3 character cause is not evidence that the detail interpolated it.
        """
        response = self._handle(500, "Failed to list plans", ValueError(""))
        assert b"Failed to list plans" in response.body
        response = self._handle(500, "Failed to list plans", ValueError("a"))
        assert b"Failed to list plans" in response.body


class TestClientFaultsDoNotInflateErrorRate:
    """A malformed UUID is the caller's fault, and must not read as a service error.

    Routing 4xx through ``_internal_error`` logged ERROR **with a full traceback** at
    eight sites where ``origin/main`` logged nothing at all. That hands any caller a way
    to drive this service's ERROR rate and traceback volume by sending garbage, and the
    fleet runs log-level-based health checks on this chain.
    """

    def test_malformed_uuid_logs_warning_not_error(self, auth_client_admin, caplog) -> None:
        import logging

        with caplog.at_level(logging.WARNING):
            response = auth_client_admin.post("/license/revoke", json={"license_id": "not-a-uuid"})

        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid UUID"

        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert not errors, f"client fault logged at ERROR: {[r.getMessage() for r in errors]}"

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings, "client fault logged nothing at all — operators lose the signal"
        assert not any(r.exc_info for r in warnings), (
            "client fault logged a traceback — caller-triggerable log volume"
        )

    def test_server_faults_still_log_error_with_traceback(self, auth_client_admin, caplog) -> None:
        """Discriminating control: the 5xx path must keep its ERROR + traceback.

        Without this, downgrading everything to WARNING would also pass the test above.
        """
        import logging
        from unittest.mock import patch

        from models import DatabaseManager

        with (
            patch.object(DatabaseManager, "list_apps", side_effect=RuntimeError("boom")),
            caplog.at_level(logging.ERROR),
        ):
            response = auth_client_admin.get("/apps")

        assert response.status_code == 500
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors, "server fault stopped logging at ERROR"
        assert any(r.exc_info for r in errors), "server fault lost its traceback"
