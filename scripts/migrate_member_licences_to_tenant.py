#!/usr/bin/env python3
"""
Move MEMBER-held licences onto the TENANT-level per-app model (one licence per
(tenant, app) inside the tenant's bundle), without breaking any live token.

Ruling 2026-09-28 (George): bundles and licences are TENANT-level, not per member.
The tenant-level shape is the one ``scripts/provision_tenant_bundle.py`` builds: the
tenant holds a bundle (``tenant_bundle``) and, for each app, exactly ONE licence with
``bundle_id`` set, held by the service subject ``svc:<tenant-uuid>:<app>`` in realm
``service``. This script moves the licences individual people hold today (realms
``portal`` and ``secure-apps`` by default; ``--realm all`` for every realm) onto that
shape, app for app — the SAME apps, never more. ``svc:`` subjects are already
tenant-level and are never a source, whatever the realm; ``all`` also leaves out the
``service`` realm (service principals, not members).

THREE STEPS, each dry-run unless ``--apply``:

1. (default) PLAN. Read-only: every transaction the plan opens is
   ``SET TRANSACTION READ ONLY`` (transaction-scoped, so it is safe through the
   pgbouncer transaction pooler — never a session ``SET``). Prints, per subject, each
   old licence -> the target tenant-level licence (``exists`` / ``adopt`` / ``create``)
   under the tenant's bundle, and every REFUSED tenant with why.
2. ``--apply``. Creates or adopts the tenant-level licences (and the ``tenant_bundle``
   row when the tenant holds none), then MARKS each old member licence
   ``pending_retirement`` in its metadata. It revokes NOTHING: old tokens keep working.
   Each CREATED licence is provisioned in billing (token budget + quota config) through
   ``main.provision_license_metering`` — the same function the HTTP create route uses —
   which stamps ``metering_provisioned_at`` only when billing accepted every push.
   Billing unreachable: the licence is still created, the stamp stays NULL, the run
   reports it and exits 6, and the daily ``licenses-metering-reconcile`` CronJob
   (``RECONCILE_REVALIDATE=true``; selection in ``main.run_metering_reconcile``) repairs
   it read-before-write. Adopted/existing licences are never pushed to (a push
   OVERWRITES); a NULL stamp on one is reported and left to that sweep.
   Writes ``--changes-out`` (default ``token-changes.json``): subject -> old licence ids
   -> new licence id. No token values, no token hashes — this script never reads either.
   ⚠️ That file is a CONVENIENCE for telling subjects, not the record: in the pod its
   default path is on the container filesystem and dies with it. Losing it loses
   nothing — ``--retire-confirmed`` reads only the DB stamps, and the same map is
   rebuilt from them with::

       SELECT s.keycloak_sub, l.tenant_id, a.key AS app, l.id AS old_license_id,
              l.metadata->'tenant_licence_migration'->>'replaced_by' AS new_license_id
       FROM license l JOIN subject s ON s.id = l.subject_id JOIN app a ON a.id = l.app_id
       WHERE l.metadata->'tenant_licence_migration'->>'state' = 'pending_retirement';
3. ``--retire-confirmed FILE [--apply]``. Revokes (licence + its tokens, via
   ``DatabaseManager.revoke_license``) ONLY the marked licences of subjects listed in
   FILE as confirmed working on the new licence, and only when that replacement licence
   is still active, in the bundle ``--apply`` recorded, on the same (tenant, app), AND
   METERED: ``metering_provisioned_at`` is set and, when its plan owes a token budget,
   billing answers that it holds an ACTIVE ``license_sync`` budget for it (the same
   filter the reconcile sweep counts as coverage). Retiring the old licence tears down
   ITS budget, so retiring onto an unmetered replacement would leave the tenant's usage
   unmetered. A pair that fails this is skipped with the reason. Then tears down billing's
   mirrors for them via ``services.metering_teardown`` (the same functions the HTTP
   revoke path and the reaper use) when ``METERING_SERVICE_URL`` is set.

BUNDLE CHOICE (per tenant, never guessed). Let A = the apps the tenant's members hold.
  * ``--bundle-for <tenant>=<key>`` wins, but must contain every app in A.
  * else a bundle the tenant already HOLDS that contains all of A, if exactly one;
  * else, if the tenant holds none, the ONE non-revoked bundle containing all of A;
  * several candidates and ``non-agentic-ai`` is among them -> ``non-agentic-ai``
    (ruling 2026-09-28; the reason is printed per tenant).
  Zero candidates, or several without ``non-agentic-ai`` -> the tenant is REFUSED and
  the candidates are listed.

``--extend-bundle KEY`` (ruling 2026-09-28): for a tenant NO bundle covers, the apps
KEY lacks are added to KEY's app set (``bundle_app``, via
``DatabaseManager.ensure_bundle_app``), and the tenant then resolves to KEY. Dry-run
lists the apps it would add; ``--apply`` adds them before any licence is written. Only
apps that exist in the app catalogue and are not revoked; anything else refuses the
tenant. Apps are added only for tenants that are otherwise going ahead.
  ⛔ ``bundle_app`` is the CATALOGUE bundle's app set, SHARED by every tenant holding
  KEY: extending it widens what EVERY holder may mint, not just the migrated tenant.
  So the dry-run lists every current holder (tenant id, name, licences carrying KEY)
  and the apps each gains, and ``--apply`` refuses unless
  ``--confirm-bundle-holders N`` names the current holder count. N is RE-READ inside
  the one transaction that writes ``bundle_app`` (bundle row locked ``FOR UPDATE``, so
  no holder can appear mid-write); a holder added since the dry-run refuses the run
  (exit 3) with nothing written. A typed confirmation, not a flag.
  A tenant is also refused when another bundle it holds contains one of A (the
  bundle-token route would answer 409 ``ambiguous_bundle`` for that app).

PER-APP DECISION inside the chosen bundle B (candidates = every effectively-active
(tenant, app) licence, as ``list_tenant_app_licences`` returns them):
  exists  the ``svc:<tenant>:<app>`` licence is active and carries B.
  adopt   the ``svc:<tenant>:<app>`` licence is active and unbundled -> set bundle_id.
  create  no such licence -> create it with the member licence's plan (and expiry).
  REFUSE  that licence is bound to ANOTHER bundle; an active (tenant, app) licence
          exists that is neither the service licence nor an in-scope member licence
          (it would be left competing); the members hold different plans; the app key
          is too long for ``svc:<tenant>:<app>`` to fit ``subject.keycloak_sub``
          (VARCHAR(255); ``models.SERVICE_SUBJECT_APP_KEY_MAX_LEN``).
A tenant with ANY refused app is refused whole: nothing is written for it, because
writing part of it would not preserve the same apps.

⚠️ TRANSITION COST, stated rather than hidden: between ``--apply`` and
``--retire-confirmed`` each migrated (tenant, app) has TWO active licences (member +
service). Billing resolves usage on (tenant_id, app) and drops a key with two
candidates (see provision_tenant_bundle.py), so usage on those keys is unattributed
until the old licence is retired. Keep the window short.

AUDIT. The licences DB has no audit table (models.py: "there is no audit table"; the
live schema has none either). The existing pattern is to record on the row itself
(``deleted_by`` / ``delete_reason``, ``metadata.provisioned_by``) plus a log line. So
every write here stamps ``metadata.tenant_licence_migration`` on the row it touches and
prints one ``MIGRATION_AUDIT {json}`` line.

EXIT CODES: 0 ok; 3 at least one tenant refused (others still planned/applied), an
adoption lost a race, or ``--confirm-bundle-holders`` no longer matches the holder
count; 2 usage / DATABASE_URL unset / ``--extend-bundle --apply`` without
``--confirm-bundle-holders``; 5 retire revoked licences but a billing teardown failed or
was skipped (METERING_SERVICE_URL unset); 6 --apply created a licence whose metering is
NOT provisioned yet (see METERING above); 7 BOTH 3 and 6 — neither masks the other.

Usage (prod runs inside the licences pod; see README "Operator scripts")::

    DATABASE_URL=... python scripts/migrate_member_licences_to_tenant.py
    DATABASE_URL=... python scripts/migrate_member_licences_to_tenant.py \\
        --extend-bundle non-agentic-ai --apply --confirm-bundle-holders 4 \\
        --changes-out /tmp/token-changes.json
    DATABASE_URL=... python scripts/migrate_member_licences_to_tenant.py \\
        --retire-confirmed /tmp/confirmed.json [--apply]
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import event, text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.orm import joinedload  # noqa: E402

from models import (  # noqa: E402
    NON_HUMAN_SUBJECT_REALMS,
    Bundle,
    DatabaseManager,
    License,
    Subject,
    effective_metering,
    service_subject_app_key_problem,
)

# Reuse the provisioning script's naming and realm — one definition of the target shape.
_spec = importlib.util.spec_from_file_location(
    "provision_tenant_bundle", Path(__file__).resolve().parent / "provision_tenant_bundle.py"
)
assert _spec is not None and _spec.loader is not None
_provision = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_provision)
subject_sub_for = _provision.subject_sub_for
SERVICE_REALM: str = _provision.SUBJECT_REALM
owed_metering = _provision.owed_metering

SCRIPT = "migrate_member_licences_to_tenant.py"
MARK = "tenant_licence_migration"
DEFAULT_REALMS = ("portal", "secure-apps")
DEFAULT_BUNDLE = "non-agentic-ai"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REFUSED = 3
EXIT_TEARDOWN_INCOMPLETE = 5
EXIT_METERING_PENDING = 6
EXIT_REFUSED_AND_METERING_PENDING = _provision.EXIT_REFUSED_AND_METERING_PENDING  # 7
final_exit = _provision.final_exit

EXIT_CODES_HELP = """exit codes:
  0  ok
  2  usage; DATABASE_URL unset; --extend-bundle --apply without --confirm-bundle-holders
  3  a tenant REFUSED, an adoption lost a race, or the bundle holder count changed
  5  retire: billing teardown failed or skipped
  6  --apply: a created licence is NOT metered yet (reconcile CronJob repairs)
  7  both 3 and 6"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _audit(action: str, **fields: Any) -> None:
    print("MIGRATION_AUDIT " + json.dumps({"action": action, "by": SCRIPT, "at": _now(), **fields}))


