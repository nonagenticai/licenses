"""Bundle ensure: give a tenant one per-app licence for every app in a bundle.

The decision logic that ``scripts/provision_tenant_bundle.py`` ``main()`` always ran
(exists / adopt / insert / REFUSE), extracted so the operator script and the HTTP
route ``POST /internal/tenants/{tenant_id}/bundles/{bundle_key}/ensure``
(tenant-inference-provisioning PLAN §2.2) run ONE copy of it. The script keeps its
CLI, its printed table and its exit codes; read its module docstring for the full
rationale of every rule below (service-subject naming, tenant-level vs member
licences, plan preference, metering, overrides).

PER-APP DECISION, over TENANT-LEVEL licences only (a member's licence is never
adopted):
  exists   a tenant-level licence for (tenant, app) already carries this bundle.
  adopt    exactly ONE tenant-level (tenant, app) licence exists and is unbundled.
  insert   no tenant-level (tenant, app) licence: create subject + licence, then
           provision its metering (the quota; the token budget only for a tenant
           whose ``budget_currency`` is NULL — see ``main._provision_budget``).
  REFUSE   ``refuse_reason`` is one of ``REFUSE_REASONS``; nothing written for it.

DEPENDENCIES (migration 059). A bundle lists only what is SOLD; the apps its apps
call for the tenant are ``app_requires`` edges. ``plan`` also decides every app in
the transitive closure (``services.app_requires.closure``; a cycle is refused
``dependency_cycle``, nothing written), with ``plan_dependency``:
  exists   the tenant already holds ONE tenant-level licence for the app — whatever
           bundle it sits in, or none, or an earlier bundle's dependency licence. It
           is SHARED: never a second licence per (tenant, app) (billing drops a
           (tenant, app) key with two candidates; bundle-token would 409).
  insert   none: a DEPENDENCY licence (``is_dependency``, ``bundle_id`` NULL —
           shared, so it names no bundle), minted only through bundle-token.
  REFUSE   several tenant-level candidates / no plan / app key too long.
A later bundle that SELLS the app adopts the dependency licence (unbundled) and
the adoption clears ``is_dependency``. Grants: APP-SCOPED ``tenant_dependency_grant``
rows — (``caller_owner_key``, required app) for every edge walked and the bundle's
``bundle_caller`` (owner, app) pairs — beside 050's issuer grants, which are unchanged.

``plan`` only reads. ``apply`` writes what ``plan`` decided. ``plan_and_apply`` is
both (the script). ``plan_and_apply_async`` / ``apply_async`` are the route's form:
DB work in a worker thread, metering awaited on the app's own event loop — never
``asyncio.run`` inside the served process.

⛔ ONE ENSURE PER TENANT AT A TIME. ``plan_and_apply`` / ``plan_and_apply_async``
(when applying) hold ``pg_advisory_xact_lock`` keyed on the TENANT across plan AND
apply — see ``_TenantEnsureLock``. Without it, prod 2026-10-14 09:57Z: CommonPractice
retried on a client timeout while the first ensure was still applying, both pods
planned the same ``insert``, and the loser's INSERT hit
``uq_license_active_tenant_app_subject`` -> 500. Tenant-level, not tenant+bundle,
because ``plan_app`` decides ``other_bundle`` from OTHER bundles' rows.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable
from typing import Any, cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from models import (
    App,
    ClientSafeError,
    DatabaseManager,
    is_externally_held_subject,
    is_tenant_level_subject,
    service_subject_app_key_problem,
    service_subject_sub,
)
from services.app_requires import DependencyCycleError, Edge, closure

logger = logging.getLogger(__name__)

DEFAULT_PLAN_CODE = "standard"
SUBJECT_REALM = "service"

# license.token_budget_override / quota_override are INTEGER (migration 041).
MAX_OVERRIDE = 2_147_483_647

#: Machine-readable REFUSE causes. The first three are the §2.2 contract's; the
#: other three name causes the script has always refused that the contract's list
#: does not mention (an app key too long for a service subject, a tenant-level
#: licence already bound to ANOTHER bundle, and an adoption that lost a race).
REFUSE_SEVERAL_CANDIDATES = "several_candidates"
REFUSE_MEMBER_LICENCE = "member_licence"
REFUSE_NO_PLAN = "no_plan"
REFUSE_OTHER_BUNDLE = "other_bundle"
REFUSE_APP_KEY_TOO_LONG = "app_key_too_long"
REFUSE_LOST_RACE = "lost_race"
REFUSE_REASONS: tuple[str, ...] = (
    REFUSE_SEVERAL_CANDIDATES,
    REFUSE_MEMBER_LICENCE,
    REFUSE_NO_PLAN,
    REFUSE_OTHER_BUNDLE,
    REFUSE_APP_KEY_TOO_LONG,
    REFUSE_LOST_RACE,
)


class EnsureError(ClientSafeError):
    """The whole request is refused before any per-app decision. Nothing written.

    A ``ClientSafeError``: every message is authored here from the caller's own
    tenant id / bundle key, so the HTTP route may echo it.

    ``code`` is one of: ``tenant_not_found``, ``tenant_archived``,
    ``bundle_not_found`` (unknown or revoked), ``bundle_empty``,
    ``dependency_cycle`` (``app_requires`` holds a cycle reachable from the bundle),
    ``token_budget_for_money_tenant``, ``ensure_in_progress`` (another ensure for
    this tenant held the tenant lock past ``ENSURE_LOCK_TIMEOUT``; retry).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


