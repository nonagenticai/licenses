"""Durable invariant: every protected route carries a role gate.

The cross-tenant holes fixed on 2026-07-27 were found by HAND-auditing
``main.py`` route by route. A hand audit is worthless the moment someone adds
route 41, so this module turns it into a gate: walk ``app.routes``, keep the
ones the auth middleware protects, and assert each one's dependency tree
contains a role checker from ``auth.dependencies``.

Scope, stated honestly: this proves a route is not *dependency-less*. It does
NOT prove the route scopes its query to the caller's tenant — that is what
``test_authz_idor.py`` covers per route. The two are complementary: this one
catches the "someone forgot the ``Depends`` entirely" class, which is the one a
reviewer's eye slides past.
"""

import pytest
from fastapi import Depends, FastAPI
from fastapi.routing import APIRoute

import main
from auth.dependencies import (
    require_any_role,
    require_bundle_ensurer,
    require_bundle_token_minter,
    require_internal_or_reader,
    require_internal_service,
    require_role,
)
from auth.middleware import PROTECTED_PREFIXES, PUBLIC_ENDPOINTS

# ``require_role`` / ``require_any_role`` / ``require_all_roles`` are dependency
# FACTORIES: they return a closure, so identity comparison is useless and the
# qualname (``require_any_role.<locals>.checker``) is the stable signal.
ROLE_GATE_QUALNAME_PREFIXES = (
    "require_role.<locals>",
    "require_any_role.<locals>",
    "require_all_roles.<locals>",
)
# Plain-function gates, compared by identity.
ROLE_GATE_FUNCTIONS = frozenset(
    {
        require_internal_service,
        require_internal_or_reader,
        require_bundle_token_minter,
        require_bundle_ensurer,
    }
)

# The ONLY intentional exemption. ``worker_management`` calls this on the
# trusted network with NO token, to enforce byo_required on S2S worker runs
# that carry no license token; the middleware exempts it explicitly
# (auth/middleware.py dispatch()). It exposes one non-secret entitlement flag.
# Anything else appearing here is a bug, not a new exemption.
TOKENLESS_S2S_ALLOWLIST = frozenset({("GET", "/tenants/{name_or_id}/byo-mode")})


def _has_role_gate(dependant) -> bool:
    """True if this route's dependency tree contains a role checker (recursive)."""
    for sub in dependant.dependencies:
        call = sub.call
        if call in ROLE_GATE_FUNCTIONS:
            return True
        qualname = getattr(call, "__qualname__", "")
        if any(qualname.startswith(p) for p in ROLE_GATE_QUALNAME_PREFIXES):
            return True
        if _has_role_gate(sub):
            return True
    return False


def _methods(route: APIRoute) -> set[str]:
    """``APIRoute.methods`` is typed ``set[str] | None``; normalise it."""
    return set(route.methods or ())


def _protected_api_routes() -> list[APIRoute]:
    routes: list[APIRoute] = []
    for route in main.app.routes:
        if not isinstance(route, APIRoute):
            continue
        path = route.path.rstrip("/") or "/"
        if path in PUBLIC_ENDPOINTS:
            continue
        if not any(path.startswith(prefix) for prefix in PROTECTED_PREFIXES):
            continue
        routes.append(route)
    return routes


class TestProtectedRoutesHaveRoleGates:
    def test_the_walk_is_not_vacuous(self):
        """A passing gate over an EMPTY route set is the classic false green."""
        routes = _protected_api_routes()
        assert len(routes) >= 30, f"only {len(routes)} protected routes found — walk is broken"

    def test_detector_actually_detects(self):
        """Prove ``_has_role_gate`` can say NO, on a route built to have no gate.

        Without this, a detector that always returned True would make the whole
        module vacuously green.
        """
        probe = FastAPI()

        @probe.get("/licenses/ungated")
        async def _ungated():  # pragma: no cover - never called
            return {}

        @probe.get("/licenses/gated")
        async def _gated(user=Depends(require_role("license-admin"))):  # pragma: no cover
            return {}

        by_path = {r.path: r for r in probe.routes if isinstance(r, APIRoute)}
        assert _has_role_gate(by_path["/licenses/ungated"].dependant) is False
        assert _has_role_gate(by_path["/licenses/gated"].dependant) is True

    def test_detector_sees_require_any_role_and_internal_service(self):
        """The other two gate shapes must also be recognised, or the invariant
        would pass by mistakenly treating a real gate as absent-but-allowlisted."""
        probe = FastAPI()

        @probe.get("/licenses/any")
        async def _any(user=Depends(require_any_role("license-admin", "license-reader"))):
            return {}  # pragma: no cover

        @probe.get("/internal/svc")
        async def _svc(user=Depends(require_internal_service)):
            return {}  # pragma: no cover

        by_path = {r.path: r for r in probe.routes if isinstance(r, APIRoute)}
        assert _has_role_gate(by_path["/licenses/any"].dependant) is True
        assert _has_role_gate(by_path["/internal/svc"].dependant) is True

    @pytest.mark.parametrize(
        ("method", "path"),
        sorted(
            (method, route.path)
            for route in _protected_api_routes()
            for method in _methods(route)
            if method not in {"HEAD", "OPTIONS"}
        ),
    )
    def test_route_has_role_gate(self, method: str, path: str):
        """Every protected route needs a role dependency, or an explicit exemption."""
        route = next(r for r in _protected_api_routes() if r.path == path and method in _methods(r))
        if (method, path) in TOKENLESS_S2S_ALLOWLIST:
            assert not _has_role_gate(route.dependant), (
                f"{method} {path} is on the tokenless-S2S allowlist but now HAS a role "
                "gate — remove it from TOKENLESS_S2S_ALLOWLIST instead of leaving a "
                "stale exemption behind."
            )
            return
        assert _has_role_gate(route.dependant), (
            f"{method} {path} is under a PROTECTED_PREFIX but has no role dependency. "
            "Add require_role/require_any_role/require_internal_service, or — only if "
            "it is a deliberate tokenless S2S endpoint — add it to "
            "TOKENLESS_S2S_ALLOWLIST with a justification."
        )

    def test_allowlist_entries_still_exist(self):
        """A stale allowlist entry silently exempts nothing and hides drift."""
        live = {(m, r.path) for r in _protected_api_routes() for m in _methods(r)}
        for entry in TOKENLESS_S2S_ALLOWLIST:
            assert entry in live, f"allowlisted route {entry} no longer exists — drop it"
