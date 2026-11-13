#!/usr/bin/env python3
"""
Give EVERY non-archived tenant a licence bundle — the fleet form of
``provision_tenant_bundle.py``.

WHY (George's provider-refactor rulings §I Q1/Q3, 2026-10-01; licenses #239). Every
non-archived tenant holds exactly ONE (tenant, app=inference-router) licence, held in
the tenant's ``inference-router`` bundle (migration 040). A tenant later pastes a token
minted on it via the portal (the EXISTING ``POST /licenses/{id}/tokens``; this script
mints NO tokens). Running the single-tenant script ~60 times by hand is how a tenant
gets skipped, so this walks the registry and makes the SAME decision for each one.

THE SAME DECISION, NOT A COPY. For each tenant this calls
``provision_tenant_bundle.main(["--tenant", <uuid>, "--bundle", <key>, "--plan", <plan>
(, "--apply")], db=db)`` — the per-app ``exists`` / ``adopt`` / ``insert`` / ``REFUSE``
logic, the service-subject naming, the metering push and every exit code are that
script's, defined in its docstring. In particular an existing ACTIVE, unbundled,
TENANT-LEVEL (tenant, app) licence (e.g. subject ``svc-inference-router-<x>`` in realm
``service``) is ADOPTED into the bundle, never duplicated: billing drops a (tenant, app)
key with two candidates, so a second licence would un-attribute both.

SCOPE.
  * Included: every ``tenants`` row with ``archived_at IS NULL`` — INCLUDING service
    actors (``kind='service'``). George §I Q1: the dormant service tenants hold the
    licence too. (``list_tenants`` hides them by default; this passes
    ``include_service_actors=True`` on purpose.)
  * Excluded: archived tenants (``provision_tenant_bundle`` would refuse them with 4
    anyway), and ``00000000-0000-0000-0000-000000000000`` (``default-anon``), the
    anonymous-S2S sentinel — it is not a tenant anyone acts as, and a licence on it
    would be a grant to "nobody in particular".
  * ``--only-tenant <uuid>`` (repeatable) restricts the run to those tenants — canary
    one first. Naming a tenant outside the scope above is a usage error (exit 2) and
    nothing runs.
  * ``--limit N`` processes at most N tenants (after ``--only-tenant``), in tenant-id
    order, so repeated runs walk the same prefix.

⛔ ``--plan`` IS REQUIRED, and every inserted licence is METERED. billing pools ALL of a
tenant's active budgets into one cap (``budget_debit._select_active_budgets``), so a
licence inserted on a budget-bearing plan (e.g. ``standard``: 50,000,000 tokens +
1,000,000 requests) RAISES that tenant's spend ceiling — netcare 11.355M -> ~61M. There
is deliberately no default plan, so a fleet run never silently picks one. Before the
walk, every requested plan code is looked up on every bundle app and its
token_budget_monthly / quota_monthly printed; a non-NULL value prints a loud WARNING
(a warning, not a refusal — the operator decides).

QUOTA vs BUDGET. The token BUDGET is tokens per month, pooled per tenant by billing
(see above), so it raises the tenant's spend cap. The request QUOTA is requests per
month for ONE licence, enforced at the edge by license-authz via
``quota:limit:{license_id}``.

PER-TENANT OVERRIDES (``--overrides <file.json>``). A JSON object mapping tenant uuid ->
``{"token_budget": int, "quota": int}`` (either key optional, values >= 0). For a
listed tenant they are passed as ``--token-budget`` / ``--quota`` to
``provision_tenant_bundle.py``, which applies them ONLY to licences it INSERTS — an
adopted or existing licence is never re-pushed, because a push OVERWRITES billing's row.
An unlisted tenant gets its plan's values. A tenant id in the file that is not in scope
(unknown, archived, or default-anon — judged against the full scope, not
``--only-tenant`` / ``--limit``, so one file serves the canary and the fleet run) or a
malformed file is a usage error: exit 2 before any tenant is processed. There is
deliberately no fleet-wide ``--token-budget`` / ``--quota``: a cap is per tenant.

DRY-RUN BY DEFAULT. Without ``--apply`` each tenant's plan table is printed and NOTHING
is written. The enumeration itself is read-only (``list_tenants``, ``get_bundle_by_key``,
``list_bundle_apps`` — never a lazily-expiring helper).

RECEIPT. ``--receipt <path>`` writes JSON: ``started_at``, ``finished_at`` (UTC ISO-8601),
``status`` (``"completed"`` | ``"aborted"``), ``abort_reason`` (the exception TYPE name of
an abort, else null), ``bundle``, ``plan``, ``apply``, ``only_tenant``, ``limit``,
``overrides_file`` (path or null), ``tenants`` (one
``{tenant_id, name, kind, overrides, exit_code}`` per tenant — ``overrides`` is the
EFFECTIVE ``{"token_budget", "quota"}`` passed for it, ``{}`` when none — plus ``error`` — the
exception TYPE only — for a crash or for the tenant in flight when the run aborted),
``totals`` (``{"<exit code>": count}``) and ``exit_code``. Written for dry-runs too, so
the canary's receipt is the evidence for the apply.
  * It is written on EVERY exit path once the run starts — including a DB error during
    the bundle check / enumeration, an unexpected exception mid-walk, and Ctrl-C — with
    the per-tenant results gathered so far. Never the exception MESSAGE: a driver
    message can carry DSN details.
  * The path must NOT exist: an existing file is refused (exit 2, before any work) so a
    previous run's evidence is never overwritten. The write is atomic (temp file in the
    same directory + ``os.replace``), so a reader never sees a half-written receipt.

PER-TENANT FAILURES DO NOT STOP THE RUN. Each tenant is independent and the single-tenant
script is idempotent, so the run continues and the operator fixes and re-runs.

EXIT CODES (the run's code is the per-tenant code of HIGHEST PRECEDENCE)
  per-tenant codes are provision_tenant_bundle.py's (0/1/2/3/4/6/7) plus:
    5  CRASHED — the per-tenant call raised. Its state is unknown (it may have written
       part of the tenant's bundle), so it outranks every "known outcome" code below it.
  run-level only:
    8  ABORTED mid-run — an exception OUTSIDE the per-tenant handler (e.g. a DB error in
       the bundle check or enumeration) or KeyboardInterrupt. Tenants after the abort
       point were never visited, and the in-flight one's state is unknown.

  precedence, highest first:  8 > 1 > 2 > 5 > 4 > 7 > 3 > 6 > 0
    8  aborted mid-run (receipt ``status: "aborted"``). Never reads as anything milder.
    1  bundle unknown / revoked / empty — every tenant would fail identically. Checked
       ONCE before the walk; the walk does not start.
    2  usage: bad --only-tenant, a tenant outside the scope, a malformed --overrides file
       or one naming a tenant outside the scope, DATABASE_URL unset, or the --receipt
       path already exists (no tenant is processed).
    5  at least one tenant crashed.
    4  a tenant vanished or was archived between enumeration and its turn.
    7  a tenant had an app REFUSED (or lost an adoption race) AND an unmetered insert.
    3  a tenant had an app REFUSED, or lost an adoption race.
    6  --apply: a tenant has an inserted licence not yet metered (the daily
       licenses-metering-reconcile CronJob repairs it).
    0  every tenant exited 0.

Usage
-----
    DATABASE_URL=... python scripts/provision_bundle_all_tenants.py \\
        --bundle inference-router --plan <code> [--only-tenant <uuid> ...] \\
        [--overrides overrides.json] [--limit N] [--receipt receipt.json] [--apply]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import DatabaseManager  # noqa: E402

_HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "provision_tenant_bundle", _HERE / "provision_tenant_bundle.py"
)
assert _spec is not None and _spec.loader is not None
provision_tenant_bundle = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(provision_tenant_bundle)

#: The anonymous-S2S sentinel (models.PLATFORM_TENANTS ``default-anon``). Never licensed.
DEFAULT_ANON_TENANT_ID = "00000000-0000-0000-0000-000000000000"

EXIT_OK = 0
EXIT_BAD_BUNDLE = 1
EXIT_USAGE = 2
EXIT_REFUSED = 3
EXIT_BAD_TENANT = 4
EXIT_CRASHED = 5
EXIT_METERING_PENDING = 6
EXIT_REFUSED_AND_METERING_PENDING = 7
EXIT_ABORTED = 8

#: Highest first. A code not listed (should never happen) ranks above 0 and below 6.
PRECEDENCE: tuple[int, ...] = (
    EXIT_ABORTED,
    EXIT_BAD_BUNDLE,
    EXIT_USAGE,
    EXIT_CRASHED,
    EXIT_BAD_TENANT,
    EXIT_REFUSED_AND_METERING_PENDING,
    EXIT_REFUSED,
    EXIT_METERING_PENDING,
)

EXIT_CODES_HELP = """exit codes (run = highest-precedence code; 8 > 1 > 2 > 5 > 4 > 7 > 3 > 6 > 0):
  0  every tenant ok
  8  ABORTED mid-run (exception outside the per-tenant handler, or Ctrl-C);
     receipt status "aborted"
  1  unknown / revoked / empty bundle (checked once; nothing runs)
  2  usage: bad --only-tenant / tenant outside scope / bad --overrides file or
     tenant in it outside scope / DATABASE_URL unset / --receipt path already exists
  5  a tenant CRASHED (state unknown)
  4  a tenant vanished or was archived mid-run
  7  a tenant had both 3 and 6
  3  a tenant had an app REFUSED or lost an adoption race
  6  --apply: a tenant has an inserted licence NOT metered yet"""


def aggregate_exit(codes: list[int]) -> int:
    """The run's exit code: the per-tenant code of highest precedence (0 if none/all 0)."""
    present = {c for c in codes if c != EXIT_OK}
    for code in PRECEDENCE:
        if code in present:
            return code
    # An unexpected non-zero code still fails the run; it never reads as success.
    return min(present) if present else EXIT_OK


