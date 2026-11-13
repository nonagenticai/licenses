"""``services.app_requires.closure`` — the transitive closure of app dependencies (059).

Pure: no database. Pins the walk ensure and the bundle-token route both use:
* the closure excludes the roots and is deterministic;
* a dependency reached twice (diamond) appears once, its edges are walked once;
* a CYCLE reachable from the roots is REFUSED with the path named — never walked
  (the test has a timeout, so a loop would fail, not hang);
* a cycle NOT reachable from the roots does not affect them;
* the seeded edges (models.APP_REQUIRES_SEED) are acyclic and give the closures the
  PLAN names: w2v -> {kag, workers, inference-router, mlx-*}; conveyor's apps ->
  {inference-router, mlx-backend, mlx-ingestion-backend}.
"""

from __future__ import annotations

import pytest

from models import APP_REQUIRES_SEED
from services.app_requires import DependencyCycleError, Edge, closure

SEED = [Edge(a, r, c) for a, r, c in APP_REQUIRES_SEED]
CONVEYOR = ["workers", "coding", "laya", "kag", "requirements", "procedures"]


def test_chain_excludes_roots():
    deps, walked = closure(["a"], [Edge("a", "b", "x"), Edge("b", "c", "y")])
    assert deps == ["c", "b"]
    assert [(e.app, e.required, e.caller) for e in walked] == [("a", "b", "x"), ("b", "c", "y")]


def test_a_root_reached_from_another_root_is_not_a_dependency():
    deps, walked = closure(["a", "b"], [Edge("a", "b", "x"), Edge("b", "c", "y")])
    assert deps == ["c"]
    assert {(e.app, e.required) for e in walked} == {("a", "b"), ("b", "c")}


def test_diamond_lists_each_dependency_and_edge_once():
    edges = [Edge("a", "b", "x"), Edge("a", "c", "x"), Edge("b", "d", "y"), Edge("c", "d", "z")]
    deps, walked = closure(["a"], edges)
    assert sorted(deps) == ["b", "c", "d"] and len(deps) == 3
    assert len(walked) == 4


@pytest.mark.timeout(5)
@pytest.mark.parametrize(
    ("edges", "cycle"),
    [
        ([Edge("a", "b", "x"), Edge("b", "a", "x")], ["a", "b", "a"]),
        (
            [Edge("r", "a", "x"), Edge("a", "b", "x"), Edge("b", "c", "x"), Edge("c", "a", "x")],
            ["a", "b", "c", "a"],
        ),
    ],
)
def test_a_reachable_cycle_is_refused_with_its_path(edges, cycle):
    root = edges[0].app
    with pytest.raises(DependencyCycleError) as exc:
        closure([root], edges)
    assert exc.value.path == cycle
    assert " -> ".join(cycle) in str(exc.value)


def test_an_unreachable_cycle_does_not_affect_the_roots():
    edges = [Edge("a", "b", "x"), Edge("p", "q", "x"), Edge("q", "p", "x")]
    assert closure(["a"], edges)[0] == ["b"]


def test_no_edges_no_dependencies():
    assert closure(["a"], []) == ([], [])


def test_seed_is_acyclic_and_gives_the_planned_closures():
    every_app = {e.app for e in SEED} | {e.required for e in SEED}
    closure(sorted(every_app), SEED)  # raises on any cycle
    w2v, _ = closure(["w2v"], SEED)
    # 062: kag -> evidence-review reaches every closure that holds kag.
    assert set(w2v) == {
        "kag",
        "evidence-review",
        "workers",
        "inference-router",
        "mlx-backend",
        "mlx-ingestion-backend",
    }
    conveyor, walked = closure(CONVEYOR, SEED)
    assert set(conveyor) == {
        "inference-router",
        "mlx-backend",
        "mlx-ingestion-backend",
        "evidence-review",
    }
    assert {e.caller for e in walked} == {"kag", "platform"}
    for app in ("marketing", "sales"):
        assert set(closure([app], SEED)[0]) == {"workers", "inference-router"}
