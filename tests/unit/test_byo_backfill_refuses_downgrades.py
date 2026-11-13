"""The backfill must never silently REMOVE an entitlement a tenant is using today.

`classify()` is keyed on hardcoded allowlists in `scripts/backfill_tenants_byo_mode.py`, and that
file has forgotten an owner-internal tenant SIX times -- `e148e8af`, the `svc-*` block,
`svc-cdmsfirst-n8n`, `fr-test` (#51 pinned `019efaa0` and missed the operative
`0195f48b-...-099`), and the two merqi GTM tenants (#163). Each omission is invisible until
something 402s `byo_llm_not_configured`.

The mirror image is worse, and it was LIVE on 2026-08-21: a tenant whose row someone had already
fixed out of band to `local_allowed`, but which is still unlisted here, is computed as
`byo_required` -- so the next `--apply` silently REVERTS the fix and takes the tenant down again,
reporting success. The prod dry-run that day showed exactly two such pending reverts (`ahtapol`
and `svc-tts-synth`), which is what makes `--apply` an all-or-nothing gamble rather than a tool.

These pin the refusal, and pin that it can still be overridden deliberately.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backfill_tenants_byo_mode.py"
_spec = importlib.util.spec_from_file_location("backfill_byo", _SCRIPT)
assert _spec is not None and _spec.loader is not None
backfill = importlib.util.module_from_spec(_spec)
# ⛔ REGISTER BEFORE EXEC. The script under test defines a frozen @dataclass, and
# `dataclasses._is_type` resolves the decorated class's own module via
# `sys.modules.get(cls.__module__).__dict__`. A module built by `module_from_spec`
# is NOT in `sys.modules` until someone puts it there, so that lookup returns None
# and this file dies at COLLECTION with
# `AttributeError: 'NoneType' object has no attribute '__dict__'` — which reads as a
# broken test file rather than a loader mistake.
sys.modules[_spec.name] = backfill
_spec.loader.exec_module(backfill)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("local_allowed", "byo_required"),
        ("platform_only", "byo_required"),
        ("platform_only", "local_allowed"),
    ],
)
def test_removing_entitlement_is_a_downgrade(current: str, target: str) -> None:
    assert backfill.is_downgrade(current, target) is True


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("byo_required", "local_allowed"),
        ("byo_required", "platform_only"),
        ("local_allowed", "platform_only"),
    ],
)
def test_granting_entitlement_is_not_a_downgrade(current: str, target: str) -> None:
    # The positive twin: if everything counted as a downgrade the guard would block the fixes it
    # exists to let through, and #163's whole purpose -- byo_required -> local_allowed -- is here.
    assert backfill.is_downgrade(current, target) is False


@pytest.mark.parametrize("mode", ["byo_required", "local_allowed", "platform_only"])
def test_a_no_op_is_not_a_downgrade(mode: str) -> None:
    assert backfill.is_downgrade(mode, mode) is False


def test_an_unknown_mode_is_treated_as_a_downgrade() -> None:
    """Refusing is recoverable; a silent revert is not.

    A mode this script does not recognise -- a new enum value, or a corrupted row -- must not be
    quietly overwritten just because it is unranked.
    """
    assert backfill.is_downgrade("some_future_mode", "byo_required") is True
    assert backfill.is_downgrade("local_allowed", "some_future_mode") is True


def test_the_ordering_covers_every_mode_classify_can_return() -> None:
    """Guard the guard.

    ``is_downgrade`` degrades to "treat as a downgrade" for anything unranked. That is the right
    failure direction, but if a real mode were missing from ``_PERMISSIVENESS`` the guard would
    refuse legitimate work forever and read as a broken script rather than a missing entry.
    """
    returnable = {"byo_required", "local_allowed", "platform_only"}
    assert returnable <= set(backfill._PERMISSIVENESS)
