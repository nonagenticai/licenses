#!/usr/bin/env python3
"""
Provision a per-tenant `fr` package license (idempotent).

The fr package is gated per-tenant at fr-backend: it calls
``GET /licenses/active?tenant_id=<uuid>&app_key=fr`` and grants access when an
active license exists. This script mints that one license for a customer
tenant. Every Keycloak user in the tenant then passes the gate; fr drives
kanban/n8n/kag/requirements on their behalf (no per-user license needed).

The `fr` app + `standard` plan must already exist — they are created by the
idempotent ``seed_apps.py`` baseline (shipped as the ``seed-apps`` k8s Job).
Run that first on a fresh DB.

A License row is bound to a Subject (FK), but ``get_active_license`` matches on
``(tenant_id, app_key)`` regardless of subject, so one license per tenant is
enough. We bind it to the tenant owner's Keycloak sub (``--owner-sub``).

Idempotent: if an active `fr` license already exists for the tenant, this is a
no-op (it does NOT mint a duplicate for the SAME subject). The DB partial unique
index ``uq_license_active_tenant_app_subject`` keys on subject, so onboarding a
second USER in the same org legitimately adds a second active row — and
``GET /licenses/active`` handles that (it picks deterministically and returns
200; it no longer raises a ``multiple_active_licenses`` conflict, which used to
surface downstream as a spurious tenant-wide 503 from license-authz).

Usage:
    DATABASE_URL="postgresql+psycopg://licenses_user:PASSWORD@host:port/licenses?sslmode=require" \
        python scripts/provision_fr_tenant.py \
            --tenant-id 0195f48b-d000-7000-8000-000000000001 \
            --owner-sub <keycloak-sub-of-tenant-owner> \
            --tenant-owner <owning-app, e.g. fr> \
            [--realm portal] [--expires-at 2027-01-01T00:00:00+00:00]

Verify afterwards:
    GET /licenses/active?tenant_id=<tenant-id>&app_key=fr  -> 200 active
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

logger = logging.getLogger("provision_fr_tenant")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")

FR_APP_KEY = "fr"
FR_PLAN_CODE = "standard"
# tenants.name DB CheckConstraint (ck_tenants_name_format).
_TENANT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Provision a per-tenant fr package license.")
    p.add_argument(
        "--tenant-id",
        required=True,
        help="Canonical tenant UUID (UUIDv7) — must match the JWT tenant_id claim fr sees.",
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

    # tenant_id MUST be a UUID — the licenses service rejects non-UUID tenant
    # context (auth/tenant.py), and fr passes the JWT's UUIDv7 tenant_id.
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

    app = db.get_app_by_key(FR_APP_KEY)
    if app is None:
        print(
            f"ERROR: app '{FR_APP_KEY}' not found — run scripts/seed_apps.py first.",
            file=sys.stderr,
        )
        return 1
    app_uuid = cast(UUID, app.id)

    # Resolve the fr/standard plan.
    plans = [p for p in db.list_plans(app_uuid) if str(p.code) == FR_PLAN_CODE]
    if not plans:
        print(
            f"ERROR: plan '{FR_APP_KEY}/{FR_PLAN_CODE}' not found — run scripts/seed_apps.py first.",
            file=sys.stderr,
        )
        return 1
    plan_uuid = cast(UUID, plans[0].id)

    # Idempotency: skip if an active fr license already exists for the tenant.
    existing = db.get_active_license(tenant_id=tenant_id, app_key=FR_APP_KEY)
    if existing:
        logger.info(
            "tenant %s already has %d active '%s' license(s) (id=%s) — skip",
            tenant_id,
            len(existing),
            FR_APP_KEY,
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
        license_metadata={"provisioned_by": "provision_fr_tenant.py", "scope": "tenant"},
    )
    logger.info(
        "provisioned fr license %s for tenant %s (subject=%s, plan=%s/%s, expires=%s)",
        license.id,
        tenant_id,
        subject.id,
        FR_APP_KEY,
        FR_PLAN_CODE,
        expires_at.isoformat() if expires_at else "never",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
