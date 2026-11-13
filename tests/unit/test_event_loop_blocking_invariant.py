"""Structural gate: no route handler may hold the event loop while it blocks.

WHY THIS FILE EXISTS, AND WHY THE ENDPOINT TESTS DO NOT COVER IT
----------------------------------------------------------------
``main.py`` runs under ``uvicorn main:app`` with **no ``--workers``**
(``Dockerfile:93``), so each pod is ONE process with ONE event loop. A blocking
SQLAlchemy call inside an ``async def`` handler therefore serialises every other
in-flight request on that pod. The repo measured this itself — see the comment
on ``introspect_token``: 30 concurrent introspects took ~10s wall and blew past
callers' 3.0s budget, which license-authz reports as ``introspect_timeout`` and
turns into a 403 on a VALID token with a cold cache.

A ``TestClient``-driven behavioural test cannot see any of this. ``TestClient``
drives requests SERIALLY through an anyio portal, so it passes identically
whether a handler is ``def`` or ``async def``, and it would pass through a full
revert of this change. The existing endpoint suite is a regression guard, not
evidence. This file is the evidence, in two halves:

  * here — a structural invariant over the LIVE route registry;
  * ``test_no_sql_on_event_loop.py`` — a behavioural discriminator that runs a
    real uvicorn and proves no SQL statement executes on the loop thread.

THE RULE (derived, bidirectional, no allowlist)
-----------------------------------------------
An ``APIRoute`` endpoint may be a coroutine function IF AND ONLY IF one of:

  (a) its body contains a real yield point — ``await`` / ``async with`` /
      ``async for``;
  (b) its body calls a LOOP-AFFINE primitive that requires a running loop —
      ``asyncio.create_task`` / ``ensure_future`` / ``get_event_loop`` /
      ``get_running_loop`` / ``run_coroutine_threadsafe``, or this module's
      ``_spawn_background_task`` helper (which calls ``create_task``);
  (c) it is itself ``await``-ed elsewhere in its defining module, i.e. another
      coroutine's contract requires it to BE a coroutine;
  (d) its body makes NO calls at all (a bare literal return). Such a handler
      cannot block, so it costs the loop nothing — and it must NOT be handed to
      the threadpool, where it would consume one of the HANDLER_THREAD_LIMIT
      tokens SHARED with every DB-backed handler.

Leg (d) is the one that was learned the hard way. ``/health/live`` is pure and
was flipped to ``def``; under a database outage, with the pool saturated by
requests blocked on the DB, the liveness probe queued behind them and blew its
own 5s timeout despite having no work to do. kubelet killed the container, and
because ``lifespan`` fail-fasts on an unreachable DB the restart exited code 3
in ~10s → CrashLoopBackOff on every replica. Measured 2026-08-11 in scratch
namespace ``licenses-verify2``. A probe must never contend for the same
resource as the workload it is monitoring.

The rule runs in BOTH directions, and both directions are load-bearing:

  * an ``async def`` with none of (a)/(b)/(c) blocks the loop for nothing — flip
    it to ``def`` and FastAPI runs it in the threadpool;
  * a plain ``def`` matching (b) raises ``RuntimeError: no running event loop``
    from a worker thread (this is exactly why ``create_user_license`` must stay
    ``async``: it would 500 AFTER committing the licence row), and a plain
    ``def`` matching (c) makes its caller's ``await`` a ``TypeError`` (this is
    why ``health_check`` must stay ``async``: ``licenses_health_alias`` does
    ``return await health_check()``, and that alias is the publicly-routed path
    ``stage-gate-check.sh`` polls).

Membership is DERIVED from that rule against the routes registered at runtime.
There is no allowlist and no exemption table, so a handler added tomorrow is
covered without anyone remembering to list it.

``TestDetectorHasDiscriminatingPower`` is the positive control. Without it a
detector that could only ever return ``[]`` would read as a passing gate.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
from functools import cache
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute

import main

# Calls that only work with a running event loop underneath them.
LOOP_AFFINE_CALLS = frozenset(
    {
        "create_task",
        "ensure_future",
        "get_event_loop",
        "get_running_loop",
        "run_coroutine_threadsafe",
        # main.py's own wrapper around asyncio.create_task (it holds a strong
        # reference to the task so it cannot be GC'd mid-flight).
        "_spawn_background_task",
    }
)


class _BodyScan(ast.NodeVisitor):
    """Collect yield points and loop-affine calls in ONE function body.

    Nested ``def`` / ``async def`` / ``lambda`` are NOT descended into: a yield
    point inside a nested coroutine says nothing about whether the OUTER
    function yields control.
    """

    def __init__(self) -> None:
        self.yield_points: list[str] = []
        self.loop_calls: list[str] = []
        # EVERY call, not just the loop-affine ones. A body with no calls at all
        # does no work that could block, which is leg (d) of the rule.
        self.calls: list[str] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
        return

    def visit_Await(self, node: ast.Await) -> None:  # noqa: N802
        self.yield_points.append(f"await@L{node.lineno}")
        self.generic_visit(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:  # noqa: N802
        self.yield_points.append(f"async-with@L{node.lineno}")
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:  # noqa: N802
        self.yield_points.append(f"async-for@L{node.lineno}")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        func = node.func
        name: str | None = None
        if isinstance(func, ast.Attribute):
            name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id
        if name in LOOP_AFFINE_CALLS:
            self.loop_calls.append(f"{name}@L{node.lineno}")
        # Any call at all makes the handler non-trivial — see leg (d).
        self.calls.append(f"{name or '<expr>'}@L{node.lineno}")
        self.generic_visit(node)


def _function_ast(fn: Any) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Parse ``fn``'s own source into its (Async)FunctionDef node."""
    try:
        source = textwrap.dedent(inspect.getsource(fn))
    except OSError, TypeError:
        return None
    try:
        module = ast.parse(source)
    except SyntaxError:
        return None
    for node in module.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            return node
    return None


