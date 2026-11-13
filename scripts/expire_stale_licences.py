#!/usr/bin/env python3
r"""
Retire time-expired-but-still-``status='active'`` licences, estate-wide, from a
scheduled job.

WHY THIS EXISTS
---------------
Measured in the prod licences database on 2026-08-10::

    SELECT status, count(*) FROM public.license GROUP BY 1;
    -> active | 144
       revoked |  64

NOT ONE ``expired`` row, ever, on an estate that has been minting time-bounded
licences for months. ``DatabaseManager.expire_stale_licenses`` is correct, but it
is lazy expire-on-WRITE scoped to the single ``(tenant_id, app_id, subject_id)``
being provisioned — its own docstring says plainly that nothing in this service
or any sibling (no CronJob, no scheduler, no sweep) ever transitions a licence
out of ``active``. A corpse therefore heals only if somebody happens to
re-provision that exact key.

Two prod rows are in that state right now, one of them a real client's
(``subhankar-kag``, app ``kag``, expired 2026-07-18). Each is simultaneously
invisible to ``/licenses/active`` (which is time-aware, so the subject is told
it has NO licence) and still occupying the ``uq_license_active_tenant_app_subject``
partial-unique slot (``WHERE status='active'``) — the shape that made renewal
deadlock. This job is the estate-wide twin of the lazy path.

CONFIGURATION (all from env — no flags, so a CronJob manifest is the only place
a knob exists):

=====================  ==================================================
``EXPIRE_DRY_RUN``     measure and report, write nothing (default **true**)
``EXPIRE_LIMIT``       max rows transitioned in one sweep, 1..5000 (default 500)
``EXPIRE_ACTOR``       name recorded on the sweep's audit line
``DATABASE_URL``       required; from the ``licenses-env`` Secret
=====================  ==================================================

⚠️ IT SHIPS DRY-RUN. ``EXPIRE_DRY_RUN`` defaults to TRUE and a blank value reads
as the default, so an ``EXPIRE_DRY_RUN: ""`` manifest key cannot arm it by
accident. Arming is a deliberate ``EXPIRE_DRY_RUN=false``.

ENVIRONMENT — ``licenses-env`` ALONE IS ENOUGH, and that is a deliberate
difference from ``scripts/reconcile_metering.py``. That job imports ``main``,
which runs ``_validate_production_config()`` at module scope with ``ENVIRONMENT``
defaulting to ``production``, so it needs ``METERING_SERVICE_URL`` AND
``CORS_ALLOWED_ORIGINS`` on top of the Secret and dies at import without them.
This job needs only the DAL, so it imports ``models`` (like
``scripts/seed_offerings.py``) and never touches ``main``. No new Secret, no new
grant, no image change: ``scripts/`` is already ``COPY``'d into the runtime image
(``Dockerfile:74``). ⚠️ Keep it that way — adding ``import main`` here silently
adds two required env vars to every manifest that runs it.

OUTPUT is ONE JSON LINE on stdout, on EVERY path including a config failure.
This service has no Prometheus client (``main.py``: "This service has NO
Prometheus scrape annotation") and no audit table, so ``kubectl logs`` IS the
audit trail. Anything human goes to stderr.

EXIT CODES
----------
0   converged: after this run, no licence in the estate is structurally active
    with a past ``expires_at``
1   NOT converged. Any of: a DRY RUN that found stale rows (it wrote nothing, so
    they are all still there); an armed sweep that transitioned nothing while
    stale rows exist (anti-vacuity); an armed sweep that left stale rows behind
    (truncation); a sweep that selected MORE rows than the independent rule calls
    stale (over-fire); a dry run that mutated the estate
2   config / DB failure — nothing can be concluded about the estate from this run

⚠️ THE ANTI-VACUITY GUARDS — read this before changing the CronJob's env.
A sweep's happy path is "found nothing, exit 0", which is also exactly what a
sweep with a broken selection looks like, and what a DRY RUN over a completely
broken estate looks like. Four ways to be green about nothing, all guarded:

* MEASURED A BROKEN ESTATE, WROTE NOTHING. A dry run cannot transition anything,
  so judging it on "how many rows did you change" makes it green while every
  corpse remains. A dry run that found stale rows is NOT converged.
* SELECTED NOTHING WHILE CORPSES EXIST. An armed run that transitioned 0 rows
  while the independent count says stale rows exist has a broken selection.
* LEFT SOME BEHIND. ``LIMIT`` caps one sweep; a schedule whose estate outgrew its
  limit would report success forever while never reaching the tail.
* SELECTED TOO MANY. The dangerous direction. A predicate that widened (the
  ``expires_at`` bound dropped, or ``starts_at`` folded in) mass-expires a
  healthy estate, and every row it touched looks like a successful transition.

The count those guards compare against is ``models.count_stale_licenses`` —
HAND-WRITTEN SQL, deliberately NOT the sweep's own ORM query, which could only
ever agree with itself. Both are evaluated at ONE captured instant (``now`` is
passed to the count, the sweep and the recount) so a licence crossing its
``expires_at`` mid-run, or clock skew between this process and postgres, can
never be mistaken for predicate drift.

Usage::

    EXPIRE_DRY_RUN=true python scripts/expire_stale_licences.py

In-cluster: a CronJob running this repo's own image with ``envFrom: licenses-env``
and nothing else.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any

# Make `models` importable when run from the repo root or inside the licenses
# container (working dir = /app). Mirrors scripts/seed_offerings.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger("expire_stale_licences")

EXIT_CONVERGED = 0
EXIT_NOT_CONVERGED = 1
EXIT_FAILURE = 2

EVENT = "expire_stale_licenses"
DEFAULT_ACTOR = "cronjob:expire-stale-licences"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


class ConfigError(ValueError):
    """A malformed or missing ``EXPIRE_*`` / ``DATABASE_URL`` value.

    Fatal, never silently defaulted: a ``dry_run`` flag that quietly reads as
    False is a job that rewrites production licence rows when the operator asked
    it not to.
    """


class StartupError(RuntimeError):
    """``import models`` failed — a broken image, not a broken estate.

    Deferred out of module scope so the failure can be REPORTED (exit 2, with
    the JSON audit line intact) instead of killing the interpreter before a
    single line of this module runs. ``scripts/reconcile_metering.py`` pays this
    price for real because it imports ``main``; this job does not, and this class
    exists so that stays true by construction rather than by luck.
    """


def _load_models() -> Any:
    """Import ``models``, or raise ``StartupError``.

    Cheap to call repeatedly — a ``sys.modules`` hit after the first success.
    """
    try:
        import models
    except Exception as e:
        raise StartupError(str(e)) from e
    return models


def _db_manager() -> Any:
    """The DAL, built from ``DATABASE_URL``.

    THE SEAM the tests replace. Deliberately not cached in a module global:
    a cached manager would outlive the per-test database and make a second test
    in the same process talk to the first one's engine.
    """
    url = os.getenv("DATABASE_URL")
    if not url or not url.strip():
        raise ConfigError("DATABASE_URL is required (it lives in the licenses-env Secret)")
    return _load_models().DatabaseManager(url.strip())


def _env_bool(name: str, default: bool) -> bool:
    """Parse a boolean env var STRICTLY.

    A typo (``ture``) raises instead of quietly meaning False, and a BLANK value
    means "unset" — i.e. the default — so ``EXPIRE_DRY_RUN: ""`` in a manifest
    cannot arm the job.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value == "":
        return default
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"{name}={raw!r} is not a boolean (use one of {sorted(_TRUE | _FALSE)})")


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    """Parse a bounded integer env var."""
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

    A value carrying a stray newline from a YAML block scalar must not reach the
    audit line with the newline in it — that would break the one-JSON-line
    contract ``kubectl logs`` parsing depends on.
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    return raw.strip()