#: The unique index a bundle INSERT can lose against (one ACTIVE licence per
#: (tenant, app, subject)). Only THIS IntegrityError is a lost race.
UQ_ACTIVE_TENANT_APP_SUBJECT = "uq_license_active_tenant_app_subject"

#: How long an ensure waits for another ensure of the SAME tenant to finish
#: (Postgres ``lock_timeout`` syntax). Bounded so a stuck holder cannot park worker
#: threads forever; on expiry the request is refused ``ensure_in_progress``.
ENSURE_LOCK_TIMEOUT = "60s"


def tenant_lock_key(tenant_id: str) -> str:
    """The text hashed (``hashtextextended(_, 0)``) into the tenant's advisory-lock key."""
    return f"bundle_ensure:{tenant_id}"


class _TenantEnsureLock:
    """Hold ``pg_advisory_xact_lock`` for one tenant across a whole plan + apply.

    WHY A DEDICATED HOLDER TRANSACTION, NOT ONE TRANSACTION FOR ALL THE WORK.
    Every ``DatabaseManager`` method plan/apply calls opens and commits its OWN
    session (``create_or_get_subject``'s tenant-principal guard, ``create_license``,
    ``ensure_bundle_grants``, ...). Folding them into one transaction would mean a
    second, session-threaded copy of each. Instead one connection opens a
    transaction, takes the TRANSACTION-scoped lock, and stays open (idle) until the
    ensure ends; the lock is released by its ROLLBACK — or by Postgres if the
    process dies and the connection drops, so it can never leak.

    ⛔ pgbouncer (transaction pooling) is SAFE here and a session-level
    ``pg_advisory_lock`` would NOT be: an open transaction pins its client to ONE
    server backend until COMMIT/ROLLBACK, so the xact lock, its acquisition and its
    release all live on that backend. ``lock_timeout`` is set with
    ``set_config(..., true)`` (= ``SET LOCAL``), so it dies with the transaction and
    never leaks to the next pgbouncer client.

    Cost: one extra server connection per in-flight ensure (pool ``licenses`` = 20).
    ⚠️ ``idle_in_transaction_session_timeout`` (prod: 5min) would kill the holder of
    an ensure slower than that; the release then logs and carries on — the work
    already done is per-app committed and the unique index plus ``_apply_one``'s
    race handling still prevent a duplicate.

    CANCELLATION: ``acquire`` may still be blocked in its worker thread when the
    awaiting request is cancelled. ``release`` then marks the lock released, and an
    ``acquire`` that obtains the lock afterwards ends its own transaction at once
    instead of holding the tenant's lock with nobody left to release it.
    """

    def __init__(self, db: DatabaseManager, tenant_id: str) -> None:
        self.db = db
        self.tenant_id = tenant_id
        self.conn: Any = None
        self._mutex = threading.Lock()
        self._released = False

    def acquire(self) -> None:
        conn = self.db.engine.connect()
        try:
            conn.execute(
                text("SELECT set_config('lock_timeout', :t, true)"), {"t": ENSURE_LOCK_TIMEOUT}
            )
            conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                {"k": tenant_lock_key(self.tenant_id)},
            )
        except OperationalError as e:
            conn.close()
            if getattr(e.orig, "sqlstate", None) == "55P03":  # lock_not_available
                raise EnsureError(
                    "ensure_in_progress",
                    f"another bundle ensure for tenant {self.tenant_id} is still running; "
                    "retry once it has finished",
                ) from e
            raise
        except BaseException:
            conn.close()
            raise
        with self._mutex:
            if not self._released:
                self.conn = conn
                return
        self._end(conn)  # released (cancelled) while we were waiting for the lock

    def release(self) -> None:
        with self._mutex:
            self._released = True
            conn, self.conn = self.conn, None
        if conn is not None:
            self._end(conn)

    def _end(self, conn: Any) -> None:
        try:
            conn.rollback()  # ends the holder transaction: releases the xact lock
        except Exception as e:  # noqa: BLE001 — holder already gone; lock went with it
            logger.warning(
                "bundle_ensure lock holder for tenant %s ended abnormally (%s); the "
                "lock was released with its connection",
                self.tenant_id,
                type(e).__name__,
            )
        finally:
            conn.close()


