#!/usr/bin/env python3
r"""
Archive terminally-expired licence ROWS to ``status='revoked'``, estate-wide, from a
scheduled job. The licence-row twin of ``scripts/revoke_expired_tokens.py``.

WHY THIS EXISTS — A GAUGE THAT CAN ONLY GO UP IS NOT AN INSTRUMENT
------------------------------------------------------------------
``services/expiry_metrics.py`` buckets its licence expiry gauge on
``(l.status = 'revoked') AS revoked``. So a row with ``status='expired'`` and a past
``expires_at`` is counted under ``revoked="false"`` — the exact series
``LicenseAlreadyExpired`` alerts on::

    max(nonagentic_license_expired_rows{kind="license",revoked="false"}) > 0

Measured live 2026-09-07T14:40Z (the metric's FIRST sample, PR #3638 having merged at
13:54Z) through 2026-09-09: a CONSTANT 2, with ZERO serving impact. The alert's
``activeAt`` is a rollout artefact, not a state change.

The two rows are 30-day KAG trials that expired 2026-07-18/19: tenants ``subhankar-kag``
(``purpose: kag-30d-trial``, a real prospect) and ``system``. Nothing is blocked, and
that was MEASURED rather than reasoned: the only token ever bound to either was revoked
2026-08-24 and last used 2026-06-18; ``SELECT count(*) FROM token WHERE revoked=false
AND expires_at < now()`` is 0; and both subjects already hold ACTIVE, perpetual KAG
licences, so renewal already succeeded and the ``uq_license_active_tenant_app_subject``
partial-unique slot is free.

THE DEFECT IS THE LATCH, NOT THOSE TWO ROWS. ``scripts/expire_stale_licences.py`` moves
rows ``active -> expired``. ``scripts/revoke_expired_tokens.py`` reaps TOKENS only.
**There was no licence-row reaper at all**, so that gauge could never return to 0 and
every future expiry would accumulate on it for ever. An alert that cannot go green is an
alert that gets silenced, and a silenced alert is worse than no alert. Hand-updating the
two rows would clear the number and leave the latch — which is why this ships the
mechanism instead.

⛔ THIS IS A STATUS TRANSITION, NOT A DELETE. The row, its tokens, its ``expires_at`` and
its metadata all survive; ``status`` can be set back by hand if an archive decision was
wrong.

⚠️ ARCHIVING A LICENCE IS NOT THE WHOLE OF REVOKING ONE (licences#195)
----------------------------------------------------------------------
``POST /license/revoke`` does THREE things: it writes the row, it deletes billing's
mirrored ``request_quota_config`` (which caps request RATE) and it deactivates billing's
``license_sync`` ``token_budgets`` row (which caps SPEND). This job used to do only the
first — a bare ORM write with no reference to budget or quota anywhere. Armed, it would
therefore have been a DAILY, UNATTENDED producer of exactly the residue those teardowns
exist to remove: orphaned, still-SPENDABLE token budgets standing behind licences that no
longer exist. MEASURED on prod 2026-09-09 from the HTTP path's own gap: 19 of 190 active
budgets — 430M tokens — already outlived their licences, and billing's own reconciler runs
on NO timer.

So every archived licence now gets the SAME two teardowns, from the SAME code:
``services.metering_teardown``. ⛔ That module exists precisely because this script cannot
reach ``main`` (see the ENVIRONMENT note below) — do not re-inline either function here, and
in particular do not re-spell the ``source='license_sync'`` scope, which is what keeps an
operator's ``manual``/``topup`` grant from being destroyed by a revoke.

⛔ A DRY RUN PERFORMS NO TEARDOWN. It reports the (licence, tenant) pairs it WOULD call
billing about, on the audit line, and puts nothing on the wire — same rule as the row write.

⚠️ THE TEARDOWN NEEDS ``METERING_SERVICE_URL``, WHICH IS **NOT** IN ``licenses-env``. When it
is unset this job archives the rows and SKIPS the teardown, saying so loudly under
``REAP_TEARDOWN_SKIPPED_UNCONFIGURED`` and on the audit line — it never reports a failure it
did not attempt, and never reports success for a teardown that never happened. Arming this
job without that variable re-creates the defect above, so the CronJob manifest must carry it.

⛔ REDIS IS DELIBERATELY ABSENT HERE. The HTTP revoke path invalidates no Redis key either;
inventing a cache invalidation the authoritative path does not perform would be a NEW
divergence, in the opposite direction, and an unmeasured one.

THE PREDICATE
-------------
::

    status = 'expired'
      AND expires_at IS NOT NULL
      AND expires_at < (now - REAP_GRACE_DAYS)

* ``status = 'expired'`` — the row is ALREADY terminal. ⛔ An ``'active'`` row is not
  this job's to judge (that is ``expire_stale_licences.py``, and its population is
  ``status='active'`` — a LIVE entitlement); ``'pending'`` is a future licence;
  ``'revoked'`` is already where this job would put it, which is what makes a re-run a
  no-op.
* ``expires_at IS NOT NULL`` — written explicitly even though NULL comparisons are never
  true. ``expires_at IS NULL`` means PERPETUAL, not unpopulated (``models.py``: 256 of
  258 rows are NULL). Dropping this leg is how a reaper eats an estate.
* ``expires_at < cutoff``, and ``<`` not ``<=``, matching ``expire_stale_licences.py``
  and ``License.effective_status``.
* ``starts_at`` IS ABSENT and must stay absent — irrelevant to a row that is already
  terminal, and folding it in could only widen the selection.

⚠️ THE GRACE WINDOW IS THE ONE DELIBERATE DIFFERENCE FROM THE TOKEN REAPER
--------------------------------------------------------------------------
``'expired'`` and ``'revoked'`` are DIFFERENT facts to a reader: the first says "your
licence lapsed, renew it", the second says "somebody killed it". ``effective_status``
returns the stored value verbatim for any non-``'active'`` row, so this transition is
LOSSY in a way the token reaper's is not — a token has one flag, a licence has a
four-value state machine.

Without a grace window the two CronJobs would simply chain (``expire-stale`` 02:07Z,
this one later the same night) and ``'expired'`` would exist for a few hours in the whole
estate's history — quietly deleting the distinction ``expire_stale_licences.py`` exists
to create, and turning a lapsed customer into a revoked one overnight.

So only rows expired LONGER than ``REAP_GRACE_DAYS`` are archived. ``REAP_GRACE_DAYS=0``
reduces this to exactly the token reaper's behaviour. The consequence is deliberate and
worth stating: ``LicenseAlreadyExpired`` STILL FIRES for a recently-lapsed licence, which
is the actionable case — it just stops firing for ever once the row is genuinely
historical. The two prod rows expired ~53 days ago, so a 30-day grace covers both.

CONFIGURATION (all from env — no flags, so a CronJob manifest is the only knob):

=====================  ==================================================
``REAP_DRY_RUN``       measure and report, write nothing (default **true**)
``REAP_LIMIT``         max rows archived in one sweep, 1..5000 (default 500)
``REAP_GRACE_DAYS``    days a row must have been expired, 0..3650 (default 30)
``REAP_ACTOR``         name recorded on the sweep's audit line
``DATABASE_URL``       required; from the ``licenses-env`` Secret
=====================  ==================================================

⚠️ IT SHIPS DRY-RUN, exactly like both siblings. ``REAP_DRY_RUN`` defaults to TRUE and a
BLANK value reads as the default, so a ``REAP_DRY_RUN: ""`` manifest key cannot arm it by
accident — deleting the value DISARMS. Arming is a deliberate ``REAP_DRY_RUN=false`` in
its own separately-revertable commit, after an operator has read a dry run.

ENVIRONMENT — this pulls in ``models`` and ``services.metering_teardown``, never ``main``:
``main`` runs ``_validate_production_config()`` at module scope and would add
``METERING_SERVICE_URL`` + ``CORS_ALLOWED_ORIGINS`` as HARD-REQUIRED env to every manifest
that runs this, plus a ``DatabaseManager``, the FastAPI app and the Prometheus collectors.
⚠️ Keep it that way — ``services.metering_teardown`` was carved out of ``main`` for exactly
this reason and imports neither FastAPI nor the app. ``METERING_SERVICE_URL`` is OPTIONAL
only in the sense that its absence must not CRASH the job: present, the teardown runs;
absent, it is skipped, reported, and — if the sweep actually archived rows — graded NOT
CONVERGED (exit 1). ⛔ A skipped teardown must never exit 0: that is precisely the state in
which an armed job is a nightly, unattended producer of orphaned still-spendable budgets
while its Job status stays green. ``scripts/`` is already ``COPY``'d into the runtime
image.

OBSERVABILITY is ONE JSON LINE on stdout on EVERY path including config failure — this
service's Deployment is scraped but a CronJob pod is not, and there is no audit table, so
``kubectl logs`` IS the audit trail. Anything human goes to stderr. Same contract as
``revoke_expired_tokens.py``; the exit code is the Job's success/failure signal, so a
silent failure is a red Job, not a quiet one.

EXIT CODES
----------
0   converged: no licence in the estate is ``'expired'`` with an expiry older than the
    grace window
1   NOT converged. Any of: a DRY RUN that found reapable rows (it wrote nothing, so they
    are all still there); an armed sweep that archived nothing while reapable rows exist
    (anti-vacuity); an armed sweep that left rows behind (truncation); a sweep that
    selected MORE rows than the independent rule calls reapable (over-fire); a dry run
    that mutated the estate; an armed sweep that archived rows but could NOT tear down
    billing's mirror for at least one of them (residue — the licence is dead and its
    budget is not); an armed sweep that archived rows and SKIPPED the teardown entirely
    because ``METERING_SERVICE_URL`` is unset (``REAP_TEARDOWN_SKIPPED_UNCONFIGURED`` —
    the teardown never ran, so its work is outstanding, not done)
2   config / DB failure — nothing can be concluded about the estate from this run

⚠️ THE ANTI-VACUITY GUARDS — read before changing the CronJob's env. A sweep's happy path
is "found nothing, exit 0", which is ALSO what a broken selection and a dry run over a
rotten estate both look like. Five ways to be green about nothing, all guarded:

* MEASURED A ROTTEN ESTATE, WROTE NOTHING. A dry run archives nothing by construction, so
  judging it on rows changed makes it green while every latched row remains.
* SELECTED NOTHING WHILE ROWS EXIST. An armed run that archived 0 while the independent
  count says reapable rows exist has a broken selection.
* LEFT SOME BEHIND. ``LIMIT`` caps one sweep; an estate that outgrew its limit would
  report success for ever while never reaching the tail.
* SELECTED TOO MANY. The dangerous direction: a predicate that widened (the ``status``
  leg folded out, or ``expires_at IS NOT NULL`` dropped) would archive live and perpetual
  licences, and every row it touched would look like a success.
* A DRY RUN THAT WROTE. If the reapable count moves during a run defined to write
  nothing, the run is not trustworthy and the job must not be armed.

The count those guards compare against is ``models.count_reapable_licenses`` —
HAND-WRITTEN SQL, deliberately NOT the sweep's own ORM query, which could only ever agree
with itself. Both are evaluated against ONE captured ``cutoff``, so a licence crossing it
mid-run, or clock skew between this process and postgres, can never be mistaken for
predicate drift.

Usage::

    REAP_DRY_RUN=true python scripts/reap_expired_licences.py

In-cluster: a CronJob running this repo's own image with ``envFrom: licenses-env`` and
nothing else.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

# Make `models` importable when run from the repo root or inside the licenses
# container (working dir = /app). Mirrors scripts/expire_stale_licences.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger("reap_expired_licences")

EXIT_CONVERGED = 0
EXIT_NOT_CONVERGED = 1
EXIT_FAILURE = 2

EVENT = "reap_expired_licenses"
DEFAULT_ACTOR = "cronjob:reap-expired-licences"

#: How many licences may have their billing mirrors torn down CONCURRENTLY.
#:
#: Not 1, and not ``REAP_LIMIT``. Each licence costs two independent billing calls whose
#: worst case is ~11s apiece (two 5s attempts plus the retry sleep), so a strictly serial
#: sweep of a 500-row backlog could sit in ``asyncio.sleep`` for well over an hour inside a
#: single CronJob pod; an unbounded ``gather`` over the same backlog would instead point
#: 1,000 concurrent requests at billing, which is a self-inflicted outage. Four is small
#: enough to be invisible to billing and large enough that the wall clock is bounded by the
#: backlog, not by the worst case of every individual call.
TEARDOWN_CONCURRENCY = 4

#: ONE greppable line for the one thing this job cannot report as either success or
#: failure: it archived licences while ``METERING_SERVICE_URL`` was unset, so the teardown
#: was never ATTEMPTED. Deliberately NOT one of the four ``*_TEARDOWN_FAILED`` /
#: ``*_TEARDOWN_ABANDONED`` markers — those name a billing verdict or a killed pod, and an
#: operator alerting on their rate must not have it moved by a missing env var. This is a
#: manifest defect, and it says so.
TEARDOWN_SKIPPED_MARKER = "REAP_TEARDOWN_SKIPPED_UNCONFIGURED"

DEFAULT_LIMIT = 500
MIN_LIMIT, MAX_LIMIT = 1, 5000

#: 30 days. Long enough that ``'expired'`` remains a state a human and an API client can
#: actually observe and act on (the renewal conversation), short enough that the latched
#: gauge drains within a billing month instead of never. See the module docstring.
DEFAULT_GRACE_DAYS = 30
MIN_GRACE_DAYS, MAX_GRACE_DAYS = 0, 3650

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


class ConfigError(ValueError):
    """A malformed or missing ``REAP_*`` / ``DATABASE_URL`` value.

    Fatal, never silently defaulted: a ``dry_run`` flag that quietly reads as False is a
    job that rewrites production licence rows when the operator asked it not to, and a
    grace that quietly reads as 0 is a job that archives this morning's lapses.
    """


class StartupError(RuntimeError):
    """``import models`` failed — a broken image, not a broken estate.

    Deferred out of module scope so the failure can be REPORTED (exit 2, with the JSON
    audit line intact) instead of killing the interpreter before a single line of this
    module runs.
    """


def _load_models() -> Any:
    """Import ``models``, or raise ``StartupError``. Cheap to call repeatedly."""
    try:
        import models
    except Exception as e:
        raise StartupError(str(e)) from e
    return models


def _load_teardown() -> Any:
    """Import ``services.metering_teardown``, or raise ``StartupError``.

    THE SAME two functions the HTTP revoke path calls — not a re-implementation. Deferred
    out of module scope for the same reason ``_load_models`` is: a broken image must be
    REPORTED (exit 2, JSON audit line intact), not kill the interpreter before this module
    runs a line.

    Also THE SEAM the tests replace, so a test can drive a fake billing without a network.
    """
    try:
        from services import metering_teardown
    except Exception as e:
        raise StartupError(str(e)) from e
    return metering_teardown


def _db_manager() -> Any:
    """The DAL, built EXPLICITLY from ``DATABASE_URL``.

    ⚠️ ``DatabaseManager()`` does NOT read the environment. It used to hard-default to
    ``localhost:5432``, a connection-refused to nothing inside a pod — that defect cost
    ``revoke_expired_tokens.py`` a whole production run (licenses#162). It now raises
    ``ValueError`` without a URL; the URL must be passed in.

    Also THE SEAM the tests replace. Deliberately not cached in a module global: a cached
    manager would outlive the per-test database and make a second test in the same process
    talk to the first one's engine.
    """
    url = os.getenv("DATABASE_URL")
    if not url or not url.strip():
        raise ConfigError("DATABASE_URL is required (it lives in the licenses-env Secret)")
    return _load_models().DatabaseManager(url.strip())


def _env_bool(name: str, default: bool) -> bool:
    """Parse a boolean env var STRICTLY.

    A typo (``ture``) raises instead of quietly meaning False, and a BLANK value means
    "unset" — i.e. the default — so ``REAP_DRY_RUN: ""`` in a manifest cannot arm the job.
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

    A value carrying a stray newline from a YAML block scalar must not reach the audit
    line — that would break the one-JSON-line contract ``kubectl logs`` parsing depends on.
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    return raw.strip()


async def _teardown_one(
    teardown: Any,
    *,
    license_id: str,
    tenant_id: str,
    gate: asyncio.Semaphore,
) -> tuple[str, bool, bool]:
    """Both halves of ONE licence's billing teardown. Returns (id, quota_ok, budget_ok).

    Quota and budget go out CONCURRENTLY and are graded SEPARATELY, exactly as
    ``revoke_token_or_license`` spawns them as two independent tasks: a quota config caps
    request RATE, a token budget caps SPEND, and one failing must never suppress the
    other's attempt or its marker.

    ``return_exceptions=True`` is belt-and-braces, not the expected path. Both teardowns
    are documented never to raise — they swallow ``Exception`` AND ``CancelledError`` under
    their own markers. If one ever did escape, this must still not abort the licences after
    it in the sweep, which is the whole point of a per-licence result.
    """
    async with gate:
        quota, budget = await asyncio.gather(
            teardown._teardown_quota_config(license_id=license_id, tenant_id=tenant_id),
            teardown._teardown_token_budget(license_id=license_id, tenant_id=tenant_id),
            return_exceptions=True,
        )
    if isinstance(quota, BaseException):
        logger.error(
            "quota-config teardown raised for license %s; treating as residue",
            license_id,
            exc_info=quota,
        )
        quota = False
    if isinstance(budget, BaseException):
        logger.error(
            "token-budget teardown raised for license %s; treating as residue",
            license_id,
            exc_info=budget,
        )
        budget = False
    return license_id, bool(quota), bool(budget)


async def _teardown_all(
    teardown: Any, pairs: list[tuple[str, str]]
) -> list[tuple[str, bool, bool]]:
    """Every licence's teardown, at most ``TEARDOWN_CONCURRENCY`` licences in flight."""
    gate = asyncio.Semaphore(TEARDOWN_CONCURRENCY)
    return list(
        await asyncio.gather(
            *(
                _teardown_one(teardown, license_id=lid, tenant_id=tid, gate=gate)
                for lid, tid in pairs
            )
        )
    )


def _teardown_billing_mirrors(db: Any, license_ids: list[str], *, dry_run: bool) -> dict[str, Any]:
    """Tear down billing's quota + token-budget mirrors for the licences just archived.

    Returns the fields this sweep's audit line carries about the teardown. NEVER raises:
    the rows are already committed, so a billing problem must be REPORTED, never allowed to
    turn a successful archive into an unexplained exit 2.

    Five outcomes, and they are deliberately different words on the audit line because an
    operator has to be able to tell them apart:

    ``not_applicable``   nothing was selected, so there is nothing to tear down.
    ``dry_run``          the pairs it WOULD have called billing about, and ZERO calls made.
                         A dry run that put a DELETE on the wire would not be a dry run.
    ``skipped``          ``METERING_SERVICE_URL`` is unset. The rows ARE archived and their
                         budgets ARE still live. Loud, greppable, and NOT counted as a
                         teardown failure — nothing was attempted, so there is no billing
                         verdict to report.
    ``performed``        billing was asked, per licence, per half.
    ``error``            the teardown could not run AT ALL (see the guard below). Every
                         archived licence is reported as residue on both halves, because
                         this cannot know which calls, if any, got out.

    ⛔ A licence whose ``tenant_id`` could not be resolved is NEVER torn down under a
    guessed tenant — it is listed under ``teardown_unresolved_tenant`` and counted as
    residue. Sending billing a DELETE under the wrong tenant is a worse failure than
    leaving the mirror up.

    ⚠️ "NEVER raises" IS ENFORCED HERE, NOT ASSUMED. ``_teardown_one`` already contains a
    per-licence failure so that one refused teardown cannot abort the licences behind it,
    but that is the INNER guard and it cannot cover the work either side of it: resolving
    the tenants is a database read, ``_load_teardown`` is an import, and ``asyncio.run``
    owns a loop. If any of those raised, the sweep would die in ``main_cli``'s catch-all as
    a bare exit 2 — and the rows are ALREADY COMMITTED at that point, so the audit line
    would lose ``archived`` entirely and an operator would be told nothing about a billing
    residue this job had just created. Losing the report is worse than the failure.

    An escape is therefore recorded as residue for EVERY licence in the batch, not for
    none: some teardowns may have completed before the raise, and this function cannot
    tell which. Same discipline as ``_fetch_active_budget_ids_for_license`` returning
    ``None`` — unknown is reported as unknown and graded as failure, never as "nothing to
    do".
    """
    summary = _empty_teardown_summary()
    if not license_ids:
        return summary
    try:
        return _teardown_billing_mirrors_unguarded(db, license_ids, dry_run=dry_run)
    except Exception:
        logger.error(
            "The billing teardown could not run at all for this sweep — the %d archived "
            "licence(s) keep their quota mirrors and their license_sync token budgets, "
            "and nothing on billing's side runs on a timer to notice. The rows ARE "
            "archived; only the teardown failed. Affected: %s",
            len(license_ids),
            license_ids,
            exc_info=True,
        )
        failed = sorted(license_ids)
        return {
            "teardown_mode": "error",
            "teardown_attempted": 0,
            # BOTH halves, because an escape says nothing about which of the two ran.
            "teardown_quota_failed": failed,
            "teardown_budget_failed": failed,
            "teardown_unresolved_tenant": [],
        }


def _empty_teardown_summary() -> dict[str, Any]:
    """The teardown fields every audit line carries, in their nothing-happened state."""
    return {
        "teardown_mode": "not_applicable",
        "teardown_attempted": 0,
        "teardown_quota_failed": [],
        "teardown_budget_failed": [],
        "teardown_unresolved_tenant": [],
    }


def _teardown_billing_mirrors_unguarded(
    db: Any, license_ids: list[str], *, dry_run: bool
) -> dict[str, Any]:
    """The body of ``_teardown_billing_mirrors``. Call THAT, not this — it is the guard."""
    summary = _empty_teardown_summary()
    tenants = db.tenants_for_licenses(license_ids)
    pairs = [(lid, tenants[lid]) for lid in license_ids if tenants.get(lid)]
    unresolved = sorted(lid for lid in license_ids if not tenants.get(lid))
    summary["teardown_unresolved_tenant"] = unresolved

    if dry_run:
        # REPORT, DO NOT PERFORM. The whole value of the dry run is that an operator can
        # read what an armed run would do to billing before arming it.
        summary["teardown_mode"] = "dry_run"
        summary["teardown_would_call"] = [
            {"license_id": lid, "tenant_id": tid} for lid, tid in sorted(pairs)
        ]
        return summary

    teardown = _load_teardown()
    if not teardown.METERING_SERVICE_URL:
        summary["teardown_mode"] = "skipped"
        logger.error(
            "%s archived %d licence(s) with METERING_SERVICE_URL unset, so billing was "
            "never told: their quota mirrors and their license_sync token budgets are "
            "STILL LIVE and nothing on billing's side runs on a timer to notice. This is a "
            "CronJob manifest defect, not a billing fault — give the job "
            "METERING_SERVICE_URL. Affected: %s",
            TEARDOWN_SKIPPED_MARKER,
            len(license_ids),
            license_ids,
        )
        return summary

    summary["teardown_mode"] = "performed"
    summary["teardown_attempted"] = len(pairs)
    results = asyncio.run(_teardown_all(teardown, pairs))
    summary["teardown_quota_failed"] = sorted(lid for lid, quota_ok, _ in results if not quota_ok)
    summary["teardown_budget_failed"] = sorted(
        lid for lid, _, budget_ok in results if not budget_ok
    )
    return summary


def _classify(
    *,
    dry_run: bool,
    limit: int,
    grace_days: int,
    reapable_before: int,
    reapable_after: int,
    selected: list[str],
    teardown: dict[str, Any],
) -> tuple[int, str]:
    """Map one sweep's outcome onto the exit-code contract. Returns (code, reason).

    Order matters. The over-fire and dry-run-wrote legs come FIRST because they describe a
    broken sweep, and a broken sweep's convergence is not a meaningful question.

    ⚠️ THE RESIDUE LEG IS LAST, AND THAT IS NOT THE SAME AS BEING LEAST IMPORTANT. Only one
    reason can win, and the row-level legs above it are the ones that say the sweep itself
    is broken. Billing residue is reported on EVERY run regardless of which reason wins,
    because ``teardown_quota_failed`` / ``teardown_budget_failed`` / ``teardown_mode`` are
    unconditional fields on the audit line — so a truncated sweep that also left residue
    still ships both facts, it just names the truncation.
    """
    if len(selected) > reapable_before:
        return EXIT_NOT_CONVERGED, (
            f"OVER-FIRED: the sweep selected {len(selected)} licence(s) but only "
            f"{reapable_before} are reapable by the independent rule (status='expired' AND "
            f"expires_at IS NOT NULL AND expires_at < now-{grace_days}d). Both are evaluated "
            f"against the same cutoff, so this is predicate drift — a widened selection "
            f"archives live or perpetual licences and every row it touched looks like a "
            f"success. Selected: {selected}"
        )
    if dry_run and reapable_after != reapable_before:
        return EXIT_NOT_CONVERGED, (
            f"DRY RUN WROTE: the reapable count moved from {reapable_before} to "
            f"{reapable_after} during a run that is defined to write nothing. Do not trust "
            f"this run, and do not arm this job until that is explained."
        )
    if dry_run and reapable_before > 0:
        # A dry run archives nothing BY CONSTRUCTION, so "how many rows did you change" is
        # 0 whether the estate is pristine or entirely latched. The estate's real state is
        # `reapable_before`.
        return EXIT_NOT_CONVERGED, (
            f"NOT CONVERGED (measure-only): {reapable_before} licence(s) are status='expired' "
            f"with an expiry older than the {grace_days}-day grace window, and a dry run "
            f"writes nothing, so all of them remain — each one latching "
            f'nonagentic_license_expired_rows{{kind="license",revoked="false"}} and '
            f"pinning LicenseAlreadyExpired. This is the answer about the estate, not a list "
            f"of failures. Would archive: {selected}. Re-run with REAP_DRY_RUN=false to "
            f"archive them."
        )
    if not dry_run and not selected and reapable_before > 0:
        return EXIT_NOT_CONVERGED, (
            f"VACUOUS SWEEP: archived 0 licence(s) while {reapable_before} are reapable by "
            f"the independent rule. A run that changes nothing over a latched estate must "
            f"never report success — the selection is not doing what this job claims."
        )
    if not dry_run and reapable_after > 0:
        if len(selected) == limit:
            return EXIT_NOT_CONVERGED, (
                f"TRUNCATED SWEEP: archived exactly the limit ({limit}) and {reapable_after} "
                f"reapable licence(s) remain. Repeating this schedule chews through the "
                f"backlog {limit} at a time, but until it drains the gauge stays latched. "
                f"Raise REAP_LIMIT above {reapable_before}."
            )
        return EXIT_NOT_CONVERGED, (
            f"NOT CONVERGED: {reapable_after} reapable licence(s) remain after archiving "
            f"{len(selected)} — under the limit ({limit}), so they were selected and not "
            f"written, or were created during the sweep."
        )
    # BEFORE the residue leg, because it is the more fundamental fact: residue names a
    # teardown that RAN and was refused, this names one that never ran at all. A skipped
    # run can also carry `teardown_unresolved_tenant` (that field is filled before the
    # config check), so without this leg first the residue reason would win and blame
    # billing for a missing env var.
    if not dry_run and selected and teardown.get("teardown_mode") == "skipped":
        return EXIT_NOT_CONVERGED, (
            f"{TEARDOWN_SKIPPED_MARKER}: archived {len(selected)} licence(s) with "
            f"METERING_SERVICE_URL unset, so billing was never told — every one of them "
            f"keeps its quota mirror and its license_sync token budget, and a revoked "
            f"licence with a live token budget is SPENDABLE. This run did HALF its job and "
            f"must not report convergence for the half it skipped: armed on a pod without "
            f"that variable it would otherwise be green every night while manufacturing "
            f"exactly the orphaned budgets it exists to prevent (19 such rows / 430M tokens "
            f"were measured on production 2026-09-09). This is a CronJob MANIFEST defect — "
            f"give the job METERING_SERVICE_URL; nothing on billing's side runs on a timer "
            f"to notice. Affected: {selected}"
        )
    quota_failed = list(teardown.get("teardown_quota_failed") or [])
    budget_failed = list(teardown.get("teardown_budget_failed") or [])
    unresolved = list(teardown.get("teardown_unresolved_tenant") or [])
    if not dry_run and (quota_failed or budget_failed or unresolved):
        return EXIT_NOT_CONVERGED, (
            f"BILLING RESIDUE: the licence rows ARE archived, but billing still holds a "
            f"mirror for some of them — quota teardown failed for {quota_failed}, token "
            f"budget teardown failed for {budget_failed}, tenant unresolvable for "
            f"{unresolved}. A revoked licence with a live license_sync token budget is "
            f"SPENDABLE, and billing's own reconciler "
            f"(POST /api/budget/sync-from-licenses) runs on NO timer, so this does not "
            f"heal on its own. The per-licence cause is on stderr under "
            f"QUOTA_CONFIG_TEARDOWN_FAILED / TOKEN_BUDGET_TEARDOWN_FAILED (billing refused "
            f"us or was unreachable) or ..._ABANDONED (this pod was terminated mid-call — "
            f"NOT a billing fault)."
        )
    return EXIT_CONVERGED, "converged"


def run(*, limit: int, grace_days: int, dry_run: bool, actor: str) -> tuple[int, dict[str, Any]]:
    """One sweep + the guards. Returns (exit_code, the JSON payload to print)."""
    db = _db_manager()
    # ONE instant, ONE cutoff, for the count, the sweep and the recount. Anything else
    # lets a licence crossing the cutoff mid-run — or clock skew between this pod and
    # postgres — masquerade as predicate drift.
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=grace_days)

    # Counted BEFORE the sweep: it must be a fact about the estate, not about what the
    # sweep just did to it.
    reapable_before = db.count_reapable_licenses(cutoff)
    selected = db.reap_all_expired_licenses(cutoff=cutoff, limit=limit, dry_run=dry_run, now=now)
    reapable_after = db.count_reapable_licenses(cutoff)

    # AFTER the rows are committed, exactly like the HTTP revoke path, and for the same
    # reason: revocation is the security-critical half and a billing outage must never
    # delay or veto it. On a dry run this REPORTS and calls nothing.
    teardown = _teardown_billing_mirrors(db, selected, dry_run=dry_run)

    code, reason = _classify(
        dry_run=dry_run,
        limit=limit,
        grace_days=grace_days,
        reapable_before=reapable_before,
        reapable_after=reapable_after,
        selected=selected,
        teardown=teardown,
    )
    payload: dict[str, Any] = {
        "event": EVENT,
        "actor": actor,
        "dry_run": dry_run,
        "limit": limit,
        "grace_days": grace_days,
        "now": now.isoformat(),
        "cutoff": cutoff.isoformat(),
        "reapable_before": reapable_before,
        "reapable_after": reapable_after,
        # SELECTED is what the sweep picked; ARCHIVED is what it WROTE, and on a dry run
        # that is zero BY DEFINITION. Reporting the selection under a past-tense name
        # would put "archived: 2" on the audit line of a run that touched nothing — the
        # line kubectl logs ships, alerts fire on and humans paste. Two fields, because
        # both questions get asked.
        "selected": len(selected),
        "selected_license_ids": selected,
        "archived": 0 if dry_run else len(selected),
        # UNCONDITIONAL, on every path. A teardown that is skipped, or that failed for one
        # licence out of forty, has to be readable from the one line kubectl logs ships —
        # only ONE `reason` can win, so these fields are how the billing half stays visible
        # even when a row-level reason names something else.
        **teardown,
        "exit_code": code,
        "reason": reason,
    }
    return code, payload


