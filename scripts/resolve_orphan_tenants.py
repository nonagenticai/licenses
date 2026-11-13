#!/usr/bin/env python3
r"""Resolve orphan ``license.tenant_id`` values toward ZERO (Stage 2 of the FK campaign).

An *orphan* is a ``license.tenant_id`` with no matching ``tenants.id`` (same
definition as ``scripts/audit_orphan_tenants.py``). Migration 016 (the real FK)
cannot be added while any orphan exists, so every orphan must first be resolved:
either **register** the missing tenant, **repoint** a mis-keyed license to its
real tenant, or leave it for a gated human decision.

This script is DRY-RUN by default and prints a per-orphan disposition report. It
only writes when a disposition is BOTH safe and machine-provable, and only when
the matching ``--apply-*`` flag is passed.

Disposition classes
--------------------
1. ``service-derivable`` (auto, ``--apply-derivable``): the tenant_id equals
   ``uuid5(SERVICE_TENANT_NS, subject.keycloak_sub)`` — i.e. it was minted by
   ``provision_service_token.py`` (a deterministic per-service tenant). The
   canonical ``tenants.name`` is therefore PROVABLY the subject slug, with no
   guessing. Registering it is exactly what the Stage-1-fixed provision script
   now does up front. Safe, additive, reversible (DELETE the tenants row).

2. ``needs-name`` (GATED): a legitimate-looking tenant (real app licenses, a
   single keycloak realm) whose canonical name is NOT derivable from this DB
   (the realm — portal/secure-apps/nonagentic — names the auth boundary, not the
   tenant). A human must supply the name. The report prints every signal needed
   to decide, and ``--register NAME=TENANT_ID`` registers one explicitly.

3. ``mis-keyed`` (GATED): ``tenant_id`` equals the subject's own
   ``keycloak_sub`` — the user-id-as-tenant money-bug (#73/#74/#75). The real
   tenant is NOT recoverable from this DB (these subjects have no correctly-keyed
   sibling license; there is no user->tenant map here or in fr — fr's tenant is
   edge-injected). Resolution is repoint (needs the user's real tenant, e.g. from
   Keycloak) or revoke. Surfaced for a human; NOT auto-applied.

4. ``dead-history`` (report): only revoked/expired licenses ride on the orphan.
   Registering preserves the audit trail; still gated on a name.

STRICTLY no writes unless an ``--apply-*``/``--register`` flag is given. Reuses
``DatabaseManager.ensure_tenant`` (the Stage-1 idempotent explicit-id register).

Usage (inside the licenses container, working dir = /app; DATABASE_URL preset):
    .venv/bin/python scripts/resolve_orphan_tenants.py                 # dry-run report
    .venv/bin/python scripts/resolve_orphan_tenants.py --apply-derivable --tenant-owner platform
    .venv/bin/python scripts/resolve_orphan_tenants.py --register acme-corp=019edc48-6060-76e9-8d8f-2e720e12c609 \
        --tenant-owner <owning-app>
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import uuid

from sqlalchemy import text

# Make `models` importable from the repo root and inside the container.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import DatabaseManager  # noqa: E402

# Namespace + realm mirror scripts/provision_service_token.py — keep in sync.
_SERVICE_TENANT_NS = uuid.UUID("6f0d1e2a-3b4c-5d6e-7f80-91a2b3c4d5e6")
_TENANT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# One row per orphan tenant_id, with the license profile AND the distinct
# subjects (keycloak_sub + realm) that hold its licenses, plus whether any
# subject's own keycloak_sub == the tenant_id (the mis-keyed money-bug), and how
# many DISTINCT REAL (registered) tenants those same subjects also point at (the
# sibling-repoint candidate count). Pure SELECT — no writes.
_ORPHAN_SQL = text(
    """
    -- migration 016 retyped license.tenant_id VARCHAR -> uuid. Post-016 the old
    -- `tenants.id::text = license.tenant_id` joins are `text = uuid`, which
    -- Postgres rejects ("operator does not exist") and crashed this resolver.
    -- tenant_id is uuid now: compare uuid-to-uuid against tenants.id, and
    -- ::text-cast the uuid only where the other side is genuinely text
    -- (subject.keycloak_sub is VARCHAR).
    WITH orphan AS (
        SELECT DISTINCT l.tenant_id
        FROM license l
        WHERE l.tenant_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM tenants t WHERE t.id = l.tenant_id)
    )
    SELECT
        o.tenant_id                                              AS tenant_id,
        count(*)                                                 AS rows,
        count(*) FILTER (WHERE l.status = 'active')              AS active,
        string_agg(DISTINCT a.key, ',' ORDER BY a.key)          AS apps,
        string_agg(DISTINCT s.keycloak_realm, ',')              AS realms,
        array_agg(DISTINCT s.keycloak_sub)                      AS subs,
        bool_or(l.tenant_id::text = s.keycloak_sub)             AS miskeyed,
        (
            SELECT count(DISTINCT g.tenant_id)
            FROM license g
            WHERE g.subject_id IN (
                SELECT l2.subject_id FROM license l2 WHERE l2.tenant_id = o.tenant_id
            )
            AND EXISTS (SELECT 1 FROM tenants t2 WHERE t2.id = g.tenant_id)
        )                                                        AS sibling_real_tenants
    FROM orphan o
    JOIN license l  ON l.tenant_id = o.tenant_id
    JOIN subject s  ON s.id = l.subject_id
    LEFT JOIN app a ON a.id = l.app_id
    GROUP BY o.tenant_id
    ORDER BY active DESC, rows DESC, o.tenant_id
    """
)


def _classify(rec: dict) -> tuple[str, str | None]:
    """Return (disposition_class, derived_name_or_None) for an orphan record."""
    # service-derivable: uuid5(NS, sub) == tenant_id for one of its subjects.
    # rec["tenant_id"] is a uuid.UUID (migration 016 retype) — str() BOTH sides
    # so the compare is str==str; a str==UUID compare is always False and would
    # silently misclassify every service-derivable orphan as needs-name.
    for sub in rec["subs"]:
        if str(uuid.uuid5(_SERVICE_TENANT_NS, sub)) == str(rec["tenant_id"]):
            name = sub if _TENANT_NAME_RE.fullmatch(sub) else None
            return "service-derivable", name
    if rec["miskeyed"]:
        return "mis-keyed", None
    if rec["active"] == 0:
        return "dead-history", None
    return "needs-name", None


def _load_orphans(db: DatabaseManager) -> list[dict]:
    session = db.get_session()
    try:
        rows = session.execute(_ORPHAN_SQL).mappings().all()
    finally:
        session.close()
    out: list[dict] = []
    for r in rows:
        rec = dict(r)
        rec["subs"] = list(rec["subs"] or [])
        rec["disposition"], rec["derived_name"] = _classify(rec)
        out.append(rec)
    return out


def _print_report(orphans: list[dict]) -> None:
    by_class: dict[str, list[dict]] = {}
    for rec in orphans:
        by_class.setdefault(rec["disposition"], []).append(rec)

    order = ["service-derivable", "needs-name", "mis-keyed", "dead-history"]
    print("ORPHAN RESOLUTION REPORT (dry-run) — license.tenant_id with no tenants row\n")
    for cls in order:
        recs = by_class.get(cls, [])
        print(f"=== {cls} ({len(recs)} tenant_id(s), {sum(r['rows'] for r in recs)} rows) ===")
        for r in recs:
            name = r["derived_name"]
            name_col = f"name={name!r}" if name else "name=?(gated)"
            print(
                f"  {r['tenant_id']}  rows={r['rows']} act={r['active']} "
                f"apps={r['apps'] or '-'} realms={r['realms'] or '-'} "
                f"sibling_real={r['sibling_real_tenants']}  {name_col}"
            )
        print()

    total = sum(r["rows"] for r in orphans)
    derivable = by_class.get("service-derivable", [])
    named = [r for r in derivable if r["derived_name"]]
    print(
        "SUMMARY\n"
        f"  orphan tenant_ids : {len(orphans)} ({total} rows)\n"
        f"  service-derivable : {len(derivable)} "
        f"({len(named)} with a valid auto-name → --apply-derivable registers these)\n"
        f"  needs-name        : {len(by_class.get('needs-name', []))} (GATED: supply a name)\n"
        f"  mis-keyed         : {len(by_class.get('mis-keyed', []))} "
        "(GATED: repoint to real tenant or revoke)\n"
        f"  dead-history      : {len(by_class.get('dead-history', []))} (GATED: name to preserve trail)\n"
    )


def _apply_derivable(db: DatabaseManager, orphans: list[dict], owner_key: str) -> int:
    """Register every service-derivable orphan with its PROVEN name. Idempotent."""
    n = 0
    for r in orphans:
        if r["disposition"] != "service-derivable" or not r["derived_name"]:
            continue
        # realm: prefer a 'service' realm if present among this tenant's subjects.
        realm = "service" if r["realms"] and "service" in r["realms"].split(",") else None
        try:
            t = db.ensure_tenant(
                r["tenant_id"], name=r["derived_name"], keycloak_realm=realm, owner_key=owner_key
            )
            print(f"registered {t.id}  name={t.name!r} realm={realm}")
            n += 1
        except ValueError as exc:
            print(f"SKIP {r['tenant_id']}: {exc}", file=sys.stderr)
    print(f"\n{n} service-derivable tenant(s) registered.")
    return n


def _register_explicit(db: DatabaseManager, spec: str, owner_key: str) -> int:
    """Register a single NAME=TENANT_ID pair (human-supplied name for a gated orphan)."""
    if "=" not in spec:
        print(f"ERROR: --register expects NAME=TENANT_ID, got {spec!r}", file=sys.stderr)
        return 2
    name, _, tid = spec.partition("=")
    name, tid = name.strip(), tid.strip()
    if not _TENANT_NAME_RE.fullmatch(name):
        print(f"ERROR: name {name!r} must match ^[a-z0-9][a-z0-9-]*$", file=sys.stderr)
        return 2
    try:
        parsed = str(uuid.UUID(tid))
    except ValueError:
        print(f"ERROR: {tid!r} is not a UUID", file=sys.stderr)
        return 2
    try:
        t = db.ensure_tenant(parsed, name=name, owner_key=owner_key)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"registered {t.id}  name={t.name!r}")
    return 0


def _delete_orphans(db: DatabaseManager, tenant_ids: list[str], dry_run: bool) -> int:
    """Hard-delete license rows for the given orphan tenant_ids (guarded)."""
    total = 0
    for tid in tenant_ids:
        try:
            parsed = str(uuid.UUID(tid))
        except ValueError:
            print(f"SKIP {tid!r}: not a UUID", file=sys.stderr)
            continue
        if dry_run:
            session = db.get_session()
            try:
                from models import License

                n = session.query(License).filter(License.tenant_id == parsed).count()
            finally:
                session.close()
            print(f"WOULD DELETE {n} license row(s) for orphan tenant {parsed}")
            total += n
            continue
        try:
            n = db.delete_orphan_licenses_by_tenant(parsed)
            print(f"deleted {n} license row(s) for orphan tenant {parsed}")
            total += n
        except ValueError as exc:
            print(f"SKIP {parsed}: {exc}", file=sys.stderr)
    print(f"\n{'(dry-run) would delete' if dry_run else 'deleted'} {total} license row(s) total.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Resolve orphan license.tenant_id values (Stage 2).")
    p.add_argument(
        "--apply-derivable",
        action="store_true",
        help="Register every service-derivable orphan (uuid5-proven name). Idempotent write.",
    )
    p.add_argument(
        "--register",
        metavar="NAME=TENANT_ID",
        default=None,
        help="Register ONE gated orphan under a human-supplied canonical name.",
    )
    p.add_argument(
        "--delete",
        metavar="TENANT_ID[,TENANT_ID...]",
        default=None,
        help="HARD-DELETE all license rows for these orphan tenant_ids (tokens cascade). "
        "Guarded: refuses any tenant_id that IS registered. Dry-run unless --yes.",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="Actually perform --delete (without it, --delete is a dry-run).",
    )
    p.add_argument(
        "--tenant-owner",
        default=None,
        help="Owning app (tenant_owner key, migration 050: platform, fr, commonpractice, "
        "w2v, merqi, ...) a NEW tenant is registered under. REQUIRED with --apply-derivable / --register (no default); "
        "ignored when the tenant is already registered.",
    )
    args = p.parse_args(argv if argv is not None else sys.argv[1:])

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL is required", file=sys.stderr)
        return 2
    db = DatabaseManager(db_url)

    if (args.register or args.apply_derivable) and not args.tenant_owner:
        print(
            "ERROR: --tenant-owner is required to register tenants (no default owner)",
            file=sys.stderr,
        )
        return 2
    if args.register:
        return _register_explicit(db, args.register, args.tenant_owner)

    if args.delete:
        tids = [t.strip() for t in args.delete.split(",") if t.strip()]
        return _delete_orphans(db, tids, dry_run=not args.yes)

    orphans = _load_orphans(db)
    if args.apply_derivable:
        _apply_derivable(db, orphans, args.tenant_owner)
        # Re-report the remaining state after the write.
        print("\n--- state after --apply-derivable ---")
        _print_report(_load_orphans(db))
        return 0

    _print_report(orphans)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