class AppPlan:
    """One app's provisioning decision. A plain class, not a dataclass: the tests load
    the script via importlib without registering it in sys.modules, which dataclass
    field resolution requires."""

    def __init__(
        self,
        app_key: str,
        app_id: UUID,
        action: str,  # exists | adopt | insert | REFUSE
        license_id: str = "",
        subject: str = "",
        note: str = "",
        candidates: list[str] | None = None,
        plan_code: str = "",
        plan_id: UUID | None = None,
        refuse_reason: str = "",
        existing_plan_id: UUID | None = None,
        dependency: bool = False,
    ) -> None:
        self.app_key = app_key
        # Migration 059: decided as a DEPENDENCY of the bundle (``plan_dependency``),
        # not one of its own apps.
        self.dependency = dependency
        self.app_id = app_id
        self.action = action
        self.license_id = license_id
        self.subject = subject
        self.note = note
        self.candidates = candidates or []
        # Set only for ``insert``: the first plan code that exists on this app.
        self.plan_code = plan_code
        self.plan_id = plan_id
        # Set only for ``REFUSE``: one of REFUSE_REASONS.
        self.refuse_reason = refuse_reason
        # Set only for ``exists`` / ``adopt``: the plan the held licence is on.
        self.existing_plan_id = existing_plan_id
        # Set by ``apply`` for an insert whose metering push did not complete.
        self.metering_pending = ""


class EnsurePlan:
    """What ``plan`` decided for the whole bundle (read-only)."""

    def __init__(
        self,
        tenant: Any,
        bundle_key: str,
        bundle_id: UUID,
        holds: bool,
        apps: list[AppPlan],
        plans: list[str],
        quota_override: int | None,
        token_budget_override: int | None,
        dependency_grants: list[tuple[str, str]] | None = None,
    ) -> None:
        self.tenant = tenant
        self.tenant_id = str(tenant.id)
        self.budget_currency: str | None = tenant.budget_currency
        self.bundle_key = bundle_key
        self.bundle_id = bundle_id
        self.holds = holds
        self.apps = apps
        self.plans = plans
        self.quota_override = quota_override
        self.token_budget_override = token_budget_override
        self.lost_race: list[str] = []
        self.unmetered: list[str] = []
        # Owners newly granted access to the tenant by this ensure (migration 050).
        self.grants_created: list[str] = []
        # Migration 059: (owner, app) APP-SCOPED dependency grants to write.
        self.dependency_grants: list[tuple[str, str]] = dependency_grants or []
        self.applied = False

    @property
    def refused(self) -> list[AppPlan]:
        return [p for p in self.apps if p.action == "REFUSE"]


def owed_metering(
    db: DatabaseManager,
    plan_id: UUID,
    token_budget_override: int | None = None,
    quota_override: int | None = None,
    *,
    money_tenant: bool = False,
) -> str:
    """What provisioning a NEW licence on ``plan_id`` pushes to billing (read-only).

    An override (non-None) replaces the plan's value, exactly as
    ``main._provision_budget`` applies it, and is marked ``(override; plan: X)``.
    ``money_tenant``: the tenant's ``budget_currency`` is set, so no token budget is
    pushed whatever the plan says (migration 043).
    """
    plan = db.get_plan(plan_id)
    if plan is None:
        return "plan row missing — nothing can be provisioned"
    budget, quota = plan.token_budget_monthly, plan.quota_monthly
    parts = []
    if money_tenant:
        parts.append("token_budget=none (money tenant)")
    elif token_budget_override is not None:
        parts.append(f"token_budget={token_budget_override} (override; plan: {budget})")
    elif budget is not None and budget >= 0:
        parts.append(f"token_budget={budget}")
    if quota_override is not None:
        parts.append(f"quota={quota_override} (override; plan: {quota})")
    elif quota is not None and quota >= 0:
        parts.append(f"quota={quota}")
    return ", ".join(parts) if parts else "nothing owed (unlimited plan)"


def _metering_call(
    db: DatabaseManager,
    license_id: str,
    tenant_id: str,
    plan_id: str,
    app_id: str,
    token_budget_override: int | None,
    quota_override: int | None,
) -> tuple[Any, dict[str, Any]] | str:
    """Import ``main`` and build the ``provision_license_metering`` call, or say why not.

    ``main`` is imported lazily, so a dry-run never imports it (its module scope
    validates production config and builds a DatabaseManager). ``main.db_manager`` is
    pointed at ``db`` first when they differ (the operator script), so the
    ``metering_provisioned_at`` stamp lands on the database the licence was just
    written to. In the served pod they are the same object.
    """
    try:
        import main
    except Exception as e:  # noqa: BLE001 — reported, licence left for the sweep
        return f"import main failed ({type(e).__name__}: {e})"
    if main.db_manager is not db:
        main.db_manager = db
    kwargs: dict[str, Any] = {
        "license_id": license_id,
        "tenant_id": tenant_id,
        "plan_id": plan_id,
        "app_id": app_id,
    }
    if token_budget_override is not None:
        kwargs["token_budget_override"] = token_budget_override
    if quota_override is not None:
        kwargs["quota_override"] = quota_override
    return main.provision_license_metering, kwargs