def _fail(actor: str | None, error: str, **extra: Any) -> int:
    """Emit the one JSON line for a run that could not conclude, and exit 2.

    ``error`` must be a STATIC string or an env-var name — never a stringified exception.
    A psycopg error stringifies to the failing statement plus its bound parameters, and a
    connection error to the DSN; licenses#104 removed exactly that from this service's
    HTTP surface and it must not come back on the audit line, which is what gets shipped,
    pasted and alerted on. The traceback goes to the logger, on stderr.
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
            f"usage: {os.path.basename(__file__)} (configured via REAP_* env)",
            file=sys.stderr,
        )
        return _fail(None, "this job takes no arguments; configure it via REAP_* env")

    actor: str | None = None
    try:
        limit = _env_int("REAP_LIMIT", DEFAULT_LIMIT, minimum=MIN_LIMIT, maximum=MAX_LIMIT)
        grace_days = _env_int(
            "REAP_GRACE_DAYS",
            DEFAULT_GRACE_DAYS,
            minimum=MIN_GRACE_DAYS,
            maximum=MAX_GRACE_DAYS,
        )
        # SHIPS DRY-RUN: arming is an explicit REAP_DRY_RUN=false.
        dry_run = _env_bool("REAP_DRY_RUN", True)
        actor = _env_str("REAP_ACTOR") or DEFAULT_ACTOR
    except ConfigError as e:
        # Safe to print: ConfigError is built from an env-var NAME and a value this job
        # owns, never from a database or transport exception.
        logger.error("Refusing to run: %s", e)
        return _fail(actor, str(e), error_kind="bad_reap_env")

    try:
        _load_models()
    except StartupError:
        logger.error(
            "Reap-expired-licences cannot start — the models module would not import. "
            "This job needs only the licenses-env Secret; a failure here is a broken "
            "image, not a missing variable.",
            exc_info=True,
        )
        return _fail(actor, "models module failed to import", error_kind="startup_import")

    try:
        code, payload = run(limit=limit, grace_days=grace_days, dry_run=dry_run, actor=actor)
    except ConfigError as e:
        logger.error("Refusing to run: %s", e)
        return _fail(actor, str(e), error_kind="bad_reap_env")
    except Exception as e:  # DB down, schema drift, ...
        logger.error(
            "Reap-expired-licences could not complete: %s", type(e).__name__, exc_info=True
        )
        return _fail(
            actor,
            "the sweep could not complete; see the traceback on stderr",
            error_kind=type(e).__name__,
        )

    print(json.dumps(payload, sort_keys=True, default=str))
    if code != EXIT_CONVERGED:
        logger.error("reap-expired-licences exit=%d: %s", code, payload["reason"])
    return code


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s | %(message)s",
        stream=sys.stderr,
    )
    raise SystemExit(main_cli())
