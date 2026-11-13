#!/usr/bin/env python3
r"""
Run one metering-reconcile sweep from a scheduled job, in-process.

WHY THIS EXISTS INSTEAD OF A curl CRONJOB
-----------------------------------------
``POST /licenses/admin/metering/reconcile`` is
``Depends(require_any_role("license-admin"))``, and ``require_any_role`` tests
REALM roles only (``auth/dependencies.py``). The full transitive composite
closure of ``service-account-licenses`` in realm ``secure-apps`` is::

    billing-internal, default-roles-secure-apps, manage-account,
    manage-account-links, offline_access, service-internal, uma_authorization,
    view-profile

— no ``license-admin``. The deployed pod's own SA token decodes to exactly
``['billing-internal', 'default-roles-secure-apps', 'offline_access',
'service-internal', 'uma_authorization']`` (verified at the wire 2026-08-10).
A curl-based CronJob against that route would 403 on its first run and every run
after, and granting the licences SA ``license-admin`` is rejected: it would hand
it cross-tenant licence mutation plus billing's pricing routes for zero
functional gain.

So this calls ``main.run_metering_reconcile`` DIRECTLY, in the same process,
with the credentials the pod already holds. The outbound leg to billing needs no
new grant either: billing's ``POST /api/budget`` is ``require_internal`` =
``{service-internal, onboarding-internal, license-admin}``, which the SA's
``service-internal`` already satisfies.

CONFIGURATION (all from env — no flags, so the CronJob manifest is the only
place a knob exists):

==============================  ==========================================
``RECONCILE_DRY_RUN``           measure and report, write nothing (default false)
``RECONCILE_REVALIDATE``        re-examine already-stamped licences (default false)
``RECONCILE_FORCE_PLAN_QUOTA``  DESTRUCTIVE: push plan.quota_monthly over a
                                billing quota row that differs from it, i.e.
                                destroy the operator override (default false)
``RECONCILE_LIMIT``             max licences in one sweep, 1..5000 (default 500)
``RECONCILE_LICENSE_ID``        restrict to ONE licence id — the canary (optional)
``RECONCILE_ACTOR``             name recorded in the sweep's log line
==============================  ==========================================

⚠️ ENVIRONMENT — ``licenses-env`` ALONE IS NOT ENOUGH, and this is the first
script in the repo for which that is true. ``import main`` runs
``main._validate_production_config()`` at module scope, and ``ENVIRONMENT``
defaults to ``production``, so ``METERING_SERVICE_URL`` and
``CORS_ALLOWED_ORIGINS`` are BOTH required — and neither lives in the
``licenses-env`` Secret. The Secret holds only ``DATABASE_URL``,
``KEYCLOAK_REALM``, ``KEYCLOAK_URL``, ``POSTGRES_SSLMODE``,
``SERVICE_CLIENT_ID``, ``SERVICE_CLIENT_SECRET``, ``SERVICE_TOKEN_URL``; the two
missing vars are set in the API Deployment's own ``env:`` block. A CronJob built
on ``envFrom: licenses-env`` and nothing else DIES AT IMPORT.

⚠️ Do not "fix" that by copying ``_jobs/licenses-seed-offerings/job.yaml``:
``seed_offerings.py`` survives on ``envFrom`` alone only because it imports
``models``, not ``main``. The resemblance to that job is limited to the
``sys.path`` bootstrap below.

No new Secret and no new grant are needed — the two extra vars are non-secret
service configuration, and importing ``main`` then gives this script the same
``DatabaseManager`` and the same service-token provider the API pod uses. No
image change either: ``scripts/`` is already ``COPY``'d into the runtime image
(see ``Dockerfile``). A missing var is reported as a config failure (exit 2)
with its JSON line intact — see ``_load_app``.

OUTPUT is ONE JSON LINE on stdout, on EVERY path including a failed import.
This service has no Prometheus client (``main.py``: "This service has NO
Prometheus scrape annotation") and no audit table, so ``kubectl logs`` IS the
audit trail. Anything human goes to stderr.

EXIT CODES
----------
0   converged: the sweep measured a population it could account for, every
    licence in it came out covered, and nothing was truncated or unknown
1   NOT converged. Any of: a licence could not be repaired; a ``dry_run``
    measured licences that are still uncovered; the sweep scanned nothing over
    a live estate (anti-vacuity); the sweep hit its ``limit`` with more of the
    estate behind it (truncation)
2   config / DB / billing transport or auth failure — nothing can be concluded
    about the estate from this run

⚠️ THE ANTI-VACUITY GUARD — read this before changing the CronJob's env.
After one repairing run every swept licence is stamped, so the NEXT run with
``revalidate=false`` selects 0 rows, repairs 0, and reports success. Green
forever, measuring nothing. That is precisely the failure mode #105 was written
to fix (a stamp being read as a measurement), reintroduced one level up in the
scheduler. So: if the sweep scanned NOTHING while live budget-owing licences
exist, this exits 1 and says so loudly. The count it compares against is
computed by its own SQL below — deliberately NOT by re-running the sweep's own
selection, which could only ever agree with itself.

The operational consequence is intended: a scheduled run must set
``RECONCILE_REVALIDATE=true`` to be meaningful, because that is the only mode in
which the sweep re-measures the estate against billing every time. A
``revalidate=false`` schedule over a non-empty estate will trip this guard from
its second run onward, which is the correct signal — that schedule is not
measuring anything.

⚠️ TWO SIBLINGS OF THE SAME FAILURE, guarded in the same place, because
"scanned 0" is only the loudest way for a run to be green about nothing:

* MEASURED EVERYTHING, ALL OF IT BROKEN. A ``dry_run`` writes nothing by
  definition, so ``failed`` is always empty on one and the estate's actual state
  lands in ``uncovered_budget_license_ids``. Judged on ``failed`` alone, a dry
  run reporting 94 of 98 licences uncovered exits 0. A dry run with a non-empty
  uncovered set is therefore NOT converged.
* TRUNCATED. Selection is ``ORDER BY created_at`` ASC with ``LIMIT``, so a
  ``revalidate=true`` schedule over an estate larger than ``limit`` re-measures
  the same oldest N forever and never reaches the tail — green indefinitely
  while the newest licences are never asked about. ``scanned == limit`` with
  more of the estate behind it is therefore NOT converged either.
  ⚠️ Under ``revalidate=false`` this leg is deliberately CONSERVATIVE: the
  independent count cannot see stamps (that is what makes it independent), so a
  sweep that legitimately finished the unverified tail in exactly ``limit`` rows
  is still reported truncated. The failure direction is "run me again", never a
  false green.

Usage::

    RECONCILE_REVALIDATE=true RECONCILE_DRY_RUN=true \
        python scripts/reconcile_metering.py

In-cluster: a CronJob running this repo's own image, with ``envFrom: licenses-env``
PLUS explicit ``METERING_SERVICE_URL`` and ``CORS_ALLOWED_ORIGINS`` — see the
ENVIRONMENT warning above; the Secret alone will not start this script.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any

# Make `main` importable when run from the repo root or inside the licenses
# container (working dir = /app). The sys.path bootstrap — and ONLY the sys.path
# bootstrap — mirrors scripts/seed_offerings.py; see the env warning above.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger("reconcile_metering")

EXIT_CONVERGED = 0
EXIT_NOT_CONVERGED = 1
EXIT_TRANSPORT = 2

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}

# The independent count the anti-vacuity guard compares ``scanned`` against.
#
# ⚠️ Hand-written SQL ON PURPOSE. If this reused the sweep's own ORM query the
# guard could not fail: any drift in that selection would move both numbers
# together and the comparison would be a tautology. This says, in its own words,
# "a live licence whose plan owes billing something" — the population the sweep
# claims to be responsible for. Since migration 041 (#239) a licence's own
# override also makes it owe that leg (an override on an unlimited plan).
_LIVE_BUDGET_OWING_SQL = """
SELECT count(*)
  FROM license l
  JOIN plan p ON p.id = l.plan_id
 WHERE l.status = 'active'
   AND (l.expires_at IS NULL OR l.expires_at > now())
   AND (p.quota_monthly IS NOT NULL OR p.token_budget_monthly IS NOT NULL
        OR l.quota_override IS NOT NULL OR l.token_budget_override IS NOT NULL)
