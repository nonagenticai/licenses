#!/usr/bin/env python3
"""READ-ONLY audit of orphan ``license.tenant_id`` values (no matching tenants row).

``license.tenant_id`` is a free-form ``VARCHAR(255)`` with NO foreign key to
``tenants.id`` (a UUID PK). An *orphan* is a ``tenant_id`` value that does not
match any ``tenants.id::text`` — i.e. a license points at a tenant that was
never provisioned into the canonical ``tenants`` registry (or whose row was
later deleted, e.g. by migration 004's ``DELETE FROM tenants WHERE name = id::text``).

This is the prep audit for adding a real FK (``migrations/016_add_license_tenant_fk.sql``):
the FK cannot be added while orphans exist, so we first need to see exactly which
tenant_ids are orphaned, how many licenses ride on each, and whether the value is
a legitimate-but-unprovisioned tenant UUID, a legacy string name, or an
**fr-user-id-shaped** UUID that was wrongly persisted into ``tenant_id`` — the
"$2,446" money-bug failure class (a per-USER id landing in a per-TENANT column;
see #73/#74/#75 and MEMORY billing-tenant-id-holds-an-fr-user-id-2026-07-20).

Heuristics per orphan value:
  * ``uuid_shaped``      — matches the canonical UUID text shape.
  * ``uuid_version``     — the RFC-4122 version nibble (7 = canonical tenant UUIDv7).
  * ``fr_user_shaped``   — UUID-shaped AND version == 4 (random v4 = Keycloak/fr
                           user id shape; tenants are minted as v7). The prime
                           "id belongs to a USER not a TENANT" suspect.
  * ``suspicious``       — UUID-shaped but NOT v7 (v4/v5/nil/other): not the
                           canonical tenant shape, so it likely should never have
                           been a tenant_id at all.

Buckets (a tenant_id can appear in the suspicious column AND in a/b):
  (a) has >= 1 ACTIVE license      — live blast radius; must be resolved before the FK.
  (b) only expired/revoked         — dead history; safe to repoint/delete.
  (c) fr-user-id-shaped / suspicious — the value itself looks wrong.

STRICTLY READ-ONLY. This script issues SELECT statements only — there is no
INSERT/UPDATE/DELETE/DDL anywhere. Safe to run against prod.

Usage (inside the licenses container, working dir = /app):
    .venv/bin/python scripts/audit_orphan_tenants.py

``DATABASE_URL`` is read from the environment (the pod already sets it); the same
DatabaseManager/SSL config as every other script in this directory is reused.
"""

from __future__ import annotations

import os
import re
import sys

from sqlalchemy import text

# Make `models` importable both from the repo root and inside the container.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import DatabaseManager  # noqa: E402

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# Read-only aggregation: one row per orphan tenant_id with its license profile.
# NOT EXISTS against tenants.id::text is the exact orphan definition. No writes.
_ORPHAN_SQL = text(
    """
    SELECT
        l.tenant_id                                            AS tenant_id,
        count(*)                                               AS rows,
        count(*) FILTER (WHERE l.status = 'active')            AS active,
        count(*) FILTER (WHERE l.status = 'expired')           AS expired,
        count(*) FILTER (WHERE l.status = 'revoked')           AS revoked,
        count(*) FILTER (
            WHERE l.status NOT IN ('active', 'expired', 'revoked')
        )                                                      AS other,
        count(DISTINCT a.key)                                  AS n_apps,
        string_agg(DISTINCT a.key, ',' ORDER BY a.key)         AS apps,
        count(DISTINCT l.subject_id)                           AS n_subjects,
        min(l.created_at)                                      AS created_min,
        max(l.created_at)                                      AS created_max
    FROM license l
    LEFT JOIN app a ON a.id = l.app_id
    WHERE NOT EXISTS (
        -- migration 016 retyped license.tenant_id VARCHAR -> uuid. Post-016 a
        -- `t.id::text = l.tenant_id` comparison is `text = uuid`, which Postgres
        -- rejects ("operator does not exist: text = uuid") and crashed this audit
        -- outright. Both sides are uuid now, so compare uuid-to-uuid directly.
        SELECT 1 FROM tenants t WHERE t.id = l.tenant_id
    )
    GROUP BY l.tenant_id
    ORDER BY active DESC, count(*) DESC, l.tenant_id
    """
)


def _uuid_version(value: str) -> int | None:
    """Return the RFC-4122 version nibble of a UUID-shaped string, else None."""
    if not UUID_RE.fullmatch(value):
        return None
    try:
        return int(value[14], 16)
    except ValueError:
        return None


