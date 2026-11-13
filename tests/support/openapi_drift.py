"""Pure comparison helpers behind the OpenAPI drift gate.

Kept free of pytest, filesystem access and app imports on purpose: the gate in
``tests/unit/test_openapi_drift.py`` exercises these two functions on synthetic
input to prove it can actually *see* a change, which is impossible to
demonstrate against a corpus that happens to be clean.

``drift_report(expected, live) == []`` means "no drift". The comparison is a
DEEP one — a path-set comparison would wave through a renamed ``operationId``
or a field whose type changed, which are exactly the edits that break a
generated client while leaving the URL list identical.
"""

from __future__ import annotations

import json
from typing import Any

# Fields that legitimately differ between two honest exports of the same code,
# because main.py reads them from the environment at import time (SERVICE_NAME,
# VERSION -> info.title, info.version). Without stripping these the gate is
# permanently red for anyone whose env differs from whoever last regenerated the
# snapshot.
#
# This list is kept as SHORT as it can possibly be. Anything stripped here is a
# field the gate can no longer see, and that is not theoretical: the
# X-Tenant-Id parameter's `schema.default`/`example` used to be stripped, which
# left the published contract free to document a real tenant id with nothing to
# catch it. The fix was to stop publishing the value, not to keep ignoring it —
# so that parameter is now compared in full.
_VOLATILE_INFO_FIELDS = ("version", "title")

#: How many pointer-level differences drift_report spells out before truncating.
_MAX_POINTER_LINES = 40


def strip_volatile(spec: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of ``spec`` with environment-derived fields removed.

    Removes exactly two fields — ``info.version`` and ``info.title`` — which
    ``main.py`` reads from ``VERSION`` / ``SERVICE_NAME`` at import time.
    Everything else in the document is compared.

    Deliberately narrow: anything stripped here is a difference the gate can no
    longer see, so the accompanying vacuity floor asserts that paths and
    operationIds survive this function intact.
    """
    stripped: dict[str, Any] = json.loads(json.dumps(spec))

    info = stripped.get("info")
    if isinstance(info, dict):
        for field in _VOLATILE_INFO_FIELDS:
            info.pop(field, None)

    return stripped


def _short(value: Any) -> str:
    text = json.dumps(value, sort_keys=True) if not isinstance(value, str) else repr(value)
    return text if len(text) <= 90 else text[:87] + "..."


def _common_prefix_len(left: str, right: str) -> int:
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return index


def _scalar_diff(expected: Any, live: Any, pointer: str) -> str:
    """Describe two differing scalars, trimming a shared prefix.

    Docstrings are the most-edited part of this spec and routinely share their
    first hundred characters; without the trim both sides truncate to the same
    text and the line says nothing.
    """
    if isinstance(expected, str) and isinstance(live, str):
        shared = _common_prefix_len(expected, live)
        if shared >= 20:
            return (
                f"{pointer}: diverges at char {shared}: "
                f"snapshot ...{_short(expected[shared:])}, code ...{_short(live[shared:])}"
            )
    return f"{pointer}: snapshot has {_short(expected)}, code has {_short(live)}"


def _pointer_diffs(expected: Any, live: Any, pointer: str, skip: frozenset[str]) -> list[str]:
    """Recursively describe every difference as a JSON-pointer-ish line."""
    if pointer in skip:
        return []
    if expected == live:
        return []

    lines: list[str] = []
    if isinstance(expected, dict) and isinstance(live, dict):
        for key in sorted(set(live) - set(expected)):
            child = f"{pointer}/{key}"
            if child not in skip:
                lines.append(f"{child}: absent from snapshot, code has {_short(live[key])}")
        for key in sorted(set(expected) - set(live)):
            child = f"{pointer}/{key}"
            if child not in skip:
                # Echo the value: for a field the code no longer emits, WHAT the
                # snapshot still publishes is the whole point (e.g. a stale
                # X-Tenant-Id default naming a real tenant).
                lines.append(
                    f"{child}: present in snapshot ({_short(expected[key])}), absent from code"
                )
        for key in sorted(set(expected) & set(live)):
            lines.extend(_pointer_diffs(expected[key], live[key], f"{pointer}/{key}", skip))
        return lines

    if isinstance(expected, list) and isinstance(live, list):
        if len(expected) != len(live):
            lines.append(f"{pointer}: snapshot has {len(expected)} items, code has {len(live)}")
            return lines
        for index, (exp_item, live_item) in enumerate(zip(expected, live, strict=True)):
            lines.extend(_pointer_diffs(exp_item, live_item, f"{pointer}/{index}", skip))
        return lines

    lines.append(_scalar_diff(expected, live, pointer))
    return lines


def drift_report(expected: dict[str, Any], live: dict[str, Any]) -> list[str]:
    """Describe how the committed snapshot (``expected``) differs from the code (``live``).

    Returns an empty list -- and only an empty list -- when the two specs are
    equal after :func:`strip_volatile`.
    """
    expected = strip_volatile(expected)
    live = strip_volatile(live)
    if expected == live:
        return []

    expected_paths = expected.get("paths", {})
    live_paths = live.get("paths", {})
    added = sorted(set(live_paths) - set(expected_paths))
    removed = sorted(set(expected_paths) - set(live_paths))

    lines: list[str] = []
    if added:
        lines.append(f"Routes added by code, missing from snapshot ({len(added)}):")
        lines.extend(f"    + {path}" for path in added)
    if removed:
        lines.append(f"Routes in snapshot, missing from code ({len(removed)}):")
        lines.extend(f"    - {path}" for path in removed)

    # Whole added/removed paths are already summarised above; suppress their
    # (very noisy) per-field expansion so the pointer budget is spent on the
    # differences that a path-set comparison would have missed entirely.
    skip = frozenset(f"/paths/{path}" for path in [*added, *removed])
    pointer_lines = _pointer_diffs(expected, live, "", skip)
    if pointer_lines:
        lines.append(f"Field-level differences ({len(pointer_lines)}):")
        lines.extend(f"    {line}" for line in pointer_lines[:_MAX_POINTER_LINES])
        if len(pointer_lines) > _MAX_POINTER_LINES:
            lines.append(f"    ... and {len(pointer_lines) - _MAX_POINTER_LINES} more")

    if not lines:
        # Unreachable via _pointer_diffs (it reports every inequality), but a
        # report that is empty for unequal specs would silently disarm the gate,
        # so never return [] on this branch.
        lines.append("Specs differ but no field-level difference could be attributed.")
    return lines