def _uuid_arg(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"{value!r} is not a UUID") from e


def _positive_int(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("--limit must be >= 1")
    return n


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Give every non-archived tenant a licence bundle (dry-run by default).",
        epilog=EXIT_CODES_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--bundle", required=True, help="Bundle key, e.g. inference-router.")
    p.add_argument(
        "--plan",
        type=provision_tenant_bundle.parse_plan_preference,
        required=True,
        help="REQUIRED. Plan-code preference for INSERTED licences, passed through to "
        "provision_tenant_bundle.py. A plan with a token budget RAISES every inserted "
        "tenant's pooled spend cap (see --overrides to set it per tenant).",
    )
    p.add_argument(
        "--overrides",
        help="JSON file: {tenant_uuid: {token_budget: int, quota: int}} (either key "
        "optional). Applied to that tenant's INSERTED licences only.",
    )
    p.add_argument("--apply", action="store_true", help="Write. Without it: dry-run, no writes.")
    p.add_argument("--receipt", help="Write a JSON receipt of the run to this path.")
    p.add_argument(
        "--only-tenant",
        action="append",
        type=_uuid_arg,
        default=[],
        help="Restrict to this tenant UUID (repeatable). Canary one first.",
    )
    p.add_argument("--limit", type=_positive_int, help="Process at most N tenants.")
    return p.parse_args(argv)


def eligible_tenants(db: DatabaseManager) -> list[Any]:
    """Non-archived tenants, service actors INCLUDED, default-anon EXCLUDED, id order.

    Read-only: ``list_tenants`` is a plain SELECT.
    """
    rows = db.list_tenants(include_archived=False, include_service_actors=True)
    return sorted((t for t in rows if str(t.id) != DEFAULT_ANON_TENANT_ID), key=lambda t: str(t.id))


def _write_receipt(path: str, receipt: dict[str, Any]) -> None:
    """Atomic: a temp file in the SAME directory, then ``os.replace`` (same filesystem)."""
    target = Path(path)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


OVERRIDE_KEYS = ("token_budget", "quota")
#: Same ceiling as provision_tenant_bundle's --token-budget/--quota (INTEGER columns, 041).
MAX_OVERRIDE: int = provision_tenant_bundle.MAX_OVERRIDE


class OverridesError(ValueError):
    """The --overrides file is unreadable or malformed (a usage error, exit 2)."""


def load_overrides(path: str) -> dict[str, dict[str, int]]:
    """Parse and validate the --overrides file. Raises ``OverridesError``.

    Keys are canonicalised tenant UUIDs; each value is an object with only
    ``token_budget`` / ``quota``, each a non-negative int (a JSON bool is refused).
    """

    def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        # json.loads keeps the LAST of two identical keys silently, so a file naming
        # one tenant twice would apply whichever value happened to come second.
        seen: dict[str, Any] = {}
        for k, v in pairs:
            if k in seen:
                raise OverridesError(f"--overrides has duplicate key {k!r}")
            seen[k] = v
        return seen

    try:
        raw = json.loads(
            Path(path).read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_keys
        )
    except OverridesError:
        raise
    except (OSError, ValueError) as e:
        raise OverridesError(f"cannot read --overrides {path}: {type(e).__name__}") from e
    if not isinstance(raw, dict):
        raise OverridesError("--overrides must be a JSON object keyed by tenant uuid")
    out: dict[str, dict[str, int]] = {}
    for key, value in raw.items():
        try:
            tenant_id = str(uuid.UUID(str(key)))
        except ValueError as e:
            raise OverridesError(f"--overrides key {key!r} is not a UUID") from e
        if tenant_id in out:
            raise OverridesError(f"--overrides names tenant {tenant_id} twice")
        if not isinstance(value, dict):
            raise OverridesError(f"--overrides[{key}] must be an object")
        unknown = sorted(set(value) - set(OVERRIDE_KEYS))
        if unknown:
            raise OverridesError(f"--overrides[{key}] has unknown key(s): {', '.join(unknown)}")
        entry: dict[str, int] = {}
        for k in OVERRIDE_KEYS:
            if k not in value:
                continue
            n = value[k]
            if isinstance(n, bool) or not isinstance(n, int) or n < 0 or n > MAX_OVERRIDE:
                raise OverridesError(
                    f"--overrides[{key}].{k} must be an integer in 0..{MAX_OVERRIDE}"
                )
            entry[k] = n
        out[tenant_id] = entry
    return out


def report_plan_spend(
    db: DatabaseManager, bundle_key: str, bundle_id: UUID, plan_codes: list[str]
) -> list[str]:
    """Print what each requested plan code pushes to billing on each bundle app.

    Read-only (``list_bundle_apps`` / ``list_plans``). A non-NULL token budget prints
    the loud WARNING below — it RAISES every inserted tenant's POOLED cap — and a
    non-NULL request quota prints its own WARNING. Returns the warning lines (also
    printed). A warning, never a refusal: the operator decides.
    """
    warnings: list[str] = []
    for app in db.list_bundle_apps(bundle_id):
        # Every non-revoked plan row with the code, regional ones included: the
        # per-tenant ``choose_plan`` matches on code alone, so any of them can be used.
        available = db.list_plans(cast(UUID, app.id))
        for code in plan_codes:
            matches = [pl for pl in available if str(pl.code) == code]
            if not matches:
                print(f"plan {code} on app {app.key}: not present (skipped for this app)")
                continue
            for pl in matches:
                budget, quota = pl.token_budget_monthly, pl.quota_monthly
                print(
                    f"plan {code} on app {app.key}: token_budget_monthly={budget} "
                    f"quota_monthly={quota}"
                )
                if budget is not None:
                    warnings.append(
                        f"WARNING: plan {code} on app {app.key} pushes a spend budget of "
                        f"{budget} — this RAISES every inserted tenant's pooled cap"
                    )
                if quota is not None:
                    warnings.append(
                        f"WARNING: plan {code} on app {app.key} pushes a request quota of "
                        f"{quota} — this ADDS to every inserted tenant's request quota"
                    )
    for w in warnings:
        print(w)
        print(w, file=sys.stderr)
    if not warnings:
        print(f"bundle '{bundle_key}': requested plan(s) push no budget and no quota.")
    return warnings


def _now() -> str:
    return datetime.now(UTC).isoformat()


def main(argv: list[str] | None = None, db: DatabaseManager | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    if args.receipt and os.path.lexists(args.receipt):
        print(
            f"ERROR: --receipt {args.receipt} already exists; refusing to overwrite it. "
            "Nothing was run.",
            file=sys.stderr,
        )
        return EXIT_USAGE
    receipt: dict[str, Any] = {
        "started_at": _now(),
        "finished_at": None,
        "status": "aborted",
        "abort_reason": None,
        "bundle": args.bundle,
        "plan": ",".join(args.plan),
        "apply": bool(args.apply),
        "only_tenant": list(args.only_tenant),
        "limit": args.limit,
        "overrides_file": args.overrides,
        "tenants": [],
        "totals": {},
        "exit_code": None,
    }
    in_flight: list[dict[str, Any]] = []  # the tenant entry being processed, if any
    code = EXIT_ABORTED
    try:
        code = _walk(args, db, receipt, in_flight)
        receipt["status"] = "completed"
    except BaseException as e:
        # The TYPE only: a driver message can carry connection details.
        receipt["abort_reason"] = type(e).__name__
        for entry in in_flight:
            entry["exit_code"] = EXIT_ABORTED
            entry["error"] = type(e).__name__
            receipt["tenants"].append(entry)
        print(f"ERROR: run ABORTED ({type(e).__name__}) -> exit {EXIT_ABORTED}", file=sys.stderr)
        if not isinstance(e, (Exception, KeyboardInterrupt)):
            raise  # SystemExit / GeneratorExit: the receipt is still written below
        code = EXIT_ABORTED
    finally:
        receipt["finished_at"] = _now()
        totals: dict[str, int] = {}
        for t in receipt["tenants"]:
            totals[str(t["exit_code"])] = totals.get(str(t["exit_code"]), 0) + 1
        receipt["totals"] = totals
        receipt["exit_code"] = code
        if args.receipt:
            _write_receipt(args.receipt, receipt)
    return code


def _walk(
    args: argparse.Namespace,
    db: DatabaseManager | None,
    receipt: dict[str, Any],
    in_flight: list[dict[str, Any]],
) -> int:
    plan = receipt["plan"]
    mode = "APPLY" if args.apply else "DRY-RUN"
    if db is None:
        db_url = os.getenv("DATABASE_URL")
        if not db_url:
            print("ERROR: DATABASE_URL is required", file=sys.stderr)
            return EXIT_USAGE
        db = DatabaseManager(db_url)

    overrides: dict[str, dict[str, int]] = {}
    if args.overrides:
        try:
            overrides = load_overrides(args.overrides)
        except OverridesError as e:
            print(f"ERROR: {e}. Nothing was run.", file=sys.stderr)
            return EXIT_USAGE

    # Checked ONCE: a bad bundle would fail every tenant identically.
    bundle = db.get_bundle_by_key(args.bundle)
    if bundle is None or bundle.revoked or not db.list_bundle_apps(cast(UUID, bundle.id)):
        print(
            f"ERROR: bundle '{args.bundle}' not found, revoked, or has no apps. Nothing was run.",
            file=sys.stderr,
        )
        return EXIT_BAD_BUNDLE

    report_plan_spend(db, args.bundle, cast(UUID, bundle.id), args.plan)

    tenants = eligible_tenants(db)
    # Judged against the FULL scope, before --only-tenant / --limit narrow it, so the
    # same file serves the canary and the fleet run.
    outside_overrides = sorted(set(overrides) - {str(t.id) for t in tenants})
    if outside_overrides:
        print(
            f"ERROR: --overrides names tenant(s) outside scope (unknown, archived, or "
            f"default-anon): {', '.join(outside_overrides)}. Nothing was run.",
            file=sys.stderr,
        )
        return EXIT_USAGE
    if args.only_tenant:
        by_id = {str(t.id): t for t in tenants}
        outside = [t for t in args.only_tenant if t not in by_id]
        if outside:
            print(
                f"ERROR: --only-tenant outside scope (unknown, archived, or default-anon): "
                f"{', '.join(outside)}. Nothing was run.",
                file=sys.stderr,
            )
            return EXIT_USAGE
        wanted = set(args.only_tenant)
        tenants = [t for t in tenants if str(t.id) in wanted]
    if args.limit is not None:
        tenants = tenants[: args.limit]

    print(f"##### {mode}: bundle '{args.bundle}' (plan {plan}) -> {len(tenants)} tenant(s) #####")
    codes: list[int] = []
    for t in tenants:
        tenant_id = str(t.id)
        mine = overrides.get(tenant_id, {})
        entry: dict[str, Any] = {
            "tenant_id": tenant_id,
            "name": t.name,
            "kind": t.kind,
            "overrides": dict(mine),
        }
        in_flight[:] = [entry]
        per_tenant = ["--tenant", tenant_id, "--bundle", args.bundle, "--plan", plan]
        if "token_budget" in mine:
            per_tenant += ["--token-budget", str(mine["token_budget"])]
        if "quota" in mine:
            per_tenant += ["--quota", str(mine["quota"])]
        if args.apply:
            per_tenant.append("--apply")
        try:
            rc = provision_tenant_bundle.main(per_tenant, db=db)
        except Exception as e:  # noqa: BLE001 — recorded, the walk continues
            rc = EXIT_CRASHED
            # The TYPE only: a driver message can carry connection details.
            entry["error"] = type(e).__name__
            print(f"ERROR: tenant {tenant_id} CRASHED ({type(e).__name__})", file=sys.stderr)
        entry["exit_code"] = rc
        in_flight.clear()  # before the append: an abort between them must not double-record
        receipt["tenants"].append(entry)
        codes.append(rc)

    code = aggregate_exit(codes)
    summary = ", ".join(f"exit {k}: {v}" for k, v in sorted(_count(codes).items()))
    print(f"##### {mode} done: {len(codes)} tenant(s) [{summary or 'none'}] -> exit {code} #####")
    if not args.apply:
        print("DRY-RUN: nothing written. Re-run with --apply.")
    return code


def _count(codes: list[int]) -> dict[int, int]:
    out: dict[int, int] = {}
    for c in codes:
        out[c] = out.get(c, 0) + 1
    return out


if __name__ == "__main__":
    sys.exit(main())