def _metering_outcome(ok: bool) -> str:
    return "" if ok else "billing did not accept every push (log: METERING_PROVISIONING_FAILED)"


def provision_metering(
    db: DatabaseManager,
    license_id: str,
    tenant_id: str,
    plan_id: str,
    app_id: str,
    token_budget_override: int | None = None,
    quota_override: int | None = None,
) -> str:
    """Provision billing for a licence just created. Returns '' on success, else why.

    Goes through ``main.provision_license_metering`` — the one path the HTTP create
    route uses — never a copy.

    ⛔ SCRIPT ONLY (``scripts/provision_tenant_bundle.py``, its own process). It runs
    its own event loop (``asyncio.run``), which is only safe where no other loop
    shares this process's async singletons. In the served pod the
    ``ServiceTokenProvider`` singleton's ``asyncio.Lock`` binds to the FIRST loop that
    contends it, so a second loop on a worker thread poisons token refresh for the
    uvicorn loop ("bound to a different event loop") until the pod restarts. The
    route therefore uses ``provision_metering_async`` on the app's own loop.
    """
    call = _metering_call(
        db, license_id, tenant_id, plan_id, app_id, token_budget_override, quota_override
    )
    if isinstance(call, str):
        return call
    fn, kwargs = call
    try:
        ok = asyncio.run(fn(**kwargs))
    except Exception as e:  # noqa: BLE001
        return f"provisioning raised ({type(e).__name__}: {e})"
    return _metering_outcome(ok)


async def provision_metering_async(
    db: DatabaseManager,
    license_id: str,
    tenant_id: str,
    plan_id: str,
    app_id: str,
    token_budget_override: int | None = None,
    quota_override: int | None = None,
) -> str:
    """``provision_metering`` awaited on the CALLER'S running loop (the served route).

    No second event loop: the token provider's ``asyncio.Lock`` and the billing
    client stay on the uvicorn loop that owns them.
    """
    call = _metering_call(
        db, license_id, tenant_id, plan_id, app_id, token_budget_override, quota_override
    )
    if isinstance(call, str):
        return call
    fn, kwargs = call
    try:
        ok = await fn(**kwargs)
    except Exception as e:  # noqa: BLE001
        return f"provisioning raised ({type(e).__name__}: {e})"
    return _metering_outcome(ok)


def subject_sub_for(tenant_id: str, app_key: str) -> str:
    """The deterministic service subject a per-app bundle licence is minted to.

    Raises ``MalformedSubjectIdentifierError`` when ``app_key`` is too long for
    ``subject.keycloak_sub`` (VARCHAR(255)); planners check
    ``service_subject_app_key_problem`` first and REFUSE the app instead.
    """
    return service_subject_sub(tenant_id, app_key)


def member_not_adopted_note(license_id: object, sub: str) -> str:
    return (
        f"member licence {license_id} ({sub}), not adopted — migrate via "
        "migrate_member_licences_to_tenant.py"
    )


def choose_plan(
    db: DatabaseManager, app_id: UUID, preference: list[str]
) -> tuple[str, UUID] | None:
    """The FIRST code in ``preference`` with a non-revoked plan on ``app_id``, or None."""
    available = db.list_plans(app_id)  # list_plans already excludes revoked plans
    for code in preference:
        for pl in available:
            if str(pl.code) == code:
                return code, cast(UUID, pl.id)
    return None


