"""Billing-mirror TEARDOWN for a revoked licence — shared by every revoke path.

WHY THIS IS A MODULE AND NOT TWO COPIES
---------------------------------------
There are TWO ways a licence stops being a licence in this service, and until
licences#195 only one of them told billing about it:

* ``POST /license/revoke`` (``main.revoke_token_or_license``) — the HTTP path. It
  spawns ``_teardown_quota_config`` and ``_teardown_token_budget``.
* ``scripts/reap_expired_licences.py`` — the nightly CronJob
  ``licenses-reap-expired-licences``. It performed a BARE ORM write
  (``status='revoked'``, ``revoked_at``, ``updated_at``) and NOTHING else. Armed,
  it would have been a daily unattended producer of exactly the residue
  ``_teardown_token_budget`` was written to remove: orphaned, still-SPENDABLE
  token budgets behind licences that no longer exist.

⛔ THE REAPER CANNOT IMPORT ``main``, AND THAT IS THE LITERAL ROOT CAUSE.
``main`` runs ``_validate_production_config()`` at MODULE SCOPE, builds a
``DatabaseManager`` from ``DATABASE_URL``, constructs the FastAPI app and
registers Prometheus collectors — so importing it from a CronJob would add
``METERING_SERVICE_URL`` + ``CORS_ALLOWED_ORIGINS`` as REQUIRED env to every
manifest that runs the script, and ``tests/unit/test_reap_expired_licences.py::
test_it_does_not_import_main`` pins that it must not. So the two teardowns live
HERE, and both call sites import them from here.

⛔ DO NOT RE-INLINE EITHER FUNCTION INTO A CALLER. A second copy is how the
divergence this module exists to close comes back — the reaper had no teardown
for three weeks precisely because the logic lived in a file it could not reach.
The ``source='license_sync'`` scope in particular (see ``_teardown_token_budget``)
must have exactly ONE definition: a copy that widened it would soft-delete
operator ``manual``/``topup`` grants, which no revoke can restore.

Nothing here imports ``fastapi``, ``models`` or the app. ``httpx`` and
``service_auth`` are imported lazily inside the functions, matching ``main``'s
style, so importing this module is cheap and side-effect-free apart from reading
``METERING_SERVICE_URL`` from the environment.

⚠️ CONSEQUENCE FOR THE CRONJOB MANIFEST: a job that wants the teardown must now be
given ``METERING_SERVICE_URL``. It is NOT in the ``licenses-env`` Secret. When it
is unset the reaper SKIPS the teardown and says so on its audit line rather than
reporting a failure it never attempted — see ``scripts/reap_expired_licences.py``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


# Service URLs MUST come from gitops env in production — no hardcoded
# `*.svc.cluster.local` literal (see the deployment config policy).
# The empty default fails loud at startup in production via _validate_production_config().
METERING_SERVICE_URL = os.getenv("METERING_SERVICE_URL", "")

# A SEPARATE marker from METERING_FAILURE_MARKER, deliberately.
#
# The two name opposite residues and have DIFFERENT repair paths, so sharing one
# string would make the operator's grep unable to tell them apart:
#
#   * METERING_PROVISIONING_FAILED — a live licence billing does not meter.
#     Repair: METERING_RECONCILE_PATH, which PUSHES the missing rows.
#   * QUOTA_CONFIG_TEARDOWN_FAILED — a REVOKED licence billing still meters.
#     Repair: QUOTA_TEARDOWN_RECONCILE_PATH, which DELETES the absent ones.
#
# Pointing the second at the reconcile path would be actively wrong: that sweep
# is read-before-write and only ever creates, so it can never remove the residue
# this marker reports.
#
# It is an ERROR for the same reason its sibling is (see the comment above): this
# service has no Prometheus scrape annotation, the work happens AFTER the 200 has
# gone back to the caller, and uvicorn's access log carries only HTTP statuses —
# so a log line is the ONLY channel that reaches an operator. Alert on the RATE.
QUOTA_TEARDOWN_FAILURE_MARKER = "QUOTA_CONFIG_TEARDOWN_FAILED"

# The backstop that DOES remove an orphaned mirror: billing's own reconciler,
# ``delete_absent_quota_configs``, reachable only via this route.
#
# ⚠️ CORRECTED 2026-09-06 — this used to say "two n8n workflows call it on a
# schedule", and that a teardown failure was therefore "bounded by the next
# sweep". BOTH halves are false, and the error mattered.
#
# MEASURED: the only two callers of this route are WEBHOOK-triggered
# (``onboard-customer``, ``change-plan``); the one cron workflow makes no billing
# call at all. So this backstop has NO TIME BOUND. It runs when somebody onboards
# a customer or changes a plan, and otherwise never.
#
# Consequence, stated plainly because it is the reason the fast path below
# exists: without the teardown, a revoked licence's billing mirror can persist
# INDEFINITELY — not "until the next scheduled sync". There is no sync interval
# to bound it. Treat this constant as an unbounded-latency, customer-lifecycle
# backstop, never a periodic reconcile.
QUOTA_TEARDOWN_RECONCILE_PATH = "/api/budget/sync-from-licenses"

# A THIRD marker, for the one cause that is neither a billing verdict nor a
# transport fault: the pod went away before the teardown finished.
#
# The realistic trigger is an ordinary rollout — the licence row is committed and
# the 200 is already with the caller, then SIGTERM cancels the in-flight task. The
# residue is identical to QUOTA_CONFIG_TEARDOWN_FAILED's, so the repair route is
# the same; what differs is the DIAGNOSIS, and conflating them costs an operator
# real time. A rising QUOTA_CONFIG_TEARDOWN_FAILED rate means billing is refusing
# us or is unreachable — go look at billing and at the licences SA's realm roles.
# QUOTA_CONFIG_TEARDOWN_ABANDONED means nothing is wrong with billing at all; the
# call never got to finish. Under the shared marker, a deploy would have looked
# exactly like a billing outage.
QUOTA_TEARDOWN_ABANDONED_MARKER = "QUOTA_CONFIG_TEARDOWN_ABANDONED"

# ── The same three markers for the TOKEN BUDGET half ─────────────────────────
#
# ⛔ WHY A SECOND SET AT ALL: revoking a licence tore down billing's quota mirror
# and left its TOKEN BUDGET live. MEASURED on prod 2026-09-09: of 190 active
# `token_budgets` rows, 19 (430M tokens) backed a licence whose status is
# `revoked`. `ahtapol` therefore reads 310.0M remaining on the operator's
# Budgets page while holding only 3 live licences — the allowance of 4 revoked
# ones is still being summed into its headline figure.
#
# The two halves are NOT interchangeable and must be reported apart: a quota
# config caps REQUEST RATE, a token budget caps SPEND. A rising
# TOKEN_BUDGET_TEARDOWN_FAILED rate means revoked licences are still carrying
# spending power, which is the more expensive of the two failures.
TOKEN_BUDGET_TEARDOWN_FAILURE_MARKER = "TOKEN_BUDGET_TEARDOWN_FAILED"

# Same backstop, same unbounded latency, and the same warning applies: this is a
# customer-lifecycle route, not a periodic reconcile. `deactivate_orphan_license_sync_budgets`
# is reachable ONLY through it, and billing's own docstring says nothing calls it
# on a timer — no scheduler, no CronJob, no watcher loop, no startup hook. That
# is precisely why the 19 rows above accumulated.
TOKEN_BUDGET_TEARDOWN_RECONCILE_PATH = "/api/budget/sync-from-licenses"

# The pod-went-away cause, kept distinct for the same reason as its sibling: a
# rollout must not read as a billing outage.
TOKEN_BUDGET_TEARDOWN_ABANDONED_MARKER = "TOKEN_BUDGET_TEARDOWN_ABANDONED"


#: HTTP statuses from billing that a second, identical POST could plausibly change.
#:
#: Everything else is a VERDICT, not a blip. The offering-provisioning helpers
#: used to sleep a second and re-POST on ANY non-2xx, which meant the live
#: failure — billing #74 gating the two money-config writes on a
#: ``pricing-writer`` realm role the licences SA does not hold yet — cost every
#: fr "Provision" click two 403s and a wasted second in the request path. No
#: amount of sleeping turns a permission error into a 201.
#:
#: 408/425/429 are the client-error members on purpose: a timeout, an
#: early-data replay and a rate limit are all "ask again", unlike 400/403/404/422.
_RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


def _is_retryable_transport_error(exc: Exception) -> bool:
    """Whether re-sending after this transport error could give a different answer.

    ``httpx.HTTPError`` spans BOTH "the network hiccuped" and "this request can
    never be sent". ``UnsupportedProtocol`` is the second kind: httpx raises it
    BEFORE any I/O when the URL has no ``http(s)://`` scheme, so a retry re-raises
    identically one wasted second later.

    That is not hypothetical here. ``METERING_SERVICE_URL`` defaults to ``""``
    (see its definition), the three metering helpers do NOT guard on it the way
    the two offering helpers do, and ``run_metering_reconcile`` calls
    ``_push_quota_config`` PER LICENCE with a route ceiling of ``le=5000`` —
    roughly 83 minutes of ``asyncio.sleep`` inside a single HTTP request for an
    answer fixed at the first attempt. Found by an adversarial reviewer, and it
    is exactly the harm the status-side narrowing was written to remove.

    ``InvalidURL`` is the same shape. Everything else (connect resets, timeouts,
    read errors) stays retryable.
    """
    # Imported here, matching this module's lazy-import style for httpx.
    import httpx

    return not isinstance(exc, httpx.UnsupportedProtocol | httpx.InvalidURL)


def _is_retryable_status(code: int) -> bool:
    """Whether re-sending the same request after ``code`` could give a different answer."""
    return code in _RETRYABLE_STATUSES


async def _fetch_active_budget_ids_for_license(
    client: Any,
    *,
    tenant_id: str,
    license_id: str,
    headers: dict[str, str],
) -> set[str] | None:
    """Budget ROW ids for one licence's ACTIVE ``license_sync`` budgets.

    The narrow counterpart to ``_fetch_license_sync_budget_ids``: that one answers
    "which licences are already covered" (a set of LICENCE ids, for the create
    path); this answers "which budget ROWS must die with this licence" (a set of
    BUDGET ids, for the teardown path). Same endpoint, same envelope, opposite
    question — hence a sibling rather than a parameter on the original, whose
    callers all want the covered-licence shape.

    Returns ``None`` for "unknown", exactly as its sibling does, and for the same
    reason: billing not answering is NOT billing answering "none". A teardown that
    read an unanswered list as empty would report success and leave the budget
    live.

    ``GET /api/budget/list/{tenant_id}`` returns a tenant's budgets INCLUDING
    inactive ones, so both filters below are load-bearing — ``active`` because an
    already-soft-deleted row needs no second delete, and ``source`` because
    ``manual``/``topup`` grants must never be torn down by a revoke.
    """
    import httpx

    url = f"{METERING_SERVICE_URL}/api/budget/list/{tenant_id}"
    resp = None
    for attempt in range(2):
        try:
            resp = await client.get(url, headers=headers)
            if resp.status_code < 300:
                break
            if attempt == 0 and _is_retryable_status(resp.status_code):
                await asyncio.sleep(1)
                continue
            logger.warning(
                "Could not read billing budgets for tenant %s: billing returned %d — "
                "teardown state for license %s is UNKNOWN",
                tenant_id,
                resp.status_code,
                license_id,
            )
            return None
        except httpx.HTTPError as exc:
            if attempt == 0 and _is_retryable_transport_error(exc):
                await asyncio.sleep(1)
                continue
            logger.warning(
                "Could not read billing budgets for tenant %s — teardown state for "
                "license %s is UNKNOWN",
                tenant_id,
                license_id,
                exc_info=True,
            )
            return None
    if resp is None:
        return None
    try:
        payload = resp.json()
        rows = payload.get("data") if isinstance(payload, dict) else payload
        doomed: set[str] = set()
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            if row.get("source") != "license_sync" or not row.get("active"):
                continue
            if str(row.get("license_id") or "") != str(license_id):
                continue
            row_id = row.get("id")
            if row_id:
                doomed.add(str(row_id))
        return doomed
    except Exception:
        logger.warning(
            "Could not parse billing's budget list for tenant %s — teardown state for "
            "license %s is UNKNOWN",
            tenant_id,
            license_id,
            exc_info=True,
        )
        return None


async def _metering_auth_headers(tenant_id: str, license_id: str) -> dict[str, str]:
    """Outbound headers for a licenses → billing call.

    X-Tenant-Id is the canonical tenant carrier for AUTHZ across the platform —
    it is NOT a substitute for a body field. billing-service binds both POSTs to
    pydantic request models (``QuotaConfigRequest`` / ``TokenBudgetCreate``, see
    billing_service/src/routers/quota.py + budget.py, published in
    billing_service/docs/openapi.json), and FastAPI NEVER populates a body model
    from a header. Both models list ``tenant_id`` in their ``required`` set, so
    every body MUST carry it explicitly. Vendored copies of both schemas, plus
    the guard that asserts these bodies against them, live in
    tests/support/billing_contract.py.
    """
    from service_auth import get_service_token_provider

    token = await get_service_token_provider().get_token()
    headers: dict[str, str] = {"X-Tenant-Id": tenant_id}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    else:
        logger.warning(
            "Service-account token unavailable; metering call will likely 401 for license %s",
            license_id,
        )
    return headers


async def _teardown_token_budget(license_id: str, tenant_id: str) -> bool:
    """Deactivate billing's `license_sync` token budget for a revoked licence.

    THE SIBLING OF ``_teardown_quota_config``, and it did not exist. Revoking a
    licence tore down the quota mirror and left the SPEND mirror live.

    ⭐ MEASURED on production 2026-09-09, which is why this is here: of 190 active
    ``token_budgets`` rows, **19 backed a licence whose status is ``revoked``** —
    430,000,000 tokens of allowance behind licences that no longer exist. The
    operator's Budgets page sums a tenant's active budgets, so ``ahtapol`` reads
    310.0M remaining while holding **3** live licences; 200M of that is the
    residue of 4 revoked ones.

    ⚠️ The control that made this the right diagnosis: every active row carries a
    DISTINCT ``license_id``. One budget per licence is the intended shape, so this
    is NOT duplicate syncing and must not be "fixed" by de-duplicating rows — the
    rows are fine, they have simply outlived their licences.

    ⛔ SCOPED TO ``source='license_sync'``, deliberately and permanently.
    ``manual`` and ``topup`` rows are operator grants that STACK by design and are
    frequently tenant-wide with ``license_id: null``; prod tenant
    019de842-c92a-7e37-8fdd-eb0d8c5396f9 holds exactly such a row (40M tokens).
    Tearing those down on a revoke would destroy a deliberate top-up that no
    licence ever backed. Same rule, and the same reason, as
    ``_fetch_license_sync_budget_ids``.

    ⛔ BEST-EFFORT, SPAWNED NOT AWAITED — identical contract to
    ``_teardown_quota_config``: revocation is the security-critical half and a
    billing outage must never delay or veto it. Every failure is COUNTED as a
    loud, greppable ``TOKEN_BUDGET_TEARDOWN_FAILED`` ERROR, because this service
    has no Prometheus scrape annotation and the work happens after the response
    has gone. Alert on the RATE.

    "Unknown" is not "empty". If billing does not answer the budget list, this
    reports failure rather than concluding there was nothing to tear down —
    concluding absence from an unanswered read is how the residue would become
    invisible instead of merely present.

    Billing's ``DELETE /api/budget/{budget_id}`` is itself a SOFT delete (it sets
    ``active=False``), is ``require_internal``, and answers 404 for a row that is
    already inactive — so a 404 here is benign and is treated as success.
    """
    if not METERING_SERVICE_URL:
        logger.warning(
            "METERING_SERVICE_URL unset; not tearing down the token budget for license %s "
            "— the budget will persist until the next %s sweep",
            license_id,
            TOKEN_BUDGET_TEARDOWN_RECONCILE_PATH,
        )
        return False

    import httpx

    last_status: int | None = None
    try:
        headers = await _metering_auth_headers(tenant_id, license_id)

        async with httpx.AsyncClient(timeout=5.0) as client:
            budget_ids = await _fetch_active_budget_ids_for_license(
                client,
                tenant_id=tenant_id,
                license_id=license_id,
                headers=headers,
            )
            if budget_ids is None:
                logger.error(
                    "%s license=%s tenant=%s status=list_unreadable — the licence IS revoked "
                    "but billing's budget list could not be read, so it may still hold live "
                    "spending power for it; repair with POST %s",
                    TOKEN_BUDGET_TEARDOWN_FAILURE_MARKER,
                    license_id,
                    tenant_id,
                    TOKEN_BUDGET_TEARDOWN_RECONCILE_PATH,
                )
                return False

            if not budget_ids:
                # A genuine 200 with no active license_sync row for this licence.
                # Nothing to tear down — not a failure, and not worth a marker.
                return True

            all_gone = True
            for budget_id in sorted(budget_ids):
                for attempt in range(2):
                    try:
                        resp = await client.delete(
                            f"{METERING_SERVICE_URL}/api/budget/{budget_id}",
                            headers=headers,
                        )
                        last_status = resp.status_code
                        # 404 == already inactive. The route is idempotent and
                        # says so; treating it as a failure would make every
                        # retry of a successful teardown look like residue.
                        if resp.status_code < 300 or resp.status_code == 404:
                            break
                        if not _is_retryable_status(resp.status_code):
                            all_gone = False
                            break
                        logger.warning(
                            "token-budget teardown for budget %s (license %s) returned %s: %s",
                            budget_id,
                            license_id,
                            resp.status_code,
                            resp.text[:200],
                        )
                    except httpx.HTTPError as exc:
                        logger.warning(
                            "token-budget teardown transport error for budget %s "
                            "(license %s, attempt %d)",
                            budget_id,
                            license_id,
                            attempt,
                        )
                        if not _is_retryable_transport_error(exc):
                            all_gone = False
                            break
                    if attempt == 0:
                        await asyncio.sleep(1)
                else:
                    all_gone = False

            if all_gone:
                return True

        logger.error(
            "%s license=%s tenant=%s status=%s — the licence IS revoked but billing may still "
            "hold a live token budget for it; repair with POST %s",
            TOKEN_BUDGET_TEARDOWN_FAILURE_MARKER,
            license_id,
            tenant_id,
            last_status,
            TOKEN_BUDGET_TEARDOWN_RECONCILE_PATH,
        )
        return False
    except asyncio.CancelledError:
        # Same reasoning as _teardown_quota_config's clause, and NOT redundant
        # with the one below: CancelledError derives from BaseException, so
        # without this the done-callback would report it under the PROVISIONING
        # marker, whose printed remedy only ever CREATES rows and therefore
        # cannot clear this residue.
        logger.error(
            "%s license=%s tenant=%s — the teardown was CANCELLED before it completed "
            "(pod terminating mid-rollout is the usual cause; this is NOT a billing fault). "
            "The licence IS revoked, but billing may still hold a live token budget for it; "
            "repair with POST %s",
            TOKEN_BUDGET_TEARDOWN_ABANDONED_MARKER,
            license_id,
            tenant_id,
            TOKEN_BUDGET_TEARDOWN_RECONCILE_PATH,
        )
        return False
    except Exception:
        logger.exception(
            "%s license=%s tenant=%s — unexpected error; the licence IS revoked but billing "
            "may still hold a live token budget for it; repair with POST %s",
            TOKEN_BUDGET_TEARDOWN_FAILURE_MARKER,
            license_id,
            tenant_id,
            TOKEN_BUDGET_TEARDOWN_RECONCILE_PATH,
        )
        return False


async def _teardown_quota_config(license_id: str, tenant_id: str) -> bool:
    """Fire-and-forget: DELETE the licence's request-quota mirror in billing.

    Called after a licence is revoked. Returns True iff billing accepted the
    delete. NEVER raises, and never propagates cancellation — see the two
    handlers at the bottom, which exist so that a failure here can never be
    reported under a marker whose repair path cannot fix it.

    Why this exists
    ---------------
    ``revoke_license`` used to set ``status``/``revoked_at`` and nothing else,
    while billing keeps a mirrored ``request_quota_config`` row per licence. No
    revocation path called billing's ``DELETE /api/quota/config/{license_id}``
    from anywhere — measured on origin/main, zero outbound call sites in this
    repo. The ONLY thing that removed an orphaned mirror was billing's
    ``delete_absent_quota_configs``, reachable exclusively through
    ``POST /api/budget/sync-from-licenses``.

    ⚠️ CORRECTED 2026-09-06 — this used to say that route is called "by two n8n
    workflows on a SCHEDULE", so the exposure was bounded by "the sync interval".
    MEASURED, and both halves are wrong: both callers are WEBHOOK-triggered
    (``onboard-customer``, ``change-plan``) and the only cron workflow makes no
    billing call at all. Neither is a revocation path, and neither is periodic.

    So there is NO TIME BOUND on the backstop. Before this fix, a revoked —
    possibly COMPROMISED — licence kept a live billing mirror until the next time
    somebody happened to onboard a customer or change a plan, which on a quiet
    account is INDEFINITELY. The original framing ("bounded by the next sweep")
    made the defect sound self-healing on a timer. It is not, and that absence of
    a time bound is precisely why this fast path is worth having.

    This is a FAST PATH in front of that sweep, not a replacement for it. The
    sweep stays exactly as it was and remains the backstop — an unbounded-latency
    one; deleting it would still be removing the detector that makes a failure
    here recoverable at all.

    ⛔ BEST-EFFORT ON PURPOSE — do not make this block or roll back the revoke.
    Revocation is the security-critical half; the mirror is derived state. If a
    billing outage could veto a revoke, a compromised licence would stay live
    for exactly as long as billing is down. So this runs through
    ``_spawn_background_task`` AFTER the licence row is already committed and
    the 200 already computed: there is no code path by which its verdict can
    reach the caller.

    ⚠️ Best-effort must not mean invisible — that is what made the original
    defect survive. Every failure is COUNTED as a loud, greppable
    ``QUOTA_CONFIG_TEARDOWN_FAILED`` ERROR naming the licence and the repair
    route, because this service has no Prometheus scrape annotation and the work
    happens after the response has gone (same reasoning as
    ``METERING_FAILURE_MARKER``). Alert on the RATE: a non-zero rate means
    revoked licences are accumulating live mirrors between sweeps.

    On billing's side (``billing_service/src/routers/quota.py`` ``delete_quota_config``)
    the route is ``require_internal``, idempotent, and answers 200 with
    ``deleted="0"`` when no row was there — so a 404 here is NOT "already gone",
    it is "billing has no such route" (a version skew), which IS residue and IS
    reported. That also means, unlike ``_fetch_quota_config_monthly``, there is
    no foreign-owner 404 rule on this route to give the status a second meaning.
    """
    if not METERING_SERVICE_URL:
        # Not silent: outside production this is ordinary (the var defaults to
        # ""), but it still means the mirror was not torn down, and the sweep is
        # what will do it. In production _validate_production_config() makes this
        # branch unreachable at import time.
        logger.warning(
            "METERING_SERVICE_URL unset; not tearing down the quota config for license %s "
            "— the mirror will persist until the next %s sweep",
            license_id,
            QUOTA_TEARDOWN_RECONCILE_PATH,
        )
        return False

    # Imported here, matching this module's lazy-import style for httpx.
    import httpx

    try:
        headers = await _metering_auth_headers(tenant_id, license_id)

        # The LAST status billing actually answered with, mirroring
        # _provision_offering_markup: "billing said 503 then the socket died" is
        # more useful to an operator than "never reached billing".
        last_status: int | None = None
        async with httpx.AsyncClient(timeout=5.0) as client:
            # One retry, exactly like every other billing write here. A DELETE is
            # idempotent, so re-sending is safe; but only a RETRYABLE answer is
            # worth re-sending — a 403 is a verdict about the licences SA's realm
            # roles and no amount of sleeping turns it into a 200.
            for attempt in range(2):
                try:
                    resp = await client.delete(
                        f"{METERING_SERVICE_URL}/api/quota/config/{license_id}",
                        headers=headers,
                    )
                    last_status = resp.status_code
                    if resp.status_code < 300:
                        return True
                    if not _is_retryable_status(resp.status_code):
                        break
                    logger.warning(
                        "quota-config teardown for license %s returned %s: %s",
                        license_id,
                        resp.status_code,
                        resp.text[:200],
                    )
                except httpx.HTTPError as exc:
                    logger.warning(
                        "quota-config teardown transport error for license %s (attempt %d)",
                        license_id,
                        attempt,
                    )
                    # A DETERMINISTIC transport fault is a verdict, exactly like a
                    # 403 — a SET-BUT-WRONG METERING_SERVICE_URL (the ordinary
                    # gitops typo, truthy so it sails past the guard above) raises
                    # UnsupportedProtocol BEFORE any I/O, so a retry re-raises
                    # identically a wasted second later.
                    if not _is_retryable_transport_error(exc):
                        break
                if attempt == 0:
                    await asyncio.sleep(1)

        logger.error(
            "%s license=%s tenant=%s status=%s — the licence IS revoked but billing may still "
            "hold a live quota mirror for it; repair with POST %s",
            QUOTA_TEARDOWN_FAILURE_MARKER,
            license_id,
            tenant_id,
            last_status,
            QUOTA_TEARDOWN_RECONCILE_PATH,
        )
        return False
    except asyncio.CancelledError:
        # The pod is going away mid-call — almost always an ordinary rollout.
        #
        # ⚠️ THIS CLAUSE IS NOT REDUNDANT WITH THE ONE BELOW. Since 3.8
        # ``CancelledError`` derives from ``BaseException``, not ``Exception``, so
        # without this it sails past the swallow, and ``_background_task_done``'s
        # ``task.cancelled()`` branch reports it as
        # ``METERING_PROVISIONING_FAILED ... repair with POST
        # /licenses/admin/metering/reconcile`` — the wrong marker AND a remedy
        # that only ever CREATES rows, so it cannot clear this residue. An
        # operator debugging a deploy was being sent to provision something.
        #
        # ⛔ And it deliberately does NOT re-raise, which is the one place this
        # file departs from the usual "never swallow CancelledError" rule. That
        # rule protects an ENCLOSING structure's cooperative cancellation; here
        # there is no enclosing structure — this coroutine IS the whole body of a
        # fire-and-forget task, and returning normally still ends it immediately,
        # which is all the canceller wants. Re-raising would buy nothing and would
        # re-publish the false remedy above through the done-callback.
        logger.error(
            "%s license=%s tenant=%s — the teardown was CANCELLED before it completed "
            "(pod terminating mid-rollout is the usual cause; this is NOT a billing "
            "fault and NOT a reason to look at billing). The licence IS revoked, but "
            "billing may still hold a live quota mirror for it; repair with POST %s",
            QUOTA_TEARDOWN_ABANDONED_MARKER,
            license_id,
            tenant_id,
            QUOTA_TEARDOWN_RECONCILE_PATH,
        )
        return False
    except Exception:
        # Swallow EVERYTHING ELSE. This coroutine is handed to
        # _spawn_background_task, whose done-callback logs an escaping exception
        # under METERING_FAILURE_MARKER — a marker whose printed repair path
        # (METERING_RECONCILE_PATH) PROVISIONS and therefore cannot remove this
        # residue. Letting an exception escape would publish a false remedy, so
        # the failure is reported here, under its own marker, or not at all.
        logger.error(
            "%s license=%s tenant=%s — teardown raised; the licence IS revoked but billing "
            "may still hold a live quota mirror for it; repair with POST %s",
            QUOTA_TEARDOWN_FAILURE_MARKER,
            license_id,
            tenant_id,
            QUOTA_TEARDOWN_RECONCILE_PATH,
            exc_info=True,
        )
        return False