"""
_LIVE_BUDGET_OWING_ONE_SQL = _LIVE_BUDGET_OWING_SQL + "   AND l.id = :license_id\n"


class ConfigError(ValueError):
    """A malformed RECONCILE_* value. Fatal — never silently defaulted."""


class StartupError(RuntimeError):
    """``import main`` failed — almost always a missing production env var.

    ⚠️ THIS MUST NOT BE A BARE IMPORT AT MODULE SCOPE. ``main`` runs
    ``_validate_production_config()`` while being imported, and ``ENVIRONMENT``
    defaults to ``production``, so a CronJob whose env lacks
    ``METERING_SERVICE_URL`` or ``CORS_ALLOWED_ORIGINS`` dies with a bare
    ``RuntimeError`` before a single line of this module's code runs: exit 1,
    no JSON line, and a config failure indistinguishable from "a licence could
    not be converged". Deferring the import is what lets the failure be
    reported as what it is (exit 2) on the one channel this job has.
    """


def _load_app() -> Any:
    """Import ``main`` (the service module), or raise ``StartupError``.

    Cheap to call repeatedly — after the first success it is a ``sys.modules``
    hit. Called through, not cached in a global, so tests patching
    ``main.run_metering_reconcile`` are seen by this module.
    """
    try:
        import main
    except Exception as e:
        raise StartupError(str(e)) from e
    return main


def _env_bool(name: str, default: bool = False) -> bool:
    """Parse a boolean env var STRICTLY.

    A typo (``ture``) raises instead of quietly meaning False. A dry-run flag
    that silently reads as False is a job that writes to billing when the
    operator asked it not to.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return default if value == "" else False
    raise ConfigError(f"{name}={raw!r} is not a boolean (use one of {sorted(_TRUE | _FALSE)})")


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    """Parse a bounded integer env var. Bounds mirror the HTTP route's Query()."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError as e:
        raise ConfigError(f"{name}={raw!r} is not an integer") from e
    if not (minimum <= value <= maximum):
        raise ConfigError(f"{name}={value} is outside {minimum}..{maximum}")
    return value


def _env_str(name: str) -> str | None:
    """A trimmed env value, or ``None`` when unset OR blank.

    Both halves are load-bearing and both are tested. A CronJob manifest that
    sets ``RECONCILE_LICENSE_ID: ""`` to mean "sweep the estate" must not be
    read as an id, and a value carrying a stray newline from a YAML block scalar
    must not be sent to the UUID parser (it would 400 the whole run).
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    return raw.strip()