class _ReadOnly:
    """Make every transaction on ``db.engine`` READ ONLY while in scope.

    ``SET TRANSACTION`` is transaction-scoped: it dies with the transaction, so nothing
    leaks to the next pgbouncer client (a session ``SET`` would).
    """

    def __init__(self, db: DatabaseManager) -> None:
        self.engine = db.engine

    @staticmethod
    def _begin(conn: Any) -> None:
        conn.exec_driver_sql("SET TRANSACTION READ ONLY")

    def __enter__(self) -> _ReadOnly:
        event.listen(self.engine, "begin", self._begin)
        return self

    def __exit__(self, *exc: object) -> None:
        event.remove(self.engine, "begin", self._begin)


# ── plan model ─────────────────────────────────────────────────────────────────


class AppTarget:
    def __init__(self, app_key: str, app_id: UUID, members: list[License]) -> None:
        self.app_key = app_key
        self.app_id = app_id
        self.members = members  # in-scope member licences (the ones being replaced)
        self.action = ""  # exists | adopt | create | REFUSE
        self.target_id = ""  # tenant-level licence id ('' until created)
        self.target_sub = ""
        self.note = ""
        self.metering = ""  # what a create would push / why an existing licence is unmetered
        self.metered: bool | None = None  # metering_provisioned_at set (create: after --apply)