@cache
def _awaited_names_in_file(path: str) -> frozenset[str]:
    """Names called under an ``await`` anywhere in ``path`` (rule leg (c))."""
    try:
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
    except OSError, SyntaxError:
        return frozenset()

    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Await):
            continue
        value = node.value
        if isinstance(value, ast.Call):
            func = value.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return frozenset(names)


def _is_awaited_elsewhere(fn: Any) -> bool:
    path = inspect.getsourcefile(fn)
    if path is None:
        return False
    return getattr(fn, "__name__", "") in _awaited_names_in_file(path)


def audit_routes(app: FastAPI) -> dict[str, list[str]]:
    """Return the two rule violations, keyed by direction.

    ``blocking_async``   — coroutine handlers with no justification: they hold
                           the single event loop while they block.
    ``sync_needs_loop``  — plain ``def`` handlers that require a running loop:
                           they raise ``RuntimeError`` / break an ``await``.
    """
    blocking_async: list[str] = []
    sync_needs_loop: list[str] = []

    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        endpoint = route.endpoint
        node = _function_ast(endpoint)
        if node is None:
            continue

        scan = _BodyScan()
        for statement in node.body:
            scan.visit(statement)

        awaited_elsewhere = _is_awaited_elsewhere(endpoint)
        name = getattr(endpoint, "__name__", repr(endpoint))

        # Leg (d): a body that makes NO calls at all cannot block, so it costs
        # the loop nothing — and it must NOT be handed to the threadpool, where
        # it would consume one of HANDLER_THREAD_LIMIT tokens shared with every
        # DB-backed handler. That is not hypothetical: /health/live was flipped
        # to sync, and under a DB outage with the pool saturated it queued
        # behind blocked requests and blew its own 5s probe timeout, killing the
        # container (measured 2026-08-11, ns licenses-verify2).
        trivial = not scan.calls and not scan.yield_points

        if inspect.iscoroutinefunction(endpoint):
            justified = (
                bool(scan.yield_points) or bool(scan.loop_calls) or awaited_elsewhere or trivial
            )
            if not justified:
                blocking_async.append(name)
        else:
            if scan.loop_calls or awaited_elsewhere or trivial:
                if scan.loop_calls:
                    reason = "loop-affine call"
                elif awaited_elsewhere:
                    reason = "awaited by another handler"
                else:
                    reason = "does no work — must not take a threadpool token"
                sync_needs_loop.append(f"{name} ({reason})")

    return {"blocking_async": blocking_async, "sync_needs_loop": sync_needs_loop}