def plan_app(
    db: DatabaseManager,
    tenant_id: str,
    bundle_id: UUID,
    app_key: str,
    app_id: UUID,
    plan: list[str],
    token_budget_override: int | None = None,
    quota_override: int | None = None,
    *,
    money_tenant: bool = False,
) -> AppPlan:
    """One app's decision. Reads only."""

    def _sub(lic) -> str:
        return str(lic.subject.keycloak_sub) if lic.subject is not None else str(lic.subject_id)

    def _tenant_level(lic) -> bool:
        s = lic.subject
        return s is not None and is_tenant_level_subject(s.keycloak_sub, s.keycloak_realm)

    too_long = service_subject_app_key_problem(app_key)
    if too_long is not None:
        return AppPlan(
            app_key, app_id, "REFUSE", note=too_long, refuse_reason=REFUSE_APP_KEY_TOO_LONG
        )

    # ⛔ Licences held by an externally-minted non-human subject (`cp-tenant:…`) are
    # neither members nor bundle holders: CommonPractice owns them. Not adopted, not
    # counted as a competing candidate, not reported as "migrate me".
    everything = [
        lic
        for lic in db.list_tenant_app_licences(tenant_id, app_id)
        if lic.subject is None or not is_externally_held_subject(lic.subject.keycloak_realm)
    ]
    members = [lic for lic in everything if not _tenant_level(lic)]
    member_in_bundle = [lic for lic in members if lic.bundle_id == bundle_id]
    if member_in_bundle:
        lic = member_in_bundle[0]
        return AppPlan(
            app_key,
            app_id,
            "REFUSE",
            str(lic.id),
            _sub(lic),
            note="MEMBER licence carries this bundle; a tenant-level one beside it would make "
            "bundle-token 409 ambiguous_app_licence — migrate via "
            "migrate_member_licences_to_tenant.py",
            refuse_reason=REFUSE_MEMBER_LICENCE,
        )
    # Member licences outside the bundle are never adopted and never candidates.
    member_notes = [member_not_adopted_note(lic.id, _sub(lic)) for lic in members]
    rows = [lic for lic in everything if _tenant_level(lic)]
    mine = [lic for lic in rows if lic.bundle_id is not None and lic.bundle_id == bundle_id]
    other_bundle = [lic for lic in rows if lic.bundle_id is not None and lic.bundle_id != bundle_id]
    unbundled = [lic for lic in rows if lic.bundle_id is None]

    if len(mine) == 1 and not unbundled and not other_bundle:
        return AppPlan(
            app_key,
            app_id,
            "exists",
            str(mine[0].id),
            _sub(mine[0]),
            "; ".join(member_notes),
            existing_plan_id=cast(UUID, mine[0].plan_id),
        )
    if len(rows) > 1:
        return AppPlan(
            app_key,
            app_id,
            "REFUSE",
            note=f"{len(rows)} active (tenant, app) licences; resolve to one first",
            candidates=[f"{lic.id} subject={_sub(lic)} bundle_id={lic.bundle_id}" for lic in rows],
            refuse_reason=REFUSE_SEVERAL_CANDIDATES,
        )
    if other_bundle:
        lic = other_bundle[0]
        return AppPlan(
            app_key,
            app_id,
            "REFUSE",
            str(lic.id),
            _sub(lic),
            note=f"licence already bound to another bundle ({lic.bundle_id})",
            refuse_reason=REFUSE_OTHER_BUNDLE,
        )
    if len(unbundled) == 1:
        lic = unbundled[0]
        notes = (
            ["metering_provisioned_at NULL: left to the reconcile sweep"]
            if lic.metering_provisioned_at is None
            else []
        )
        return AppPlan(
            app_key,
            app_id,
            "adopt",
            str(lic.id),
            _sub(lic),
            note="; ".join(notes + member_notes),
            existing_plan_id=cast(UUID, lic.plan_id),
        )
    chosen = choose_plan(db, app_id, plan)
    if chosen is None:
        codes = sorted({str(pl.code) for pl in db.list_plans(app_id)})
        return AppPlan(
            app_key,
            app_id,
            "REFUSE",
            subject=subject_sub_for(tenant_id, app_key),
            note=f"no plan {','.join(plan)!r} on this app (has: {codes or 'none'})",
            refuse_reason=REFUSE_NO_PLAN,
        )
    plan_code, plan_id = chosen
    return AppPlan(
        app_key,
        app_id,
        "insert",
        "",
        subject_sub_for(tenant_id, app_key),
        note="; ".join(
            [
                "metering: "
                + owed_metering(
                    db,
                    plan_id,
                    token_budget_override,
                    quota_override,
                    money_tenant=money_tenant,
                ),
                *member_notes,
            ]
        ),
        plan_code=plan_code,
        plan_id=plan_id,
    )


def shared_tenant_licences(db: DatabaseManager, tenant_id: str, app_id: UUID) -> list[Any]:
    """The tenant's effectively-active TENANT-LEVEL licences for ``app_id`` (059).

    Exactly the licence a DEPENDENCY resolves to — ensure (``plan_dependency``) and
    the bundle-token route read the same set, so they cannot disagree. Externally
    held (``cp-tenant``) and member (person) licences are never it. Read-only.
    """
    return [
        lic
        for lic in db.list_tenant_app_licences(tenant_id, app_id)
        if lic.subject is not None
        and not is_externally_held_subject(lic.subject.keycloak_realm)
        and is_tenant_level_subject(lic.subject.keycloak_sub, lic.subject.keycloak_realm)
    ]