class TenantPlan:
    def __init__(self, tenant_id: str, name: str) -> None:
        self.tenant_id = tenant_id
        self.name = name
        self.bundle_key = ""
        self.bundle_reason = ""
        self.bundle_id: UUID | None = None
        self.extend_apps: dict[str, UUID] = {}  # app key -> id, added to the bundle
        self.holds_bundle = False
        self.refusals: list[str] = []
        self.apps: list[AppTarget] = []

    @property
    def refused(self) -> bool:
        return bool(self.refusals) or any(a.action == "REFUSE" for a in self.apps)


def _sub(lic: License) -> str:
    return str(lic.subject.keycloak_sub) if lic.subject is not None else str(lic.subject_id)


def _member_licences(
    db: DatabaseManager, realms: list[str] | None, tenants: set[str]
) -> list[License]:
    """Active member licences. ``svc:`` subjects never.

    ``realms=None`` ('all') is every realm except ``service``: that realm holds service
    principals (``svc-<name>`` machine clients, 63 subjects live 2026-09-28), not members.
    Every other non-human realm (``NON_HUMAN_SUBJECT_REALMS``, e.g. ``cp-tenant``) is
    excluded too, whatever ``realms`` says: those licences are not members' and are
    owned by the service that minted them.
    """
    session = db.get_session()
    try:
        q = (
            session.query(License)
            .join(Subject, Subject.id == License.subject_id)
            .options(joinedload(License.subject), joinedload(License.app))
            .filter(
                License.status == "active",
                License.deleted_at.is_(None),
                ~Subject.keycloak_sub.like("svc:%"),
                Subject.keycloak_realm.notin_(
                    [r for r in NON_HUMAN_SUBJECT_REALMS if r != SERVICE_REALM]
                ),
            )
            .order_by(License.tenant_id, License.created_at, License.id)
        )
        if realms is not None:
            q = q.filter(Subject.keycloak_realm.in_(realms))
        else:  # 'all': every realm except the service-principal realm
            q = q.filter(Subject.keycloak_realm != SERVICE_REALM)
        if tenants:
            q = q.filter(License.tenant_id.in_([UUID(t) for t in tenants]))
        return [lic for lic in q.all() if lic.is_active()]
    finally:
        session.close()


def _bundle_catalogue(db: DatabaseManager) -> dict[str, tuple[UUID, set[str]]]:
    session = db.get_session()
    try:
        bundles = session.query(Bundle).filter(Bundle.revoked.is_(False)).order_by(Bundle.key).all()
    finally:
        session.close()
    return {
        str(b.key): (cast(UUID, b.id), {str(a.key) for a in db.list_bundle_apps(cast(UUID, b.id))})
        for b in bundles
    }


def _choose_bundle(
    db: DatabaseManager,
    tp: TenantPlan,
    apps: set[str],
    catalogue: dict[str, tuple[UUID, set[str]]],
    override: str | None,
) -> None:
    held = [str(b.key) for b in db.list_tenant_bundles(tp.tenant_id)]
    if override is not None:
        if override not in catalogue:
            tp.refusals.append(f"--bundle-for names unknown/revoked bundle '{override}'")
            return
        choice, reason = override, "--bundle-for"
    else:
        covering_held = [k for k in held if apps <= catalogue.get(k, (None, set()))[1]]
        if held:
            if len(covering_held) > 1 and DEFAULT_BUNDLE in covering_held:
                covering_held = [DEFAULT_BUNDLE]
                reason = f"default {DEFAULT_BUNDLE}: held and among fitting {sorted(held)}"
            else:
                reason = "held, the only held bundle with all apps"
            if len(covering_held) != 1:
                tp.refusals.append(
                    f"holds bundles {held}; {len(covering_held)} of them contain all of "
                    f"{sorted(apps)} (need exactly 1)"
                )
                return
            choice = covering_held[0]
        else:
            covering = [k for k, (_bid, bapps) in catalogue.items() if apps <= bapps]
            reason = "only bundle with all apps"
            if len(covering) > 1 and DEFAULT_BUNDLE in covering:
                reason = f"default {DEFAULT_BUNDLE}: among {len(covering)} fitting {covering}"
                covering = [DEFAULT_BUNDLE]
            if len(covering) != 1:
                if covering:
                    why = f"AMBIGUOUS: {len(covering)} bundles contain all apps: {covering}"
                else:
                    missing = {k: sorted(apps - bapps) for k, (_b, bapps) in catalogue.items()}
                    best = min(missing.items(), key=lambda kv: len(kv[1])) if missing else None
                    why = "NO bundle contains all apps" + (
                        f" (closest '{best[0]}' lacks {best[1]})" if best else ""
                    )
                tp.refusals.append(why + " — pass --bundle-for <tenant>=<key>")
                return
            choice = covering[0]
    bundle_id, bundle_apps = catalogue[choice]
    lacking = sorted(apps - bundle_apps)
    if lacking:
        tp.refusals.append(f"bundle '{choice}' lacks apps {lacking}; same apps cannot be kept")
        return
    for other in held:
        if other != choice and apps & catalogue.get(other, (None, set()))[1]:
            tp.refusals.append(
                f"also holds bundle '{other}' containing {sorted(apps & catalogue[other][1])} "
                "(bundle-token would 409 ambiguous_bundle)"
            )
            return
    tp.bundle_key, tp.bundle_id, tp.holds_bundle = choice, bundle_id, choice in held
    tp.bundle_reason = reason


