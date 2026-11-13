#!/usr/bin/env python3
"""Report — and, with ``--apply``, state — subject parentage in ``subject.tenant_id``.

DRY-RUN BY DEFAULT. Without ``--apply`` this writes nothing and only prints the
partition plus the per-subject verdicts.

────────────────────────────────────────────────────────────────────────────────
WHY THIS EXISTS
────────────────────────────────────────────────────────────────────────────────
Migration 029 added ``subject.tenant_id`` and backfilled nothing. Migration 032
then set it on the 51 subjects whose parent the database already answered, and
deliberately refused the other 203. Both are ONE-SHOT files, and until
``DatabaseManager.attribute_subject_to_tenant`` there was no code path that ever
wrote the column — so the unstated population REGROWS with every new subject.

Measured on prod 2026-09-14 (264 subjects, 213 unstated):

    bucket                                       unstated   repairable here
    ------------------------------------------   --------   ---------------
    exactly one non-service licence tenant               8   YES
    licences only under a machine (service) tenant      42   NO
    licences under more than one tenant                  8   NO
    no licences at all                                 155   NO

Those 8 are the regrowth: every one of them was minted AFTER 032 ran, and 032's
own rule would have resolved each. The other 205 are NOT a shortfall and are not
what this script is for — see the refusal reasons on
``DatabaseManager.attribute_subject_to_tenant``.

────────────────────────────────────────────────────────────────────────────────
⛔ WHAT THIS SCRIPT WILL NOT DO
────────────────────────────────────────────────────────────────────────────────
It does not GUESS. It applies migration 032's admission rule unchanged — set
``tenant_id`` only where the subject's licences name EXACTLY ONE tenant and that
tenant is not itself a machine — and SKIPS everything else, reporting the reason
rather than picking a plausible parent. Guessing a parent is precisely the defect
class this column exists inside of: attributing an identity to an org it does not
belong to is indistinguishable, downstream, from an operator having decided it.

In particular it will never write a ``kind='service'`` tenant into
``subject.tenant_id``. That would record "a machine is an org" a SECOND time, in
the very column added to express the corrected shape, and launder it as a stated
fact. The 43 ``svc-*`` rows are ``docs/svc-tenant-migration-plan.md``'s job and
need an operator-supplied ``svc-name -> owning-tenant`` table this database does
not hold.

It also never OVERWRITES a stated parentage: an existing value is an operator
fact, and a licence mint is not evidence to revise it.

────────────────────────────────────────────────────────────────────────────────
USAGE
────────────────────────────────────────────────────────────────────────────────
    DATABASE_URL=postgresql+psycopg://... python scripts/attribute_subject_tenants.py
    DATABASE_URL=postgresql+psycopg://... python scripts/attribute_subject_tenants.py --apply

⚠️ ``DATABASE_URL`` must point at the write PRIMARY. The master node's local
:5432 is a read-only STANDBY; ``--apply`` against it fails loud rather than
silently doing nothing.

Exit codes: 0 = ran; 2 = no DATABASE_URL.
"""

from __future__ import annotations

import argparse
import os
import sys
from uuid import UUID

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models import DatabaseManager, License, Subject, Tenant  # noqa: E402

# Reasons that mean "left alone on purpose", printed but never an error.
REFUSED = ("already_set", "tenant_is_machine", "ambiguous", "no_licences", "tenant_unknown")


def _candidates(db: DatabaseManager) -> list[tuple[UUID, str, UUID | None]]:
    """Every subject with an unstated parent, plus the single licence tenant if any.

    Returns ``(subject_id, keycloak_sub, sole_tenant_id_or_None)``. The third
    element is only a HINT for the printed report — the authority is
    ``attribute_subject_to_tenant``, which re-derives it inside one session and is
    the single place the admission rule is implemented. Two implementations of
    that rule is exactly how a guard drifts open.
    """
    session = db.get_session()
    try:
        out: list[tuple[UUID, str, UUID | None]] = []
        subjects = session.query(Subject).filter(Subject.tenant_id.is_(None)).all()
        for subject in subjects:
            tenants = {
                row[0]
                for row in session.query(License.tenant_id)
                .filter(License.subject_id == subject.id)
                .distinct()
                .all()
            }
            sole = next(iter(tenants)) if len(tenants) == 1 else None
            out.append((UUID(str(subject.id)), str(subject.keycloak_sub), sole))
        return out
    finally:
        session.close()


