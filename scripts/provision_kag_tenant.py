#!/usr/bin/env python3
"""
Provision a per-tenant `kag` package license (idempotent).

kag-frontend is gated per-tenant at kag-backend (mirroring the fr per-tenant
plan): it calls ``GET /licenses/active?tenant_id=<uuid>&app_key=kag`` and grants
access when an active license exists. This is the SAME `kag` app_key entitlement
that n8n-nodes-kag requires — so one entitlement covers both the n8n node and
the kag-frontend web app. This script mints that one license for a customer
tenant; every Keycloak user in the tenant then passes the gate.

The `kag` app + the chosen plan must already exist. Unlike `fr` (seeded with a
``standard`` plan by ``seed_apps.py``), the `kag` app is seeded by
``seed_test_licenses.py`` (plan code ``test``) / ``seed_portal_licenses.py``
(plan code ``portal-test``) — there is **no** ``kag/standard`` plan. Pass the
real plan code via ``--plan`` (default ``perpetual``); the error message lists
the plan codes that actually exist for `kag` if the chosen one is missing.

A License row is bound to a Subject (FK), but ``get_active_license`` matches on
``(tenant_id, app_key)`` regardless of subject, so one license per tenant is
enough. We bind it to the tenant owner's Keycloak sub (``--owner-sub``).

Idempotent: if an active `kag` license already exists for the tenant, this is a
no-op (does NOT mint a duplicate for the same subject — the DB partial unique
index ``uq_license_active_tenant_app_subject`` is the real guard).

Usage:
    DATABASE_URL="postgresql+psycopg://licenses_user:PASSWORD@host:port/licenses?sslmode=require" \
        python scripts/provision_kag_tenant.py \
            --tenant-id 0195f48b-d000-7000-8000-000000000001 \
            --owner-sub <keycloak-sub-of-tenant-owner> \
            --tenant-owner <owning-app, e.g. fr> \
            [--plan perpetual] [--realm portal] [--expires-at 2027-01-01T00:00:00+00:00]

Verify afterwards:
    GET /licenses/active?tenant_id=<tenant-id>&app_key=kag  -> 200 active
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import uuid
from datetime import datetime
from typing import cast
from uuid import UUID

# Make `models` importable when run from the licenses repo root or from inside
# the licenses container (working dir = /app).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import DatabaseManager, DatetimeArgError, parse_aware_datetime_arg  # noqa: E402

logger = logging.getLogger("provision_kag_tenant")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")

KAG_APP_KEY = "kag"
# kag is NOT seeded with a `standard` plan (that's fr). Its plans come from
# seed_test_licenses.py (`test`) / seed_portal_licenses.py (`portal-test`), and
# real tenants are typically provisioned `perpetual`. Override with --plan.
DEFAULT_KAG_PLAN_CODE = "perpetual"
# tenants.name DB CheckConstraint (ck_tenants_name_format).
_TENANT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Provision a per-tenant kag package license.")
    p.add_argument(
        "--tenant-id",
        required=True,
        help="Canonical tenant UUID (UUIDv7) — must match the JWT tenant_id claim kag-backend sees.",
    )
    p.add_argument(
        "--tenant-name",
        required=True,
        help="Human tenant name for the tenants registry (^[a-z0-9][a-z0-9-]*$). "
        "Registered idempotently before the license is minted so the tenant_id "
        "can never be an orphan (the license.tenant_id -> tenants.id FK invariant).",
    )
    p.add_argument(
        "--owner-sub",
        required=True,
        help="Keycloak sub of the tenant owner to bind the license to (Subject FK).",
    )
    p.add_argument(
        "--plan",
        default=DEFAULT_KAG_PLAN_CODE,
        help=(
            "kag plan code to bind the license to (default: perpetual). "
            "Note: there is no kag/standard plan — seeded codes are 'test' "
            "(seed_test_licenses.py) and 'portal-test' (seed_portal_licenses.py)."
        ),
    )
    p.add_argument(
        "--realm",
        default="portal",
        help="Keycloak realm the owner sub belongs to (default: portal).",
    )
    p.add_argument(
        "--expires-at",
        default=None,
        help="Optional ISO-8601 expiry (default: perpetual).",
    )
    p.add_argument(
        "--tenant-owner",
        required=True,
        help="Owning app (tenant_owner key, migration 050: platform, fr, commonpractice, "
        "w2v, merqi, ...) a NEW tenant is registered under. Required, no default; "
        "ignored when the tenant id is already registered.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    try:
        tenant_id = str(uuid.UUID(args.tenant_id))
    except ValueError:
        print(
            f"ERROR: --tenant-id {args.tenant_id!r} is not a UUID. "
            "Pass the canonical tenant UUIDv7 (not the tenant name).",
            file=sys.stderr,
        )
        return 2

    if not _TENANT_NAME_RE.fullmatch(args.tenant_name):
        print(
            f"ERROR: --tenant-name {args.tenant_name!r} must match ^[a-z0-9][a-z0-9-]*$.",
            file=sys.stderr,
        )
        return 2

    expires_at: datetime | None = None
    if args.expires_at:
        try:
            expires_at = parse_aware_datetime_arg(args.expires_at, arg="--expires-at")
        except DatetimeArgError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL is required", file=sys.stderr)
        return 2

    db = DatabaseManager(db_url)

    app = db.get_app_by_key(KAG_APP_KEY)
    if app is None:
        print(
            f"ERROR: app '{KAG_APP_KEY}' not found. Unlike `fr`, the `kag` app is "
            "NOT seeded by scripts/seed_apps.py — seed it via "
            "scripts/seed_test_licenses.py (plan 'test') or "
            "scripts/seed_portal_licenses.py (plan 'portal-test') first.",
            file=sys.stderr,
        )
        return 1
    app_uuid = cast(UUID, app.id)

    all_plans = db.list_plans(app_uuid)
    plans = [p for p in all_plans if str(p.code) == args.plan]
    if not plans:
        available = sorted({str(p.code) for p in all_plans})
        print(
            f"ERROR: plan '{KAG_APP_KEY}/{args.plan}' not found. "
            f"Available '{KAG_APP_KEY}' plan codes: {available or '(none — run a kag seed first)'}. "
            "Pass an existing code via --plan.",
            file=sys.stderr,
        )
        return 1
    plan_uuid = cast(UUID, plans[0].id)

    existing = db.get_active_license(tenant_id=tenant_id, app_key=KAG_APP_KEY)
    if existing:
        logger.info(
            "tenant %s already has %d active '%s' license(s) (id=%s) — skip",
            tenant_id,
            len(existing),
            KAG_APP_KEY,
            existing[0].id,
        )
        return 0

    # Register the tenant BEFORE minting its license (Stage 1 of the FK campaign)
    # so license.tenant_id can never be an orphan. Idempotent: a tenant already
    # registered under this id is left untouched.
    try:
        db.ensure_tenant(
            tenant_id,
            name=args.tenant_name,
            keycloak_realm=args.realm,
            owner_key=args.tenant_owner,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    subject = db.create_or_get_subject(keycloak_sub=args.owner_sub, keycloak_realm=args.realm)
    license = db.create_license(
        subject_id=cast(UUID, subject.id),
        app_id=app_uuid,
        plan_id=plan_uuid,
        tenant_id=tenant_id,
        status="active",
        expires_at=expires_at,
        license_metadata={"provisioned_by": "provision_kag_tenant.py", "scope": "tenant"},
    )
    logger.info(
        "provisioned kag license %s for tenant %s (subject=%s, plan=%s/%s, expires=%s)",
        license.id,
        tenant_id,
        subject.id,
        KAG_APP_KEY,
        args.plan,
        expires_at.isoformat() if expires_at else "never",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