def _decide_app(db: DatabaseManager, tp: TenantPlan, at: AppTarget) -> None:
    too_long = service_subject_app_key_problem(at.app_key)
    if too_long is not None:
        at.action, at.note = "REFUSE", too_long
        return
    svc = subject_sub_for(tp.tenant_id, at.app_key)
    at.target_sub = svc
    member_ids = {str(m.id) for m in at.members}
    rows = db.list_tenant_app_licences(tp.tenant_id, at.app_id)
    mine = [lic for lic in rows if _sub(lic) == svc]
    others = [lic for lic in rows if _sub(lic) != svc and str(lic.id) not in member_ids]
    plans = {str(m.plan_id) for m in at.members}
    if others:
        at.action = "REFUSE"
        at.note = "other active (tenant, app) licences left competing: " + ", ".join(
            f"{lic.id} subject={_sub(lic)}" for lic in others
        )
    elif len(mine) > 1:
        at.action, at.note = "REFUSE", f"{len(mine)} active service licences"
    elif mine and mine[0].bundle_id is not None and mine[0].bundle_id != tp.bundle_id:
        at.action = "REFUSE"
        at.note = f"service licence {mine[0].id} bound to another bundle ({mine[0].bundle_id})"
    elif mine:
        at.action = "exists" if mine[0].bundle_id == tp.bundle_id else "adopt"
        at.target_id = str(mine[0].id)
        at.metered = mine[0].metering_provisioned_at is not None
        if mine[0].metering_provisioned_at is None:
            at.metering = "metering_provisioned_at NULL: left to the reconcile sweep"
    elif len(plans) > 1:
        at.action, at.note = "REFUSE", f"members hold {len(plans)} different plans"
    else:
        at.action = "create"
        at.metering = "would push " + owed_metering(db, cast(UUID, at.members[0].plan_id))


def _plan_extension(
    db: DatabaseManager,
    tp: TenantPlan,
    apps: set[str],
    catalogue: dict[str, tuple[UUID, set[str]]],
    extend: str,
) -> dict[str, tuple[UUID, set[str]]]:
    """For a tenant NO bundle covers: the catalogue view with ``extend`` widened.

    Records the apps to add on ``tp.extend_apps``; refuses (and returns the catalogue
    unchanged) when an app is not in the app catalogue or is revoked.
    """
    if any(apps <= bapps for _bid, bapps in catalogue.values()):
        return catalogue
    bid, bapps = catalogue[extend]
    wanted: dict[str, UUID] = {}
    bad: list[str] = []
    for key in sorted(apps - bapps):
        app = db.get_app_by_key(key)
        if app is None or app.revoked:
            bad.append(key)
        else:
            wanted[key] = cast(UUID, app.id)
    if bad:
        tp.refusals.append(f"--extend-bundle {extend}: unknown or revoked app(s) {bad}")
        return catalogue
    tp.extend_apps = wanted
    return {**catalogue, extend: (bid, bapps | set(wanted))}


def build_plan(
    db: DatabaseManager,
    realms: list[str] | None,
    tenants: set[str],
    overrides: dict[str, str],
    extend: str | None = None,
) -> list[TenantPlan]:
    members = _member_licences(db, realms, tenants)
    catalogue = _bundle_catalogue(db)
    by_tenant: dict[str, dict[str, list[License]]] = {}
    for lic in members:
        by_tenant.setdefault(str(lic.tenant_id), {}).setdefault(str(lic.app.key), []).append(lic)
    plans: list[TenantPlan] = []
    for tenant_id, apps in by_tenant.items():
        tenant = db.get_tenant(tenant_id)
        tp = TenantPlan(tenant_id, str(tenant.name) if tenant is not None else "?")
        tp.apps = [AppTarget(k, cast(UUID, v[0].app_id), v) for k, v in sorted(apps.items())]
        plans.append(tp)
        if tenant is None or tenant.archived_at is not None:
            tp.refusals.append("tenant missing or ARCHIVED")
            continue
        override = overrides.get(tenant_id) or overrides.get(tp.name)
        view = catalogue
        if extend is not None and override is None:
            view = _plan_extension(db, tp, set(apps), catalogue, extend)
            if tp.refusals:
                continue
        _choose_bundle(db, tp, set(apps), view, override)
        if tp.extend_apps and not tp.refusals:
            tp.bundle_reason = f"--extend-bundle {extend} adds {sorted(tp.extend_apps)}"
        if tp.refusals:
            continue
        for at in tp.apps:
            _decide_app(db, tp, at)
    plans.sort(key=lambda p: p.name)
    return plans


def bundle_extension(plans: list[TenantPlan]) -> dict[str, UUID]:
    """App key -> id to add to the extended bundle: only for tenants going ahead."""
    out: dict[str, UUID] = {}
    for tp in plans:
        if not tp.refused:
            out.update(tp.extend_apps)
    return dict(sorted(out.items()))


# ── output ─────────────────────────────────────────────────────────────────────