def _classify(
    *,
    dry_run: bool,
    limit: int,
    stale_before: int,
    stale_after: int,
    selected: list[str],
) -> tuple[int, str]:
    """Map one sweep's outcome onto the exit-code contract. Returns (code, reason).

    Order matters. The over-fire and dry-run-wrote legs come FIRST because they
    describe a broken sweep, and a broken sweep's convergence is not a meaningful
    question. See the module docstring for why each leg exists.
    """
    if len(selected) > stale_before:
        return EXIT_NOT_CONVERGED, (
            f"OVER-FIRED: the sweep selected {len(selected)} licence(s) but only "
            f"{stale_before} are stale by the independent rule (status='active' AND "
            f"expires_at IS NOT NULL AND expires_at < now). The two are evaluated at the "
            f"same instant, so this is predicate drift — a widened selection mass-expires "
            f"a healthy estate and every row it touched looks like a success. "
            f"Selected: {selected}"
        )
    if dry_run and stale_after != stale_before:
        return EXIT_NOT_CONVERGED, (
            f"DRY RUN WROTE: the stale count moved from {stale_before} to {stale_after} "
            f"during a run that is defined to write nothing. Do not trust this run, and do "
            f"not arm this job until that is explained."
        )
    if dry_run and stale_before > 0:
        # A dry run transitions nothing BY CONSTRUCTION, so "how many rows did
        # you change" is 0 whether the estate is pristine or entirely broken.
        # The estate's real state is `stale_before`.
        return EXIT_NOT_CONVERGED, (
            f"NOT CONVERGED (measure-only): {stale_before} licence(s) are structurally "
            f"active with a past expires_at and a dry run writes nothing, so all of them "
            f"remain. This is the answer about the estate, not a list of failures. "
            f"Would transition: {selected}. Re-run with EXPIRE_DRY_RUN=false to "
            f"retire them."
        )
    if not dry_run and not selected and stale_before > 0:
        return EXIT_NOT_CONVERGED, (
            f"VACUOUS SWEEP: transitioned 0 licence(s) while {stale_before} are stale by "
            f"the independent rule. A run that changes nothing over a broken estate must "
            f"never report success — the selection is not doing what this job claims."
        )
    if not dry_run and stale_after > 0:
        if len(selected) == limit:
            return EXIT_NOT_CONVERGED, (
                f"TRUNCATED SWEEP: transitioned exactly the limit ({limit}) and "
                f"{stale_after} stale licence(s) remain. Repeating this schedule chews "
                f"through the backlog {limit} at a time, but until it drains, every run "
                f"leaves corpses holding their unique-index slots. Raise EXPIRE_LIMIT "
                f"above {stale_before}."
            )
        return EXIT_NOT_CONVERGED, (
            f"NOT CONVERGED: {stale_after} stale licence(s) remain after transitioning "
            f"{len(selected)} — under the limit ({limit}), so they were selected and "
            f"not retired, or were re-created during the sweep."
        )
    return EXIT_CONVERGED, "converged"


