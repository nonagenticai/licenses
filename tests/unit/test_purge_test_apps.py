"""Unit guards for the catalog purge classifier (``scripts/purge_test_apps.py``).

Pure-unit: exercises ``classify()`` / ``JUNK_RE`` only — no DB, no deletes. The
classifier IS the safety property of that script (a hard cascade to plans,
licenses and tokens hangs off its verdict), so the interesting cases are the
ones where it must REFUSE, not the ones where it deletes.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

_PURGE = Path(__file__).resolve().parents[2] / "scripts" / "purge_test_apps.py"


def _mod() -> Any:
    spec = importlib.util.spec_from_file_location("purge_test_apps", _PURGE)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_real_prod_shapes_classify_as_junk() -> None:
    """The key shapes actually observed on prod, 2026-07-17."""
    m = _mod()
    for key in (
        "inttest-018c644f",
        "inttest-{uuid}",  # literal unformatted f-string, really in prod
        "inttest-4f3ffe15.hex[:8]}",  # ditto
        "demo-app",
        "demo-app-1771072269533",
        "test-app-delete-me",
        "dbg2-06d82f",
        "dbg3-307258",
        "debug-149eef5a",
        "probe-app-del-me",
    ):
        assert m.JUNK_RE.match(key), f"{key!r} should classify as junk"


def test_real_app_keys_never_match_the_junk_pattern() -> None:
    """A false positive here is an irreversible cascade on a real app."""
    m = _mod()
    for key in (
        "fr",
        "fr-frontend",
        "w2v",
        "w2v-image",
        "signal",
        "commonpractice",
        "artifacts",
        "coding",
        "kag",
        "kag-graph",
        "kag-orchestrator",
        "kanban",
        "karma",
        "hirag",
        "ingestion",
        "licenses",
        "mcp",
        "workers",
        "secrets",
        "tools",
        "procedures",
        "requirements",
        "skill-builder",
        "skill_builder",
        "storage",
        "npm",
        "vllm",
        "laya",
        "nlp-service",
        "graph-adapter",
        "keycloak",
        "n8n",
        "n8n-gerard",
        "metering",
        "marketing",
        "sales",
        "observability",
        "book-fulfillment",
        "raise-my-tickets",
        "procedure_management",
        "kaizen_core_actions",
        "meta_prompt_core_actions",
        "data-collector",
        "model-registry",
        "model-evaluator",
        "training-data-generator",
        "improvement-loop",
        "inference-router",
    ):
        assert not m.JUNK_RE.match(key), f"{key!r} must NOT classify as junk"


def test_keep_always_wins_over_the_junk_pattern() -> None:
    """Allowlist-first: a KEEP key is never deleted even if it matches JUNK_RE.

    Guards the ordering in classify(). If a real app were ever named e.g.
    `test-harness`, KEEP must still win.
    """
    m = _mod()
    kept, junk, unknown = m.classify(["test-harness"], {"test-harness"})
    assert kept == ["test-harness"]
    assert junk == [] and unknown == []


def test_unrecognised_key_is_unknown_not_junk() -> None:
    """An app that is neither known-real nor known-junk must stop the run.

    This is the bucket that protects a NEW real app someone forgot to seed:
    it must never fall through into the delete set.
    """
    m = _mod()
    kept, junk, unknown = m.classify(["some-brand-new-service"], {"fr"})
    assert unknown == ["some-brand-new-service"]
    assert junk == [] and kept == []


def test_classify_splits_a_mixed_catalog() -> None:
    m = _mod()
    kept, junk, unknown = m.classify(
        ["fr", "inttest-abc", "demo-app-1", "mystery-app", "kag"], {"fr", "kag"}
    )
    assert kept == ["fr", "kag"]
    assert junk == ["demo-app-1", "inttest-abc"]
    assert unknown == ["mystery-app"]


def test_keep_extra_covers_every_real_unseeded_prod_app() -> None:
    """KEEP_EXTRA + the seed must together cover every real app on prod.

    Anything missing classifies UNKNOWN and aborts the purge (safe, but noisy);
    the point of this test is to catch that at CI time instead of mid-run.
    """
    m = _mod()
    keep = m._seeded_keys() | m.KEEP_EXTRA
    real_prod_apps = {
        "artifacts",
        "book-fulfillment",
        "coding",
        "commonpractice",
        "fr",
        "fr-frontend",
        "hirag",
        "ingestion",
        "kag",
        "kag-graph",
        "kaizen_core_actions",
        "kaizen_discover_actions",
        "kanban",
        "karma",
        "keycloak",
        "licenses",
        "marketing",
        "mcp",
        "meta_prompt_core_actions",
        "meta_prompt_discover_actions",
        "metering",
        "n8n",
        "n8n-gerard",
        "observability",
        "procedure_management",
        "procedures",
        "raise-my-tickets",
        "requirements",
        "sales",
        "secrets",
        "signal",
        "skill_builder",
        "tools",
        "w2v",
        "w2v-image",
        "workers",
    }
    missing = sorted(real_prod_apps - keep)
    assert not missing, f"real prod app(s) not covered by keep-list: {missing}"