def print_plan(
    plans: list[TenantPlan],
    mode: str,
    extend: str | None = None,
    holders: list[tuple[str, str | None, int]] | None = None,
) -> None:
    rows: list[tuple[str, str, str, str, str, str, str, str]] = []
    for tp in plans:
        for at in tp.apps:
            for m in at.members:
                action = "REFUSED" if tp.refused else at.action
                target = at.target_id or (f"(new) {at.target_sub}" if at.target_sub else "-")
                rows.append(
                    (
                        _sub(m),
                        tp.name,
                        at.app_key,
                        str(m.id),
                        action,
                        target,
                        tp.bundle_key or "-",
                        _short_reason(tp),
                    )
                )
    rows.sort()
    head = (
        "subject",
        "tenant",
        "app",
        "old licence",
        "action",
        "target licence",
        "bundle",
        "why",
    )
    widths = [
        max(len(head[i]), *(len(r[i]) for r in rows)) if rows else len(head[i])
        for i in range(len(head))
    ]
    print(f"=== {mode}: member licences -> tenant-level per-app licences ===")
    print("  ".join(h.ljust(w) for h, w in zip(head, widths, strict=True)))
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)))
    print()
    print("tenants:")
    for tp in plans:
        counts = {
            a: sum(1 for t in tp.apps if t.action == a) for a in ("exists", "adopt", "create")
        }
        state = "REFUSED" if tp.refused else "ok"
        bundle = f"bundle={tp.bundle_key or '-'}" + (
            ""
            if not tp.bundle_key
            else (" (held)" if tp.holds_bundle else " (PROVISION tenant_bundle)")
        )
        print(
            f"  {tp.name} ({tp.tenant_id}) {state} {bundle} apps={len(tp.apps)} "
            f"exists={counts['exists']} adopt={counts['adopt']} create={counts['create']}"
        )
        if tp.bundle_reason:
            print(f"      bundle reason: {tp.bundle_reason}")
        for why in tp.refusals:
            print(f"      refused: {why}")
        for at in tp.apps:
            if at.action == "REFUSE":
                print(f"      refused app {at.app_key}: {at.note}")
            elif at.metering and not tp.refused:
                print(f"      metering {at.app_key}: {at.metering}")
    ok = [p for p in plans if not p.refused]
    subjects = {_sub(m) for p in plans for a in p.apps for m in a.members}
    n_old = sum(len(a.members) for p in plans for a in p.apps)
    summary = {
        a: sum(1 for p in ok for t in p.apps if t.action == a)
        for a in ("exists", "adopt", "create")
    }
    print(
        f"\nsummary: {n_old} member licences, {len(subjects)} subjects, {len(plans)} tenants "
        f"({len(ok)} ok, {len(plans) - len(ok)} REFUSED); for ok tenants: "
        f"exists={summary['exists']} adopt={summary['adopt']} create={summary['create']}, "
        f"tenant_bundle to provision={sum(1 for p in ok if not p.holds_bundle)}"
    )
    if extend is not None:
        ext = bundle_extension(plans)
        verb = "adds" if mode == "APPLY" else "would add"
        print(f"--extend-bundle {extend}: {verb} {len(ext)} app(s) to its app set: {list(ext)}")
        if ext:
            hs = holders or []
            print(
                f"  ⚠️ {extend}'s app set is SHARED: every current holder gains {list(ext)}. "
                f"current holders: {len(hs)}"
            )
            for tid, name, n in hs:
                print(f"    holder {tid} ({name or '?'}) licences={n} gains {list(ext)}")
            print(f"  --apply requires --confirm-bundle-holders {len(hs)}")


def _short_reason(tp: TenantPlan) -> str:
    if tp.refused:
        return "-"
    r = tp.bundle_reason
    if r.startswith("--extend-bundle"):
        return "extended"
    if r.startswith("default"):
        return "default"
    if r == "--bundle-for":
        return "override"
    return "held" if r.startswith("held") else "only-fit"


def token_changes(plans: list[TenantPlan]) -> dict[str, Any]:
    subjects: dict[str, list[dict[str, Any]]] = {}
    for tp in plans:
        if tp.refused:
            continue
        for at in tp.apps:
            by_sub: dict[str, list[str]] = {}
            for m in at.members:
                by_sub.setdefault(_sub(m), []).append(str(m.id))
            for sub, ids in by_sub.items():
                subjects.setdefault(sub, []).append(
                    {
                        "tenant_id": tp.tenant_id,
                        "tenant": tp.name,
                        "app": at.app_key,
                        "bundle": tp.bundle_key,
                        "old_license_ids": ids,
                        "new_license_id": at.target_id,
                        "metering_provisioned": at.metered,
                    }
                )
    return {
        "generated_by": SCRIPT,
        "generated_at": _now(),
        "note": "licence ids only; no token values. Old licences stay valid until --retire-confirmed.",
        "subjects": subjects,
        "refused_tenants": [
            {"tenant_id": p.tenant_id, "tenant": p.name} for p in plans if p.refused
        ],
    }


# ── apply ──────────────────────────────────────────────────────────────────────


# Live rows carry metadata as SQL NULL, JSON ``null`` (7 of 85 portal licences, measured
# 2026-09-28) or an object. ``coalesce`` does not turn JSON null into ``{}``, and
# ``'null'::jsonb || object`` is an ARRAY — so normalise explicitly, and refuse (rowcount 0)
# to touch a row whose metadata is some other JSON type rather than rewrite it.
_BASE = "(CASE WHEN jsonb_typeof(metadata) = 'object' THEN metadata ELSE '{}'::jsonb END)"
_STAMPABLE = "(metadata IS NULL OR jsonb_typeof(metadata) IN ('object', 'null'))"


def _stamp(
    db: DatabaseManager, license_id: str, mark: dict[str, Any], *, only_if_unmarked: bool
) -> bool:
    guard = f" AND NOT ({_BASE} ? '{MARK}')" if only_if_unmarked else ""
    with db.engine.begin() as conn:
        res = conn.execute(
            text(
                f"UPDATE license SET metadata = {_BASE} "
                "|| jsonb_build_object(CAST(:k AS text), CAST(:v AS jsonb)), updated_at = now() "
                f"WHERE id = :id AND {_STAMPABLE}" + guard
            ),
            {"k": MARK, "v": json.dumps(mark), "id": license_id},
        )
        return res.rowcount == 1


