#!/usr/bin/env python3
r"""Revoke time-expired-but-still-unrevoked tokens, estate-wide, from a scheduled job.

WHY THIS EXISTS
---------------
`revoked` is a flag this service sets; `expires_at` is a clock. **Nothing moves a token
from the second state to the first.** `revoke_tokens_for_license` is scoped to one licence
and only runs on an explicit revoke or a rotation, so expired-but-unrevoked rows
accumulate for ever.

That is precisely the population #135 had to defend against. Its `max()` over
`Token.revoked.is_(False)` meant "unrevoked", not "unexpired", so for a licence whose every
unrevoked token had expired it returned a PAST timestamp: the rotation minted a credential
that was already dead **and the same transaction destroyed the tokens it replaced**.
Measured in prod 2026-08-17: 2 tokens in exactly that state, one of them on an `active`,
perpetual licence where the licence-scoped 409 gate passes.

WHY NOT THE n8n WORKFLOW THAT WAS SUPPOSED TO DO THIS
-----------------------------------------------------
`License Expiration Handler` (`JRXQyTxn8T7YNP1K`, 04:00Z daily) has run 12/12 `success`
while revoking nothing. Its first node lists licences via `GET /licenses?status=active`,
which is **tenant-scoped by the `X-Tenant-Id` header**, and its credential carries a single
tenant — one that owns neither of the two affected licences. It also sends a tenant *slug*
where a UUID is required, so it takes a 400, swallows it via `continueErrorOutput`, and
reports success.

Fixing the credential would make it green and still revoke nothing. **An estate-wide sweep
cannot be built on a tenant-scoped list call.** So this runs at the DAL, with no tenant
concept at all.

CONFIGURATION (all from env — no flags, so a CronJob manifest is the only knob)

=====================  ==================================================
``REVOKE_DRY_RUN``     measure and report, write nothing (default **true**)
``REVOKE_LIMIT``       max tokens revoked in one sweep, 1..5000 (default 500)
``REVOKE_ACTOR``       name recorded on the sweep's audit line
``DATABASE_URL``       required; from the ``licenses-env`` Secret
=====================  ==================================================

⚠️ IT SHIPS DRY-RUN, deliberately, exactly like `expire_stale_licences.py`.
`REVOKE_DRY_RUN` defaults to TRUE and a blank value reads as the default, so a
`REVOKE_DRY_RUN: ""` manifest key cannot arm it by accident. Arming is a deliberate
`REVOKE_DRY_RUN=false`. **Revoking a token is not reversible from this script** — the row
carries no "unrevoke" — so the default is the safe direction and the operator arms it after
reading a dry run.

This imports `models` ONLY, never `main`: `main` runs `_validate_production_config()` at
module scope and would add `METERING_SERVICE_URL` + `CORS_ALLOWED_ORIGINS` as required env
to every manifest that runs this. Keep it that way.

OUTPUT is ONE JSON LINE on stdout on EVERY path including config failure — this service has
no Prometheus client and no audit table, so `kubectl logs` IS the audit trail. Anything
human goes to stderr.

EXIT CODES
----------
0   converged: after this run, no token in the estate is unrevoked with a past `expires_at`
1   NOT converged. Any of: a DRY RUN that found expired tokens (it wrote nothing, so they
    are all still there); an armed sweep that revoked nothing while expired tokens exist
    (anti-vacuity); an armed sweep that left some behind (truncation); a sweep that touched
    MORE rows than the independent rule calls expired (over-fire); a dry run that mutated
2   config / DB failure — nothing can be concluded about the estate from this run

⚠️ THE ANTI-VACUITY GUARDS — read before changing the CronJob's env.
A sweep's happy path is "found nothing, exit 0", which is also what a broken selection and
a dry run over a rotten estate both look like. Four ways to be green about nothing:

* MEASURED A ROTTEN ESTATE, WROTE NOTHING. A dry run cannot revoke, so judging it on rows
  changed makes it green while every expired token remains. A dry run that FOUND rows is
  NOT converged.
* SELECTED NOTHING WHILE ROWS EXIST. An armed run that revoked 0 while the independent
  count says expired tokens exist has a broken selection.
* LEFT SOME BEHIND. `LIMIT` caps one sweep; an estate that outgrew its limit would report
  success for ever while never reaching the tail.
* REVOKED TOO MANY. The dangerous direction, and worse here than for licences because a
  revoked token cannot be un-revoked: a predicate that widened (the `expires_at` bound
  dropped, or `revoked` folded out) would kill live credentials, and every row it touched
  looks like a successful revocation.

The count those guards compare against is `models.count_expired_unrevoked_tokens` —
HAND-WRITTEN SQL, deliberately NOT the sweep's own ORM query, which could only ever agree
with itself. Both are evaluated at ONE captured instant (`now` is passed to the count, the
sweep and the recount) so a token crossing its `expires_at` mid-run, or clock skew between
this process and postgres, can never be mistaken for predicate drift.

Usage::

    REVOKE_DRY_RUN=true python scripts/revoke_expired_tokens.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import DatabaseManager  # noqa: E402

_MIN_LIMIT, _MAX_LIMIT, _DEFAULT_LIMIT = 1, 5000, 500


def _db_manager() -> DatabaseManager:
    """The DAL, built EXPLICITLY from ``DATABASE_URL``.

    ⚠️ ``DatabaseManager()`` does NOT read the environment. It used to hard-default
    to ``localhost:5432`` (``models.py`` ``__init__``); in a pod that was a
    connection-refused to nothing — the first production dry run returned
    ``status=db_error`` / exit 2 against ``127.0.0.1:5432``. It now raises
    ``ValueError`` without a URL.

    The URL must therefore be passed in, exactly as
    ``scripts/expire_stale_licences.py`` does.

    Also the seam the tests replace. Deliberately not cached in a module global: a
    cached manager would outlive the per-test database and make a second test in the
    same process talk to the first one's engine.
    """
    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url:
        raise ValueError("DATABASE_URL is required (it lives in the licenses-env Secret)")
    return DatabaseManager(url)


def _emit(payload: dict[str, Any]) -> None:
    """One JSON line on stdout. The audit trail — emitted on every path."""
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    sys.stdout.flush()


def _env_flag(name: str, default: bool) -> bool:
    """A BLANK value reads as the default, so `REVOKE_DRY_RUN: ""` cannot arm the sweep."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    return raw.lower() not in {"false", "0", "no", "off"}


