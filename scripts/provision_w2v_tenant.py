#!/usr/bin/env python3
"""
Provision a per-tenant `w2v` package license (idempotent).

The w2v package is gated per-tenant by the w2v_orchestrator license gate
(`require_license`, flag `W2V_LICENSE_GATE_ENABLED`): it calls
``GET /licenses/active?tenant_id=<uuid>&app_key=w2v`` and admits the tenant when
an active license exists. This script mints that one license for a tenant. Every
Keycloak user in the tenant then passes the gate (w2v has no per-user license).

The `w2v` app + its plan must already exist — seeded live via the licenses
``w2v`` app key (PR #44; plans ``standard`` + ``production``). This is the same
shape as ``provision_fr_tenant.py``; the only differences are the app key and the
selectable plan.

A License row is bound to a Subject (FK), but ``get_active_license`` matches on
``(tenant_id, app_key)`` regardless of subject, so one license per tenant is
enough. We bind it to the tenant owner's Keycloak sub (``--owner-sub``).

Idempotent: if an active `w2v` license already exists for the tenant, this is a
no-op (it does NOT mint a duplicate); duplicate actives are also prevented by the
DB partial unique index ``uq_license_active_tenant_app_subject``.

Usage:
    DATABASE_URL="postgresql+psycopg://licenses_user:PASSWORD@host:port/licenses?sslmode=require" \
        python scripts/provision_w2v_tenant.py \
            --tenant-id e4f70a83-81ee-429a-abca-6999e3a0d24f \
            --owner-sub <keycloak-sub-of-tenant-owner> \
            --tenant-owner <owning-app, e.g. fr> \
            [--realm portal] [--plan standard] [--expires-at 2027-01-01T00:00:00+00:00] \
            [--byo-mode local_allowed|platform_only]

The optional ``--byo-mode`` also (idempotently) sets the tenant's BYO posture.
The ``tenants.byo_mode`` column DB-defaults to ``byo_required`` for a NEW tenant
row (migration 058, licenses#273; it was ``local_allowed`` under 042). The flag
still matters: it is the only way this command changes an EXISTING tenant's
posture (the default applies on INSERT only), and ``--byo-mode local_allowed`` /
``platform_only`` is how a tenant is set to anything other than the default.

Verify afterwards:
    GET /licenses/active?tenant_id=<tenant-id>&app_key=w2v  -> 200 active
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

logger = logging.getLogger("provision_w2v_tenant")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")

W2V_APP_KEY = "w2v"
W2V_DEFAULT_PLAN_CODE = "standard"
# tenants.name DB CheckConstraint (ck_tenants_name_format).
_TENANT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Provision a per-tenant w2v package license.")
    p.add_argument(
        "--tenant-id",
        required=True,
        help="Canonical tenant UUID (UUIDv7) — must match the JWT tenant_id claim w2v sees.",
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
        "--plan",
        default=W2V_DEFAULT_PLAN_CODE,
        help=f"w2v plan code (default: {W2V_DEFAULT_PLAN_CODE}; 'production' also exists).",
    )
    p.add_argument(
        "--expires-at",
        default=None,
        help="Optional ISO-8601 expiry (default: perpetual).",
    )
    p.add_argument(
        "--byo-mode",
        choices=["local_allowed", "byo_required", "platform_only"],
        default=None,
        help="If set, also set the tenant's byo_mode posture (idempotent). "
        "Internal/e2e tenants should be local_allowed or platform_only.",
    )
    p.add_argument(
        "--tenant-owner",
        required=True,
        help="Owning app (tenant_owner key, migration 050: platform, fr, commonpractice, "
        "w2v, merqi, ...) a NEW tenant is registered under. Required, no default; "
        "ignored when the tenant id is already registered.",
    )
    return p.parse_args(argv)


def _apply_byo_mode(db: DatabaseManager, tenant_id: str, byo_mode: str) -> None:
    """Idempotently set the tenant's BYO posture after the license is confirmed.

    Re-running with the same ``byo_mode`` is a no-op that just re-sets the same
    value. If the tenant row is absent (``set_tenant_byo_mode`` returns None) the
    posture cannot be set: log a WARNING with guidance rather than crashing, since
    the license mint has already succeeded.
    """
    updated = db.set_tenant_byo_mode(tenant_id, byo_mode)
    if updated is None:
        logger.warning(
            "could not set byo_mode=%s: no tenant row for %s. The license was "
            "minted, but the tenant is not registered. Register the tenant "
            "(ensure_tenant) first, then re-run with --byo-mode %s.",
            byo_mode,
            tenant_id,
            byo_mode,
        )
    else:
        logger.info("set tenant %s byo_mode=%s", tenant_id, byo_mode)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    # tenant_id MUST be a UUID — the licenses service rejects non-UUID tenant
    # context (auth/tenant.py), and w2v passes the JWT's UUIDv7 tenant_id.
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

    app = db.get_app_by_key(W2V_APP_KEY)
    if app is None:
        print(
            f"ERROR: app '{W2V_APP_KEY}' not found — seed the w2v app first.",
            file=sys.stderr,
        )
        return 1
    app_uuid = cast(UUID, app.id)

    # Resolve the requested w2v plan.
    plans = [p for p in db.list_plans(app_uuid) if str(p.code) == args.plan]
    if not plans:
        print(
            f"ERROR: plan '{W2V_APP_KEY}/{args.plan}' not found.",
            file=sys.stderr,
        )
        return 1
    plan_uuid = cast(UUID, plans[0].id)

    # Register the tenant BEFORE the idempotent-skip check (Stage 1 of the FK
    # campaign) so license.tenant_id can never be an orphan. Idempotent: a tenant
    # already registered under this id is left untouched. Doing this ahead of the
    # skip return is what lets the byo_mode posture be (re)set on BOTH the
    # re-provision (skip) path and the first-provision (mint) path below.
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

    # Set the BYO posture right after the row is guaranteed to exist and BEFORE
    # the idempotent-skip return, so re-provisioning an already-licensed tenant
    # still corrects a stale (DB-default `byo_required`) posture instead of
    # leaving it commercial -> downstream 402s.
    if args.byo_mode is not None:
        _apply_byo_mode(db, tenant_id, args.byo_mode)

    # Idempotency: skip if an active w2v license already exists for the tenant.
    existing = db.get_active_license(tenant_id=tenant_id, app_key=W2V_APP_KEY)
    if existing:
        logger.info(
            "tenant %s already has %d active '%s' license(s) (id=%s) — skip",
            tenant_id,
            len(existing),
            W2V_APP_KEY,
            existing[0].id,
        )
        return 0

    subject = db.create_or_get_subject(keycloak_sub=args.owner_sub, keycloak_realm=args.realm)
    license = db.create_license(
        subject_id=cast(UUID, subject.id),
        app_id=app_uuid,
        plan_id=plan_uuid,
        tenant_id=tenant_id,
        status="active",
        expires_at=expires_at,
        license_metadata={"provisioned_by": "provision_w2v_tenant.py", "scope": "tenant"},
    )
    logger.info(
        "provisioned w2v license %s for tenant %s (subject=%s, plan=%s/%s, expires=%s)",
        license.id,
        tenant_id,
        subject.id,
        W2V_APP_KEY,
        args.plan,
        expires_at.isoformat() if expires_at else "never",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