def _apply_tenant(db: DatabaseManager, tp: TenantPlan) -> tuple[list[str], list[str]]:
    """Write one tenant. Returns (app keys whose adoption lost a race, unmetered creates)."""
    bundle_id = cast(UUID, tp.bundle_id)
    lost: list[str] = []
    unmetered: list[str] = []
    if not tp.holds_bundle and db.ensure_tenant_bundle(tp.tenant_id, bundle_id):
        _audit("tenant_bundle_insert", tenant_id=tp.tenant_id, bundle=tp.bundle_key)
    for at in tp.apps:
        old_ids = [str(m.id) for m in at.members]
        if at.action == "adopt":
            if not db.adopt_license_into_bundle(UUID(at.target_id), bundle_id):
                lost.append(at.app_key)
                continue
            _stamp(
                db,
                at.target_id,
                {
                    "role": "tenant_licence",
                    "adopted_at": _now(),
                    "bundle": tp.bundle_key,
                    "replaces": old_ids,
                },
                only_if_unmarked=False,
            )
            _audit(
                "adopt",
                tenant_id=tp.tenant_id,
                app=at.app_key,
                license_id=at.target_id,
                bundle=tp.bundle_key,
            )
        elif at.action == "create":
            first = at.members[0]
            expiries = [m.expires_at for m in at.members]
            expires = (
                None if any(e is None for e in expiries) else max(cast(list[datetime], expiries))
            )
            regions = {m.region_id for m in at.members}
            subject = db.create_or_get_subject(
                keycloak_sub=at.target_sub, keycloak_realm=SERVICE_REALM
            )
            try:
                lic = db.create_license(
                    subject_id=cast(UUID, subject.id),
                    app_id=at.app_id,
                    plan_id=cast(UUID, first.plan_id),
                    tenant_id=tp.tenant_id,
                    region_id=cast(UUID, regions.pop()) if len(regions) == 1 else None,
                    status="active",
                    expires_at=expires,
                    bundle_id=bundle_id,
                    license_metadata={
                        "provisioned_by": SCRIPT,
                        "bundle": tp.bundle_key,
                        MARK: {"role": "tenant_licence", "replaces": old_ids, "created_at": _now()},
                    },
                )
            except IntegrityError:
                # A concurrent run created it first (uq active tenant/app/subject).
                lost.append(at.app_key)
                continue
            db.attribute_subject_to_tenant(cast(UUID, subject.id), tp.tenant_id)
            at.target_id = str(lic.id)
            _audit(
                "create",
                tenant_id=tp.tenant_id,
                app=at.app_key,
                license_id=at.target_id,
                subject=at.target_sub,
                bundle=tp.bundle_key,
            )
            why = _provision.provision_metering(
                db, at.target_id, tp.tenant_id, str(first.plan_id), str(at.app_id)
            )
            at.metered = not why
            _audit(
                "metering_provision",
                tenant_id=tp.tenant_id,
                app=at.app_key,
                license_id=at.target_id,
                ok=at.metered,
                reason=why or None,
            )
            if why:
                unmetered.append(f"{tp.name}:{at.app_key} {at.target_id}: {why}")
        for oid in old_ids:
            marked = _stamp(
                db,
                oid,
                {
                    "state": "pending_retirement",
                    "replaced_by": at.target_id,
                    "bundle": tp.bundle_key,
                    "marked_at": _now(),
                    "by": SCRIPT,
                },
                only_if_unmarked=True,
            )
            if marked:
                _audit(
                    "mark_pending_retirement",
                    tenant_id=tp.tenant_id,
                    app=at.app_key,
                    license_id=oid,
                    replaced_by=at.target_id,
                )
    return lost, unmetered


# ── retire ─────────────────────────────────────────────────────────────────────


def _load_confirmed(path: str) -> list[tuple[str, str | None]]:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text()
    try:
        data = json.loads(raw)
        items = data.get("confirmed", []) if isinstance(data, dict) else data
    except json.JSONDecodeError:
        items = [ln.strip() for ln in raw.splitlines() if ln.strip() and not ln.startswith("#")]
    out: list[tuple[str, str | None]] = []
    for item in items:
        sub, _, tenant = str(item).partition("@")
        out.append((sub, tenant or None))
    return out


def _pending(db: DatabaseManager) -> list[License]:
    session = db.get_session()
    try:
        return (
            session.query(License)
            .options(joinedload(License.subject), joinedload(License.app))
            .filter(
                License.status == "active",
                License.deleted_at.is_(None),
                License.license_metadata[MARK]["state"].astext == "pending_retirement",
            )
            .order_by(License.tenant_id, License.id)
            .all()
        )
    finally:
        session.close()


def _teardown(pairs: list[tuple[str, str]]) -> tuple[str, list[str]]:
    from services import metering_teardown

    if not metering_teardown.METERING_SERVICE_URL:
        return "skipped_unconfigured", [lid for lid, _ in pairs]

    async def _all() -> list[tuple[str, bool]]:
        async def one(lid: str, tid: str) -> tuple[str, bool]:
            q, b = await asyncio.gather(
                metering_teardown._teardown_quota_config(license_id=lid, tenant_id=tid),
                metering_teardown._teardown_token_budget(license_id=lid, tenant_id=tid),
                return_exceptions=True,
            )
            return lid, q is True and b is True

        return list(await asyncio.gather(*(one(lid, tid) for lid, tid in pairs)))

    failed = [lid for lid, ok in asyncio.run(_all()) if not ok]
    return ("ok" if not failed else "failed"), failed


def _billing_budget_ids(license_id: str, tenant_id: str) -> set[str] | None:
    """Billing's ACTIVE ``license_sync`` budget ids for one licence; None = unknown.

    Read-only (a GET). Uses ``services.metering_teardown``'s reader — the same
    active + ``source='license_sync'`` filter the reconcile sweep counts as coverage.
    """
    from services import metering_teardown as mt

    if not mt.METERING_SERVICE_URL:
        return None

    async def _read() -> set[str] | None:
        import httpx

        headers = await mt._metering_auth_headers(tenant_id, license_id)
        async with httpx.AsyncClient(timeout=5.0) as client:
            return await mt._fetch_active_budget_ids_for_license(
                client, tenant_id=tenant_id, license_id=license_id, headers=headers
            )

    try:
        return asyncio.run(_read())
    except Exception:  # noqa: BLE001 — unknown, and the gate refuses on unknown
        return None