def count_live_budget_owing_licenses(license_id: str | None = None) -> int:
    """How many LIVE licences are on a plan that owes billing a quota or budget.

    Scoped to ``license_id`` when given, so a single-licence canary is judged
    against its own subject rather than against the whole estate.
    """
    from sqlalchemy import text

    sql = _LIVE_BUDGET_OWING_ONE_SQL if license_id else _LIVE_BUDGET_OWING_SQL
    params: dict[str, Any] = {"license_id": license_id} if license_id else {}
    session = _load_app().db_manager.get_session()
    try:
        row = session.execute(text(sql), params).first()
        return int(row[0]) if row else 0
    finally:
        session.close()


def _classify(result: Any, live_budget_owing: int, limit: int) -> tuple[int, str]:
    """Map one sweep's outcome onto the exit-code contract. Returns (code, reason).

    FOUR ways to be un-converged, not one. The anti-vacuity leg gets the most
    attention because it is the one #105's own bug reintroduces, but a run can
    equally be green about nothing by measuring an estate that is entirely
    broken (``dry_run`` never populates ``failed``) or by measuring only the
    oldest ``limit`` rows of it forever. See the module docstring.
    """
    if result.unknown_budget_license_ids:
        return EXIT_TRANSPORT, (
            f"billing would not disclose coverage for "
            f"{len(result.unknown_budget_license_ids)} licence(s); nothing was created for "
            f"them and their coverage remains UNKNOWN"
        )
    if result.unknown_quota_license_ids:
        # Same class as the budget unknown, one column over. The quota read is
        # per-licence (billing has no quota list route), so a route the licences
        # SA cannot reach sends the WHOLE sweep here — which is the point: that
        # is a loud exit 2, not a silent fallback to pushing the plan default
        # over rows nobody could see.
        # The reason breakdown rides in the exit message because THIS STRING is
        # what the failing CronJob shows an operator. On 2026-09-04 03:41Z it
        # said only that one licence's quota was UNKNOWN; the cause (billing 404
        # = its foreign-owner rule = a diverged tenant of record) cost ~30
        # minutes of Loki archaeology. See `main.QUOTA_UNKNOWN_REASONS`.
        reasons = getattr(result, "unknown_quota_reasons", None) or {}
        hint = ""
        if "foreign_tenant" in reasons:
            hint = (
                " foreign_tenant means billing holds a request_quota_config row under a "
                "DIFFERENT tenant, so the licence's tenant of record has diverged: compare "
                "licenses.license.tenant_id against billing.request_quota_config.tenant_id "
                "for those license_ids."
            )
        return EXIT_TRANSPORT, (
            f"billing would not disclose the configured quota for "
            f"{len(result.unknown_quota_license_ids)} licence(s); nothing was pushed for "
            f"them and their quota remains UNKNOWN. By cause: "
            f"{dict(sorted(reasons.items()))}.{hint}"
        )
    if result.failed:
        return EXIT_NOT_CONVERGED, (
            f"{len(result.failed_license_ids)} licence(s) could not be converged: "
            f"{result.failed_license_ids}"
        )
    if result.dry_run and result.uncovered_quota_license_ids:
        # The quota sibling of the leg below. A dry run writes nothing, so these
        # licences genuinely have NO request_quota_config row in billing.
        return EXIT_NOT_CONVERGED, (
            f"NOT CONVERGED (measure-only): {len(result.uncovered_quota_license_ids)} of "
            f"{result.scanned} scanned licence(s) have no request_quota_config row in "
            f"billing: {result.uncovered_quota_license_ids}. Re-run without "
            f"RECONCILE_DRY_RUN to create them."
        )
    if result.dry_run and result.uncovered_budget_license_ids:
        # On a dry run NOTHING is written, so `failed` is empty by construction
        # and the estate's real state is here. Judged on `failed` alone this
        # exits 0 while measuring 94 of 98 licences uncovered — the sibling of
        # the vacuity failure, one column over.
        return EXIT_NOT_CONVERGED, (
            f"NOT CONVERGED (measure-only): {len(result.uncovered_budget_license_ids)} of "
            f"{result.scanned} scanned licence(s) still have no active license_sync budget "
            f"in billing: {result.uncovered_budget_license_ids}. A dry run writes nothing, "
            f"so this is the coverage answer, not a list of what was attempted. Re-run "
            f"without RECONCILE_DRY_RUN to repair them."
        )
    if result.scanned == 0 and live_budget_owing > 0:
        return EXIT_NOT_CONVERGED, (
            f"VACUOUS SWEEP: scanned 0 licences while {live_budget_owing} live "
            f"budget-owing licence(s) exist. A run that measures nothing must never "
            f"report success — that is the stamp-read-as-a-measurement failure this "
            f"job exists to prevent. Set RECONCILE_REVALIDATE=true so the sweep "
            f"re-measures the estate against billing instead of trusting its own stamps."
        )
    if result.scanned == limit and live_budget_owing > limit:
        # Selection is ORDER BY created_at ASC + LIMIT, so a schedule whose
        # estate outgrew its limit re-measures the same oldest N forever and
        # never reaches the newest licences — green indefinitely.
        return EXIT_NOT_CONVERGED, (
            f"TRUNCATED SWEEP: scanned exactly the limit ({limit}) while "
            f"{live_budget_owing} live budget-owing licence(s) exist, so at least "
            f"{live_budget_owing - limit} were never examined. Selection is ordered by "
            f"created_at, so repeating this run re-measures the same oldest {limit} and "
            f"never reaches the tail. Raise RECONCILE_LIMIT above {live_budget_owing}."
        )
    return EXIT_CONVERGED, "converged"