def _env_limit() -> int:
    raw = (os.getenv("REVOKE_LIMIT") or "").strip()
    if not raw:
        return _DEFAULT_LIMIT
    value = int(raw)
    if not _MIN_LIMIT <= value <= _MAX_LIMIT:
        raise ValueError(f"REVOKE_LIMIT must be {_MIN_LIMIT}..{_MAX_LIMIT}, got {value}")
    return value


def main_cli(argv: list[str] | None = None) -> int:
    actor = (os.getenv("REVOKE_ACTOR") or "unknown").strip() or "unknown"
    dry_run = _env_flag("REVOKE_DRY_RUN", default=True)

    try:
        limit = _env_limit()
    except ValueError as exc:
        _emit(
            {
                "job": "revoke_expired_tokens",
                "actor": actor,
                "status": "config_error",
                "error": str(exc),
                "dry_run": dry_run,
            }
        )
        return 2

    if not (os.getenv("DATABASE_URL") or "").strip():
        _emit(
            {
                "job": "revoke_expired_tokens",
                "actor": actor,
                "status": "config_error",
                "error": "DATABASE_URL is required",
                "dry_run": dry_run,
            }
        )
        return 2

    # ONE instant for count, sweep and recount — see the module docstring.
    now = datetime.now(UTC)
    try:
        db = _db_manager()
        before = db.count_expired_unrevoked_tokens(now)
        acted = db.revoke_all_expired_tokens(limit=limit, dry_run=dry_run, now=now)
        after = db.count_expired_unrevoked_tokens(now)
    except Exception as exc:  # noqa: BLE001 — nothing can be concluded; say so and exit 2
        _emit(
            {
                "job": "revoke_expired_tokens",
                "actor": actor,
                "status": "db_error",
                "error": f"{type(exc).__name__}: {exc}",
                "dry_run": dry_run,
            }
        )
        return 2

    problems: list[str] = []
    if dry_run:
        if after != before:
            problems.append(f"dry_run_mutated: before={before} after={after}")
        if before:
            problems.append(f"dry_run_found_expired_tokens: {before} still unrevoked")
    else:
        if before and not acted:
            problems.append(f"selected_nothing_while_{before}_expired_tokens_exist")
        if len(acted) > before:
            problems.append(f"over_fire: revoked {len(acted)} but only {before} were expired")
        if after:
            problems.append(f"left_behind: {after} still unrevoked (limit={limit})")

    _emit(
        {
            "job": "revoke_expired_tokens",
            "actor": actor,
            "status": "ok" if not problems else "not_converged",
            "dry_run": dry_run,
            "limit": limit,
            "expired_before": before,
            "acted": len(acted),
            "expired_after": after,
            "converged": not problems,
            "problems": problems,
            "at": now.isoformat(),
        }
    )
    if problems:
        print(f"NOT CONVERGED: {'; '.join(problems)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli(sys.argv[1:]))
