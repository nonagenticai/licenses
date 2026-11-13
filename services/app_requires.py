"""App dependencies (migration 059): the transitive closure of ``app_requires``.

A bundle lists what is SOLD. An ``app_requires`` edge ``A -> B`` says app A calls
app B on the tenant's behalf, and the owner ``caller`` mints B's bundle token for
it. Ensuring a bundle provisions every app reachable from its members as a
DEPENDENCY licence (one per tenant x app, shared across bundles) and grants each
edge's ``caller`` on the tenant. The bundle-token route uses the same closure to
let a held bundle mint a dependency app.

Pure functions over an edge list: the table is tiny (one row per evidenced call
edge), so callers read it whole (``DatabaseManager.list_app_requirements``).

⛔ A CYCLE IS REFUSED, NEVER WALKED. ``app_requires`` has no structural guard
against ``A -> B -> A`` (only ``ck_app_requires_not_self``), so the walk keeps the
current path and raises ``DependencyCycleError`` naming it. Ensure turns that into
a 409 with nothing written; it never loops.
"""

from __future__ import annotations

from collections.abc import Iterable

from models import ClientSafeError


class DependencyCycleError(ClientSafeError):
    """``app_requires`` holds a cycle reachable from the apps being resolved."""

    def __init__(self, path: list[str]) -> None:
        self.path = path
        super().__init__("app_requires cycle: " + " -> ".join(path))


class Edge:
    """One ``app_requires`` row: ``app`` requires ``required``, minted by ``caller``."""

    __slots__ = ("app", "caller", "required")

    def __init__(self, app: str, required: str, caller: str) -> None:
        self.app = app
        self.required = required
        self.caller = caller

    def __repr__(self) -> str:
        return f"Edge({self.app!r} -> {self.required!r} by {self.caller!r})"


def _adjacency(edges: Iterable[Edge]) -> dict[str, list[Edge]]:
    out: dict[str, list[Edge]] = {}
    for e in edges:
        out.setdefault(e.app, []).append(e)
    for v in out.values():
        v.sort(key=lambda e: e.required)
    return out


def closure(roots: Iterable[str], edges: Iterable[Edge]) -> tuple[list[str], list[Edge]]:
    """Every app reachable from ``roots`` (excluding the roots) and the edges walked.

    Returns ``(dependencies, edges_walked)``, both in a deterministic order. Raises
    ``DependencyCycleError`` when a cycle is reachable from ``roots``.
    """
    adj = _adjacency(edges)
    root_set = set(roots)
    done: set[str] = set()
    deps: list[str] = []
    walked: list[Edge] = []

    def visit(app: str, path: list[str]) -> None:
        if app in path:
            raise DependencyCycleError([*path[path.index(app) :], app])
        if app in done:
            return
        path.append(app)
        for e in adj.get(app, []):
            walked.append(e)
            visit(e.required, path)
            if e.required not in root_set and e.required not in deps:
                deps.append(e.required)
        path.pop()
        done.add(app)

    for r in sorted(root_set):
        visit(r, [])
    return deps, walked