async def run(
    *,
    limit: int,
    revalidate: bool,
    dry_run: bool,
    force_plan_quota: bool,
    actor: str,
    license_id: str | None,
) -> tuple[int, dict[str, Any]]:
    """One sweep + the guard. Returns (exit_code, the JSON payload to print)."""
    # Counted BEFORE the sweep: it must be a fact about the estate, not about
    # what the sweep just did to it.
    live_budget_owing = count_live_budget_owing_licenses(license_id)
    result = await _load_app().run_metering_reconcile(
        limit=limit,
        revalidate=revalidate,
        dry_run=dry_run,
        force_plan_quota=force_plan_quota,
        actor=actor,
        license_ids=[license_id] if license_id is not None else None,
    )
    code, reason = _classify(result, live_budget_owing, limit)
    payload: dict[str, Any] = {
        "event": "metering_reconcile",
        "actor": actor,
        "limit": limit,
        "revalidate": revalidate,
        "force_plan_quota": force_plan_quota,
        "license_id": license_id,
        "live_budget_owing_licenses": live_budget_owing,
        "exit_code": code,
        "reason": reason,
        **result.model_dump(),
    }
    return code, payload


def _fail(actor: str | None, license_id: str | None, error: str, **extra: Any) -> int:
    """Emit the one JSON line for a run that could not conclude, and exit 2.

    ``error`` must be a STATIC string or an env-var name — never a stringified
    exception. A psycopg error stringifies to the failing statement plus its
    bound parameters, and a connection error to the DSN; #104 removed exactly
    that from this service's HTTP surface and it must not come back on the audit
    line. ``kubectl logs`` carries both streams, but stdout here is the
    structured record that gets shipped, pasted and alerted on, so it stays
    clean. The traceback goes to the logger, on stderr.
    """
    payload = {
        "event": "metering_reconcile",
        "actor": actor,
        "license_id": license_id,
        "error": error,
        "exit_code": EXIT_TRANSPORT,
        **extra,
    }
    print(json.dumps(payload, sort_keys=True, default=str))
    return EXIT_TRANSPORT


