#!/usr/bin/env python3
r"""
Provision an opaque *service* token (idempotent) for an in-cluster service
identity, for the KAG on-behalf-of / cross-tenant trust path.

Background
----------
graph_adapter_service + kag_graph_service are moving off the "any trusted-network
pod may forge X-Tenant-Id" bypass onto a fleet-standard on-behalf-of model
(WS-4): a caller presents its own service credential in ``Authorization: Bearer
<token>`` while the *data* tenant rides the separate plain ``X-Tenant-Id`` header.
The downstream trusts that asserted tenant only when the credential introspects
to a *subject_id* on an allowlist (``GRAPH_ADAPTER_ONBEHALF_SUBJECTS`` /
``KAG_GRAPH_ONBEHALF_SUBJECTS``).

This script mints one such credential per service. It is the repeatable form of
the by-hand ``kubectl exec`` mint used for the reconciler (svc-kag-graph-
reconciler); see memory ``kag-graph-security-rechallenge-2026-07-03``.

What it does
------------
1. ``create_or_get_subject(keycloak_sub=<--subject>, realm=<--realm>)`` — the
   service identity (idempotent by keycloak_sub). Its ``Subject.id`` UUID is the
   value that introspection returns as ``subject_id`` — i.e. the ALLOWLIST KEY.
2. A dedicated, deterministic per-service tenant (``uuid5(_SERVICE_TENANT_NS,
   <subject>)`` unless ``--tenant-id`` given -- the script's OWN namespace, NOT
   ``uuid.NAMESPACE_URL``: for ``svc-commonpractice`` that is
   ``b8e94a81-dbd3-5f75-8a58-45deceaef000``, and the URL-namespace value
   ``b2583f0d-...`` is a different, never-registered id). The service's OWN tenant is irrelevant to the data
   tenant (which is asserted per-request via X-Tenant-Id) — a dedicated isolated
   tenant just keeps any accidental fallback off a real customer.
3. ``create_license(subject, app=<--app-key>, plan=<--plan>, tenant, active)`` —
   idempotent: skipped if an active (tenant, app, subject) license already exists.
4. The opaque token (``secrets.token_urlsafe(32)``, SHA-256 stored), printed
   ONCE. A brand-new license gets ``create_token``; an EXISTING one goes through
   ``regenerate_token_for_license`` — see below.

Idempotency contract
--------------------
- Re-run with the same ``--subject`` and no ``--rotate``: no-op (active license
  exists → we do not mint a second token). Prints the subject_id so you can still
  read the allowlist key.
- ``--rotate``: ROTATES. Every live token on the license is revoked and the
  replacement is minted in ONE transaction. Use when the raw token was lost or
  must be rotated.

  ⚠️ REVOKING IS THE POINT, AND IT USED NOT TO HAPPEN. Until 2026-08-25 this
  branch was an unconditional ``create_token(..., expires_at=None)`` and the word
  "revoke" did not appear in this file. So the documented incident response —
  "the token leaked, rotate it" — left the LEAKED credential introspecting
  ``active`` on the same license, with ``expires_at IS NULL`` so no clock would
  ever close it. And the credential this script mints is not an ordinary one: its
  ``subject_id`` is the on-behalf-of ALLOWLIST KEY
  (``GRAPH_ADAPTER_ONBEHALF_SUBJECTS`` / ``KAG_GRAPH_ONBEHALF_SUBJECTS``), so the
  holder of the old token kept the right to assert an arbitrary ``X-Tenant-Id``
  against graph_adapter and kag_graph — for ever, across every tenant.

  There is deliberately NO ``--keep-old`` escape hatch. An overlap window is a
  real operational need, but it is a MINT, not a rotation, and the honest way to
  ask for it is the explicit route ``POST /licenses/{license_id}/tokens`` where
  the expiry is an argument. A flag here would restore the exact silent default
  this closes.

  Rotation goes through ``DatabaseManager.regenerate_token_for_license`` rather
  than a hand-rolled revoke-then-mint, because that method already owns the parts
  that are easy to get wrong: revoke and insert share ONE transaction (a crash
  between two commits would leave the service with zero valid tokens, and a
  perpetual token is never auto-replaced, so that window is permanent), and the
  replacement INHERITS the expiry of what it replaces instead of silently
  becoming perpetual. It REFUSES rather than laundering a dead credential into an
  unbounded one — ``AllTokensRevokedError`` / ``AllTokensExpiredError``, which
  this script reports and exits non-zero on. The way forward in both cases is the
  explicit mint route named above.

``--register-only``: the tenant, and NOTHING else
------------------------------------------------
Some consumers mint their OWN licence on the service tenant over the licences API
and only need the tenant to exist (the mint guard refuses an unregistered tenant).
commonpractice is one: ``ensurePlatformInference`` mints the platform's
inference-router licence under ``cp-tenant:<tenant>:a`` on the tenant named by
``INFERENCE_PLATFORM_TENANT_ID``, exactly as it does for every CP workspace. If this
script ALSO minted (under ``svc-commonpractice``), that tenant would hold two
active ``inference-router`` licences under two subjects -- the duplicate
(tenant, app) pair billing cannot attribute (see the guard below).

``--register-only`` therefore runs step 2 alone: it registers the kind='service',
byo_mode='local_allowed' tenant and creates no subject, no licence and no token.
It is idempotent, and it still validates ``--app-key``: if the tenant ALREADY
holds an active licence for that app under THIS script's own ``--subject`` (an
earlier run without the flag), it exits 3 naming them, because the consumer's own
mint would then create the duplicate. A licence held by ANOTHER subject is the
consumer's mint having already happened; that is reported and exits 0. ``--rotate`` / ``--allow-shared-tenant`` are refused with it (exit 2).

    python scripts/provision_service_token.py \
        --subject svc-commonpractice --app-key inference-router --register-only \
        --tenant-owner platform

``--grant-owner``: let ANOTHER app act on the service tenant
------------------------------------------------------------
The tenant is registered under ``--tenant-owner``; every other owning app is refused
on it (``tenant_not_accessible`` / ``tenant_owned_by_other_app``) unless it holds a
``tenant_app_grant`` (migration 050). ``--grant-owner <owner_key>`` (repeatable)
writes that grant once the run succeeds (never on an exit-1/2/3 refusal), through the SAME helper as
``PUT /tenants/{id}/grants/{owner_key}`` (``DatabaseManager.put_tenant_grant``),
``granted_by='provision_service_token.py'``. Idempotent: an existing active grant is
left alone. An owner that is already the tenant's owner is skipped (it needs no
grant); an unknown owner key exits 2 before anything is written. It applies on the
mint path and with ``--register-only`` alike.

A cross-app grant opens a platform service tenant to another app, so it must be an
explicit decision, not a default. Do NOT use it to let w2v (Studio) see an agent-platform
tenant: by owner ruling (2026-10-22) Studio has no access to the agent platform. The grant
that migration 073 wrote on ``svc-agent-api`` (``9a4366bf-…``) was revoked live the same
day (``revoked_at`` stamped, history kept). How agent-api reaches Studio's TTS/ASR after
realm separation belongs to the realm-per-app design, not to a grant.

Duplicate (tenant, app) guard  --  exit 3
-----------------------------------------
``--tenant-id`` aimed at a REAL CUSTOMER tenant is how this script breaks billing.
The DB index ``uq_license_active_tenant_app_subject`` keys on **subject**, which is
correct (a second employee in one org legitimately adds a second active row) and
therefore stays silent when this script mints under a fresh *service* subject. But
billing's ``license_resolver`` keys on ``(tenant_id, app_name)`` and drops any key
with more than one candidate -- so the second row makes BOTH licences unresolvable
and every event for that tenant and app is stored with ``license_id`` NULL.

That is not hypothetical: licence ``01a06374`` was minted onto the ``fr-test-user``
tenant at 2026-09-02 18:50:03Z and re-broke attribution that had been repaired 2.5
hours earlier. So when the tenant already holds an active licence for this app
under a DIFFERENT subject, this script refuses with exit 3 and writes nothing.
Pass ``--allow-shared-tenant`` to override when a second subject in the same org
is genuinely intended.

The guard applies to the DEFAULT (uuid5-derived) tenant too. It used to cover only
an explicit ``--tenant-id``, on the theory that the derived tenant is private to
this service. It is not: under ``--register-only`` a consumer mints onto it
(commonpractice's ``cp-tenant:<tenant>:a``), so a later plain run would have
minted a second active (tenant, app) licence and exited 0.

Exit codes
----------
0 = minted, rotated, or idempotent no-op   1 = app/plan not found, or rotate refused
2 = bad --tenant-id, missing DATABASE_URL, unknown --grant-owner, or --register-only
    combined with --rotate / --allow-shared-tenant
3 = refused: cross-subject duplicate, or (--register-only) the tenant already
    holds an active licence for --app-key under this --subject

Usage
-----
    DATABASE_URL="postgresql+psycopg://licenses_user:PASSWORD@host:port/licenses?sslmode=require" \  # pragma: allowlist secret
        python scripts/provision_service_token.py \
            --subject svc-ingestion-graph --tenant-owner platform \
            [--app-key kag] [--plan perpetual] [--realm service] \
            [--tenant-id <uuid>] [--rotate] [--allow-shared-tenant] [--register-only] \
            [--grant-owner w2v [--grant-owner fr ...]]

Verify afterwards (introspect returns active + subject_id):
    curl -sS -X POST http://licenses.licenses.svc:8544/license/introspect \
        -H 'content-type: application/json' -d '{"token":"<raw>"}' | jq '{active,tenant_id,subject_id}'
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import uuid
from typing import cast
from uuid import UUID

# Make `models` importable when run from the licenses repo root or from inside
# the licenses container (working dir = /app).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import (  # noqa: E402
    AllTokensExpiredError,
    AllTokensRevokedError,
    DatabaseManager,
    LongLivedRegistration,
    TokenExpiryPolicyError,
)

logger = logging.getLogger("provision_service_token")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")

DEFAULT_APP_KEY = "kag"
# kag real-tenant licenses are provisioned `perpetual` (see provision_kag_tenant.py).
DEFAULT_PLAN_CODE = "perpetual"
# licenses#244: a service token is perpetual ONLY as a REGISTERED long-lived token.
# Whether service tokens become bounded + rotated is decided on 2026-12-01.
DEFAULT_LONG_LIVED_REASON = (
    "service identity token minted by provision_service_token.py; perpetual pending the "
    "service-token lifecycle decision (licenses#244, 2026-12-01)"
)
# Stable namespace so a per-service tenant is deterministic across re-runs.
_SERVICE_TENANT_NS = uuid.UUID("6f0d1e2a-3b4c-5d6e-7f80-91a2b3c4d5e6")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Provision an opaque service token for a service identity."
    )
    p.add_argument(
        "--subject",
        required=True,
        help="Service identity slug (keycloak_sub), e.g. svc-ingestion-graph. Idempotent by this value.",
    )
    p.add_argument(
        "--app-key", default=DEFAULT_APP_KEY, help="App key to bind the license to (default: kag)."
    )
    p.add_argument("--plan", default=DEFAULT_PLAN_CODE, help="Plan code (default: perpetual).")
    p.add_argument(
        "--realm",
        default="service",
        help="keycloak_realm metadata for the subject (default: service). Opaque-token path does not validate it.",
    )
    p.add_argument(
        "--tenant-id",
        default=None,
        help="Service's own tenant UUID (default: deterministic uuid5 of --subject). Not the data tenant.",
    )
    p.add_argument(
        "--owner",
        default=None,
        help=(
            "Owner recorded in the long-lived token register (default: the --subject "
            "service itself). See licenses#244."
        ),
    )
    p.add_argument(
        "--long-lived-reason",
        default=DEFAULT_LONG_LIVED_REASON,
        help="Reason recorded in the long-lived token register.",
    )
    p.add_argument(
        "--rotate",
        action="store_true",
        help=(
            "Rotate: revoke every live token on the existing license and mint a replacement "
            "in one transaction (otherwise an existing active license is left alone). "
            "The old token stops introspecting active immediately."
        ),
    )
    p.add_argument(
        "--allow-shared-tenant",
        action="store_true",
        help=(
            "Permit minting into a tenant that ALREADY holds an active license for this app "
            "under a DIFFERENT subject. Off by default: --tenant-id pointed at a real customer "
            "tenant is how a duplicate (tenant, app) pair gets created, and that pair is "
            "unresolvable by billing attribution (the key is dropped, license_id goes NULL). "
            "Only pass this when a second subject in the same org is genuinely intended."
        ),
    )
    p.add_argument(
        "--register-only",
        action="store_true",
        help=(
            "Register the kind='service' tenant ONLY: no subject, no licence, no token. "
            "For a consumer that mints its own licence on the tenant over the API "
            "(commonpractice's ensurePlatformInference). Exits 3 if the tenant already holds "
            "an active licence for --app-key under this --subject, since the consumer's mint "
            "would then be a duplicate (tenant, app) pair; another subject's licence is a "
            "no-op (exit 0)."
        ),
    )
    p.add_argument(
        "--tenant-owner",
        required=True,
        help="Owning app (tenant_owner key, migration 050: platform, fr, commonpractice, "
        "w2v, merqi, ...) a NEW tenant is registered under. Required, no default; "
        "ignored when the tenant is already registered.",
    )
    p.add_argument(
        "--grant-owner",
        action="append",
        default=[],
        metavar="OWNER_KEY",
        help=(
            "Repeatable. After registering the tenant, grant this OTHER owning app "
            "(tenant_owner key) access to it -- the same tenant_app_grant as "
            "PUT /tenants/{id}/grants/{owner_key}. Idempotent; the tenant's own owner is "
            "skipped; an unknown key exits 2 before anything is written."
        ),
    )
    return p.parse_args(argv)


def _default_tenant_id(subject: str) -> str:
    """The service's deterministic own tenant: ``uuid5(_SERVICE_TENANT_NS, subject)``."""
    return str(uuid.uuid5(_SERVICE_TENANT_NS, subject))