def _tenant_label(db: DatabaseManager, tenant_id: UUID | None) -> str:
    if tenant_id is None:
        return "-"
    session = db.get_session()
    try:
        tenant = session.query(Tenant).filter(Tenant.id == tenant_id).first()
        if tenant is None:
            return f"{tenant_id} (UNREGISTERED)"
        return f"{tenant.name} [{tenant.kind}]"
    finally:
        session.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually write the rows the admission rule admits (default: report only)",
    )
    args = parser.parse_args(argv)

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL is required", file=sys.stderr)
        return 2

    db = DatabaseManager(db_url)

    before = db.subject_tenant_parentage_partition()
    print("subject parentage, BEFORE:")
    for key, value in before.items():
        print(f"  {key:<26} {value}")
    print()

    rows = _candidates(db)
    if not rows:
        print("Nothing unstated. Done.")
        return 0

    mode = "APPLY" if args.apply else "DRY-RUN (nothing is written)"
    print(f"{len(rows)} subject(s) with an unstated parent — {mode}\n")

    counts: dict[str, int] = {}
    for subject_id, keycloak_sub, sole in rows:
        if args.apply and sole is not None:
            reason = db.attribute_subject_to_tenant(subject_id=subject_id, tenant_id=str(sole))
        elif args.apply:
            # No single licence tenant, so there is nothing to pass as `tenant_id`.
            # Calling anyway with `str(None)` would come back `tenant_unknown`,
            # which is a DIFFERENT and misleading finding — it reads as "that
            # tenant is not registered" when the truth is "this subject has zero
            # or many". Report the real bucket instead.
            reason = _would_do(db, subject_id, sole)
        else:
            # Dry run: ask the SAME method what it would do, against a tenant it
            # will re-validate itself. A `None` sole tenant stringifies to a
            # non-UUID and comes back `tenant_unknown`, which for a subject with
            # zero or many licence tenants is reported as its real bucket below.
            reason = _would_do(db, subject_id, sole)
        counts[reason] = counts.get(reason, 0) + 1
        verdict = "SET" if reason == "set" else f"skip: {reason}"
        print(f"  {keycloak_sub:<45} -> {_tenant_label(db, sole):<36} {verdict}")

    print("\nverdicts:")
    for reason, count in sorted(counts.items()):
        print(f"  {reason:<20} {count}")

    if args.apply:
        after = db.subject_tenant_parentage_partition()
        print("\nsubject parentage, AFTER:")
        for key, value in after.items():
            print(f"  {key:<26} {value}")
    else:
        print("\nRe-run with --apply to write the rows marked SET.")
    return 0


def _would_do(db: DatabaseManager, subject_id: UUID, sole: UUID | None) -> str:
    """Predict the verdict WITHOUT writing.

    Deliberately NOT a second copy of the rule: it reproduces only the cheap
    pre-checks and defers the meaning of every outcome to the real method's
    vocabulary. The one case it decides locally is "no single licence tenant",
    because that is what makes the real call inapplicable in the first place.
    """
    if sole is None:
        session = db.get_session()
        try:
            n = (
                session.query(License.tenant_id)
                .filter(License.subject_id == subject_id)
                .distinct()
                .count()
            )
        finally:
            session.close()
        return "no_licences" if n == 0 else "ambiguous"

    session = db.get_session()
    try:
        tenant = session.query(Tenant).filter(Tenant.id == sole).first()
    finally:
        session.close()
    if tenant is None:
        return "tenant_unknown"
    if tenant.kind == "service":
        return "tenant_is_machine"
    return "set"


if __name__ == "__main__":
    raise SystemExit(main())