def run(*, limit: int, dry_run: bool, actor: str) -> tuple[int, dict[str, Any]]:
    """One sweep + the guards. Returns (exit_code, the JSON payload to print)."""
    db = _db_manager()
    # ONE instant for the count, the sweep and the recount. Anything else lets a
    # licence crossing its expires_at mid-run — or clock skew between this pod
    # and postgres — masquerade as predicate drift.
    now = datetime.now(UTC)

    # Counted BEFORE the sweep: it must be a fact about the estate, not about
    # what the sweep just did to it.
    stale_before = db.count_stale_licenses(now)
    selected = db.expire_all_stale_licenses(limit=limit, dry_run=dry_run, now=now)
    stale_after = db.count_stale_licenses(now)

    code, reason = _classify(
        dry_run=dry_run,
        limit=limit,
        stale_before=stale_before,
        stale_after=stale_after,
        selected=selected,
    )
    payload: dict[str, Any] = {
        "event": EVENT,
        "actor": actor,
        "dry_run": dry_run,
        "limit": limit,
        "now": now.isoformat(),
        "stale_before": stale_before,
        "stale_after": stale_after,
        # SELECTED is what the sweep picked; TRANSITIONED is what it WROTE, and
        # on a dry run that is zero BY DEFINITION. Reporting the selection under
        # a past-tense name would put "transitioned: 2" on the audit line of a
        # run that touched nothing — the line kubectl logs ships, alerts fire on
        # and humans paste. Two fields, because both questions get asked.
        "selected": len(selected),
        "selected_license_ids": selected,
        "transitioned": 0 if dry_run else len(selected),
        "exit_code": code,
        "reason": reason,
    }
    return code, payload