def plan_dependency(
    db: DatabaseManager,
    tenant_id: str,
    app_key: str,
    app_id: UUID,
    plans: list[str],
    token_budget_override: int | None = None,
    quota_override: int | None = None,
    *,
    money_tenant: bool = False,
) -> AppPlan:
    """One DEPENDENCY app's decision (migration 059). Reads only.

    ONE tenant-level licence per (tenant, app), shared: any existing one satisfies
    the dependency (``exists``, nothing written), wherever it sits.
    """
    too_long = service_subject_app_key_problem(app_key)
    if too_long is not None:
        return AppPlan(
            app_key,
            app_id,
            "REFUSE",
            note=too_long,
            refuse_reason=REFUSE_APP_KEY_TOO_LONG,
            dependency=True,
        )
    rows = shared_tenant_licences(db, tenant_id, app_id)
    if len(rows) > 1:
        return AppPlan(
            app_key,
            app_id,
            "REFUSE",
            note=f"{len(rows)} active tenant-level (tenant, app) licences; resolve to one first",
            candidates=[f"{lic.id} bundle_id={lic.bundle_id}" for lic in rows],
            refuse_reason=REFUSE_SEVERAL_CANDIDATES,
            dependency=True,
        )
    if rows:
        lic = rows[0]
        held_as = (
            "dependency licence"
            if lic.is_dependency
            else (f"bundle {lic.bundle_id}" if lic.bundle_id is not None else "unbundled licence")
        )
        return AppPlan(
            app_key,
            app_id,
            "exists",
            str(lic.id),
            str(lic.subject.keycloak_sub),
            note=f"dependency satisfied by the tenant's {held_as} (shared)",
            existing_plan_id=cast(UUID, lic.plan_id),
            dependency=True,
        )
    chosen = choose_plan(db, app_id, plans)
    if chosen is None:
        codes = sorted({str(pl.code) for pl in db.list_plans(app_id)})
        return AppPlan(
            app_key,
            app_id,
            "REFUSE",
            subject=subject_sub_for(tenant_id, app_key),
            note=f"no plan {','.join(plans)!r} on this app (has: {codes or 'none'})",
            refuse_reason=REFUSE_NO_PLAN,
            dependency=True,
        )
    plan_code, plan_id = chosen
    return AppPlan(
        app_key,
        app_id,
        "insert",
        "",
        subject_sub_for(tenant_id, app_key),
        note="dependency; metering: "
        + owed_metering(
            db, plan_id, token_budget_override, quota_override, money_tenant=money_tenant
        ),
        plan_code=plan_code,
        plan_id=plan_id,
        dependency=True,
    )


def bundle_dependencies(
    db: DatabaseManager, member_keys: list[str]
) -> tuple[list[App], list[Edge]]:
    """The closure of ``member_keys`` over ``app_requires`` (non-revoked apps only).

    Raises ``EnsureError('dependency_cycle')`` on a cycle — refused, never walked.
    """
    edges = [Edge(a, r, c) for a, r, c in db.list_app_requirements()]
    try:
        dep_keys, walked = closure(member_keys, edges)
    except DependencyCycleError as e:
        raise EnsureError("dependency_cycle", str(e)) from e
    apps: list[App] = []
    for key in dep_keys:
        app = db.get_app_by_key(key)
        if app is not None and not app.revoked:
            apps.append(app)
    return apps, walked


def plan(
    db: DatabaseManager,
    tenant_id: str,
    bundle_key: str,
    plans: list[str],
    quota_override: int | None = None,
    *,
    token_budget_override: int | None = None,
) -> EnsurePlan:
    """Decide every app of ``bundle_key`` for ``tenant_id``. Reads only.

    Raises ``EnsureError`` for a refusal of the whole request (unknown / archived
    tenant, unknown / revoked / empty bundle, or a ``token_budget_override`` for a
    tenant whose budget is money). It deliberately avoids ``find_active_license``,
    which lazily expires stale rows, i.e. writes.
    """
    # ⛔ Refuse an unknown tenant FIRST: this grants a REGISTERED tenant, never
    # invents one.
    tenant = db.get_tenant(tenant_id)
    if tenant is None or str(tenant.id) != tenant_id:
        raise EnsureError("tenant_not_found", f"tenant {tenant_id} is not in tenants")
    if tenant.archived_at is not None:
        raise EnsureError("tenant_archived", f"tenant {tenant_id} is ARCHIVED")
    money_tenant = tenant.budget_currency is not None
    if money_tenant and token_budget_override is not None:
        raise EnsureError(
            "token_budget_for_money_tenant",
            f"tenant {tenant_id} has budget_currency={tenant.budget_currency!r}: its budget "
            "is money in billing and no token budget is ever pushed for it, so a "
            "token_budget_override cannot be honoured",
        )

    bundle = db.get_bundle_by_key(bundle_key)
    if bundle is None or bundle.revoked:
        raise EnsureError("bundle_not_found", f"bundle '{bundle_key}' not found or revoked")
    bundle_id = cast(UUID, bundle.id)
    apps = db.list_bundle_apps(bundle_id)
    if not apps:
        raise EnsureError("bundle_empty", f"bundle '{bundle_key}' has no apps")

    holds = db.tenant_holds_bundle(tenant_id, bundle_id)
    decisions = [
        plan_app(
            db,
            tenant_id,
            bundle_id,
            str(a.key),
            cast(UUID, a.id),
            plans,
            token_budget_override,
            quota_override,
            money_tenant=money_tenant,
        )
        for a in apps
    ]
    dep_apps, walked = bundle_dependencies(db, [str(a.key) for a in apps])
    decisions += [
        plan_dependency(
            db,
            tenant_id,
            str(a.key),
            cast(UUID, a.id),
            plans,
            token_budget_override,
            quota_override,
            money_tenant=money_tenant,
        )
        for a in dep_apps
    ]
    dependency_grants = sorted(
        {(e.caller, e.required) for e in walked} | set(db.list_bundle_callers(bundle_id))
    )
    return EnsurePlan(
        tenant,
        bundle_key,
        bundle_id,
        holds,
        decisions,
        plans,
        quota_override,
        token_budget_override,
        dependency_grants,
    )