# ---------------------------------------------------------------------------
# Positive control: a throwaway app whose four routes span every rule outcome.
# These live at module level so ``inspect.getsource`` can read them.
# ---------------------------------------------------------------------------

_control_app = FastAPI()


@_control_app.get("/ctl/blocking")
async def ctl_async_no_await() -> dict[str, str]:
    """VIOLATION: a coroutine with no yield point and no loop affinity.

    It must actually CALL something (standing in for a blocking DB call) —
    otherwise it is leg (d), does no work, and is legitimately allowed to stay
    async. "Async and does nothing" is fine; "async and blocks" is the defect.
    """
    return {"blocked": str(len("the loop"))}


@_control_app.get("/ctl/awaits")
async def ctl_async_awaits() -> dict[str, str]:
    """OK by leg (a): a real yield point."""
    await asyncio.sleep(0)
    return {"ok": "true"}


@_control_app.get("/ctl/loop-affine-async")
async def ctl_async_create_task() -> dict[str, str]:
    """OK by leg (b): needs a running loop, has one."""
    asyncio.create_task(asyncio.sleep(0))  # noqa: RUF006
    return {"ok": "true"}


@_control_app.get("/ctl/loop-affine-sync")
def ctl_sync_create_task() -> dict[str, str]:
    """VIOLATION in the other direction: a worker thread has no running loop."""
    asyncio.create_task(asyncio.sleep(0))  # noqa: RUF006
    return {"broken": "RuntimeError"}


@_control_app.get("/ctl/plain-sync")
def ctl_sync_plain() -> dict[str, str]:
    """OK: plain blocking work, correctly off the loop in the threadpool.

    It must actually CALL something — a bare literal return would be leg (d)
    (does no work ⇒ must stay on the loop), which is a different case entirely.
    """
    return {"ok": str(len("blocking"))}


@_control_app.get("/ctl-async-trivial")
async def ctl_async_trivial() -> dict[str, str]:
    """OK by leg (d): does no work, so it is free on the loop and must stay there.

    This is ``/health/live``'s shape. Flipping it to ``def`` would make a probe
    that does nothing wait for a threadpool token behind DB-blocked requests.
    """
    return {"status": "alive"}


@_control_app.get("/ctl-sync-trivial")
def ctl_sync_trivial() -> dict[str, str]:
    """VIOLATION of leg (d): does no work, yet consumes a threadpool token."""
    return {"status": "alive"}


class TestDetectorHasDiscriminatingPower:
    """The gate cannot prove anything about main.py until it proves it fires.

    A detector that can only return ``[]`` — because a leg never runs, or
    because someone replaced the body with ``return []`` — is indistinguishable
    from a clean codebase. These assertions are EXACT set equality in both
    directions, so neither "flags nothing" nor "flags everything" survives.
    """

    def test_control_flags_exactly_the_broken_routes(self):
        report = audit_routes(_control_app)
        assert set(report["blocking_async"]) == {"ctl_async_no_await"}, report
        assert {entry.split(" ")[0] for entry in report["sync_needs_loop"]} == {
            "ctl_sync_create_task",
            # leg (d): does no work, so a threadpool token is pure waste and a
            # starvation hazard for probes. This is the /health/live case.
            "ctl_sync_trivial",
        }, report

    def test_control_does_not_flag_the_correct_routes(self):
        report = audit_routes(_control_app)
        flagged = set(report["blocking_async"]) | {
            entry.split(" ")[0] for entry in report["sync_needs_loop"]
        }
        for correct in (
            "ctl_async_awaits",
            "ctl_async_create_task",
            "ctl_sync_plain",
            # leg (d), the permitted direction: pure AND async is correct.
            "ctl_async_trivial",
        ):
            assert correct not in flagged, f"{correct} was flagged: {report}"


