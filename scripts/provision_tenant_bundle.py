#!/usr/bin/env python3
"""
Give a tenant a licence BUNDLE: one per-app licence for every app in it.

Ruling 2026-09-28 (and migration 028:21-24's own design): a bundle is the
PROVISIONING and BILLING object; runtime usage presents the SPECIFIC per-app licence
from the tenant's bundle. Bundles and licences are tenant-level (not member);
consumption is tracked per tenant-member and per project on the metering side. So a
tenant holding bundle B holds, for every non-revoked app A in B, exactly ONE
effectively-active licence (tenant, A) with ``bundle_id = B``. The short-lived
runtime token is minted on that licence by
``POST /internal/tenants/{tenant_id}/bundle-token?app=<A>``; this script mints NO
tokens (an adopted licence keeps whatever tokens it already has).

THE DECISION LOGIC LIVES IN ``services/bundle_ensure.py`` (tenant-inference-provisioning
PLAN §2.2), shared with ``POST /internal/tenants/{tenant_id}/bundles/{bundle_key}/ensure``.
This script is its CLI: arguments, the printed table and the exit codes. For a tenant
whose ``budget_currency`` is set (migration 043) inserts push the QUOTA only, and
``--token-budget`` is refused with exit 2.

DRY-RUN BY DEFAULT. Without ``--apply`` it prints the per-app plan table and writes
nothing. It only reads, and deliberately avoids ``find_active_license`` (which lazily
expires stale rows, i.e. writes).

SERVICE SUBJECT NAMING. A licence this script INSERTS is held by the service subject
``svc:<tenant-uuid>:<app-key>`` (realm ``service``, so ``count_tenant_seats`` does not
bill it as a person), one per (tenant, app). The FULL tenant uuid is used, not a
short form, on purpose: ``subject.keycloak_sub`` is globally unique, and the five
system tenants share the prefix ``0195f48b-d000-7000-8000-00000000000``, so any
truncation risks two tenants resolving onto ONE subject — i.e. one licence holder
across tenants. It never equals a tenant id (tenant_subject_invariant) and is not
UUID-shaped, so introspect never presents it as a Keycloak identity.

TENANT-LEVEL vs MEMBER licences (ruling 2026-09-28: bundles and licences are
TENANT-level, never member-level; usage per member/project is attributed by headers).
A licence is TENANT-LEVEL when its subject is a ``svc:`` service subject or sits in a
non-human realm (``models.is_tenant_level_subject`` — the same split the seat count
bills by). Any other holder is a MEMBER (a person) and its licence is NEVER adopted
into a bundle: it is reported as "member licence, not adopted — migrate via
migrate_member_licences_to_tenant.py" and left OUTSIDE the bundle, so the bundle-token
route (which selects by ``bundle_id``) still sees exactly one licence per app and can
never answer 409 ``ambiguous_app_licence`` because of it. Until the member licence is
retired, billing sees two (tenant, app) candidates — the same transition cost
migrate_member_licences_to_tenant.py documents.

PER-APP DECISION (the ``action`` column), over the TENANT-LEVEL licences only:
  exists   a tenant-level licence for (tenant, app) already carries this bundle.
  adopt    exactly ONE tenant-level (tenant, app) licence exists and it is unbundled —
           set its bundle_id (compare-and-set) and keep its subject and tokens.
           Billing resolves on (tenant_id, app) and drops a key with two candidates,
           so inserting a second licence would un-attribute BOTH.
  insert   no tenant-level (tenant, app) licence — create the subject + licence.
  REFUSE   several tenant-level candidates (listed), a licence already bound to
           ANOTHER bundle, a MEMBER licence already carrying THIS bundle (a second one
           would make the bundle-token route 409 ``ambiguous_app_licence``), an app key
           too long for the service subject, or NONE of the ``--plan`` codes exists
           (non-revoked) on that app. Nothing is written for that app.

PLAN PREFERENCE. ``--plan`` is an ordered, comma-separated preference list, e.g.
``--plan standard,perpetual``. Each INSERTED app gets the FIRST code in the list that
exists (and is not revoked) on that app; the chosen code is printed per app in the
``plan`` column. A single code behaves exactly as before. Needed because no single
plan covers a real bundle (``non-agentic-ai``, measured 2026-09-28: keycloak and n8n
lack ``standard``; fr-frontend, improvement-loop and signal lack ``perpetual``).

METERING. An INSERTED licence is provisioned in billing (token budget + quota config,
per its plan) through ``main.provision_license_metering`` — the SAME function the HTTP
create route uses — which stamps ``metering_provisioned_at`` only when billing accepted
every push. A direct ``db.create_license`` without it is the 2026-08-06
unmetered-licence leak. Dry-run prints what each insert WOULD push and makes no billing
call. If billing is unreachable the licence is still created, the stamp stays NULL, the
run reports it and exits 6; the daily ``licenses-metering-reconcile`` CronJob
(``RECONCILE_REVALIDATE=true``, selection in ``main.run_metering_reconcile``) repairs it
read-before-write. Adopted/existing licences are never pushed to (a push OVERWRITES
billing's row); a NULL stamp on one is reported and left to that sweep.

PER-LICENCE OVERRIDES (``--token-budget N`` / ``--quota N``, non-negative ints). They
replace the chosen plan's ``token_budget_monthly`` / ``quota_monthly`` for every
licence THIS run INSERTS; the ``plan`` row itself is never modified, and an omitted flag
keeps the plan's value. They are STORED on the licence (``license.token_budget_override``
/ ``license.quota_override``, migration 041) in the same INSERT, before the push, so the
reconcile sweep that repairs an exit-6 licence pushes the OVERRIDE — before 041 it pushed
the plan's value, raising the tenant's pooled spend cap. They NEVER touch an adopted or existing licence:
billing may already hold a (possibly operator-tuned) row for it, and a push OVERWRITES
that row rather than adding to it (see ``main._push_token_budget``), so re-pushing
would silently reset it — change an existing licence's budget in billing instead. The
plan table's ``note`` column shows the EFFECTIVE values each insert pushes (dry-run
included), marked ``(override; plan: X)`` where an override applies.

QUOTA vs BUDGET. The token BUDGET is tokens per month; billing POOLS it per tenant
(``budget_debit._select_active_budgets`` debits the licence's budgets plus the tenant
pool), so a new budget RAISES the tenant's overall spend cap. The request QUOTA is
requests per month for THIS licence, enforced at the edge by license-authz against
``quota:limit:{license_id}``.

EXIT CODES
  0  every app is exists/adopt/insert (and, with --apply, was written)
  6  --apply wrote everything but at least one inserted licence is NOT metered yet
  3  at least one app was REFUSED, or an adoption lost a race — the others are still
     planned/applied (each app is independent and the run is idempotent, so fix and
     re-run)
  7  BOTH 3 and 6: something was refused / lost a race AND an inserted licence is not
     metered yet. Neither masks the other.
  2  --tenant is not a UUID, or DATABASE_URL is unset
  4  --tenant is not a row in ``tenants``, or is ARCHIVED
  1  unknown / revoked / empty bundle

TENANT RENAME IS DELIBERATELY NOT HERE. ``tenants.name`` is a resolver key:
``DatabaseManager.get_tenant`` / every ``/tenants/{name_or_id}`` route resolve by
name, billing_service's tenancy overlay maps NAME -> uuid on the hot path for inbound
``X-Tenant-Id``, and license-authz resolves service-principal tenants by name. The
name ``system`` is also already held by 00000000-0000-0000-0000-000000000001
(migration 014). Renaming is an operator decision via ``PATCH /tenants/{name_or_id}``.

Usage
-----
    DATABASE_URL=... python scripts/provision_tenant_bundle.py \\
        --tenant <uuid> --bundle non-agentic-ai [--plan standard[,perpetual...]] \\
        [--token-budget N] [--quota N] [--apply]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import DatabaseManager  # noqa: E402
from services import bundle_ensure  # noqa: E402
from services.bundle_ensure import (  # noqa: E402,F401 — re-exported for callers/tests
    MAX_OVERRIDE,
    AppPlan,
    EnsureError,
    choose_plan,
    member_not_adopted_note,
    owed_metering,
    provision_metering,
    subject_sub_for,
)

logger = logging.getLogger("provision_tenant_bundle")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")

# The decision logic lives in services/bundle_ensure.py (tenant-inference-provisioning
# PLAN §2.2), shared with POST /internal/tenants/{id}/bundles/{bundle}/ensure. This
# script keeps its CLI, its table and its exit codes.
DEFAULT_PLAN_CODE = bundle_ensure.DEFAULT_PLAN_CODE
SUBJECT_REALM = bundle_ensure.SUBJECT_REALM

EXIT_OK = 0
EXIT_BAD_BUNDLE = 1
EXIT_USAGE = 2
EXIT_REFUSED = 3
EXIT_BAD_TENANT = 4
EXIT_METERING_PENDING = 6
EXIT_REFUSED_AND_METERING_PENDING = 7

EXIT_CODES_HELP = """exit codes:
  0  ok
  1  unknown / revoked / empty bundle
  2  usage: --tenant not a UUID, DATABASE_URL unset, or --token-budget for a
     tenant whose budget_currency is set (money tenant)
  3  an app was REFUSED or an adoption lost a race (others still applied)
  4  tenant not in tenants, or ARCHIVED
  6  --apply: an inserted licence is NOT metered yet (reconcile CronJob repairs)
  7  both 3 and 6"""


def final_exit(refused_or_lost: bool, unmetered: bool) -> int:
    """One exit code that reports BOTH conditions; neither masks the other."""
    if refused_or_lost and unmetered:
        return EXIT_REFUSED_AND_METERING_PENDING
    if refused_or_lost:
        return EXIT_REFUSED
    return EXIT_METERING_PENDING if unmetered else EXIT_OK


def report_unmetered(unmetered: list[str]) -> None:
    print(
        "METERING NOT PROVISIONED (licence created, metering_provisioned_at NULL): "
        + "; ".join(unmetered)
        + ". The daily licenses-metering-reconcile CronJob repairs these; to repair now: "
        "kubectl -n licenses create job --from=cronjob/licenses-metering-reconcile "
        "metering-reconcile-manual-$(date +%s)",
        file=sys.stderr,
    )


def parse_plan_preference(value: str) -> list[str]:
    """``"standard,perpetual"`` -> ``["standard", "perpetual"]`` (order kept)."""
    codes = [c.strip() for c in value.split(",")]
    if not codes or any(not c for c in codes):
        raise argparse.ArgumentTypeError(f"empty plan code in {value!r}")
    if len(set(codes)) != len(codes):
        raise argparse.ArgumentTypeError(f"duplicate plan code in {value!r}")
    return codes


def non_negative_int(value: str) -> int:
    """argparse type for ``--token-budget`` / ``--quota``: 0 <= int <= MAX_OVERRIDE (else exit 2)."""
    try:
        n = int(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from e
    if n < 0:
        raise argparse.ArgumentTypeError(f"{value!r} must be >= 0")
    if n > MAX_OVERRIDE:
        raise argparse.ArgumentTypeError(f"{value!r} exceeds {MAX_OVERRIDE}")
    return n


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Give a tenant a licence bundle (dry-run by default).",
        epilog=EXIT_CODES_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--tenant", required=True, help="Tenant UUID; must already exist in tenants.")
    p.add_argument("--bundle", required=True, help="Bundle key, e.g. non-agentic-ai.")
    p.add_argument(
        "--plan",
        type=parse_plan_preference,
        default=[DEFAULT_PLAN_CODE],
        help="Ordered, comma-separated plan-code preference for INSERTED licences, e.g. "
        "'standard,perpetual': each app gets the first code that exists on it "
        f"(default: {DEFAULT_PLAN_CODE}).",
    )
    p.add_argument(
        "--token-budget",
        type=non_negative_int,
        default=None,
        help="Tokens/month pushed for each INSERTED licence instead of the plan's "
        "token_budget_monthly (pooled per tenant: raises its spend cap). Never applied "
        "to adopted/existing licences.",
    )
    p.add_argument(
        "--quota",
        type=non_negative_int,
        default=None,
        help="Requests/month pushed for each INSERTED licence instead of the plan's "
        "quota_monthly (per licence, enforced by license-authz). Never applied to "
        "adopted/existing licences.",
    )
    p.add_argument("--apply", action="store_true", help="Write. Without it: dry-run, no writes.")
    return p.parse_args(argv)


def _print_table(plans: list[AppPlan]) -> None:
    w_app = max([len("app"), *(len(p.app_key) for p in plans)])
    w_sub = max([len("subject"), *(len(p.subject) for p in plans)])
    w_plan = max([len("plan"), *(len(p.plan_code) for p in plans)])
    print(
        f"{'app':<{w_app}}  {'action':<7}  {'plan':<{w_plan}}  {'license_id':<36}  "
        f"{'subject':<{w_sub}}  note"
    )
    for p in plans:
        print(
            f"{p.app_key:<{w_app}}  {p.action:<7}  {p.plan_code or '-':<{w_plan}}  "
            f"{p.license_id or '-':<36}  {p.subject or '-':<{w_sub}}  {p.note}"
        )
        for c in p.candidates:
            print(f"{'':<{w_app}}    candidate: {c}")


def main(argv: list[str] | None = None, db: DatabaseManager | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    mode = "APPLY" if args.apply else "DRY-RUN"

    try:
        tenant_id = str(uuid.UUID(args.tenant))
    except ValueError:
        print(f"ERROR: --tenant {args.tenant!r} is not a UUID.", file=sys.stderr)
        return EXIT_USAGE

    if db is None:
        db_url = os.getenv("DATABASE_URL")
        if not db_url:
            print("ERROR: DATABASE_URL is required", file=sys.stderr)
            return EXIT_USAGE
        db = DatabaseManager(db_url)

    try:
        ep = bundle_ensure.plan(
            db,
            tenant_id,
            args.bundle,
            args.plan,
            args.quota,
            token_budget_override=args.token_budget,
        )
    except EnsureError as e:
        if e.code in ("tenant_not_found", "tenant_archived"):
            what = "is not in tenants" if e.code == "tenant_not_found" else "is ARCHIVED"
            print(f"ERROR: tenant {tenant_id} {what}. Nothing was written.", file=sys.stderr)
            return EXIT_BAD_TENANT
        if e.code == "bundle_not_found":
            print(f"ERROR: bundle '{args.bundle}' not found or revoked.", file=sys.stderr)
            return EXIT_BAD_BUNDLE
        if e.code == "bundle_empty":
            print(f"ERROR: bundle '{args.bundle}' has no apps.", file=sys.stderr)
            return EXIT_BAD_BUNDLE
        print(f"ERROR: {e.message}. Nothing was written.", file=sys.stderr)
        return EXIT_USAGE
    plans = ep.apps
    refused = ep.refused

    print(f"=== {mode}: tenant {tenant_id} ({ep.tenant.name}) <- bundle '{args.bundle}' ===")
    print(f"tenant_bundle : {'exists' if ep.holds else 'INSERT'}")
    print(f"apps          : {len(plans)} (plan preference for inserts: {','.join(args.plan)})")
    if ep.budget_currency is not None:
        print(f"budget        : {ep.budget_currency} money tenant — quota only, no token budget")
    if args.token_budget is not None or args.quota is not None:
        print(
            f"overrides     : token_budget={args.token_budget} quota={args.quota} "
            "(INSERTED licences only)"
        )
    _print_table(plans)
    counts = {a: sum(1 for p in plans if p.action == a) for a in ("exists", "adopt", "insert")}
    print(
        f"summary       : exists={counts['exists']} adopt={counts['adopt']} "
        f"insert={counts['insert']} REFUSE={len(refused)}"
    )
    if refused:
        print(
            "REFUSED (nothing written for these): "
            + ", ".join(f"{p.app_key} ({p.note})" for p in refused),
            file=sys.stderr,
        )

    if not args.apply:
        print("DRY-RUN: nothing written. Re-run with --apply.")
        return EXIT_REFUSED if refused else EXIT_OK

    to_write = [(p.app_key, p.action) for p in plans]
    bundle_ensure.apply(db, ep, provisioned_by="provision_tenant_bundle.py")
    for (app_key, action), p in zip(to_write, plans, strict=True):
        if action == "adopt" and app_key not in ep.lost_race:
            logger.info("adopted: tenant=%s app=%s license=%s", tenant_id, app_key, p.license_id)
        elif action == "insert":
            logger.info("inserted: tenant=%s app=%s license=%s", tenant_id, app_key, p.license_id)
            if not p.metering_pending:
                logger.info(
                    "metered: tenant=%s app=%s license=%s", tenant_id, app_key, p.license_id
                )
    if ep.unmetered:
        report_unmetered(ep.unmetered)
    if ep.lost_race:
        print(
            "ERROR: adoption lost a race (licence re-bundled concurrently): "
            + ", ".join(ep.lost_race)
            + ". Re-run.",
            file=sys.stderr,
        )
    else:
        print("APPLIED.")
    return final_exit(bool(refused or ep.lost_race), bool(ep.unmetered))


if __name__ == "__main__":
    sys.exit(main())