class _PendingMetering:
    """An inserted licence whose billing push ``apply`` still owes."""

    def __init__(self, p: AppPlan, license_id: str, plan_id: UUID) -> None:
        self.p = p
        self.license_id = license_id
        self.plan_id = plan_id


def _apply_one(
    db: DatabaseManager, ep: EnsurePlan, p: AppPlan, provisioned_by: str
) -> _PendingMetering | None:
    """The DB writes for ONE app (blocking). Returns the metering push it owes, if any."""
    tenant_id = ep.tenant_id
    if p.action == "adopt":
        if not db.adopt_license_into_bundle(UUID(p.license_id), ep.bundle_id):
            ep.lost_race.append(p.app_key)
            p.action = "REFUSE"
            p.refuse_reason = REFUSE_LOST_RACE
        return None
    if p.action != "insert":
        return None
    plan_id = cast(UUID, p.plan_id)
    subject = db.create_or_get_subject(keycloak_sub=p.subject, keycloak_realm=SUBJECT_REALM)
    try:
        lic = db.create_license(
            subject_id=cast(UUID, subject.id),
            app_id=p.app_id,
            plan_id=plan_id,
            tenant_id=tenant_id,
            status="active",
            # A dependency licence is SHARED by every bundle whose closure needs the
            # app, so it names none (ck_license_dependency_unbundled).
            bundle_id=None if p.dependency else ep.bundle_id,
            is_dependency=p.dependency,
            # Stored in the SAME INSERT (migration 041), so the reconcile sweep that
            # repairs an unmetered licence pushes the override, never the plan's value.
            token_budget_override=ep.token_budget_override,
            quota_override=ep.quota_override,
            license_metadata=(
                {"provisioned_by": provisioned_by, "dependency_of": ep.bundle_key}
                if p.dependency
                else {"provisioned_by": provisioned_by, "bundle": ep.bundle_key}
            ),
        )
    except IntegrityError as e:
        if _constraint_name(e) != UQ_ACTIVE_TENANT_APP_SUBJECT:
            raise
        _lost_insert(db, ep, p, str(subject.id))
        return None
    # State the subject's parent org (migration 029), same as the mint route.
    db.attribute_subject_to_tenant(cast(UUID, subject.id), tenant_id)
    p.license_id = str(lic.id)
    return _PendingMetering(p, str(lic.id), plan_id)


def _constraint_name(e: IntegrityError) -> str | None:
    diag = getattr(e.orig, "diag", None)
    return getattr(diag, "constraint_name", None)


def _lost_insert(db: DatabaseManager, ep: EnsurePlan, p: AppPlan, subject_id: str) -> None:
    """The INSERT lost ``uq_license_active_tenant_app_subject`` to a concurrent writer.

    Defence in depth behind the tenant lock (a writer outside it — the operator
    script's separate plan/apply, a future caller — can still race). ``create_license``
    runs in its OWN session, which its ``finally`` closed (rolling the failed INSERT
    back), so no other app's work is affected. Re-read the winner: carrying THIS
    bundle it is ``exists`` (what a re-plan would say); otherwise the app is
    REFUSE ``lost_race``, as for an adoption that lost its compare-and-set.
    """
    winner = next(
        (
            lic
            for lic in db.list_tenant_app_licences(ep.tenant_id, p.app_id)
            if str(lic.subject_id) == subject_id
        ),
        None,
    )
    if winner is not None and (
        winner.bundle_id == ep.bundle_id or (p.dependency and winner.bundle_id is None)
    ):
        p.action = "exists"
        p.license_id = str(winner.id)
        p.existing_plan_id = cast(UUID, winner.plan_id)
        p.plan_code = ""
        p.plan_id = None
        p.note = "insert lost a concurrent race; the winner's licence carries this bundle"
        return
    ep.lost_race.append(p.app_key)
    p.action = "REFUSE"
    p.refuse_reason = REFUSE_LOST_RACE
    p.note = "insert lost a concurrent race to a licence not carrying this bundle"


def _record_metering(ep: EnsurePlan, pending: _PendingMetering, why: str) -> None:
    if why:
        pending.p.metering_pending = why
        ep.unmetered.append(f"{pending.p.app_key} {pending.license_id}: {why}")