class TestNoHandlerBlocksTheEventLoop:
    def test_no_unjustified_async_handler(self):
        """Every ``async def`` route must have a real reason to be one."""
        report = audit_routes(main.app)
        assert report["blocking_async"] == [], (
            "These handlers are `async def` but contain no await / async-with / "
            "async-for, no loop-affine call, and are not awaited by another "
            "handler. They therefore hold the single event loop while their "
            "blocking SQLAlchemy calls run, serialising every other request on "
            "the pod. Either delete the `async ` keyword (FastAPI then runs them "
            "in the threadpool) or wrap the blocking call in "
            "`await asyncio.to_thread(...)`. Offenders: "
            f"{report['blocking_async']}"
        )

    def test_no_sync_handler_needs_a_running_loop(self):
        """The other direction: flipping too far is a crash, not a slowdown."""
        report = audit_routes(main.app)
        assert report["sync_needs_loop"] == [], (
            "These handlers are plain `def` but require a running event loop. "
            "FastAPI runs them in a worker thread, where `asyncio.create_task` "
            "raises `RuntimeError: no running event loop`, and where an "
            "`await handler()` in another handler raises `TypeError`. They must "
            "stay `async def`. Offenders: "
            f"{report['sync_needs_loop']}"
        )


class TestGateIsNotVacuous:
    """Floors, so a collapsed route registry cannot make the gate trivially true."""

    def test_route_registry_is_populated(self):
        api_routes = [r for r in main.app.routes if isinstance(r, APIRoute)]
        assert len(api_routes) >= 40, (
            f"only {len(api_routes)} APIRoutes found — the registry collapsed, so "
            "the invariant above was asserted against almost nothing"
        )

    def test_most_handlers_are_now_sync(self):
        api_routes = [r for r in main.app.routes if isinstance(r, APIRoute)]
        sync = [r for r in api_routes if not inspect.iscoroutinefunction(r.endpoint)]
        assert len(sync) >= 30, (
            f"only {len(sync)} of {len(api_routes)} handlers are sync `def`. The "
            "point of this change is that the pure-blocking majority runs in the "
            "threadpool; if this drops, the flip was reverted."
        )

    def test_some_handlers_are_still_async(self):
        """A blanket flip to `def` would satisfy the rule and break the service."""
        api_routes = [r for r in main.app.routes if isinstance(r, APIRoute)]
        coros = [r for r in api_routes if inspect.iscoroutinefunction(r.endpoint)]
        assert len(coros) >= 7, (
            f"only {len(coros)} coroutine handlers left. Handlers that genuinely "
            "await (introspect, the billing hops, get_tenant_id) and handlers "
            "with loop affinity MUST stay `async def`."
        )

    def test_every_rule_leg_is_exercised_by_the_real_app(self):
        """Each justification leg must actually be reachable in main.py.

        A leg no route uses is a leg whose correctness nobody has tested — and
        legs (b) and (c) are the two that turn a wrong flip into a 500 rather
        than a slowdown, so their being live in main.py is the whole reason the
        sync-side half of the rule exists.
        """
        legs: dict[str, list[str]] = {"await": [], "loop_affine": [], "awaited_elsewhere": []}
        for route in main.app.routes:
            if not isinstance(route, APIRoute) or not inspect.iscoroutinefunction(route.endpoint):
                continue
            node = _function_ast(route.endpoint)
            if node is None:
                continue
            scan = _BodyScan()
            for statement in node.body:
                scan.visit(statement)
            name = getattr(route.endpoint, "__name__", repr(route.endpoint))
            if scan.yield_points:
                legs["await"].append(name)
            if scan.loop_calls:
                legs["loop_affine"].append(name)
            if _is_awaited_elsewhere(route.endpoint):
                legs["awaited_elsewhere"].append(name)

        assert legs["await"], "no handler justified by leg (a) — the detector may be broken"
        assert legs["loop_affine"], (
            "no handler justified by leg (b). `create_user_license` schedules the "
            "metering budget push via `_spawn_background_task` -> "
            "`asyncio.create_task`; as a sync `def` that raises RuntimeError AFTER "
            f"the licence row is committed. Found: {legs}"
        )
        assert legs["awaited_elsewhere"], (
            "no handler justified by leg (c). `licenses_health_alias` does "
            "`return await health_check()`, so `health_check` must stay a "
            f"coroutine. Found: {legs}"
        )