def _fail(actor: str | None, error: str, **extra: Any) -> int:
    """Emit the one JSON line for a run that could not conclude, and exit 2.

    ``error`` must be a STATIC string or an env-var name — never a stringified
    exception. A psycopg error stringifies to the failing statement plus its
    bound parameters, and a connection error to the DSN; #104 removed exactly
    that from this service's HTTP surface and it must not come back on the audit
    line, which is what gets shipped, pasted and alerted on. The traceback goes
    to the logger, on stderr.
    """
    payload = {
        "event": EVENT,
        "actor": actor,
        "error": error,
        "exit_code": EXIT_FAILURE,
        **extra,
    }
    print(json.dumps(payload, sort_keys=True, default=str))
    return EXIT_FAILURE


def main_cli(argv: list[str] | None = None) -> int:
    """Entry point. Prints exactly one JSON line on stdout, ALWAYS."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        print(
            f"usage: {os.path.basename(__file__)} (configured via EXPIRE_* env)",
            file=sys.stderr,
        )
        return _fail(None, "this job takes no arguments; configure it via EXPIRE_* env")

    actor: str | None = None
    try:
        limit = _env_int("EXPIRE_LIMIT", 500, minimum=1, maximum=5000)
        # SHIPS DRY-RUN: arming is an explicit EXPIRE_DRY_RUN=false.
        dry_run = _env_bool("EXPIRE_DRY_RUN", True)
        actor = _env_str("EXPIRE_ACTOR") or DEFAULT_ACTOR
    except ConfigError as e:
        # Safe to print: ConfigError is built from an env-var NAME and a value
        # this job owns, never from a database or transport exception.
        logger.error("Refusing to run: %s", e)
        return _fail(actor, str(e), error_kind="bad_expire_env")

    try:
        _load_models()
    except StartupError:
        logger.error(
            "Expire-stale-licences cannot start — the models module would not import. "
            "This job needs only the licenses-env Secret; a failure here is a broken "
            "image, not a missing variable.",
            exc_info=True,
        )
        return _fail(actor, "models module failed to import", error_kind="startup_import")

    try:
        code, payload = run(limit=limit, dry_run=dry_run, actor=actor)
    except ConfigError as e:
        logger.error("Refusing to run: %s", e)
        return _fail(actor, str(e), error_kind="bad_expire_env")
    except Exception as e:  # DB down, schema drift, ...
        logger.error(
            "Expire-stale-licences could not complete: %s", type(e).__name__, exc_info=True
        )
        return _fail(
            actor,
            "the sweep could not complete; see the traceback on stderr",
            error_kind=type(e).__name__,
        )

    print(json.dumps(payload, sort_keys=True, default=str))
    if code != EXIT_CONVERGED:
        logger.error("expire-stale-licences exit=%d: %s", code, payload["reason"])
    return code


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s | %(message)s",
        stream=sys.stderr,
    )
    raise SystemExit(main_cli())