def _metering_refusal(db: DatabaseManager, new: License) -> str:
    """'' when the replacement is metered; otherwise why the pair must not retire.

    Retiring tears down the OLD licence's billing budget, so the replacement must
    already carry one — else the tenant's usage on this app goes unmetered.
    """
    if new.metering_provisioned_at is None:
        return "replacement is NOT metered (metering_provisioned_at NULL)"
    plan = db.get_plan(cast(UUID, new.plan_id))
    if plan is None:
        return "replacement's plan row is missing"
    # The EFFECTIVE budget (#239 / migration 041): the replacement's own
    # token_budget_override wins over its plan's value. Judging by the plan alone
    # would wave through an unbudgeted licence whose override owes billing a budget
    # on an unlimited plan.
    budget, _quota = effective_metering(new, plan)
    if budget is None or budget < 0:
        return ""  # this licence owes billing no token budget
    ids = _billing_budget_ids(str(new.id), str(new.tenant_id))
    if ids is None:
        return (
            "billing budget for the replacement is UNKNOWN "
            "(billing unreachable or METERING_SERVICE_URL unset)"
        )
    if not ids:
        return "billing holds no active license_sync budget for the replacement"
    return ""


def retire(db: DatabaseManager, path: str, apply: bool) -> int:
    confirmed = _load_confirmed(path)
    if not confirmed:
        print("ERROR: the confirmed file lists no subjects.", file=sys.stderr)
        return EXIT_USAGE
    mode = "APPLY" if apply else "DRY-RUN"
    with _ReadOnly(db):
        pending = _pending(db)
        chosen: list[tuple[License, str]] = []
        skipped: list[str] = []
        recorded_ids: dict[str, UUID | None] = {}  # bundle key -> id, one lookup per key
        for lic in pending:
            sub, tid = _sub(lic), str(lic.tenant_id)
            if not any(s == sub and (t is None or t == tid) for s, t in confirmed):
                continue
            mark = lic.license_metadata[MARK]
            new_id = str(mark.get("replaced_by") or "")
            new = db.get_license(UUID(new_id)) if new_id else None
            # The bundle --apply RECORDED on this licence's stamp (the same value it wrote
            # to token-changes.json). The replacement must still carry THAT bundle — any
            # bundle is not enough: a replacement moved to another bundle since is not
            # what the subject confirmed working.
            recorded = str(mark.get("bundle") or "")
            if recorded and recorded not in recorded_ids:
                b = db.get_bundle_by_key(recorded)
                recorded_ids[recorded] = cast(UUID, b.id) if b is not None else None
            recorded_id = recorded_ids.get(recorded) if recorded else None
            if (
                new is None
                or not new.is_active()
                or new.deleted_at is not None
                or recorded_id is None
                or new.bundle_id != recorded_id
                or str(new.tenant_id) != tid
                or new.app_id != lic.app_id
            ):
                skipped.append(
                    f"{lic.id} ({sub}): replacement {new_id or '-'} not an active licence "
                    f"in the recorded bundle {recorded or '-'}"
                )
                continue
            unmetered = _metering_refusal(db, new)
            if unmetered:
                skipped.append(f"{lic.id} ({sub}): replacement {new_id}: {unmetered}")
                continue
            chosen.append((lic, new_id))
    matched = {_sub(lic) for lic, _ in chosen}
    print(f"=== {mode}: retire confirmed member licences ===")
    for lic, new_id in chosen:
        print(f"  {_sub(lic)}  {lic.app.key}  {lic.id} -> replaced by {new_id}")
    for s in skipped:
        print(f"  SKIP {s}")
    for sub, tid in confirmed:
        if sub not in matched:
            print(f"  (no pending licence for confirmed entry {sub}{'@' + tid if tid else ''})")
    print(f"summary: revoke={len(chosen)} skipped={len(skipped)}")
    if not apply:
        print("DRY-RUN: nothing written. Re-run with --apply.")
        return EXIT_OK
    for lic, new_id in chosen:
        db.revoke_license(cast(UUID, lic.id))
        _stamp(
            db,
            str(lic.id),
            {"state": "retired", "replaced_by": new_id, "retired_at": _now(), "by": SCRIPT},
            only_if_unmarked=False,
        )
        _audit(
            "retire",
            tenant_id=str(lic.tenant_id),
            app=str(lic.app.key),
            license_id=str(lic.id),
            replaced_by=new_id,
        )
    if not chosen:
        return EXIT_OK
    status, residue = _teardown([(str(lic.id), str(lic.tenant_id)) for lic, _ in chosen])
    _audit("billing_teardown", status=status, residue=residue)
    if status != "ok":
        print(
            f"WARNING: billing teardown {status} for {len(residue)} revoked licence(s); "
            "re-run POST /api/budget/sync-from-licenses or the reaper.",
            file=sys.stderr,
        )
        return EXIT_TEARDOWN_INCOMPLETE
    return EXIT_OK