def apply(
    db: DatabaseManager,
    ep: EnsurePlan,
    *,
    provisioned_by: str,
    provision: Callable[..., str] = provision_metering,
    granted_by: str = "bundle_ensure",
) -> EnsurePlan:
    """Write what ``plan`` decided. Each app is independent; REFUSE apps are skipped.

    Fills ``ep.lost_race`` (adoptions that lost a compare-and-set; the app is then
    marked REFUSE ``lost_race``) and ``ep.unmetered`` (inserted licences whose billing
    push did not complete; the reconcile sweep repairs them).

    SYNCHRONOUS — the operator script's form. The served route uses ``apply_async``.

    The bundle hold and the GRANTS (migration 050, least privilege — see
    ``models.bundle_grantees``: a single-issuer app grants its issuer, a
    multi-issuer app grants only ``platform``, and nothing is granted for an app
    the tenant's owner itself issues) are written in ONE transaction before any
    licence.
    """
    ep.grants_created = db.ensure_bundle_grants(
        ep.tenant_id,
        ep.bundle_id,
        granted_by,
        hold=not ep.holds,
        dependency_grants=ep.dependency_grants,
    )
    for p in ep.apps:
        pending = _apply_one(db, ep, p, provisioned_by)
        if pending is None:
            continue
        why = provision(
            db,
            pending.license_id,
            ep.tenant_id,
            str(pending.plan_id),
            str(p.app_id),
            token_budget_override=ep.token_budget_override,
            quota_override=ep.quota_override,
        )
        _record_metering(ep, pending, why)
    ep.applied = True
    return ep


async def apply_async(
    db: DatabaseManager,
    ep: EnsurePlan,
    *,
    provisioned_by: str,
    provision: Callable[..., Awaitable[str]] = provision_metering_async,
    granted_by: str = "bundle_ensure",
) -> EnsurePlan:
    """``apply`` for the served route: same writes, same order, no second event loop.

    The blocking DB writes run in a worker thread (``asyncio.to_thread``); each
    inserted licence's metering coroutine is AWAITED on the caller's running loop,
    so the process-wide token provider's ``asyncio.Lock`` is only ever contended
    from the uvicorn loop.
    """
    ep.grants_created = await asyncio.to_thread(
        lambda: db.ensure_bundle_grants(
            ep.tenant_id,
            ep.bundle_id,
            granted_by,
            hold=not ep.holds,
            dependency_grants=ep.dependency_grants,
        )
    )
    for p in ep.apps:
        pending = await asyncio.to_thread(_apply_one, db, ep, p, provisioned_by)
        if pending is None:
            continue
        why = await provision(
            db,
            pending.license_id,
            ep.tenant_id,
            str(pending.plan_id),
            str(p.app_id),
            token_budget_override=ep.token_budget_override,
            quota_override=ep.quota_override,
        )
        _record_metering(ep, pending, why)
    ep.applied = True
    return ep


def plan_and_apply(
    db: DatabaseManager,
    tenant_id: str,
    bundle_key: str,
    plans: list[str],
    quota_override: int | None,
    apply_: bool,
    *,
    token_budget_override: int | None = None,
    provisioned_by: str = "bundle_ensure",
    granted_by: str = "bundle_ensure",
) -> EnsurePlan:
    """``plan`` and, when ``apply_``, ``apply``. Raises ``EnsureError`` (nothing written).

    When applying, both run under the tenant lock (``_TenantEnsureLock``). A dry run
    takes no lock: it writes nothing and must not queue behind a live ensure.
    """
    if not apply_:
        return plan(
            db,
            tenant_id,
            bundle_key,
            plans,
            quota_override,
            token_budget_override=token_budget_override,
        )
    lock = _TenantEnsureLock(db, tenant_id)
    try:
        lock.acquire()
        ep = plan(
            db,
            tenant_id,
            bundle_key,
            plans,
            quota_override,
            token_budget_override=token_budget_override,
        )
        apply(db, ep, provisioned_by=provisioned_by, granted_by=granted_by)
        return ep
    finally:
        lock.release()


async def plan_and_apply_async(
    db: DatabaseManager,
    tenant_id: str,
    bundle_key: str,
    plans: list[str],
    quota_override: int | None,
    apply_: bool,
    *,
    token_budget_override: int | None = None,
    provisioned_by: str = "bundle_ensure",
    granted_by: str = "bundle_ensure",
) -> EnsurePlan:
    """``plan_and_apply`` for an ``async`` route: ``plan`` in a worker thread, then
    ``apply_async`` (metering awaited on the caller's loop). Raises ``EnsureError``.

    When applying, plan AND apply run under the tenant lock (``_TenantEnsureLock``);
    acquiring it blocks, so it is taken in a worker thread too. Dry run: no lock.
    """

    def _plan() -> EnsurePlan:
        return plan(
            db,
            tenant_id,
            bundle_key,
            plans,
            quota_override,
            token_budget_override=token_budget_override,
        )

    if not apply_:
        return await asyncio.to_thread(_plan)
    lock = _TenantEnsureLock(db, tenant_id)
    try:
        # Inside the try: a cancellation while acquire still blocks in its thread
        # must reach ``release`` (see _TenantEnsureLock, CANCELLATION).
        await asyncio.to_thread(lock.acquire)
        ep = await asyncio.to_thread(_plan)
        await apply_async(db, ep, provisioned_by=provisioned_by, granted_by=granted_by)
        return ep
    finally:
        await asyncio.to_thread(lock.release)
