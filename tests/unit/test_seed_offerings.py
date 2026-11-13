"""Unit guards for the commercial offering seed (``scripts/seed_offerings.py``).

Pure-unit: loads the seed module's ``OFFERINGS`` list by file path and asserts
its shape. No DB — the idempotent upsert path is exercised by the offering
provisioning suite. These guards pin the KAG-pipeline bundle's commercial policy
so a bad edit (a $-carrying access fee, a $0-risking byo_mode, a missing stage)
is caught before it ships to the release Job.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

_SEED = Path(__file__).resolve().parents[2] / "scripts" / "seed_offerings.py"

_KAG_STAGES = {"ingestion", "karma", "hirag", "kag-graph"}


def _load_offerings() -> list[dict[str, Any]]:
    spec = importlib.util.spec_from_file_location("seed_offerings", _SEED)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.OFFERINGS


def test_offering_app_code_pairs_are_unique() -> None:
    pairs = [(o["app_key"], o["code"]) for o in _load_offerings()]
    assert len(pairs) == len(set(pairs)), f"duplicate (app_key, code) in seed: {pairs}"


def test_kag_pipeline_covers_all_four_stages() -> None:
    """The bundle must span exactly the four metered KAG stages under one code."""
    kag = [o for o in _load_offerings() if o["code"] == "kag-pipeline"]
    assert {o["app_key"] for o in kag} == _KAG_STAGES, (
        f"kag-pipeline must cover {_KAG_STAGES}, got {[o['app_key'] for o in kag]}"
    )


def test_kag_pipeline_is_pure_usage_with_markup() -> None:
    """Pure-usage bundle (George 2026-07-09): markup only, no access fee/allowance.

    ``byo_mode`` MUST be ``byo_required`` — the revenue-safe commercial posture;
    ``platform_only``/``local_allowed`` live in billing's internal/$0 set and
    would risk a $0 bill for a paying offering.

    The ``kag`` precondition is load-bearing: every assertion below sits inside
    a loop that filters on ``code == "kag-pipeline"``, so renaming or dropping
    the bundle empties the iterable and this test reports GREEN having checked
    no commercial policy at all.
    """
    kag = [o for o in _load_offerings() if o["code"] == "kag-pipeline"]
    assert len(kag) == len(_KAG_STAGES), (
        f"expected one kag-pipeline offering per stage {_KAG_STAGES}, got {len(kag)}"
    )
    for o in kag:
        assert o["byo_mode"] == "byo_required", (
            f"{o['app_key']}: paying offering must be byo_required, got {o['byo_mode']!r}"
        )
        assert o.get("usage", {}).get("markup_pct") == 30, f"{o['app_key']}: KAG markup must be 30"
        # Pure usage: no access charge, no included allowance.
        assert o.get("access_price_usd") is None, f"{o['app_key']}: expected no access fee"
        assert o.get("included_allowance_usd") is None, f"{o['app_key']}: expected no allowance"
        assert o.get("name"), f"{o['app_key']}: offering missing name"


def test_strata_apps_are_byo_required_with_no_platform_paid_offering() -> None:
    """Migration 067: care-bus is BYO-only (strata-configurator retired by 084).

    Exactly one offering each, ``byo_required``, no access fee — and no offering of
    either app in a platform-paid posture (``platform_only`` / ``local_allowed``).
    """
    assert not [o for o in _load_offerings() if o["app_key"] == "strata-configurator"]  # 084
    for key in ("care-bus", "agent"):  # 070: agent, same posture
        rows = [o for o in _load_offerings() if o["app_key"] == key]
        assert len(rows) == 1, (key, rows)
        assert rows[0]["byo_mode"] == "byo_required", rows[0]
        assert rows[0].get("access_price_usd") is None, rows[0]