def _ensure_service_tenant(db: DatabaseManager, tenant_id: str, args: argparse.Namespace):
    """Register the service tenant (INSERT-only; see the comment at the call in ``main``)."""
    return db.ensure_tenant(
        tenant_id,
        name=args.subject,
        keycloak_realm=args.realm,
        kind="service",
        byo_mode="local_allowed",
        owner_key=args.tenant_owner,
    )


GRANTED_BY = "provision_service_token.py"


def _apply_grants(db: DatabaseManager, tenant, args: argparse.Namespace) -> None:
    """``--grant-owner``: ``put_tenant_grant`` per owner, exactly as the PUT route does."""
    for owner_key in dict.fromkeys(args.grant_owner):
        if owner_key == str(tenant.owner_key):
            print(f"grant {owner_key:<10}: skipped (owns tenant {tenant.id}; needs no grant)")
            continue
        _grant, created = db.put_tenant_grant(str(tenant.id), owner_key, GRANTED_BY)
        print(f"grant {owner_key:<10}: {'created' if created else 'already active'}")
        logger.info(
            "tenant_grant_put: tenant=%s owner=%s grantee=%s by=%s created=%s",
            tenant.id,
            tenant.owner_key,
            owner_key,
            GRANTED_BY,
            created,
        )


def _register_only(db: DatabaseManager, tenant_id: str, args: argparse.Namespace) -> int:
    """``--register-only``: the tenant row (plus any ``--grant-owner`` grants) and nothing
    else. See the module docstring."""
    tenant = _ensure_service_tenant(db, tenant_id, args)
    print("\n=== service tenant (registered; no licence minted) ===")
    print(f"tenant_id       : {tenant_id}")
    print(f"name            : {tenant.name}")
    print(f"kind            : {tenant.kind}")
    print(f"byo_mode        : {tenant.byo_mode}")
    if str(tenant.kind) != "service":
        # ensure_tenant is INSERT-only, so a pre-existing row keeps its kind. Say so
        # rather than print a header that implies this run classified it.
        print(
            f"WARNING: tenant {tenant_id} already existed with kind={tenant.kind!r}; "
            "ensure_tenant never overwrites it.",
            file=sys.stderr,
        )
    held = db.get_active_license(tenant_id, args.app_key)
    # Only the script's OWN subject is the duplicate shape: a licence this script
    # minted (an earlier run without the flag) plus the consumer's own mint. A
    # licence held by ANOTHER subject is the consumer's mint having already happened
    # (commonpractice's cp-tenant:<tenant>:a), which is the intended end state, so a
    # re-run must stay a no-op. register-only never creates the subject, so a
    # missing subject means the script never minted here.
    own = db.get_subject_by_keycloak_sub(args.subject)
    own_id = str(own.id) if own is not None else None
    mine = [lic for lic in held if own_id is not None and str(lic.subject_id) == own_id]
    if mine:
        print(
            f"ERROR: tenant {tenant_id} already holds {len(mine)} active '{args.app_key}' "
            f"license(s) under this script's own subject '{args.subject}':",
            file=sys.stderr,
        )
        for lic in mine:
            print(f"  license={lic.id} subject_id={lic.subject_id}", file=sys.stderr)
        print(
            "A consumer that mints its own licence here (e.g. commonpractice "
            "ensurePlatformInference) would create a duplicate (tenant, app) pair, which "
            "billing attribution resolves to NOTHING. Revoke these first, or use them "
            "instead of minting.",
            file=sys.stderr,
        )
        return 3
    # Grants only after the exit-3 refusal: a refused run writes no grant.
    _apply_grants(db, tenant, args)
    if held:
        print(
            f"OK: tenant {tenant_id} already holds {len(held)} active '{args.app_key}' "
            "license(s) under other subject(s) -- the consumer has minted; nothing to do."
        )
        for lic in held:
            print(f"  license={lic.id} subject_id={lic.subject_id}")
    logger.info(
        "registered service tenant %s (%s) for app '%s' -- no licence minted",
        tenant_id,
        args.subject,
        args.app_key,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    if args.register_only and (args.rotate or args.allow_shared_tenant):
        print(
            "ERROR: --register-only mints nothing, so --rotate / --allow-shared-tenant "
            "have nothing to act on. Drop them.",
            file=sys.stderr,
        )
        return 2

    if args.tenant_id:
        try:
            tenant_id = str(uuid.UUID(args.tenant_id))
        except ValueError:
            print(f"ERROR: --tenant-id {args.tenant_id!r} is not a UUID.", file=sys.stderr)
            return 2
    else:
        tenant_id = _default_tenant_id(args.subject)

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL is required", file=sys.stderr)
        return 2

    db = DatabaseManager(db_url)

    app = db.get_app_by_key(args.app_key)
    if app is None:
        print(
            f"ERROR: app '{args.app_key}' not found. Seed it first "
            "(scripts/seed_test_licenses.py / seed_portal_licenses.py for kag).",
            file=sys.stderr,
        )
        return 1
    app_uuid = cast(UUID, app.id)

    unknown = [o for o in dict.fromkeys(args.grant_owner) if not db.owner_exists(o)]
    if unknown:
        print(
            f"ERROR: --grant-owner {', '.join(unknown)}: not a tenant_owner key. Nothing was written.",
            file=sys.stderr,
        )
        return 2

    if args.register_only:
        return _register_only(db, tenant_id, args)

    all_plans = db.list_plans(app_uuid)
    plans = [pl for pl in all_plans if str(pl.code) == args.plan]
    if not plans:
        available = sorted({str(pl.code) for pl in all_plans})
        print(
            f"ERROR: plan '{args.app_key}/{args.plan}' not found. "
            f"Available codes: {available or '(none)'}. Pass one via --plan.",
            file=sys.stderr,
        )
        return 1
    plan_uuid = cast(UUID, plans[0].id)

    subject = db.create_or_get_subject(keycloak_sub=args.subject, keycloak_realm=args.realm)
    subject_id = str(subject.id)

    # Register the service's own tenant BEFORE minting its license, so the row can
    # never be an orphan (Stage 1 of the license.tenant_id -> tenants.id FK
    # campaign). The tenant id is deterministic (uuid5 of --subject, or --tenant-id);
    # its natural name is the service slug. Idempotent; ensure_tenant keeps an
    # already-registered tenant untouched. The service slug must match the tenants
    # name format ^[a-z0-9][a-z0-9-]*$.
    # kind='service' at MINT time. Without it the row is born 'unclassified' and
    # lands in the operator tenant inventory that GET /tenants deliberately keeps
    # free of service principals (migration 027: "NOT a tenant in the commercial
    # sense; these should be excluded from any tenant inventory"). Measured
    # 2026-09-03: 43 of 76 registry rows were service actors minted by this very
    # script, and every one had to be reclassified by hand afterwards.
    #
    # byo_mode='local_allowed' at MINT time, for the SAME reason and to close the
    # SAME loop. `tenants.byo_mode` server-defaulted to 'byo_required' (models.py;
    # 'local_allowed' under 042, 'byo_required' AGAIN since migration 058 /
    # licenses#273 — so this explicit value is load-bearing), which means "commercial: must bring their own provider; worker_management
    # hard-fails a run with no configured one". A machine identity has no account
    # and nowhere to have put an API key, so that is not a strict policy, it is an
    # unsatisfiable one: the only reachable outcome is a 402
    # `byo_llm_not_configured` on the first BYO-gated call — raised WITHOUT
    # LOGGING (worker_management/src/api/message_endpoints.py:628), so the refusal
    # is not even greppable. svc-kag took exactly that 402 on
    # POST /workers/kag_synthesize/message at 2026-09-03T04:16:53Z.
    #
    # Until this argument existed, ensure_tenant could not accept a byo_mode at
    # all, so every principal this script minted was born
    # kind='service' + byo_mode='byo_required'. Migration 031 repaired 34 such
    # rows and its header names this call site as the unfixed generator; this line
    # is what stops the 35th. 'local_allowed' (not 'platform_only') because it
    # permits platform inference AND still honours a configured external provider,
    # so a principal that delegates on behalf of a customer keeps working — and it
    # is what all 9 already-correct service tenants carry.
    #
    # INSERT-only inside ensure_tenant: re-running this script against an existing
    # principal never overwrites an operator's later choice.
    tenant = _ensure_service_tenant(db, tenant_id, args)

    existing = db.find_active_license(
        tenant_id=tenant_id, app_id=app_uuid, subject_id=cast(UUID, subject.id)
    )
    if existing and not args.rotate:
        logger.info(
            "active '%s' license already exists for subject '%s' (license=%s) — no token minted "
            "(re-run with --rotate to mint a fresh one).",
            args.app_key,
            args.subject,
            existing.id,
        )
        _apply_grants(db, tenant, args)
        print("\n=== service identity (existing) ===")
        print(f"subject         : {args.subject}")
        print(
            f"subject_id      : {subject_id}   <-- ALLOWLIST KEY (GRAPH_ADAPTER_ONBEHALF_SUBJECTS / KAG_GRAPH_ONBEHALF_SUBJECTS)"
        )
        print(f"tenant_id       : {tenant_id}")
        print(f"license_id      : {existing.id}")
        print("token           : (unchanged — use --rotate to mint a new one)")
        return 0

    # A duplicate (tenant, app) pair is unresolvable DOWNSTREAM, not here: billing's
    # license_resolver keys on (tenant_id, app_name) and drops any key with more than one
    # candidate, so license_id -> NULL and the spend becomes unattributable. The existing
    # index uq_license_active_tenant_app_subject keys on SUBJECT and is therefore silent
    # for a new subject -- which is exactly how licence 01a06374 was minted onto the
    # fr-test-user tenant on 2026-09-02 and re-broke attribution that had just been fixed.
    #
    # Guard EVERY tenant, explicit --tenant-id or the derived uuid5 alike. The derived
    # tenant is NOT private to this service: --register-only hands it to a consumer
    # that mints its own licence there (commonpractice#431, cp-tenant:<tenant>:a), so a
    # later plain run would add a second active (tenant, app) licence and exit 0.
    # Only guard the MINT path: rotating an existing licence adds no row.
    if existing is None and not args.allow_shared_tenant:
        others = [
            lic
            for lic in db.get_active_license(tenant_id, args.app_key)
            if str(lic.subject_id) != subject_id
        ]
        if others:
            print(
                f"ERROR: tenant {tenant_id} already holds "
                f"{len(others)} active '{args.app_key}' license(s) under a different subject:",
                file=sys.stderr,
            )
            for lic in others:
                print(f"  license={lic.id} subject_id={lic.subject_id}", file=sys.stderr)
            print(
                "Minting another would create a duplicate (tenant, app) pair, which billing "
                "attribution resolves to NOTHING -- every event for this tenant and app would "
                "be stored with a NULL license_id. Nothing was written.\n"
                "If a second subject in this org is genuinely intended, re-run with "
                "--allow-shared-tenant.",
                file=sys.stderr,
            )
            return 3

    if existing:
        # Reaching here means --rotate (the no-rotate arm returned above). ROTATE,
        # do not mint alongside: leaving the previous token live is what made
        # "rotate the leaked credential" a no-op. See the module docstring.
        license = existing
        try:
            revoked_count, _token, raw = db.regenerate_token_for_license(
                license_id=cast(UUID, license.id), token_type="internal"
            )
        except (AllTokensRevokedError, AllTokensExpiredError, TokenExpiryPolicyError) as exc:
            # Refusing is the designed behaviour, not a bug: with nothing live to
            # inherit an expiry from, rotating would mint a PERPETUAL replacement
            # for a bounded predecessor. Nothing is written; the operator mints
            # explicitly instead.
            print(f"ERROR: cannot rotate license {license.id}: {exc}", file=sys.stderr)
            return 1
    else:
        revoked_count = 0
        license = db.create_license(
            subject_id=cast(UUID, subject.id),
            app_id=app_uuid,
            plan_id=plan_uuid,
            tenant_id=tenant_id,
            status="active",
            license_metadata={"provisioned_by": "provision_service_token.py", "scope": "service"},
        )
        # REGISTERED long-lived, never silently perpetual (licenses#244).
        _token, raw = db.create_token(
            license_id=cast(UUID, license.id),
            token_type="internal",
            long_lived=LongLivedRegistration(
                owner=args.owner or args.subject, reason=args.long_lived_reason
            ),
        )

    # Grants only once the mint/rotate succeeded: the exit-3 and rotate refusals above
    # promise "Nothing was written".
    _apply_grants(db, tenant, args)

    logger.info(
        "provisioned service token for subject '%s' (subject_id=%s, license=%s, tenant=%s, "
        "revoked_previous=%d)",
        args.subject,
        subject_id,
        license.id,
        tenant_id,
        revoked_count,
    )
    print("\n=== service identity (SAVE THE TOKEN — shown once) ===")
    print(f"subject         : {args.subject}")
    print(
        f"subject_id      : {subject_id}   <-- ALLOWLIST KEY (GRAPH_ADAPTER_ONBEHALF_SUBJECTS / KAG_GRAPH_ONBEHALF_SUBJECTS)"
    )
    print(f"tenant_id       : {tenant_id}")
    print(f"license_id      : {license.id}")
    print(f"revoked previous: {revoked_count}   <-- old tokens killed by this rotation")
    print(f"token           : {raw}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
