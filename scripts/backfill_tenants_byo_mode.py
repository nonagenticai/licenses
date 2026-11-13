#!/usr/bin/env python3
"""Backfill a classified ``tenants`` row for every active-license tenant_id.

Phase 3b of the BYO-LLM posture flip. Phase 3 made ``byo_mode`` default to
``byo_required`` for *new* tenants, and the fail-closed flip is now LIVE: the
``tenants`` table is a sparse policy overlay, and ``DatabaseManager._lookup_byo_mode``
already fails CLOSED to ``byo_required`` for an unresolved/missing tenant row (no
platform GPU by default); only a genuine DB/infra fault fails OPEN to
``local_allowed`` to protect the auth hot path. This script provisions an explicit,
classified row for **every** active-license tenant_id so the posture is auditable
and each tenant's ``byo_mode`` is set intentionally rather than by the fail-closed
default.

Classification (decided with George 2026-06-21):
  * ``local_allowed`` — the owner's own internal/dev tenants (may use platform
    GPU OR bring their own).
  * ``byo_required``  — real external/commercial tenants (must bring their own
    LLM; worker_management hard-fails a run with no configured provider).
  * ``platform_only`` — system / internal-infrastructure tenants only (the
    all-zero default S2S tenant, the platform control-plane family, and the
    internal book-fulfillment service), locked to platform inference so they
    keep working; enumerated in ``PLATFORM_ONLY_KEEP``.

2026-07-07 posture flip (decided with George): BYO is now the DEFAULT for every
tenant. The classification is keyed by ``license.tenant_id``. Because that column
is a free-form VARCHAR that may hold either the canonical tenant UUID or a legacy
string name (e.g. ``"n8n-dev"``), the allowlist sets below carry BOTH forms.
Anything not in ``LOCAL_ALLOWED`` (owner-internal + fr-test) or
``PLATFORM_ONLY_KEEP`` (system/internal infra) now falls to ``byo_required`` —
the commercial default: trials/demos must bring their own LLM key.

Idempotent: re-running only updates rows whose ``byo_mode`` differs and inserts
rows that are still missing. Dry-run by default — pass ``--apply`` to commit.

⛔ IT WILL NOT MINT A TENANT WHOSE ID IS A KEYCLOAK PRINCIPAL. The ids this script
inserts come out of ``license.tenant_id``, which is precisely the column the known
violators live in, so the INSERT branch applies the same refusal
``models.ensure_tenant`` does — importing the SAME
``services.tenant_subject_invariant.subject_for_tenant_id`` predicate rather than
restating the join, exactly as ``seed_platform_tenants`` does. A hit is REPORTED
and SKIPPED; nothing is re-keyed and nothing is repaired. See the comment at the
INSERT branch for why the predicate is imported instead of calling ``ensure_tenant``
(it commits on its own session, which would write during a dry run).

Usage (inside the licenses container, working dir = /app):
    .venv/bin/python scripts/backfill_tenants_byo_mode.py --tenant-owner platform           # dry-run
    .venv/bin/python scripts/backfill_tenants_byo_mode.py --tenant-owner platform --apply   # commit

``DATABASE_URL`` is read from the environment (the pod already sets it).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from uuid import UUID

# Make `models` importable both from the repo root and inside the container.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import DatabaseManager, License, Tenant  # noqa: E402
from services.tenant_subject_invariant import subject_for_tenant_id  # noqa: E402

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# --- Classification allowlists (both UUID and legacy-name forms) -------------

# Owner's own internal/dev tenants → may use platform GPU or BYO.
LOCAL_ALLOWED: set[str] = {
    # UUIDs
    "019de842-c92b-74cd-a7ce-848302bce3b2",  # gerard-dev
    "019de842-c92a-7e37-8fdd-eb0d8c5396f9",  # n8n-dev
    "019de842-c92b-752e-b39c-30d64950e1cc",  # n8n-test
    "019de842-c92b-7564-b932-98240d274b3b",  # portal
    "019edc61-9495-76bc-b65f-c92a8e6880df",  # marketing
    "019edc61-9495-7ee7-93dd-ea9e1f687f42",  # sales
    "0195f48b-d000-7000-8000-000000000002",  # platform control-plane
    "019efaa0-512b-733e-9e98-b1ac82a6f7ee",  # fr-test (super-admin test tenant)
    "0195f48b-d000-7000-8000-000000000099",  # fr-test Keycloak-user tenant (holds
    # fr+kag licenses; the identity the browser-test user actually carries — the
    # tenant NAMED "fr-test" above (019efaa0) is a separate/older row, so the #51
    # exception missed the operative one and it fell through to byo_required)
    "01a1113b-674f-72b0-a84b-dc746d39afca",  # render/E2E service-account tenant
    # (re-keyed from e148e8af-… by migration 046; that id is now a principal only)
    # (channel owner "George Dekker"; the identity both nightly E2E harnesses bind
    # to). Omitting it on the 2026-07-07 posture flip dropped it to byo_required,
    # which broke the tokenless-S2S run stages (WM 402 byo_llm_not_configured) —
    # exactly the failure mode the PLATFORM_ONLY_KEEP comment warns about. It is
    # owner-internal like n8n-dev/portal → local_allowed (rides platform default).
    # Per-service SA tenants (svc-*) minted by provision_service_token.py for the
    # kag-ecosystem embed hop. Their outbound /api/ml/embeddings call to the
    # modality-gateway resolves BYO under the SA's own tenant (the 5 non-delegating
    # services) or the customer tenant (kag-graph, which forwards X-Effective-Tenant-Id).
    # These SA tenants are owner-internal infra → local_allowed so they ride the
    # platform embedding default; byo_required would 402 "byo_llm_not_configured"
    # exactly like the e148e8af regression above (verified live 2026-07-23).
    "c13dfe28-f5af-5656-8786-51f34b2629ec",  # svc-nlp-service
    "a8035a90-4210-5900-9529-45b3b90b72a2",  # svc-kag-graph
    "9ec0a01d-78cd-54e1-9f40-8e48e93f6707",  # svc-hirag
    "e929ef57-5670-5c26-85ac-eab725866516",  # svc-karma
    "f5e6212e-5a16-5a16-9864-0ce1ab1a08ee",  # svc-discovery-engine
    "bbdb4939-4c4c-5b4c-8286-3ffd3921b8a7",  # svc-media-discovery
    "16f5fb5b-141d-5fc1-b513-24ca7d4a31b4",  # svc-scientific-processor
    # svc-tts-synth was minted by the same provision_service_token.py run but was
    # never listed here, so classify() returns byo_required for it while its live
    # `tenants` row already says local_allowed — someone fixed it out of band, and
    # the next `--apply` would silently REVERT it (the 2026-08-21 dry-run shows
    # exactly that pending "local_allowed->byo_required" downgrade). Not a
    # judgement call: uuid5(_SERVICE_TENANT_NS, "svc-tts-synth") reproduces this
    # UUID exactly, so it is provably one of the owner's own per-service SA
    # tenants — the same class as the six above.
    "333c001e-03eb-5508-901c-b05409b0df85",  # svc-tts-synth
    # svc-cdmsfirst-n8n: the SA identity the CDMSFIRST resolution n8n stack carries
    # on its Workers API license token when it calls worker-management for the
    # Classify/Draft/Chase inference (model=local-llm, provider=inference_router).
    # It succeeded until the 2026-07-20/21 licenses auth fixes (#73/#74/#75) started
    # resolving its real tenant instead of a sentinel subject; being unlisted, it
    # fell to the byo_required default and every Classify run 402'd
    # "byo_llm_not_configured" — the SAME regression as e148e8af / the svc-* block
    # above. It is owner-internal pilot infra → local_allowed (rides platform GPU).
    # Fixed live + proven (WM 200, intent=password_reset) 2026-07-23.
    "97d33e0a-fc56-5ad6-8c32-1ccc565ff327",  # svc-cdmsfirst-n8n
    # The two owner-internal merqi GTM tenants. The `marketing` and `sales`
    # tenants were listed on the 2026-07-07 flip, but the tenants the merqi GTM
    # stack actually RESOLVES to at run time were not, so both fell to the
    # byo_required default and marketing's recon enrichment has been dead in
    # production ever since (worker-management 402 byo_llm_not_configured;
    # modality-gateway 402 "tenant must configure its own embedding provider").
    # This is the FIFTH instance of the regression documented above (e148e8af,
    # the svc-* block, svc-cdmsfirst-n8n) and once for fr-test (#51 pinned
    # 019efaa0 and missed the operative 0195f48b-…-099). Both are the owner's
    # OWN internal GTM tenants — the same class as marketing/sales — so by this
    # file's own rule they are local_allowed. Nothing external or commercial is
    # entitled. Verified live 2026-08-21: inference-router:8150 /health 200 →
    # the local vLLM on :8001 serves the exact seeded model id `local-llm`;
    # the local MLX ingestion backend on :9120 reports model_loaded=true.
    "e4f70a83-81ee-429a-abca-6999e3a0d24f",  # nonagentic-gtm — the tenant
    # marketing's token actually resolves to, and the only tenant the
    # `icp-analysis` worker row is registered under.
    "639c333a-1613-4fbc-aa14-a7b1430dc0db",  # merqi
    # Nonagentic AI — set local_allowed live 2026-10-05 via the licences API
    # (embeddings 402 for merqi round 7); keep the backfill from flagging it.
    "3806d6e5-4698-571e-9bb2-22ffd076ac73",  # tenant-dev (Nonagentic AI, kind=internal)
    # Legacy string-name forms (in case a license stored the name, not the UUID)
    "n8n-dev",
    "n8n-test",
    "portal",
    "marketing",
    "sales",
    "nonagentic-gtm",
    "merqi",
    "fr-test",
    "svc-nlp-service",
    "svc-kag-graph",
    "svc-hirag",
    "svc-karma",
    "svc-discovery-engine",
    "svc-media-discovery",
    "svc-scientific-processor",
    "svc-tts-synth",
}

# Real external/commercial tenants → must bring their own LLM.
# NOTE: as of the 2026-07-07 posture flip the DEFAULT for any unlisted tenant is
# also 'byo_required' (see classify()), so this set is no longer the only source
# of byo_required — it just documents the known commercial tenants explicitly.
BYO_REQUIRED: set[str] = {
    "019edc1e-313d-779d-9330-fe013c705be8",  # subhankar (kag)
    "918c1948-0f9e-4f1d-b15e-7ca1a950a184",  # subhankar (kag)
}

# System / internal-infrastructure tenants that must STAY on platform inference
# (BYO disabled) — flipping these to byo_required would break tokenless S2S runs
# (the all-zero default/anonymous tenant), an internal service (book-fulfillment),
# and the platform control-plane family. Explicit keep-list overrides the
# byo_required fall-through in classify().
PLATFORM_ONLY_KEEP: set[str] = {
    "00000000-0000-0000-0000-000000000000",  # default / anonymous S2S tenant
    "00000000-0000-0000-0000-000000000001",  # system tenant
    "0195f48b-d000-7000-8000-000000000001",  # platform control-plane family
    "0195f48b-d000-7000-8000-000000000003",  # platform control-plane family
    "0195f48b-d000-7000-8000-000000000004",  # platform control-plane family
    "0195f48b-d000-7000-8000-000000000005",  # platform control-plane family
    "01a1109c-f895-7766-ba28-6ea56e56a5f7",  # book-fulfillment (re-keyed by migration 045)
    "book-fulfillment",
}

# Optional human-readable names for rows we create (else the tenant_id string is
# used as the name — it is unique and satisfies the name CHECK constraint).
FRIENDLY_NAMES: dict[str, str] = {
    "0195f48b-d000-7000-8000-000000000002": "platform-core",
    "01a1113b-674f-72b0-a84b-dc746d39afca": "portal-render-e2e",
    "019edc1e-313d-779d-9330-fe013c705be8": "subhankar-kag",
    "918c1948-0f9e-4f1d-b15e-7ca1a950a184": "subhankar-kag-2",
    "01a1109c-f895-7766-ba28-6ea56e56a5f7": "book-fulfillment",
    "e4f70a83-81ee-429a-abca-6999e3a0d24f": "nonagentic-gtm",
    "639c333a-1613-4fbc-aa14-a7b1430dc0db": "merqi",
    "333c001e-03eb-5508-901c-b05409b0df85": "svc-tts-synth",
    # fr-test Keycloak-user tenant (the identity the browser-test user carries;
    # holds fr+kag licenses). "fr-test" is already taken by 019efaa0.
    "0195f48b-d000-7000-8000-000000000099": "fr-test-user",
    # System / platform control-plane family (PLATFORM_ONLY_KEEP).
    "00000000-0000-0000-0000-000000000000": "default-anon",
    "00000000-0000-0000-0000-000000000001": "system",
    # Real Keycloak portal-realm identities, not anonymous slots — see migration
    # 024. `n8n-dev` also exists at 019de842-c92a-… (billing #86, unresolved).
    "0195f48b-d000-7000-8000-000000000001": "n8n-dev-portal-realm",
    "0195f48b-d000-7000-8000-000000000003": "george-dev-portal-realm",
    # fr's prompt/procedure improvement-loop tenants — see migration 026. Named
    # from fr migrations 062/066 + the Keycloak realm-patch script, not invented.
    "0195f48b-d000-7000-8000-000000000004": "prompt-improvement",
    "0195f48b-d000-7000-8000-000000000005": "procedure-improvement",
}


#: How permissive each mode is. A move DOWN this scale removes an entitlement a tenant is
#: currently using, which is the only direction that can break a live tenant.
_PERMISSIVENESS = {"byo_required": 0, "local_allowed": 1, "platform_only": 2}


def is_downgrade(current: str, target: str) -> bool:
    """Would moving ``current`` -> ``target`` REMOVE entitlement the tenant has today?

    The allowlists in this file are the only input to :func:`classify`, and this file has
    forgotten an owner-internal tenant SIX times (e148e8af, the svc-* block, svc-cdmsfirst-n8n,
    fr-test's 019efaa0-vs-0195f48b, and the two merqi GTM tenants). Each omission is invisible
    until something 402s. The mirror-image failure is worse and was live on 2026-08-21: a tenant
    whose row someone had already fixed OUT OF BAND to ``local_allowed``, but which is still
    unlisted here, is computed as ``byo_required`` -- so the next ``--apply`` silently REVERTS
    the fix and takes the tenant down again, with a success message.

    An unknown mode on either side is treated as a downgrade: refusing is recoverable, a silent
    revert is not.
    """
    return _PERMISSIVENESS.get(target, -1) < _PERMISSIVENESS.get(current, 99)


def classify(tenant_id: str) -> str:
    # ⚠️ NOT CHANGED BY MIGRATION 042 (George 2026-10-02: new tenants default to
    # 'local_allowed'). That ruling moves the COLUMN default, which applies to new
    # rows on INSERT. This function CLASSIFIES EXISTING tenants on an --apply run,
    # and its fall-through is what every unlisted existing tenant is set to — so
    # flipping it to 'local_allowed' would silently UPGRADE every unlisted
    # byo_required tenant on the next --apply (an upgrade, so
    # `is_downgrade` would not refuse it). Moving existing tenants is a separate,
    # explicit operator action (set_tenant_byo_mode / PUT .../byo-mode). Its only
    # INSERT path (`plan_new_tenant`) is unreachable post-016 (see its docstring).
    #
    # Explicit keep-lists win over the default. Order: system/internal must stay
    # platform_only; known commercial + owner-internal are pinned; everything
    # else defaults to byo_required (2026-07-07 posture: BYO is the default for
    # all tenants except the owner's internal tenants and fr-test).
    if tenant_id in PLATFORM_ONLY_KEEP:
        return "platform_only"
    if tenant_id in BYO_REQUIRED:
        return "byo_required"
    if tenant_id in LOCAL_ALLOWED:
        return "local_allowed"
    return "byo_required"


def _as_uuid(value: str) -> UUID | None:
    try:
        return UUID(str(value))
    except ValueError, TypeError:
        return None


def _pick_name(tenant_id: str) -> str:
    friendly = FRIENDLY_NAMES.get(tenant_id)
    if friendly is None:
        # No friendly name → derive a name that can NEVER equal id::text, so
        # migration 004's `DELETE FROM tenants WHERE name = id::text` cannot wipe
        # a legitimately backfilled row. (That self-conflict — the backfill
        # creating name==uuid rows that 004 is built to delete — silently removed
        # ~39 rows when 004 applied 2026-07-09, incl. the render/E2E service acct.)
        name = f"tenant-{tenant_id}".lower()
    else:
        name = friendly.lower()
    if not NAME_RE.fullmatch(name):
        # Sanitize to satisfy ck_tenants_name_format; keep it deterministic.
        name = re.sub(r"[^a-z0-9-]", "-", name).strip("-") or "tenant"
        if not NAME_RE.fullmatch(name):
            name = f"t-{name}"
    return name


@dataclass(frozen=True)
class NewTenantPlan:
    """What to do about a ``license.tenant_id`` that has NO ``tenants`` row yet.

    A decision, not an effect: nothing here touches the session. ``main()`` is what
    adds ``tenant`` or files the refusal, so a dry run can compute the whole plan
    and commit none of it.
    """

    action: str  # "create" | "refuse_principal"
    tenant_id: str
    name: str
    tenant: Tenant | None = None  # the row to add, when action == "create"
    subject_id: str | None = None  # the colliding principal, when refused
    realm: str | None = None
    match_kind: str | None = None


def plan_new_tenant(
    session, tid: str, tid_uuid: UUID | None, mode: str, owner_key: str
) -> NewTenantPlan:
    """Decide whether a missing ``tenants`` row may be minted for ``tid``.

    ⛔ THE GUARD `ensure_tenant` APPLIES, APPLIED HERE TOO.

    THE DEFECT. This decision used to be inline in ``main()``: it built
    ``Tenant(id=…)`` straight from ``License.tenant_id`` and ``session.add``ed it,
    so it was a second door into ``tenants`` that never passed the shared
    primitive's refusal — and ``license.tenant_id`` is EXACTLY the column the known
    violators live in. That is the mechanism behind the firing
    ``LicenseTenantIdIsKeycloakPrincipal`` alert: a Keycloak principal id already
    sitting in ``license.tenant_id`` was read back out and MINTED as a tenant row,
    making the identity that AUTHENTICATES the thing that PAYS — which nothing
    downstream can detect (``billing_cost_events`` has no FK to ``tenants``).

    ⛔ WHY THE PREDICATE AND NOT ``db.ensure_tenant(...)`` ITSELF. ``ensure_tenant``
    opens its OWN session and COMMITS. Calling it from the backfill loop would write
    tenant rows during a DRY RUN — this script's default mode — because the commit
    would not be the one at the bottom of ``main()`` that ``--apply`` gates. A guard
    that silently converts the safe mode into the writing mode is not an
    improvement. So this imports the SAME ``subject_for_tenant_id`` predicate
    ``ensure_tenant`` imports, exactly as ``seed_platform_tenants`` does and for the
    same reason (models.py records that precedent: "Same predicate, different
    failure mode"). One definition of a violation, three callers, no copy of the
    join.

    ⚠️ REFUSE, NEVER RE-KEY. A hit is skipped and reported. Repairing an existing
    row is a data migration with its own blast radius and is explicitly out of
    scope — this script has never UPDATEd an id and still does not.

    ────────────────────────────────────────────────────────────────────────────
    ⛔ DO NOT DELETE THIS AS DEAD CODE — IT IS UNREACHABLE, DELIBERATELY, AND THE
    THING THAT MAKES IT UNREACHABLE IS NOT IN THIS FILE.

    ``main()`` only calls this when ``license.tenant_id`` has no matching
    ``tenants`` row. **Migration 016** (``016_add_license_tenant_fk.sql``) made
    ``license.tenant_id`` a UUID column with a validated FK to ``tenants.id``
    (``fk_license_tenant``, ``ON DELETE RESTRICT``, and ``nullable=False`` in
    ``models.License``), so on any post-016 database an orphan licence CANNOT
    exist and this branch CANNOT fire. It is belt-and-braces against the schema
    regressing, against a pre-016 database, and against anyone re-widening the
    column — and it costs one indexed lookup per genuinely-new tenant, which is
    zero of them today.

    ⚠️ Its tests therefore call this function DIRECTLY. They cannot construct an
    orphan licence to drive it end-to-end, because the FK refuses to store one —
    ``tests/unit/test_backfill_refuses_a_principal_id.py`` proves that refusal
    rather than asserting it. If you drop or weaken ``fk_license_tenant``, this
    branch becomes LIVE and those tests must go back to end-to-end.
    ────────────────────────────────────────────────────────────────────────────
    """
    name = _pick_name(tid)
    if tid_uuid is not None:
        hit = subject_for_tenant_id(session, tid_uuid)
        if hit is not None:
            subject_id, realm, match_kind = hit
            return NewTenantPlan(
                action="refuse_principal",
                tenant_id=tid,
                name=name,
                subject_id=subject_id,
                realm=realm,
                match_kind=match_kind,
            )
        return NewTenantPlan(
            action="create",
            tenant_id=tid,
            name=name,
            tenant=Tenant(id=tid_uuid, name=name, byo_mode=mode, owner_key=owner_key),
        )
    # Legacy non-UUID tenant_id: introspect resolves it by name, and the row is
    # born with a fresh random uuid7 rather than the caller's value. ⚠️
    # Deliberately UNGUARDED, and that is not an oversight: the predicate matches a
    # candidate UUID against `subject.keycloak_sub` / `subject.id`, and there is no
    # caller-supplied id here to collide with either. Same reasoning that leaves
    # `create_tenant` unguarded in models.py.
    return NewTenantPlan(
        action="create",
        tenant_id=tid,
        name=name,
        tenant=Tenant(name=name, byo_mode=mode, owner_key=owner_key),
    )


def report_refused_principals(plans: list[NewTenantPlan]) -> None:
    """Print the refusal block. Separate from the downgrade block, on purpose.

    ⛔ TWO REFUSAL CLASSES, TWO REMEDIES. A BYO-mode downgrade is fixed by editing
    the allowlists in this file; this one is fixed UPSTREAM, by pointing
    ``license.tenant_id`` at the organisation. Printing them under one heading
    would put the wrong remedy next to a money defect. It is printed ABOVE the
    downgrade block and above the summary because it must not be read past.
    """
    if not plans:
        return
    print(
        f"\n*** {len(plans)} TENANT(S) REFUSED: THE ID IS A KEYCLOAK "
        "PRINCIPAL, NOT AN ORGANISATION. Each id below was read out of "
        "license.tenant_id and would have been minted as a tenants row -- making "
        "the identity that AUTHENTICATES the thing that PAYS, which nothing "
        "downstream can detect (billing_cost_events has no FK to tenants). This "
        "is the mechanism behind the LicenseTenantIdIsKeycloakPrincipal alert. "
        "NOTHING WAS WRITTEN AND NOTHING WAS RE-KEYED. The fix is upstream: "
        "point license.tenant_id at the ORGANISATION's id, or attach the subject "
        "to a tenant via subject.tenant_id. Same predicate as models.ensure_tenant "
        "(services.tenant_subject_invariant.subject_for_tenant_id)."
    )
    print(f"{'MATCH':<14} {'REALM':<14} {'SUBJECT':<38} {'NAME':<30} TENANT_ID")
    for plan in plans:
        print(
            f"{plan.match_kind or '':<14} {plan.realm or '':<14} "
            f"{plan.subject_id or '':<38} {plan.name:<30} {plan.tenant_id}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allow-downgrade",
        action="store_true",
        help=(
            "Permit moves that REMOVE entitlement (local_allowed/platform_only -> byo_required). "
            "Refused by default: such a move is almost always this file having forgotten a "
            "tenant, not a decision. Without it those rows are reported and skipped."
        ),
    )
    parser.add_argument(
        "--only",
        action="append",
        metavar="TENANT_ID",
        help=(
            "Restrict the run to these tenant_id(s); repeatable. Lets an operator apply one "
            "known-good classification without taking responsibility for every other row in "
            "the same commit."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit changes. Without this flag the script is a dry-run.",
    )
    parser.add_argument(
        "--tenant-owner",
        required=True,
        help="Owning app (tenant_owner key, migration 050) any tenant this run CREATES is "
        "registered under. Required, no default.",
    )
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL is required", file=sys.stderr)
        return 2

    db = DatabaseManager(db_url)
    session = db.get_session()

    created: list[tuple[str, str, str]] = []
    updated: list[tuple[str, str, str, str]] = []
    unchanged: list[tuple[str, str]] = []
    refused: list[tuple[str, str, str, str]] = []
    # ⚠️ A SEPARATE LIST FROM `refused`, on purpose. That one is a BYO-mode
    # downgrade, whose remedy is to edit the allowlists in this file. This one is
    # a tenant that must never be created at all, whose remedy is to fix the
    # `license.tenant_id` upstream. Reporting them under one heading would put the
    # wrong remedy next to a money defect.
    refused_principal: list[NewTenantPlan] = []

    try:
        tenant_ids = [
            # str(): License.tenant_id is a UUID column since migration 016, but
            # classify()/_pick_name compare against sets of UUID *strings* — a raw
            # UUID would miss every entry and silently fall through to the
            # byo_required default (flipping platform_only/local_allowed tenants).
            str(row[0])
            for row in session.query(License.tenant_id)
            .filter(License.status == "active")
            .distinct()
            .all()
        ]
        print(f"Found {len(tenant_ids)} distinct active-license tenant_id(s).")
        if args.only:
            wanted = set(args.only)
            missing = wanted - set(tenant_ids)
            if missing:
                # Silently matching nothing would print a clean "0 changes" and read as success.
                print(
                    f"ERROR: --only named unknown tenant_id(s): {sorted(missing)}", file=sys.stderr
                )
                return 2
            tenant_ids = [t for t in tenant_ids if t in wanted]
            print(f"--only: restricted to {len(tenant_ids)} tenant_id(s).")
        print()

        for tid in sorted(tenant_ids):
            mode = classify(tid)
            tid_uuid = _as_uuid(tid)

            existing = None
            if tid_uuid is not None:
                existing = session.query(Tenant).filter(Tenant.id == tid_uuid).first()
            if existing is None:
                existing = session.query(Tenant).filter(Tenant.name == tid).first()

            if existing is not None:
                current = str(existing.byo_mode)
                if current != mode:
                    if is_downgrade(current, mode) and not args.allow_downgrade:
                        refused.append((tid, str(existing.name), current, mode))
                    else:
                        setattr(existing, "byo_mode", mode)
                        updated.append((tid, str(existing.name), current, mode))
                else:
                    unchanged.append((tid, mode))
                continue

            # ⛔ THE GUARD `ensure_tenant` APPLIES, APPLIED HERE TOO — and it lives
            # in `plan_new_tenant` rather than inline, because migration 016's FK
            # makes this whole branch unreachable through the database and a
            # directly-callable decision is the only way its tests can reach the
            # refusal. Read that function's docstring before touching either.
            #
            # ⚠️ A REFUSAL IS A `continue`, NEVER AN ABORT: one bad id must not stop
            # the other tenants being backfilled, or the guard converts a single
            # upstream defect into a total outage of this script and an operator's
            # only way out is to remove the guard.
            plan = plan_new_tenant(session, tid, tid_uuid, mode, args.tenant_owner)
            if plan.action == "refuse_principal":
                refused_principal.append(plan)
                continue
            assert plan.tenant is not None  # action == "create" always carries one
            session.add(plan.tenant)
            created.append((tid, plan.name, mode))

        # Report.
        def _count(mode: str) -> int:
            return (
                sum(1 for _, _, m in created if m == mode)
                + sum(1 for _, _, _, m in updated if m == mode)
                + sum(1 for _, m in unchanged if m == mode)
            )

        print(f"{'ACTION':<10} {'BYO_MODE':<14} {'NAME':<40} TENANT_ID")
        for tid, name, mode in created:
            print(f"{'CREATE':<10} {mode:<14} {name:<40} {tid}")
        for tid, name, old, mode in updated:
            print(f"{'UPDATE':<10} {old + '->' + mode:<14} {name:<40} {tid}")
        for tid, name, old, mode in refused:
            print(f"{'REFUSED':<10} {old + '->' + mode:<14} {name:<40} {tid}")
        for tid, mode in unchanged:
            print(f"{'(same)':<10} {mode:<14} {'':<40} {tid}")

        # ⛔ ABOVE the downgrade block and above the summary: this is the money
        # defect, and it must not be read past. Exit code stays 0, matching the
        # downgrade refusal directly below -- a refusal is this script working, not
        # failing, and flipping the code would break any caller that treats non-zero
        # as "the backfill did not run". The loudness is the signal.
        report_refused_principals(refused_principal)

        if refused:
            # Loud, and above the summary line: a skipped downgrade means this file is missing a
            # tenant, and the fix is to LIST it here -- not to re-run with --allow-downgrade.
            print(
                f"\n*** {len(refused)} DOWNGRADE(S) REFUSED. Each is a tenant whose live row is "
                "more permissive than this file computes -- i.e. someone fixed it out of band and "
                "the allowlists above never caught up. Add it to LOCAL_ALLOWED/PLATFORM_ONLY_KEEP, "
                "or pass --allow-downgrade if the removal is genuinely intended."
            )
        print(
            f"\nSummary: create={len(created)} update={len(updated)} "
            f"unchanged={len(unchanged)} refused={len(refused)} "
            f"refused_principal={len(refused_principal)} | "
            f"local_allowed={_count('local_allowed')} "
            f"byo_required={_count('byo_required')} "
            f"platform_only={_count('platform_only')}"
        )

        if args.apply:
            session.commit()
            print("\nAPPLIED (committed).")
        else:
            session.rollback()
            print("\nDRY-RUN (no changes committed). Re-run with --apply to commit.")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