def _fmt_date(value: object) -> str:
    """Format a datetime/None as YYYY-MM-DD for the summary table."""
    if value is None:
        return "-"
    iso = getattr(value, "isoformat", None)
    return iso()[:10] if callable(iso) else str(value)[:10]


def main(argv: list[str] | None = None) -> int:
    _ = argv  # no options; the script is unconditionally read-only.

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL is required", file=sys.stderr)
        return 2

    db = DatabaseManager(db_url)
    session = db.get_session()
    try:
        rows = session.execute(_ORPHAN_SQL).mappings().all()
    finally:
        session.close()

    bucket_a: list[dict] = []  # has >= 1 active
    bucket_b: list[dict] = []  # only expired/revoked
    bucket_c: list[dict] = []  # fr-user-id-shaped / suspicious (non-v7 UUID)

    total_rows = 0
    for r in rows:
        # license.tenant_id is a uuid column (migration 016), so the driver hands
        # back a uuid.UUID here; str() it before the regex/text heuristics below
        # (UUID_RE.fullmatch needs a str, and it is rendered into the report as text).
        tid = str(r["tenant_id"])
        version = _uuid_version(tid)
        uuid_shaped = version is not None
        fr_user_shaped = version == 4
        suspicious = uuid_shaped and version != 7

        rec = {
            "tenant_id": tid,
            "rows": r["rows"],
            "active": r["active"],
            "expired": r["expired"],
            "revoked": r["revoked"],
            "other": r["other"],
            "apps": r["apps"] or "",
            "n_subjects": r["n_subjects"],
            "created_min": _fmt_date(r["created_min"]),
            "created_max": _fmt_date(r["created_max"]),
            "uuid_shaped": uuid_shaped,
            "uuid_version": version,
            "fr_user_shaped": fr_user_shaped,
            "suspicious": suspicious,
        }
        total_rows += r["rows"]

        if r["active"] > 0:
            bucket_a.append(rec)
        else:
            bucket_b.append(rec)
        if suspicious:
            bucket_c.append(rec)

    # --- Report -------------------------------------------------------------
    header = (
        f"{'TENANT_ID':<38} {'ROWS':>4} {'ACT':>3} {'EXP':>3} {'REV':>3} "
        f"{'OTH':>3} {'V':>2} {'FRUSER':>6} {'SUBJ':>4}  {'CREATED (min..max)':<24} APPS"
    )

    def _print_rows(label: str, recs: list[dict]) -> None:
        print(f"\n=== BUCKET {label} ({len(recs)} tenant_id(s)) ===")
        print(header)
        for rec in recs:
            ver = rec["uuid_version"]
            print(
                f"{rec['tenant_id']:<38} {rec['rows']:>4} {rec['active']:>3} "
                f"{rec['expired']:>3} {rec['revoked']:>3} {rec['other']:>3} "
                f"{('v' + str(ver)) if ver is not None else 'str':>2} "
                f"{('yes' if rec['fr_user_shaped'] else '-'):>6} "
                f"{rec['n_subjects']:>4}  "
                f"{rec['created_min'] + '..' + rec['created_max']:<24} {rec['apps']}"
            )

    print("ORPHAN license.tenant_id AUDIT (READ-ONLY) — values with no tenants.id match")
    _print_rows("A — has >= 1 ACTIVE license (blast radius; resolve before FK)", bucket_a)
    _print_rows("B — only expired/revoked (dead history)", bucket_b)
    _print_rows("C — fr-user-id-shaped / suspicious non-v7 UUID (value looks wrong)", bucket_c)

    n_distinct = len(rows)
    n_fr_user = sum(1 for r in bucket_c if r["fr_user_shaped"])
    print(
        "\nSUMMARY\n"
        f"  distinct orphan tenant_ids : {n_distinct}\n"
        f"  total orphan license rows  : {total_rows}\n"
        f"  bucket A (has active)      : {len(bucket_a)} tenant_ids, "
        f"{sum(r['rows'] for r in bucket_a)} rows\n"
        f"  bucket B (revoked/expired) : {len(bucket_b)} tenant_ids, "
        f"{sum(r['rows'] for r in bucket_b)} rows\n"
        f"  bucket C (suspicious/non-v7): {len(bucket_c)} tenant_ids "
        f"(of which fr-user-id-shaped v4: {n_fr_user})\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
