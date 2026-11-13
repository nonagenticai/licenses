#!/usr/bin/env python3
"""Report — and, with ``--apply``, correct — the Keycloak REALM stored on a Subject.

DRY-RUN BY DEFAULT. Without ``--apply`` this writes nothing.

────────────────────────────────────────────────────────────────────────────────
WHY THIS EXISTS
────────────────────────────────────────────────────────────────────────────────
``POST /licenses/user/{keycloak_sub}`` stamps a NEW Subject with the realm of the
CALLER's token unless the minting service states ``subject_realm`` (that field is
new — see ``LicenseCreate.subject_realm``). CommonPractice's service account
authenticates in ``secure-apps``, so every employee it licensed before that field
existed became a ``secure-apps`` Subject. Measured 2026-09-17: 3 CP subjects in
``secure-apps`` (minted by the SA) against 2 in ``portal`` (minted via portal-bff,
where the caller WAS the human).

An EXISTING Subject deliberately keeps its realm — two minting services
disagreeing must not flip a person in and out of the seat count — so the mint
path logs ``subject_realm_mismatch`` and moves on. This script is the operator
tool that log line assumes: the only supported way to correct one.

⚠️ IT IS HARMLESS TODAY ONLY BY LUCK, WHICH IS THE REASON TO FIX IT RATHER THAN
RECORD IT. ``human_subjects`` = subjects − ``NON_HUMAN_SUBJECT_REALMS`` −
unresolved, and that tuple is ``("service",)``. Add ``secure-apps`` to it —
plausible, it is the machine realm holding most subjects — and every CP employee
silently stops being a seat and the invoice under-bills to zero, with no error
anywhere. Correcting the rows removes the trap instead of documenting it.

────────────────────────────────────────────────────────────────────────────────
⛔ WHAT THIS SCRIPT WILL NOT DO
────────────────────────────────────────────────────────────────────────────────
* It does not SEARCH for rows to fix. Every subject is named explicitly, by
  ``keycloak_sub`` or by Subject id, on the command line or in a file. A realm is
  an operator fact about where a person's identity lives; deriving "these look
  like CP employees" from licence rows would be exactly the guess the mint path
  refuses to make.
* It will not write a NON-HUMAN realm (``NON_HUMAN_SUBJECT_REALMS``). That would
  remove a person from the seat count — an under-bill — through a tool whose
  whole purpose is to make the count right.
* It will not touch a subject already in the requested realm; that is reported as
  ``already`` and is not an error, so a re-run after a partial apply is safe.

────────────────────────────────────────────────────────────────────────────────
USAGE
────────────────────────────────────────────────────────────────────────────────
    DATABASE_URL=postgresql+psycopg://... python scripts/fix_subject_realm.py \\
        --realm portal --subject <keycloak_sub|subject_id> [--subject ...]

    DATABASE_URL=postgresql+psycopg://... python scripts/fix_subject_realm.py \\
        --realm portal --from-file subjects.txt --apply

⚠️ ``DATABASE_URL`` must point at the write PRIMARY. The master node's local
:5432 is a read-only STANDBY; ``--apply`` against it fails loud rather than
silently doing nothing.

Exit codes: 0 = ran (including "nothing to do"); 2 = bad input or no DATABASE_URL.
"""

from __future__ import annotations

import argparse
import os
import sys
from uuid import UUID

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models import NON_HUMAN_SUBJECT_REALMS, DatabaseManager, Subject  # noqa: E402


def _as_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError, AttributeError, TypeError:
        return None


def _load(db: DatabaseManager, idents: list[str]) -> list[tuple[Subject | None, str]]:
    """Resolve each identifier to a Subject row, keeping the caller's spelling.

    Accepts either the Keycloak ``sub`` or the internal Subject UUID, because the
    two are routinely confused — that confusion is what the billing contract
    (``subject_id`` = Subject UUID, not the sub) exists to separate, and an
    operator holding one of them should not have to convert it by hand.
    """
    session = db.get_session()
    try:
        out: list[tuple[Subject | None, str]] = []
        for ident in idents:
            as_uuid = _as_uuid(ident)
            row = None
            if as_uuid is not None:
                row = session.query(Subject).filter(Subject.id == as_uuid).first()
            if row is None:
                row = session.query(Subject).filter(Subject.keycloak_sub == ident).first()
            if row is not None:
                session.expunge(row)
            out.append((row, ident))
        return out
    finally:
        session.close()


def _set_realm(db: DatabaseManager, subject_id: UUID, realm: str) -> str:
    """Write the realm for ONE subject. Returns the verdict word."""
    session = db.get_session()
    try:
        row = session.query(Subject).filter(Subject.id == subject_id).first()
        if row is None:
            return "not_found"
        if row.keycloak_realm == realm:
            return "already"
        # A targeted UPDATE rather than attribute assignment: the mapped column is typed
        # `Column[str]` on the declarative class, so assigning a `str` to it is a type error the
        # repo's `ty` gate rejects. The statement form says the same thing and is checkable.
        session.query(Subject).filter(Subject.id == subject_id).update(
            {Subject.keycloak_realm: realm}, synchronize_session=False
        )
        session.commit()
        return "set"
    finally:
        session.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--realm", required=True, help="the realm to write, e.g. 'portal'")
    parser.add_argument(
        "--subject",
        action="append",
        default=[],
        metavar="KEYCLOAK_SUB|SUBJECT_ID",
        help="a subject to correct; repeatable",
    )
    parser.add_argument(
        "--from-file",
        help="file with one keycloak_sub or subject id per line (# comments allowed)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually write (default: report only)",
    )
    args = parser.parse_args(argv)

    if args.realm in NON_HUMAN_SUBJECT_REALMS:
        print(
            f"ERROR: '{args.realm}' is a non-human realm ({NON_HUMAN_SUBJECT_REALMS}); "
            "writing it would remove these people from the seat count and under-bill.",
            file=sys.stderr,
        )
        return 2

    idents: list[str] = list(args.subject)
    if args.from_file:
        with open(args.from_file, encoding="utf-8") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if line:
                    idents.append(line)
    if not idents:
        print("ERROR: name at least one --subject or use --from-file", file=sys.stderr)
        return 2

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL is required", file=sys.stderr)
        return 2

    db = DatabaseManager(db_url)
    rows = _load(db, idents)

    mode = "APPLY" if args.apply else "DRY-RUN (nothing is written)"
    print(f"{len(rows)} subject(s) named — target realm {args.realm!r} — {mode}\n")

    counts: dict[str, int] = {}
    for row, ident in rows:
        if row is None:
            verdict = "not_found"
        elif row.keycloak_realm == args.realm:
            verdict = "already"
        elif args.apply:
            verdict = _set_realm(db, UUID(str(row.id)), args.realm)
        else:
            verdict = "would_set"
        counts[verdict] = counts.get(verdict, 0) + 1
        current = row.keycloak_realm if row is not None else "-"
        subject_id = str(row.id) if row is not None else "-"
        print(f"  {ident:<45} {current:<14} -> {args.realm:<10} {verdict}  [{subject_id}]")

    print("\nverdicts:")
    for verdict, count in sorted(counts.items()):
        print(f"  {verdict:<12} {count}")
    if not args.apply:
        print("\nRe-run with --apply to write the rows marked would_set.")
    # A named subject that does not exist is an operator error worth an exit code:
    # it usually means a Keycloak sub was pasted where a Subject id was meant, and
    # the run silently "succeeded" having corrected nothing.
    return 2 if counts.get("not_found") else 0


if __name__ == "__main__":
    raise SystemExit(main())