# ── main ───────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Member licences -> tenant-level (dry-run by default).",
        epilog=EXIT_CODES_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--apply", action="store_true", help="Write. Without it: dry-run, read-only.")
    p.add_argument(
        "--realm",
        action="append",
        help=f"Subject realm(s) in scope; repeatable or comma-separated; 'all' = every realm "
        f"(default {','.join(DEFAULT_REALMS)}). svc: subjects are never in scope.",
    )
    p.add_argument(
        "--extend-bundle",
        metavar="KEY",
        help="Add the apps KEY lacks to KEY's app set for tenants no bundle covers "
        "(dry-run lists them and every current holder of KEY; --apply writes bundle_app "
        "and needs --confirm-bundle-holders).",
    )
    p.add_argument(
        "--confirm-bundle-holders",
        type=int,
        metavar="N",
        help="Required with --extend-bundle --apply when apps are added: the number of "
        "tenants currently holding KEY (the dry-run prints it). Re-checked inside the "
        "write transaction; a mismatch refuses with nothing written.",
    )
    p.add_argument("--tenant", action="append", default=[], help="Limit to tenant uuid(s).")
    p.add_argument(
        "--bundle-for",
        action="append",
        default=[],
        metavar="TENANT=KEY",
        help="Choose the bundle for a tenant (uuid or name).",
    )
    p.add_argument(
        "--changes-out",
        default="token-changes.json",
        help="Where --apply writes the subject -> old -> new licence map ('-' = stdout). "
        "Informational only: --retire-confirmed reads the DB stamps, and the map can be "
        "rebuilt from them (see the module docstring), so a lost file loses nothing.",
    )
    p.add_argument(
        "--retire-confirmed",
        metavar="FILE",
        help="Retire marked licences of the subjects in FILE (JSON {confirmed:[...]}, "
        "a JSON list, or one 'sub' or 'sub@tenant-uuid' per line; '-' = stdin).",
    )
    return p.parse_args(argv)


def _realms(values: list[str] | None) -> list[str] | None:
    """``--realm`` values -> realm list, or None for 'all'."""
    if not values:
        return list(DEFAULT_REALMS)
    out = [r.strip() for v in values for r in v.split(",") if r.strip()]
    return None if "all" in out else sorted(set(out))


def main(argv: list[str] | None = None, db: DatabaseManager | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    overrides: dict[str, str] = {}
    for item in args.bundle_for:
        tenant, sep, key = item.partition("=")
        if not sep or not tenant or not key:
            print(f"ERROR: --bundle-for {item!r} is not TENANT=KEY", file=sys.stderr)
            return EXIT_USAGE
        overrides[tenant] = key
    try:
        tenants = {str(uuid.UUID(t)) for t in args.tenant}
    except ValueError:
        print("ERROR: --tenant takes tenant UUIDs", file=sys.stderr)
        return EXIT_USAGE
    if db is None:
        url = os.getenv("DATABASE_URL", "").strip()
        if not url:
            print("ERROR: DATABASE_URL is required", file=sys.stderr)
            return EXIT_USAGE
        db = DatabaseManager(url)

    if args.retire_confirmed:
        return retire(db, args.retire_confirmed, args.apply)

    realms = _realms(args.realm)
    extend = args.extend_bundle

    ext_bid: UUID | None = None
    holders: list[tuple[str, str | None, int]] = []

    def _read() -> list[TenantPlan] | None:
        nonlocal ext_bid, holders
        if extend is not None:
            b = db.get_bundle_by_key(extend)
            if b is None or b.revoked:
                print(
                    f"ERROR: --extend-bundle names unknown/revoked bundle {extend!r}",
                    file=sys.stderr,
                )
                return None
            ext_bid = cast(UUID, b.id)
            holders = db.list_bundle_holders(ext_bid)
        return build_plan(db, realms, tenants, overrides, extend)

    if args.apply:
        maybe = _read()
    else:
        with _ReadOnly(db):  # every dry-run read, the bundle lookup included
            maybe = _read()
    if maybe is None:
        return EXIT_USAGE
    plans = maybe
    mode = "APPLY" if args.apply else "DRY-RUN"
    scope = f"ALL except {SERVICE_REALM}" if realms is None else ",".join(realms)
    print(f"realms in scope: {scope} (svc: subjects excluded)")
    print_plan(plans, mode, extend, holders)
    any_refused = any(p.refused for p in plans)
    if not args.apply:
        print("DRY-RUN: nothing written (every transaction READ ONLY). Re-run with --apply.")
        return EXIT_REFUSED if any_refused else EXIT_OK

    ext = bundle_extension(plans) if extend is not None else {}
    if ext:
        if args.confirm_bundle_holders is None:
            print(
                f"ERROR: --extend-bundle {extend} changes the app set EVERY holder of it shares "
                f"({len(holders)} now). Re-run with --confirm-bundle-holders {len(holders)}. "
                "Nothing was written.",
                file=sys.stderr,
            )
            return EXIT_USAGE
        inserted, now = db.extend_bundle_apps(
            cast(UUID, ext_bid), list(ext.values()), args.confirm_bundle_holders
        )
        if inserted is None:
            print(
                f"ERROR: --confirm-bundle-holders {args.confirm_bundle_holders} but {extend} "
                f"has {now} holder(s) now. Re-run the dry-run and review. Nothing was written.",
                file=sys.stderr,
            )
            return EXIT_REFUSED
        by_id = {v: k for k, v in ext.items()}
        for app_id in inserted:
            _audit(
                "bundle_app_insert",
                bundle=extend,
                app=by_id[app_id],
                app_id=str(app_id),
                holders=now,
            )
    lost: list[str] = []
    unmetered: list[str] = []
    for tp in plans:
        if not tp.refused:
            t_lost, t_unmetered = _apply_tenant(db, tp)
            lost += [f"{tp.name}:{k}" for k in t_lost]
            unmetered += t_unmetered
    changes = json.dumps(token_changes(plans), indent=2, sort_keys=True)
    if args.changes_out == "-":
        print(changes)
    else:
        Path(args.changes_out).write_text(changes + "\n")
        print(f"token changes written to {args.changes_out}")
    if unmetered:
        _provision.report_unmetered(unmetered)
    if lost:
        print("ERROR: lost a race on: " + ", ".join(lost) + ". Re-run.", file=sys.stderr)
    else:
        print("APPLIED. Old member licences are marked pending_retirement and still valid.")
    return final_exit(bool(any_refused or lost), bool(unmetered))


if __name__ == "__main__":
    sys.exit(main())