def main_cli(argv: list[str] | None = None) -> int:
    """Entry point. Prints exactly one JSON line on stdout, ALWAYS.

    Including when ``import main`` fails, which is the realistic day-one failure
    for a new CronJob — see ``StartupError``.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        print(
            f"usage: {os.path.basename(__file__)} (configured via RECONCILE_* env)", file=sys.stderr
        )
        return _fail(None, None, "this job takes no arguments; configure it via RECONCILE_* env")

    actor: str | None = None
    license_id: str | None = None
    try:
        limit = _env_int("RECONCILE_LIMIT", 500, minimum=1, maximum=5000)
        revalidate = _env_bool("RECONCILE_REVALIDATE")
        # DEFAULT FALSE, and it must stay false in the scheduled manifest. True
        # makes the sweep push plan.quota_monthly over any billing quota row that
        # differs from it — i.e. it restores, on purpose and under an operator's
        # name, exactly the nightly revert that the read-before-write fix of
        # 2026-08-24 removed. It exists so a genuine "propagate the plan" is
        # possible without deleting the guard.
        force_plan_quota = _env_bool("RECONCILE_FORCE_PLAN_QUOTA")
        dry_run = _env_bool("RECONCILE_DRY_RUN")
        license_id = _env_str("RECONCILE_LICENSE_ID")
        actor = _env_str("RECONCILE_ACTOR") or "cronjob:reconcile-metering"
    except ConfigError as e:
        # Safe to print: ConfigError is built from an env-var NAME and a value
        # this job owns, never from a database or transport exception.
        logger.error("Refusing to run: %s", e)
        return _fail(actor, license_id, str(e), error_kind="bad_reconcile_env")

    try:
        _load_app()
    except StartupError as e:
        logger.error(
            "Metering reconcile cannot start — the service module would not import. "
            "This job needs METERING_SERVICE_URL and CORS_ALLOWED_ORIGINS IN ADDITION to "
            "the licenses-env Secret (they live in the API Deployment's env: block, not "
            "the Secret): %s",
            e,
            exc_info=True,
        )
        return _fail(
            actor,
            license_id,
            "service module failed to import — check METERING_SERVICE_URL and "
            "CORS_ALLOWED_ORIGINS are set in addition to the licenses-env Secret",
            error_kind="startup_config",
        )

    try:
        code, payload = asyncio.run(
            run(
                limit=limit,
                revalidate=revalidate,
                dry_run=dry_run,
                force_plan_quota=force_plan_quota,
                actor=actor,
                license_id=license_id,
            )
        )
    except Exception as e:  # DB down, billing auth dead, malformed licence id, ...
        logger.error("Metering reconcile could not complete: %s", type(e).__name__, exc_info=True)
        return _fail(
            actor,
            license_id,
            "the sweep could not complete; see the traceback on stderr",
            error_kind=type(e).__name__,
        )

    print(json.dumps(payload, sort_keys=True, default=str))
    if code != EXIT_CONVERGED:
        logger.error("%s exit=%d: %s", _load_app().METERING_FAILURE_MARKER, code, payload["reason"])
    return code


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s | %(message)s",
        stream=sys.stderr,
    )
    raise SystemExit(main_cli())
