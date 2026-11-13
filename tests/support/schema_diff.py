"""Compare two schema snapshots produced by ``scripts/dump_schema.py``.

Deliberately pytest-free and filesystem-free — the same shape as
``tests/support/openapi_drift.py`` — so the gate can prove its discriminating
power against SYNTHETIC snapshots, not only against the live pair. A gate cannot
demonstrate that it detects a divergence by running against a corpus that has
none.

A "divergence" is one atomic disagreement: one missing object, or one field of
one object differing. Reports are stable-sorted so a pinned count and a committed
snapshot cannot disagree about ordering.
"""

from __future__ import annotations

from typing import Any

# The snapshot keys that are plain lists of names.
LIST_KINDS = ("tables", "sequences", "extensions")
# The snapshot keys that map "owner.name" -> {field: value}.
MAPPING_KINDS = ("columns", "indexes", "constraints")

ALL_KINDS = LIST_KINDS + MAPPING_KINDS

# Not a schema object; carried by the corpus arm for provenance only.
_PROVENANCE = "_provenance"


def divergence_report(orm: dict[str, Any], corpus: dict[str, Any]) -> list[str]:
    """Every way the two arms disagree, one string per atomic difference.

    ``orm`` is what a FRESH database gets; ``corpus`` is what the migration
    corpus says the schema is. Direction is preserved in the message, because
    "the ORM is missing it" and "the corpus is missing it" call for opposite
    fixes.
    """
    out: list[str] = []

    for kind in LIST_KINDS:
        left, right = set(orm.get(kind, [])), set(corpus.get(kind, []))
        for name in sorted(left - right):
            out.append(
                f"{kind}: {name!r} in ORM only (a fresh database has it; the corpus does not)"
            )
        for name in sorted(right - left):
            out.append(
                f"{kind}: {name!r} in CORPUS only (the corpus has it; a fresh database does NOT)"
            )

    for kind in MAPPING_KINDS:
        left_map: dict = orm.get(kind, {})
        right_map: dict = corpus.get(kind, {})
        for key in sorted(set(left_map) | set(right_map)):
            if key not in right_map:
                out.append(
                    f"{kind}: {key!r} in ORM only (a fresh database has it; the corpus does not)"
                )
            elif key not in left_map:
                out.append(
                    f"{kind}: {key!r} in CORPUS only (the corpus has it; a fresh database does NOT)"
                )
            else:
                lhs, rhs = left_map[key], right_map[key]
                for field in sorted(set(lhs) | set(rhs)):
                    lv, rv = lhs.get(field), rhs.get(field)
                    if lv != rv:
                        out.append(f"{kind}: {key!r}.{field} ORM={lv!r} CORPUS={rv!r}")

    return out


def snapshot_object_counts(snapshot: dict[str, Any]) -> dict[str, int]:
    """Object counts per kind — the input to the gate's vacuity floor.

    Exact pins are built on this, not ``>=`` floors: a floor cannot catch the
    drop it exists for (set it to 0 and everything stays green).
    """
    return {kind: len(snapshot.get(kind, [])) for kind in ALL_KINDS}


def comparable(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Strip non-schema bookkeeping so two snapshots can be compared directly."""
    return {k: v for k, v in snapshot.items() if k != _PROVENANCE}
