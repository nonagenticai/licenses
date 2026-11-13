#!/usr/bin/env python3
"""
Provision the ``demo`` tenant and mint demo-only licence tokens (demo-instance plan PR-8).

⛔ THE TARGET TENANT IS HARD-LOCKED. It is the constant id
``0195f48b-d000-7000-8000-000000000103`` named ``demo``; there is no ``--tenant-id`` /
``--tenant-name``. An earlier revision took both as free arguments and only checked
that they AGREED with each other, so ``--tenant-id <n8n-dev id> --tenant-name n8n-dev
--rotate-demo`` passed every guard and revoked n8n-dev's live token with exit 0
(audit on #219). A script whose whole purpose is "never touch a real tenant" must not
be pointable at one.

What it does, in ONE REPEATABLE READ transaction:

1. ``ensure_tenant`` the demo tenant: ``kind='test'``, ``cost_centre='tests'``,
   ``keycloak_realm='portal'``, ``byo_mode`` COPIED from ``n8n-dev`` at run time.
   If a tenant with the demo id OR the name ``demo`` already exists it must be exactly
   ``(id, name, kind, cost_centre) == (DEMO id, 'demo', 'test', 'tests')`` and not
   archived; anything else exits 2 with nothing written.
2. For each ``--apps`` key: ``create_license`` bound to ``--owner-sub`` (the n8n-demo
   user's Keycloak sub), stamped ``metadata.provisioned_by='provision_demo_tenant'``
   plus the ids of the tokens this script minted on it; then ``create_token``.
3. Prints the raw tokens ONCE, as JSON, on STDOUT — only after the commit. Everything
   else goes to STDERR; no token or token hash is ever logged.

FINGERPRINT. Over ALL ``token`` and ``license`` rows in the database — ``count(*)``,
``max(created_at)`` and an md5 of every row's state. Taken before the writes and again
after, both EXCLUDING only the exact row ids this run inserts or updates (tracked in
memory; the update set is fixed at plan time and the UPDATE's RETURNING must equal it).
Any other difference rolls the whole transaction back (exit 3). REPEATABLE READ means
both reads see one snapshot, so concurrent live traffic cannot fake a difference.

``--rotate-demo`` revokes ONLY tokens whose id is recorded in a demo licence's
``metadata.provision_demo_tenant_token_ids`` — i.e. tokens THIS SCRIPT minted, on a
licence THIS SCRIPT created, in the demo tenant. There is no UPDATE by tenant. (The
``token`` table has no free marker column — ``token_type`` is CHECK-constrained to
api/node/internal — so the marker lives on the licence's JSONB metadata; no migration.)
A demo licence without the marker is never rotated (exit 1).

Never called: ``regenerate_*``, ``revoke_*``, ``update_*`` on DatabaseManager.

⛔ DRY RUN IS THE DEFAULT. Nothing is written — no tenant, licence or token, and
``--rotate-demo`` revokes nothing — unless ``--apply`` is given. ``--dry-run`` is kept as
a no-op alias.

TENANT ROW LOCK. The guard reads the demo tenant ``FOR SHARE`` as the transaction's
first statement and re-reads it ``FOR SHARE`` right before commit, so another writer
cannot flip it to ``kind='customer'`` under a run that then commits (exit 4 on a
serialization/lock conflict, 2 if the re-check fails).

Exit codes: 0 ok / dry-run, 1 refused (missing app/plan/reference, unmarked licence,
poisoned token record), 2 bad arguments or tenant-identity guard, 3 fingerprint or
revoke-set mismatch (rolled back), 4 concurrent change (rolled back).

Usage (the DB URL only exists inside the pod; needs cluster-admin):
    kubectl -n licenses exec deploy/licenses -- python scripts/provision_demo_tenant.py \\
        --owner-sub <keycloak-sub-of-n8n-demo> --tenant-owner platform \\
        --apps kanban,kag,coding          # dry run
    kubectl -n licenses exec deploy/licenses -- python scripts/provision_demo_tenant.py \\
        --owner-sub <keycloak-sub-of-n8n-demo> --tenant-owner platform \\
        --apps kanban,kag,coding --apply  # write
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

# Make `models` importable from the repo root or the container (working dir /app).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from psycopg.errors import LockNotAvailable, SerializationFailure  # noqa: E402
from sqlalchemy import bindparam, text  # noqa: E402
from sqlalchemy.dialects.postgresql import ARRAY  # noqa: E402
from sqlalchemy.dialects.postgresql import UUID as PGUUID  # noqa: E402
from sqlalchemy.engine import Connection  # noqa: E402
from sqlalchemy.exc import DBAPIError  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from models import App, DatabaseManager, License, Plan, Tenant  # noqa: E402

logger = logging.getLogger("provision_demo_tenant")

DEMO_TENANT_ID = UUID("0195f48b-d000-7000-8000-000000000103")
DEMO_TENANT_NAME = "demo"
DEMO_REALM = "portal"
DEMO_KIND = "test"
DEMO_COST_CENTRE = "tests"
DEMO_BUDGET_PLAN_CODE = "demo"
#: Lifetime of every demo token this script mints (licenses#244: explicit, never perpetual).
DEMO_TOKEN_EXPIRY_DAYS = 365
REFERENCE_TENANT_NAME = "n8n-dev"  # the tenant whose byo_mode the demo tenant copies
MARKER_KEY = "provisioned_by"
MARKER_VALUE = "provision_demo_tenant"
TOKEN_IDS_KEY = "provision_demo_tenant_token_ids"

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_GUARD = 2
EXIT_FINGERPRINT = 3
EXIT_CONCURRENT = 4


class RefusalError(Exception):
    """A precondition failed (exit 1). Nothing is committed."""


class TenantGuardError(Exception):
    """The demo tenant identity guard refused (exit 2). Nothing is committed."""


class FingerprintChangedError(Exception):
    """A row this run did not track changed. Rolled back (exit 3)."""


# --- fingerprint -----------------------------------------------------------------

_UUIDS = ARRAY(PGUUID(as_uuid=True))
_TOKEN_FINGERPRINT_SQL = text(
    """
    SELECT count(*),
           max(t.created_at),
           md5(coalesce(string_agg(
               concat_ws('|', t.id, t.license_id, t.token_hash, t.token_type, t.revoked,
                         t.revoked_at, t.expires_at, t.created_at),
               ',' ORDER BY t.id), ''))
      FROM token t
     WHERE NOT (t.id = ANY(:exclude))
    """
).bindparams(bindparam("exclude", type_=_UUIDS))
_LICENSE_FINGERPRINT_SQL = text(
    """
    SELECT count(*),
           max(l.created_at),
           md5(coalesce(string_agg(
               concat_ws('|', l.id, l.tenant_id, l.subject_id, l.app_id, l.plan_id, l.status,
                         l.starts_at, l.expires_at, l.created_at, l.updated_at, l.deleted_at,
                         l.metadata::text),
               ',' ORDER BY l.id), ''))
      FROM license l
     WHERE NOT (l.id = ANY(:exclude))
    """
).bindparams(bindparam("exclude", type_=_UUIDS))


def fingerprint(
    conn: Connection, *, exclude_tokens: set[UUID], exclude_licences: set[UUID]
) -> dict[str, Any]:
    """(count, max(created_at), state md5) of ALL tokens and licences minus exact ids."""
    out: dict[str, Any] = {}
    for label, sql, excl in (
        ("tokens", _TOKEN_FINGERPRINT_SQL, exclude_tokens),
        ("licences", _LICENSE_FINGERPRINT_SQL, exclude_licences),
    ):
        row = conn.execute(sql, {"exclude": sorted(excl)}).one()
        out[label] = {
            "count": int(row[0]),
            "max_created_at": row[1].isoformat() if row[1] is not None else None,
            "state_md5": row[2],
        }
    return out


# --- plan ------------------------------------------------------------------------


@dataclass
class AppAction:
    app_key: str
    app_id: UUID
    plan_code: str
    plan_id: UUID | None  # None => the demo budget plan will be created
    action: str  # "create" | "skip" | "rotate"
    existing_license_id: UUID | None = None
    tokens_to_revoke: list[UUID] = field(default_factory=list)


@dataclass
class ProvisionPlan:
    tenant_exists: bool
    byo_mode: str
    owner_sub: str
    monthly_token_budget: int | None
    tenant_owner: str
    apps: list[AppAction] = field(default_factory=list)

    @property
    def planned_token_updates(self) -> set[UUID]:
        return {t for a in self.apps for t in a.tokens_to_revoke}

    @property
    def planned_licence_updates(self) -> set[UUID]:
        return {cast(UUID, a.existing_license_id) for a in self.apps if a.action == "rotate"}

    def as_json(self) -> dict[str, Any]:
        return {
            "tenant": {
                "id": str(DEMO_TENANT_ID),
                "name": DEMO_TENANT_NAME,
                "action": "reuse" if self.tenant_exists else "create",
                "keycloak_realm": DEMO_REALM,
                "kind": DEMO_KIND,
                "cost_centre": DEMO_COST_CENTRE,
                "byo_mode": self.byo_mode,
                "byo_mode_source": REFERENCE_TENANT_NAME,
                "owner_key": self.tenant_owner,
            },
            "owner_sub": self.owner_sub,
            "monthly_token_budget": self.monthly_token_budget,
            "apps": [
                {
                    "app": a.app_key,
                    "plan": a.plan_code,
                    "plan_action": "reuse" if a.plan_id is not None else "create",
                    "action": a.action,
                    "existing_license_id": str(a.existing_license_id)
                    if a.existing_license_id
                    else None,
                    "tokens_to_revoke": [str(t) for t in a.tokens_to_revoke],
                }
                for a in self.apps
            ],
        }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=f"Provision the {DEMO_TENANT_NAME!r} tenant ({DEMO_TENANT_ID}) and mint "
        "demo tokens. The tenant is fixed; it cannot be chosen."
    )
    p.add_argument(
        "--owner-sub",
        required=True,
        help="Keycloak sub (portal realm) of the n8n-demo user the licences bind to.",
    )
    p.add_argument("--apps", required=True, help="Comma-separated app keys, e.g. kanban,kag.")
    p.add_argument(
        "--plan-code",
        default="standard",
        help="Plan to license under when no --monthly-token-budget is given (default standard).",
    )
    p.add_argument(
        "--monthly-token-budget",
        type=int,
        default=None,
        help=f"Monthly LLM token budget; uses a per-app '{DEMO_BUDGET_PLAN_CODE}' plan carrying it.",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Actually write. WITHOUT this flag the script is a dry run: it prints the plan "
        "and writes nothing, and --rotate-demo revokes nothing.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="No-op alias for the default (dry run). Cannot be combined with --apply.",
    )
    p.add_argument(
        "--rotate-demo",
        action="store_true",
        help="For apps with an existing demo licence: mint a new token and revoke only the "
        "tokens this script previously minted on that licence.",
    )
    p.add_argument(
        "--tenant-owner",
        required=True,
        help="Owning app (tenant_owner key, migration 050: platform, fr, commonpractice, "
        "w2v, merqi, ...) a NEW tenant is registered under. Required, no default; "
        "ignored when the tenant is already registered.",
    )
    return p.parse_args(argv)


def _check_demo_tenant(conn: Connection) -> bool:
    """True if the demo tenant exists (and is exactly it), False if absent; else raise.

    Runs on the OUTER connection, as the transaction's first statement, ``FOR SHARE``.
    Not on a DatabaseManager session: those sit in a SAVEPOINT that ``session.close()``
    rolls back, and a row lock taken inside a rolled-back savepoint is RELEASED — the
    lock would silently evaporate before the writes (measured on PG18). Held on the
    outer transaction, no other writer can reclassify the row until this run ends, and
    (REPEATABLE READ) a change committed after our snapshot raises SerializationFailure.
    """
    rows = conn.execute(
        text(
            "SELECT id, name, kind, cost_centre, archived_at FROM tenants "
            "WHERE id = :id OR name = :name FOR SHARE"
        ),
        {"id": DEMO_TENANT_ID, "name": DEMO_TENANT_NAME},
    ).all()
    wanted = (DEMO_TENANT_ID, DEMO_TENANT_NAME, DEMO_KIND, DEMO_COST_CENTRE)
    for r in rows:
        actual = (r[0], r[1], r[2], r[3])
        if actual != wanted:
            raise TenantGuardError(
                f"existing tenant (id, name, kind, cost_centre)={actual} is not the demo "
                f"tenant {wanted}; refusing"
            )
        if r[4] is not None:
            raise TenantGuardError(f"demo tenant {DEMO_TENANT_ID} is archived; refusing")
    return bool(rows)


def _reassert_demo_tenant(conn: Connection) -> None:
    """Right before commit: the demo id/name must be exactly ONE row, still the demo tenant."""
    rows = conn.execute(
        text(
            "SELECT id, name, kind, cost_centre, archived_at FROM tenants "
            "WHERE id = :id OR name = :name FOR SHARE"
        ),
        {"id": DEMO_TENANT_ID, "name": DEMO_TENANT_NAME},
    ).all()
    wanted = (DEMO_TENANT_ID, DEMO_TENANT_NAME, DEMO_KIND, DEMO_COST_CENTRE, None)
    if [tuple(r) for r in rows] != [wanted]:
        raise TenantGuardError(f"pre-commit re-check: demo tenant rows are {rows}, not [{wanted}]")


def _is_marked(lic: License) -> bool:
    meta = lic.license_metadata or {}
    return isinstance(meta, dict) and meta.get(MARKER_KEY) == MARKER_VALUE


def _build_plan(
    session: Session, args: argparse.Namespace, apps: list[str], tenant_exists: bool
) -> ProvisionPlan:

    ref = session.query(Tenant).filter(Tenant.name == REFERENCE_TENANT_NAME).first()
    if ref is None:
        raise RefusalError(
            f"reference tenant {REFERENCE_TENANT_NAME!r} not found; cannot copy its byo_mode"
        )
    plan = ProvisionPlan(
        tenant_exists=tenant_exists,
        byo_mode=str(ref.byo_mode),
        owner_sub=args.owner_sub,
        monthly_token_budget=args.monthly_token_budget,
        tenant_owner=args.tenant_owner,
    )

    for key in apps:
        app = session.query(App).filter(App.key == key).first()
        if app is None:
            raise RefusalError(f"app {key!r} not found — run scripts/seed_apps.py first")
        if bool(app.revoked):
            raise RefusalError(f"app {key!r} is revoked")
        app_id = cast(UUID, app.id)

        budget = args.monthly_token_budget
        plan_code = DEMO_BUDGET_PLAN_CODE if budget is not None else args.plan_code
        row = (
            session.query(Plan)
            .filter(Plan.app_id == app_id, Plan.code == plan_code, Plan.region_id.is_(None))
            .first()
        )
        if budget is not None:
            if row is not None and row.token_budget_monthly != budget:
                raise RefusalError(
                    f"plan {key}/{plan_code} exists with token_budget_monthly="
                    f"{row.token_budget_monthly}, not {budget}; refusing to change it"
                )
        elif row is None:
            raise RefusalError(f"plan {key}/{plan_code} not found — run scripts/seed_apps.py first")
        if row is not None and bool(row.revoked):
            raise RefusalError(f"plan {key}/{plan_code} is revoked")

        existing = (
            session.query(License)
            .filter(
                License.tenant_id == DEMO_TENANT_ID,
                License.app_id == app_id,
                License.status == "active",
                License.deleted_at.is_(None),
            )
            .order_by(License.created_at.desc())
            .first()
        )
        action, existing_id, to_revoke = "create", None, []
        if existing is not None:
            existing_id = cast(UUID, existing.id)
            action = "rotate" if args.rotate_demo else "skip"
            if action == "rotate":
                if not _is_marked(existing):
                    raise RefusalError(
                        f"{key}: demo licence {existing_id} was not created by this script "
                        f"(no metadata.{MARKER_KEY}={MARKER_VALUE!r}); refusing to rotate it"
                    )
                recorded = [UUID(str(t)) for t in existing.license_metadata.get(TOKEN_IDS_KEY, [])]
                rows = session.execute(
                    text(
                        "SELECT id, license_id, revoked FROM token WHERE id = ANY(:ids) ORDER BY id"
                    ).bindparams(bindparam("ids", type_=_UUIDS)),
                    {"ids": recorded},
                ).all()
                # OWNERSHIP CHECK 1 of 2. A recorded id that lives on ANY other licence
                # means the metadata was poisoned (or corrupted): refuse the whole run.
                # Check 2 is the licence + tenant scope in _REVOKE_SQL.
                foreign = [str(r[0]) for r in rows if r[1] != existing_id]
                if foreign:
                    raise RefusalError(
                        f"{key}: demo licence {existing_id} records token id(s) {foreign} "
                        "that belong to another licence; refusing to revoke anything"
                    )
                to_revoke = [cast(UUID, r[0]) for r in rows if r[1] == existing_id and not r[2]]
        plan.apps.append(
            AppAction(
                app_key=key,
                app_id=app_id,
                plan_code=plan_code,
                plan_id=cast(UUID, row.id) if row is not None else None,
                action=action,
                existing_license_id=existing_id,
                tokens_to_revoke=to_revoke,
            )
        )
    return plan


# --- writes ----------------------------------------------------------------------

# OWNERSHIP CHECK 2 of 2. Exact ids only, double-scoped to the recorded licence AND the
# constant demo tenant, so a wrong id in the plan still cannot reach another tenant's
# token; the RETURNING set must then equal the plan or the run rolls back (exit 3).
_REVOKE_SQL = text(
    """
    UPDATE token t
       SET revoked = true, revoked_at = now()
      FROM license l
     WHERE t.license_id = l.id
       AND t.id = ANY(:ids)
       AND l.id = :lid
       AND l.tenant_id = :demo
       AND t.revoked = false
    RETURNING t.id
    """
).bindparams(bindparam("ids", type_=_UUIDS))


@dataclass
class Written:
    tokens: dict[str, str] = field(default_factory=dict)  # app -> raw token
    inserted_tokens: set[UUID] = field(default_factory=set)
    inserted_licences: set[UUID] = field(default_factory=set)
    updated_tokens: set[UUID] = field(default_factory=set)
    updated_licences: set[UUID] = field(default_factory=set)


def _record_token(conn: Connection, license_id: UUID, token_id: UUID, w: Written) -> None:
    rows = conn.execute(
        text(
            """
            UPDATE license
               SET metadata = jsonb_set(
                       coalesce(metadata, '{}'::jsonb),
                       ARRAY[CAST(:key AS text)],
                       coalesce(metadata -> CAST(:key AS text), '[]'::jsonb)
                           || jsonb_build_array(CAST(:tok AS text)))
             WHERE id = :lid AND tenant_id = :demo
               AND metadata ->> CAST(:mkey AS text) = CAST(:mval AS text)
            RETURNING id
            """
        ),
        {
            "key": TOKEN_IDS_KEY,
            "tok": str(token_id),
            "lid": license_id,
            "demo": DEMO_TENANT_ID,
            "mkey": MARKER_KEY,
            "mval": MARKER_VALUE,
        },
    ).all()
    if len(rows) != 1:
        raise FingerprintChangedError(f"could not record token on marked demo licence {license_id}")
    if license_id not in w.inserted_licences:
        w.updated_licences.add(license_id)


def _execute(db: DatabaseManager, conn: Connection, plan: ProvisionPlan) -> Written:
    w = Written()
    if not plan.tenant_exists:
        db.ensure_tenant(
            DEMO_TENANT_ID,
            name=DEMO_TENANT_NAME,
            keycloak_realm=DEMO_REALM,
            kind=DEMO_KIND,
            byo_mode=plan.byo_mode,
            cost_centre=DEMO_COST_CENTRE,
            owner_key=plan.tenant_owner,
        )
        logger.info("registered tenant %s (%s)", DEMO_TENANT_ID, DEMO_TENANT_NAME)

    subject = db.create_or_get_subject(keycloak_sub=plan.owner_sub, keycloak_realm=DEMO_REALM)
    for a in plan.apps:
        if a.action == "skip":
            logger.info(
                "%s: active demo licence %s exists — skip", a.app_key, a.existing_license_id
            )
            continue
        if a.action == "rotate":
            license_id = cast(UUID, a.existing_license_id)
        else:
            plan_id = a.plan_id
            if plan_id is None:
                created = db.create_plan(
                    app_id=a.app_id,
                    code=DEMO_BUDGET_PLAN_CODE,
                    name=f"{a.app_key} demo",
                    description="Demo tenant plan (provision_demo_tenant.py)",
                    token_budget_monthly=plan.monthly_token_budget,
                )
                plan_id = cast(UUID, created.id)
                logger.info("%s: created plan %s", a.app_key, DEMO_BUDGET_PLAN_CODE)
            lic = db.create_license(
                subject_id=cast(UUID, subject.id),
                app_id=a.app_id,
                plan_id=plan_id,
                tenant_id=str(DEMO_TENANT_ID),
                status="active",
                license_metadata={MARKER_KEY: MARKER_VALUE, TOKEN_IDS_KEY: []},
            )
            license_id = cast(UUID, lic.id)
            w.inserted_licences.add(license_id)
            logger.info("%s: created licence %s", a.app_key, license_id)

        # EXPLICIT bounded expiry (licenses#244): a demo token is never perpetual.
        token, raw = db.create_token(
            license_id=license_id,
            token_type="api",
            expires_at=datetime.now(UTC) + timedelta(days=DEMO_TOKEN_EXPIRY_DAYS),
        )
        token_id = cast(UUID, token.id)
        w.inserted_tokens.add(token_id)
        _record_token(conn, license_id, token_id, w)
        logger.info("%s: minted token %s on licence %s", a.app_key, token_id, license_id)

        if a.tokens_to_revoke:
            revoked = {
                cast(UUID, r[0])
                for r in conn.execute(
                    _REVOKE_SQL,
                    {"ids": a.tokens_to_revoke, "lid": license_id, "demo": DEMO_TENANT_ID},
                ).all()
            }
            if revoked != set(a.tokens_to_revoke):
                raise FingerprintChangedError(
                    f"{a.app_key}: revoke touched {len(revoked)} rows, planned "
                    f"{len(a.tokens_to_revoke)}"
                )
            w.updated_tokens |= revoked
            logger.info("%s: revoked %d script-minted demo token(s)", a.app_key, len(revoked))
        w.tokens[a.app_key] = raw
    return w


def _bind_to_transaction(db: DatabaseManager, conn: Connection) -> None:
    """Make every DatabaseManager call join ONE outer transaction.

    DatabaseManager methods each ``session.commit()``; with
    ``join_transaction_mode='create_savepoint'`` those only release a SAVEPOINT, so this
    script alone decides whether anything is committed — which is what makes the
    fingerprint abort a real rollback rather than a report.
    """
    db.SessionLocal = sessionmaker(
        bind=conn,
        autoflush=False,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )


def run(db: DatabaseManager, args: argparse.Namespace, apps: list[str]) -> int:
    conn = db.engine.connect()
    trans = conn.begin()
    try:
        # First statement of the transaction: one snapshot for every read below.
        conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        _bind_to_transaction(db, conn)

        tenant_exists = _check_demo_tenant(conn)  # first statement: snapshot + row lock
        session = db.get_session()
        try:
            plan = _build_plan(session, args, apps, tenant_exists)
        finally:
            session.close()

        planned_tok, planned_lic = plan.planned_token_updates, plan.planned_licence_updates
        before = fingerprint(conn, exclude_tokens=planned_tok, exclude_licences=planned_lic)
        print(f"fingerprint before (all rows): {json.dumps(before)}", file=sys.stderr)

        if not args.apply:
            print(json.dumps({"dry_run": True, "plan": plan.as_json()}, indent=2))
            print("DRY RUN (default): nothing written. Re-run with --apply.", file=sys.stderr)
            trans.rollback()
            return EXIT_OK

        print(f"plan: {json.dumps(plan.as_json())}", file=sys.stderr)
        w = _execute(db, conn, plan)

        if not w.updated_tokens <= planned_tok or not w.updated_licences <= planned_lic:
            raise FingerprintChangedError("updated rows outside the planned set")
        after = fingerprint(
            conn,
            exclude_tokens=planned_tok | w.inserted_tokens,
            exclude_licences=planned_lic | w.inserted_licences,
        )
        print(f"fingerprint after  (all rows): {json.dumps(after)}", file=sys.stderr)
        if after != before:
            raise FingerprintChangedError(f"untracked rows changed: before={before} after={after}")
        _reassert_demo_tenant(conn)
        trans.commit()
    except TenantGuardError as exc:
        trans.rollback()
        print(f"REFUSED (tenant guard): {exc}", file=sys.stderr)
        return EXIT_GUARD
    except RefusalError as exc:
        trans.rollback()
        print(f"REFUSED: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except FingerprintChangedError as exc:
        trans.rollback()
        print(f"ABORTED, rolled back: {exc}", file=sys.stderr)
        return EXIT_FINGERPRINT
    except DBAPIError as exc:
        trans.rollback()
        if isinstance(exc.orig, (SerializationFailure, LockNotAvailable)):
            print(f"ABORTED, concurrent change, rolled back: {exc.orig}", file=sys.stderr)
            return EXIT_CONCURRENT
        raise
    except ValueError as exc:  # ClientSafeError from ensure_tenant et al.
        trans.rollback()
        print(f"REFUSED: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except BaseException:
        trans.rollback()
        raise
    finally:
        conn.close()

    # Only after a successful commit: printed ONCE, never logged.
    out = {"tenant_id": str(DEMO_TENANT_ID), "tenant_name": DEMO_TENANT_NAME, "tokens": w.tokens}
    print(json.dumps(out, indent=2))
    if not w.tokens:
        print("no tokens minted (every app already licensed; see --rotate-demo)", file=sys.stderr)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s | %(message)s", stream=sys.stderr
    )
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    apps = list(dict.fromkeys(k.strip() for k in args.apps.split(",") if k.strip()))
    if not apps:
        print("ERROR: --apps is empty", file=sys.stderr)
        return EXIT_GUARD
    if args.monthly_token_budget is not None and args.monthly_token_budget < 0:
        print("ERROR: --monthly-token-budget must be >= 0", file=sys.stderr)
        return EXIT_GUARD
    if not args.owner_sub.strip():
        print("ERROR: --owner-sub is empty", file=sys.stderr)
        return EXIT_GUARD
    if args.apply and args.dry_run:
        print("ERROR: --apply and --dry-run are mutually exclusive", file=sys.stderr)
        return EXIT_GUARD

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL is required", file=sys.stderr)
        return EXIT_GUARD
    return run(DatabaseManager(db_url), args, apps)


if __name__ == "__main__":
    sys.exit(main())
