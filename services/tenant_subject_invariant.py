"""A tenant id must never be a KEYCLOAK PRINCIPAL id — in either of its forms.

The invariant, stated once: **no `tenants.id` may equal any
`subject.keycloak_sub` (a Keycloak principal's `sub` claim) NOR any
`subject.id` (this registry's own primary key for that principal).**

⚠️ "PRINCIPAL", NOT "PERSON", AND THE DISTINCTION IS LOAD-BEARING. Of the four
known offending ids, only `portal-render-e2e`'s subject is a human;
`book-fulfillment` (`service-account-book-fulfillment-svc`) and `nonagentic-gtm`
(`service-account-control-service-account`) are SERVICE ACCOUNTS in realm
`secure-apps` with no email. The defect is not "a human got billed as an org" —
it is "an identity that authenticates was registered as the thing that PAYS".
That is equally wrong for a machine, and writing "person" into the help text
would make three of the four rows look like false positives.

⚠️ STATE AS OF 2026-10-06 (owner ruling). The measurements below are dated and
are kept as the record of what was found; two of the rows have since been ruled
on, and BOTH were re-keyed — neither tenant shares a principal's id any more:
  * `1d0090ac-…` `book-fulfillment` was RE-KEYED to
    `01a1109c-f895-7766-ba28-6ea56e56a5f7` by migration 045. The principal keeps
    its `sub`; no tenant shares it.
  * `e148e8af-…` `portal-render-e2e` was RE-KEYED to
    `01a1113b-674f-72b0-a84b-dc746d39afca` by migration 046. Earlier the same day
    it had been made a RECORDED EXCEPTION (`class="retired-principal"`); that
    ruling was superseded and the id REMOVED from `RETIRED_PRINCIPAL_TENANT_IDS`,
    so `e148e8af-…` turning up on a tenant row again is a plain
    `class="violation"`. Subject `8382f0b3-…` keeps it as `keycloak_sub`.

TWO MATCH CLASSES, counted under a `match` label rather than summed, because
they have different causes and different fixes:

  * `match="keycloak_sub"` — a Keycloak `sub` registered as a tenant. 3 rows on
    prod (2026-09-10): `e148e8af-…` `portal-render-e2e` (`kind='internal'`),
    `1d0090ac-adbf-4c07-b704-bc66061b572e` `book-fulfillment`
    (⚠️ `kind='customer'` — a CUSTOMER-FACING row, not an internal one), and the
    `00000000-…0000` `default-anon` sentinel. Fix: register the ORG id instead.
  * `match="subject_id"` — this registry's own `subject.id` registered as a
    tenant. 0 such rows on prod today. Fix: the id was never an org at all.

────────────────────────────────────────────────────────────────────────────────
⛔ THE SOURCE LABEL, AND THE BLIND SPOT IT MAKES MACHINE-READABLE
────────────────────────────────────────────────────────────────────────────────
A SINGLE-SOURCE DETECTOR IS BLIND BY CONSTRUCTION, and this one is single-source.
There are TWO doors an identity can come through, they overlap only partially,
and the union measured 2026-09-10 is FOUR rows — neither door sees all four:

    id            name               kind      in subject.keycloak_sub  live in KC
    e148e8af-…    portal-render-e2e  internal  YES                      NO (deleted
                                                                        2026-08-27)
    e4f70a83-…    nonagentic-gtm     internal  NO  (in_subject=0)       YES (SA)
    1d0090ac-…    book-fulfillment   customer  YES                      YES (SA)
    00000000-…    default-anon       internal  YES                      n/a sentinel

So a licences-DB-only predicate MISSES `nonagentic-gtm` — it was never written to
`subject` — and a live-Keycloak-only predicate misses `portal-render-e2e`, the
one actually spending money, because a deleted principal cannot be joined to.

⛔ THIS MODULE IMPLEMENTS ONLY THE `source="licences_subject"` DOOR. It is blind
to any tenant whose Keycloak principal was never recorded as a `subject` row,
and `e4f70a83-81ee-429a-abca-6999e3a0d24f` / `nonagentic-gtm` is the KNOWN,
MEASURED example of exactly that. Why the second door is not implemented here,
measured rather than assumed:

  * This service holds NO Keycloak admin capability. `auth/config.py` carries
    only the OUTBOUND service-account client-credentials grant, and its own
    docstring records that `client_id`/`client_secret` were REMOVED in 2026-08
    because the sole consumer — `auth/validator.py` — was deleted in PR #140.
    Nothing here authenticates as a Keycloak client, and there is no
    `realm-management` / `view-users` grant to enumerate principals with.
  * Adding one would put a new admin secret, and a network round-trip to two
    realms, on `/metrics` and on `_run_db_bootstrap` — the boot path of the
    service that backs ext_authz for the whole platform at `replicas: 2`.

A DOCUMENTED blind spot is acceptable; an UNDOCUMENTED one is what produced this
defect. So the gap is not left in prose alone:
`nonagentic_license_tenant_is_subject_source_measured{source=…}` reports 1 for
the door that ran and **0 for `keycloak_principal`**, and the violation counts
carry a `source` label so a second door can be added without renaming anything.

⛔ THE KEYCLOAK SOURCE IS NOT SEEDED AT ZERO IN THE VIOLATION GAUGE. A seeded 0
is right for "measured, found none" and WRONG for "never looked" — and here the
truth is known to be non-zero (`nonagentic-gtm`). A zero there would be a
fabricated measurement; the `…_source_measured` gauge says "not looked" instead.

────────────────────────────────────────────────────────────────────────────────
⛔ READ THIS BEFORE TRUSTING A ZERO ON `match="subject_id"`. The live instance of
that class — `8382f0b3-c082-4f97-8d47-120ce5a18e05`, a `subject.id` carrying 61
`token_budgets` rows in the BILLING database — is NOT IN `tenants` AT ALL
(measured: `as_subject_id=1, as_kc_sub=0, as_tenant=0`), and its own
`subject.tenant_id` is NULL, so there is no owning tenant to repair it to. This
module therefore CANNOT see it, and no predicate rooted in `tenants` can: the row
it would have to match does not exist. `token_budgets` lives in the billing
service's database (`models.py:1587` calls it `billing.token_budgets`), which
this service has no connection to, so that detector has to be written where the
data is. What is measurable HERE is the moment such an id is ALSO given a
registry row — the step that would make it billable by this service too.

WHY THIS EXISTS, MEASURED 2026-09-10 on prod (the primary, `licenses`).

`e148e8af-3751-49bf-a0fa-56ab46d31ddc` is a row in `licenses.tenants` named
`portal-render-e2e` (`kind='internal'`, `cost_centre='ci'`, created
2026-07-20 21:08Z) — AND it is the `keycloak_sub` of a `licenses.subject` row
(`subject.id = 8382f0b3-…`, realm `portal`). So one identity is simultaneously
the billable ORG and an authenticating principal within it. It drew 1,399
billing rows / $86.02 in the last 24h. The Keycloak user was deleted 2026-08-27
and the billing did not stop, because it is sustained by the registry row plus 5
licences, not by anything in Keycloak.

⚠️ NOTHING DOWNSTREAM CAN CATCH THIS, WHICH IS WHY IT LIVES HERE. `billing_cost_events`
has no FK to any tenant table; both ids are bare `uuid` on the wire; and UUID
VERSION IS NOT A DISCRIMINATOR — the genuine tenants `george-dekker`, `merqi`
and `nonagentic-gtm` are v4, exactly like a Keycloak sub. The ONLY authoritative
test is the cross-table predicate below, and until this module nothing ran it.

────────────────────────────────────────────────────────────────────────────────
WHY THIS IS NOT A CHECK CONSTRAINT, AND NOT A TRIGGER EITHER
────────────────────────────────────────────────────────────────────────────────
It is a CROSS-TABLE predicate (`tenants` × `subject`), so a `CHECK` cannot
express it — PostgreSQL requires a CHECK to be a function of the row alone.
The remaining DB-level option is a trigger, and it was REJECTED on a measured
fact rather than on taste:

  * `models.PLATFORM_TENANTS` contains BOTH violating platform ids
    (`e148e8af-…` and the `00000000-…0000` sentinel), and
    `DatabaseManager.seed_platform_tenants()` re-INSERTs every one of them on
    EVERY boot from `_run_db_bootstrap`, relying on `ON CONFLICT DO NOTHING`.
  * A `BEFORE INSERT` row trigger fires BEFORE ON CONFLICT arbitration. A
    trigger that raised on those ids would therefore raise on every boot of a
    service that backs ext_authz for the whole platform at `replicas: 2` —
    turning a data-hygiene guard into a fleet-wide auth outage.

A trigger could be written to skip an id that already exists, but that is a
subtle exemption living in a mechanism this repo uses NOWHERE else (there is not
one `CREATE TRIGGER` in `migrations/`, `init.sql` or `models.py`), which
`create_all` would not reproduce on a fresh database. So this follows the
pattern the repo DOES use for an invariant a constraint cannot hold — migrations
031/033: **fix the generator, guard the shared primitive, and measure the
residue** — namely

  1. the write-time guard in `DatabaseManager.ensure_tenant` (raises
     `ClientSafeError`, the same shape as the `kind='service'` coupling guard),
  2. the apply-time report in `migrations/035_tenant_id_must_not_be_a_subject.sql`,
  3. the boot-time report `report_at_boot()` below, and
  4. the Prometheus gauge below, which is the only one of the four that keeps
     measuring after the change ships.

⛔ NONE OF THE FOUR IS FATAL ON AN EXISTING ROW. Three rows violate today; a
guard that refused to serve on them would CrashLoop the platform auth path over
a billing-hygiene defect. Existing violations are REPORTED. New ones are
REFUSED at the primitive.

⭐ "AT THE PRIMITIVE" MEANT ONE PRIMITIVE OF TWO UNTIL 2026-09-23. All four
mechanisms above ask "is this NEW TENANT id already a principal?". The collision
can equally be made from the other side — register a NEW SUBJECT whose
`keycloak_sub` is an id that is already a `tenants.id` — and that door had no
guard, no log line and no counter, while being reachable at a LOWER privilege
than the guarded one (`license-manager` via a caller-supplied path parameter on
POST /licenses/user/{subject_id}, versus `license-admin` for PUT /tenants/{id}).
`tenant_for_subject_sub` below is the missing half; `models.create_or_get_subject`
applies it, and `nonagentic_license_subject_principal_refusals` counts it. See
the block above `_TENANT_FOR_SUB` for the measurement that makes this the
door that matters: 82 of 85 tenants are one call away from it, and one of the 82
is `nonagentic-gtm` — the known fourth offender this module is blind to.

────────────────────────────────────────────────────────────────────────────────
THE SENTINEL IS A SEPARATE SERIES. THIS IS THE LOAD-BEARING METRIC DECISION.
────────────────────────────────────────────────────────────────────────────────
Of the three violating rows, `00000000-0000-0000-0000-000000000000`
(`default-anon`) is DELIBERATE and PERMANENT: it is the anonymous-S2S slot,
declared in `models.PLATFORM_TENANTS`, seeded on every boot, and it is a
subject as well as a tenant on purpose.

If it shared a series with the real violations the gauge could never reach 0,
every alert written on it would fire forever, and it would be silenced within a
week — at which point the instrument is worse than none, because the dashboard
still looks instrumented. So the count is split by a `class` label
(`"violation"` / `"sentinel"`) and BOTH label values are seeded at zero, which
is what makes `…{class="violation"} == 0` a provable statement rather than an
absent series.

⛔ THE PREDICATE IS NOT LOOSENED TO ACHIEVE THAT. `default-anon` still MATCHES —
it is still counted, still named in its own series, still visible. Exempting it
from the predicate would hide the day someone adds a fourth "deliberate"
exception; splitting the metric hides nothing.

────────────────────────────────────────────────────────────────────────────────
THE CAST IS MANDATORY, AND CASE FOLDING IS NOT OPTIONAL EITHER.
`subject.keycloak_sub` is a STRING (`TEXT` in `init.sql:28` / prod;
`String(255)` in `models.py:751` — those disagree, and that divergence is noted
but not fixed here) while `tenants.id` is `uuid`. An uncast join does not return
the wrong answer, it does not compile:

    ERROR: operator does not exist: text = uuid

So `t.id::text` is required. And because the column is free text that merely
happens to hold a UUID today, a bare `t.id::text = s.keycloak_sub` would miss a
sub written in upper case, while PostgreSQL renders `uuid::text` lower-case.
Both sides are `lower()`ed so the predicate cannot be defeated by the one
transformation the column's own type permits. `subject.id` IS a `uuid`, so that
half needs no cast for correctness — it is cast anyway, to keep one comparison
shape across both classes.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from prometheus_client.core import GaugeMetricFamily
from sqlalchemy import text

logger = logging.getLogger(__name__)

# ── The exemption register ────────────────────────────────────────────────────
# ⚠️ This is NOT a filter on the predicate. Every id here is still MATCHED,
# still COUNTED and still emitted as its own labelled series — it is only routed
# to `class="sentinel"` so the actionable series can reach zero. Adding an id
# here is a permanent statement that "this identity is deliberately both an org
# and a subject", so it costs a code review; a real violation cannot be quieted
# by an operator with database access.
SENTINEL_TENANT_IDS: frozenset[str] = frozenset(
    {
        # `default-anon` — the anonymous-S2S slot, models.PLATFORM_TENANTS.
        "00000000-0000-0000-0000-000000000000",
    }
)

# ── The retired-principal register ────────────────────────────────────────────
# ⚠️ Like the sentinel register this is NOT a filter on the predicate: an id here
# is still MATCHED, still COUNTED and still named in its own series. It is routed
# to `class="retired-principal"` — a RECORDED EXCEPTION — instead of
# `class="violation"`, so `LicenseTenantIdIsKeycloakPrincipal` can reach zero
# without the row being hidden.
#
# It is deliberately NOT merged into SENTINEL_TENANT_IDS. A sentinel is an
# identity that is an org AND a subject ON PURPOSE, for ever. A retired principal
# is a DEFECT THAT WAS RULED ON: the tenant id was a Keycloak principal's `sub`,
# that principal no longer exists, and re-keying the tenant was rejected as more
# dangerous than the collision. Two different statements, two different series.
#
# ⛔ KEEP IT NARROW. tenant id -> the reason it may stay. An id belongs here only
# when BOTH hold: (1) the colliding Keycloak principal is GONE, so nothing can
# authenticate as the thing that pays; (2) the owner ruled the re-key out. Any
# other colliding id is still `class="violation"`. The exception also covers the
# `match="keycloak_sub"` class ONLY (see `classify`): the same tenant id turning
# up as a `subject.id` is a different defect that no ruling covered.
#
# ⚠️ THE REGISTER IS EMPTY, AND THAT IS THE INTENDED STATE. Its only entry ever,
# `e148e8af-…` `portal-render-e2e`, was removed the day it was added: the owner
# reversed the "re-key rejected" ruling and migration
# 046_rekey_portal_render_e2e_tenant.sql REPAIRED the row instead, exactly as
# 045_rekey_book_fulfillment_tenant.sql had repaired `book-fulfillment`. The
# mechanism (class, SQL CASE, seeded zero series) is kept because it is generic
# and tested against a synthetic id; with nothing registered every
# `retired-principal` series reads 0 and every colliding id is a `violation`.
RETIRED_PRINCIPAL_TENANT_IDS: dict[str, str] = {}

CLASS_VIOLATION = "violation"
CLASS_SENTINEL = "sentinel"
CLASS_RETIRED_PRINCIPAL = "retired-principal"
# Every class, in the order the series are seeded and emitted. ONLY
# CLASS_VIOLATION is actionable.
CLASSES: tuple[str, ...] = (CLASS_SENTINEL, CLASS_RETIRED_PRINCIPAL, CLASS_VIOLATION)

# The two ways a tenant id can be a Keycloak principal id. Split rather than
# summed (not one number): they have
# different causes and different remediations (see the module docstring), and one
# number would hide which of the two moved.
MATCH_KEYCLOAK_SUB = "keycloak_sub"
MATCH_SUBJECT_ID = "subject_id"
MATCH_KINDS = (MATCH_KEYCLOAK_SUB, MATCH_SUBJECT_ID)

# The two doors an identity can arrive through. Only the first is implemented
# here — see the module docstring for the measured reason and for the four-row
# union that shows neither door alone is sufficient.
SOURCE_LICENCES_SUBJECT = "licences_subject"
SOURCE_KEYCLOAK_PRINCIPAL = "keycloak_principal"
SOURCES_MEASURED: tuple[str, ...] = (SOURCE_LICENCES_SUBJECT,)
SOURCES_UNMEASURED: tuple[str, ...] = (SOURCE_KEYCLOAK_PRINCIPAL,)

# ⚠️ NOT A METRIC, AND DELIBERATELY NOT ONE. These are ids this check is KNOWN to
# be blind to, transcribed from a measurement rather than derived from a query
# this service can run. Emitting them as a gauge would dress a frozen 2026-09-10
# reading up as a live one; they are surfaced in the boot log instead, where a
# reader can see the date attached to the claim. Remove an entry only when the
# `keycloak_principal` source is genuinely implemented, or when the row is fixed
# AND re-measured.
KNOWN_KEYCLOAK_ONLY_BLIND_SPOTS: tuple[tuple[str, str], ...] = (
    (
        "e4f70a83-81ee-429a-abca-6999e3a0d24f",
        "nonagentic-gtm (service-account-control-service-account, realm secure-apps) "
        "— a LIVE Keycloak principal registered as a tenant that was never written "
        "to `subject`, so the licences-DB predicate below cannot see it. Measured "
        "2026-09-10.",
    ),
)

# Cap on per-row identity series. `tenants` is 76 rows, so the join cannot
# exceed 76 today and 500 leaves an order of magnitude. The COUNTS are computed
# by a separate unbounded aggregate (`_COUNT_BY_CLASS`), so a truncated identity
# list can never understate the number the alert reads — the failure mode
# `nonagentic_license_expiry_series_truncated` exists for in the sibling module.
MAX_ROW_SERIES = 500

# Cached so scrape frequency does not drive database load: the engine uses
# NullPool, so an uncached collector opens a fresh connection per scrape per
# replica. 60s against a 15s scrape is irrelevant for a fact that changes when a
# an operator registers a tenant.
SNAPSHOT_TTL_SECONDS = 60.0


def classify(tenant_id: str, match_kind: str) -> str:
    """Which series a matching row belongs to. `"violation"` unless registered.

    `"sentinel"` for a deliberately-exempt id (either match class);
    `"retired-principal"` for an id in RETIRED_PRINCIPAL_TENANT_IDS matched via
    `keycloak_sub` ONLY — the recorded reason is "the Keycloak principal is
    gone", which says nothing about a `subject.id` collision. Everything else,
    including any NEW colliding id, is `"violation"`.

    ⚠️ `_COUNT_BY_CLASS` states this same rule in SQL (the counts must stay exact
    when the identity list is truncated). Change one, change the other;
    `TestTheRetiredPrincipalIsARecordedException` compares the two.
    """
    tid = tenant_id.strip().lower()
    if tid in SENTINEL_TENANT_IDS:
        return CLASS_SENTINEL
    if match_kind == MATCH_KEYCLOAK_SUB and tid in RETIRED_PRINCIPAL_TENANT_IDS:
        return CLASS_RETIRED_PRINCIPAL
    return CLASS_VIOLATION


@dataclass
class TenantSubjectRow:
    tenant_id: str
    tenant_name: str
    subject_id: str
    keycloak_realm: str
    match_kind: str

    @property
    def series_class(self) -> str:
        return classify(self.tenant_id, self.match_kind)


@dataclass
class TenantSubjectSnapshot:
    rows: list[TenantSubjectRow] = field(default_factory=list)
    # (class, match_kind) -> count, over ALL matching rows (never truncated).
    counts: dict[tuple[str, str], int] = field(default_factory=dict)
    truncated: int = 0
    collector_up: bool = True


# ── SQL ───────────────────────────────────────────────────────────────────────
# ⛔ SELECT-only. This module must never write to the licences database.
#
# One UNION ALL over the two classes rather than two joins, so a tenant id that
# is BOTH a sub and a subject.id yields two rows and is counted under both — a
# single join with an OR would collapse them and silently under-report.

_MATCHING_ROWS = text(
    """
    SELECT tenant_id, tenant_name, subject_id, keycloak_realm, match_kind FROM (
        SELECT t.id::text       AS tenant_id,
               t.name           AS tenant_name,
               s.id::text       AS subject_id,
               s.keycloak_realm AS keycloak_realm,
               'keycloak_sub'   AS match_kind
          FROM tenants t
          JOIN subject s ON lower(s.keycloak_sub) = lower(t.id::text)
        UNION ALL
        SELECT t.id::text       AS tenant_id,
               t.name           AS tenant_name,
               s.id::text       AS subject_id,
               s.keycloak_realm AS keycloak_realm,
               'subject_id'     AS match_kind
          FROM tenants t
          JOIN subject s ON lower(s.id::text) = lower(t.id::text)
    ) m
     ORDER BY match_kind, tenant_name
     LIMIT :cap
    """
)

# Unbounded on purpose — see MAX_ROW_SERIES. `:sentinels` / `:retired` are text[]
# of the lower-cased registered ids; the split happens in SQL so the number the
# alert reads is exact even if the identity list above is truncated. The CASE is
# `classify()` restated — sentinel first, then retired-principal for the
# `keycloak_sub` match ONLY, else violation.
_COUNT_BY_CLASS = text(
    """
    SELECT match_kind,
           CASE
             WHEN lower(tenant_id) = ANY(:sentinels) THEN 'sentinel'
             WHEN match_kind = 'keycloak_sub'
                  AND lower(tenant_id) = ANY(:retired) THEN 'retired-principal'
             ELSE 'violation'
           END      AS series_class,
           count(*) AS n
      FROM (
        SELECT t.id::text AS tenant_id, 'keycloak_sub' AS match_kind
          FROM tenants t
          JOIN subject s ON lower(s.keycloak_sub) = lower(t.id::text)
        UNION ALL
        SELECT t.id::text AS tenant_id, 'subject_id' AS match_kind
          FROM tenants t
          JOIN subject s ON lower(s.id::text) = lower(t.id::text)
      ) m
     GROUP BY 1, 2
    """
)

# The write-time predicate. Keyed on `subject`, because the question at a tenant
# INSERT is "is this id already a principal?" — the tenant row does not exist yet.
# Both classes, `keycloak_sub` first so the more common one names the refusal.
_SUBJECT_FOR_ID = text(
    """
    SELECT subject_id, keycloak_realm, match_kind FROM (
        SELECT s.id::text       AS subject_id,
               s.keycloak_realm AS keycloak_realm,
               'keycloak_sub'   AS match_kind
          FROM subject s
         WHERE lower(s.keycloak_sub) = lower(:candidate)
        UNION ALL
        SELECT s.id::text       AS subject_id,
               s.keycloak_realm AS keycloak_realm,
               'subject_id'     AS match_kind
          FROM subject s
         WHERE lower(s.id::text) = lower(:candidate)
    ) hit
     ORDER BY match_kind
     LIMIT 1
    """
)

# ── THE SECOND DOOR: the SAME invariant, approached from the subject side ─────
#
# ⛔ READ THIS BEFORE CONCLUDING THE INVARIANT IS GUARDED. Everything above and
# every guard that imports `subject_for_tenant_id` asks ONE question: "is this
# NEW TENANT id already a principal?" That is one of TWO ways the collision can
# be created, and until this predicate existed the other way was completely
# unguarded — no refusal, no log line, no counter.
#
# The other way is `models.DatabaseManager.create_or_get_subject`: register a
# NEW SUBJECT whose `keycloak_sub` is an id that is ALREADY a `tenants.id`. The
# resulting row pair is INDISTINGUISHABLE from the ones the gauge is firing on
# — same two tables, same join, same defect — but no tenant was inserted, so
# nothing on the tenant side could possibly have fired.
#
# ⚠️ THE UNGUARDED DOOR IS THE LESS PRIVILEGED ONE, WHICH IS THE WRONG WAY ROUND.
# `ensure_tenant` is reached from `PUT /tenants/{id}`, which requires
# `license-admin`. `create_or_get_subject` is reached from
# `POST /licenses/user/{subject_id}` — where the sub is a caller-supplied PATH
# PARAMETER — under `license-admin` OR `license-manager`, and from five
# `scripts/provision_*` entry points via an operator-supplied `--owner-sub` /
# `--subject`. So the door with the guard needed the higher role.
#
# ⭐ MEASURED ON PROD 2026-09-23, and this is why it is not theoretical:
#   * 85 tenants; 82 of them have NO subject row carrying their id as a
#     `keycloak_sub`. Each of those 82 is ONE `create_or_get_subject` call away
#     from becoming a new `class="violation"` row.
#   * Among the 82 is `e4f70a83-81ee-429a-abca-6999e3a0d24f` `nonagentic-gtm` —
#     the KNOWN fourth offender, a LIVE `secure-apps` service-account principal
#     already registered as a tenant (5 licences, 3 tokens) that the licences-DB
#     detector cannot see BECAUSE it was never written to `subject`. Measured:
#     0 subject rows match it.
#
# ⛔ SO THE GAUGE'S BLIND SPOT AND THIS UNGUARDED DOOR ARE THE SAME HOLE SEEN
# FROM TWO SIDES. The single event that would make `nonagentic-gtm` VISIBLE to
# the gauge — minting a licence for that principal, which writes the `subject`
# row — is exactly the event nothing was refusing. The detector would have
# reported the violation the moment it became unfixable-in-silence, having been
# unable to prevent it. This predicate is what makes that event a REFUSAL.
#
# Deliberately NOT symmetric in one respect: it matches only against
# `tenants.id`, because that is the only id a `subject` write can collide with.
# There is no "subject.id is a tenant id" class here — `subject.id` is minted by
# the mapper (uuid7), never caller-supplied, so there is nothing to guard.
_TENANT_FOR_SUB = text(
    """
    SELECT t.id::text AS tenant_id,
           t.name     AS tenant_name
      FROM tenants t
     WHERE lower(t.id::text) = lower(:candidate)
     LIMIT 1
    """
)


# ── The boot-seed refusal register ────────────────────────────────────────────
# ⛔ WHY A PROCESS-LIFETIME COUNTER AND NOT A CRASH. `models.seed_platform_tenants`
# runs inside `_run_db_bootstrap`, where anything that is not a connect fault
# propagates and the pod REFUSES TO SERVE — and this service backs ext_authz for
# the whole platform. A seeded id that would now fail the principal guard is a
# condition that has been true for weeks (`e148e8af-…` is both a tenant and a
# `subject.keycloak_sub` on prod), so raising here would CrashLoop both replicas
# of the auth path over nothing new. It is refused, logged LOUDLY, and counted.
#
# ⚠️ NOT the same number as `nonagentic_license_tenant_is_subject_rows`. That gauge
# counts rows that EXIST and violate; this counts seed INSERTs this process
# declined to perform. On prod both refusals are for ids whose rows already exist,
# so the insert was a no-op anyway and this number is a statement about the SEED
# LIST, not about the database. A nonzero value means PLATFORM_TENANTS still names
# a principal id and should be corrected at the source.
_seed_refusals: dict[tuple[str, str], str] = {}
_seed_refusals_lock = threading.Lock()


def record_seed_refusal(tenant_id: str, name: str, match_kind: str) -> None:
    """Register that the boot seed declined to INSERT `tenant_id`. Never raises.

    Idempotent per (tenant_id, name): the seed runs on every boot and, in tests,
    more than once per process, and a refusal is a FACT about the seed list rather
    than an event to be tallied. Counting occurrences would make the gauge a
    function of boot count.
    """
    with _seed_refusals_lock:
        _seed_refusals[(str(tenant_id), str(name))] = str(match_kind)


def seed_refusals() -> dict[tuple[str, str], str]:
    """A copy of the refusal register — `(tenant_id, name) -> match_kind`."""
    with _seed_refusals_lock:
        return dict(_seed_refusals)


def reset_seed_refusals() -> None:
    """Clear the register. For tests only — the register is process-lifetime."""
    with _seed_refusals_lock:
        _seed_refusals.clear()


def subject_for_tenant_id(session, tenant_id) -> tuple[str, str, str] | None:
    """`(subject_id, realm, match_kind)` if `tenant_id` is already a principal.

    The single source of the write-time predicate — `models.ensure_tenant`
    imports THIS rather than restating the join, so the guard and the gauge can
    never drift into disagreeing about what a violation is.
    """
    row = session.execute(_SUBJECT_FOR_ID, {"candidate": str(tenant_id)}).mappings().first()
    if row is None:
        return None
    return str(row["subject_id"]), str(row["keycloak_realm"]), str(row["match_kind"])


def tenant_for_subject_sub(session, keycloak_sub) -> tuple[str, str] | None:
    """`(tenant_id, tenant_name)` if `keycloak_sub` is already a registered tenant.

    The mirror of `subject_for_tenant_id`, and the write-time predicate for the
    SECOND door — `models.create_or_get_subject` imports THIS rather than
    restating the lookup, exactly as `ensure_tenant`, `seed_platform_tenants` and
    `scripts/backfill_tenants_byo_mode` import the first one. One definition per
    direction, no copy of the comparison.

    ⚠️ A NON-UUID `keycloak_sub` IS NOT AN ERROR HERE, and that is load-bearing.
    `subject.keycloak_sub` is free `TEXT` and most real subs are not UUIDs at
    all; the comparison is `lower(t.id::text) = lower(:candidate)`, text to text,
    so a sub like `service-account-foo` simply matches nothing. Comparing against
    `tenants.id` UNCAST would not merely mis-answer, it would not compile
    (`operator does not exist: text = uuid`) — the same trap the module docstring
    records for the other direction.
    """
    row = session.execute(_TENANT_FOR_SUB, {"candidate": str(keycloak_sub)}).mappings().first()
    if row is None:
        return None
    return str(row["tenant_id"]), str(row["tenant_name"])


# ── Is this colliding id an identity that is ALREADY TRADING? ─────────────────
# ⛔ THIS IS THE NARROWING PREDICATE, AND IT EXISTS BECAUSE THE UNNARROWED GUARD
# BROKE A LIVE MINT PATH. The first version of the door-2 guard refused every
# collision it saw. CI refuted it in one line
# (`TestCreateUserLicenseMintGuard::test_guard_does_not_fire_for_real_self_tenant`
# → `assert 409 == 201`), and prod would have refuted it harder: measured
# 2026-09-23, `e4f70a83-…` `nonagentic-gtm` holds 5 LIVE LICENCES and has ZERO
# `subject` rows, so its very next mint — and the first mint of every identity in
# that shape after a DR rebuild, where a `subject` row comes back only if it
# existed — would have been refused with a 409.
#
# ⚠️ THE ROW THE GUARD CAN SEE IS NOT THE ROW THAT PROVES PRE-EXISTENCE. The
# idempotent early return in `create_or_get_subject` spares only violators that
# happen to have been WRITTEN to `subject` already; `nonagentic-gtm` never was.
# LICENCES are the durable evidence instead: they are restored with the database,
# they are what the identity is actually DOING, and an id carrying them is one
# this registry has already been trading with for months.
#
# ⛔ NOT AN ALLOWLIST, DELIBERATELY. A hardcoded set of the three known ids would
# fail OPEN for a fourth and would rot at the next DR rebuild; this predicate is
# derived from live data on every call, so an id that never traded is never
# exempt.
_TENANT_HAS_LICENCES = text(
    """
    SELECT 1
      FROM license l
     WHERE l.tenant_id = CAST(:tenant_id AS uuid)
     LIMIT 1
    """
)


def tenant_has_licences(session, tenant_id) -> bool:
    """True if any `license` row is held under `tenant_id` — ANY status.

    ⚠️ ANY status, not just `active`, and that is deliberate. The question this
    answers is "has this id ever traded as an organisation", not "is it trading
    right now": an identity whose licences are all currently revoked is still a
    pre-existing identity whose next mint must not be refused by a hygiene guard.
    Keying on `active` would make the exemption blink in and out with ordinary
    licence lifecycle, turning a hygiene guard into an intermittent outage.

    ⚠️ `license.tenant_id` is a `uuid` column while the caller holds text, so the
    parameter is CAST rather than compared raw — the same trap `_TENANT_FOR_SUB`
    records for the other direction (`operator does not exist: text = uuid` does
    not mis-answer, it fails to compile). The caller only ever passes an id that
    `tenant_for_subject_sub` has already matched against `tenants.id`, so it is a
    well-formed uuid by construction.
    """
    return session.execute(_TENANT_HAS_LICENCES, {"tenant_id": str(tenant_id)}).first() is not None


# ── The subject-side refusal register ─────────────────────────────────────────
# ⛔ A DIFFERENT NUMBER FROM BOTH OF THE OTHERS, AND THE DISTINCTION IS THE POINT.
#   * `…_tenant_is_subject_rows`        — violating rows that EXIST. Data.
#   * `…_tenant_seed_principal_refusals`— PLATFORM_TENANTS entries the boot seed
#                                         declined. A statement about a CONSTANT,
#                                         and on prod both its entries refer to
#                                         rows that already exist, so it refuses
#                                         an INSERT that was already a no-op.
#   * `…_subject_principal_refusals`    — THIS. Subject registrations this process
#                                         declined because the `keycloak_sub` is
#                                         already a `tenants.id`. Unlike the other
#                                         two it is CALLER-DRIVEN and every entry
#                                         is a violation that was PREVENTED from
#                                         being born, not one being reported.
#
# Nonzero here is NOT a defect in this service — it is this guard doing its job,
# and it names the caller's mistake. It is worth alerting on all the same,
# because a caller retrying into a refusal means some upstream flow believes an
# organisation id is a principal id, and that flow is the real generator.
#
# Process-lifetime and idempotent per (sub, realm), for the same reason the seed
# register is: counting occurrences would make the gauge a function of how often
# a caller retried rather than of how many distinct identities are confused.
_subject_refusals: dict[tuple[str, str], str] = {}
_subject_refusals_lock = threading.Lock()


def record_subject_refusal(keycloak_sub: str, keycloak_realm: str, tenant_name: str) -> None:
    """Register that a subject registration was declined. Never raises."""
    with _subject_refusals_lock:
        _subject_refusals[(str(keycloak_sub), str(keycloak_realm))] = str(tenant_name)


def subject_refusals() -> dict[tuple[str, str], str]:
    """A copy of the register — `(keycloak_sub, realm) -> tenant_name`."""
    with _subject_refusals_lock:
        return dict(_subject_refusals)


def reset_subject_refusals() -> None:
    """Clear the register. For tests only — the register is process-lifetime."""
    with _subject_refusals_lock:
        _subject_refusals.clear()


def collect_snapshot(session) -> TenantSubjectSnapshot:
    """Read every fact this module exposes. SELECT-only."""
    snap = TenantSubjectSnapshot()

    fetched = session.execute(_MATCHING_ROWS, {"cap": MAX_ROW_SERIES}).mappings().all()
    for r in fetched:
        snap.rows.append(
            TenantSubjectRow(
                tenant_id=str(r["tenant_id"]),
                tenant_name=str(r["tenant_name"]),
                subject_id=str(r["subject_id"]),
                keycloak_realm=str(r["keycloak_realm"]),
                match_kind=str(r["match_kind"]),
            )
        )
    snap.truncated = 1 if len(fetched) >= MAX_ROW_SERIES else 0

    # Seed EVERY (class, match) pair at zero. An absent series and a healthy one
    # read identically to `== 0`, so seeding is what makes "no tenant is a
    # subject" an assertion instead of an inference — the same reason
    # `nonagentic_license_expired_rows` seeds its revoked/not-revoked split. It
    # matters twice as much here: `match="subject_id"` is expected to sit at 0
    # on prod, and an unseeded 0 would be indistinguishable from a predicate
    # that silently stopped matching.
    for cls in CLASSES:
        for match_kind in MATCH_KINDS:
            snap.counts[(cls, match_kind)] = 0
    registers = {
        "sentinels": sorted(SENTINEL_TENANT_IDS),
        "retired": sorted(RETIRED_PRINCIPAL_TENANT_IDS),
    }
    for row in session.execute(_COUNT_BY_CLASS, registers).mappings().all():
        snap.counts[(str(row["series_class"]), str(row["match_kind"]))] = int(row["n"])

    return snap


def report_at_boot(session_factory) -> int:
    """Log every violating row at boot. Returns the actionable count.

    ⛔ NEVER RAISES — not on a violation, not on a database fault. It runs inside
    `_run_db_bootstrap`, where anything that is not a connect fault propagates
    and the pod REFUSES TO SERVE. Three rows violate today; making a report
    fatal would CrashLoop both replicas of the platform auth path over a
    condition that has been true for weeks. The same fail-soft rule
    `seed_platform_tenants` states for itself.
    """
    try:
        session = session_factory()
        try:
            snap = collect_snapshot(session)
        finally:
            session.close()
    except Exception:
        logger.exception(
            "tenant_subject_invariant: could not evaluate the invariant at boot "
            "(reported, never fatal — this check may not take the auth path down)"
        )
        return 0

    # The blind spot is announced on EVERY boot, alongside whatever was found —
    # not only when the found-set is empty. A clean licences-DB reading is the
    # exact moment a reader is most likely to conclude "no problem", and this is
    # the line that stops that conclusion being wrong.
    for blind_id, note in KNOWN_KEYCLOAK_ONLY_BLIND_SPOTS:
        logger.warning(
            "tenant_subject_invariant: BLIND SPOT — this check evaluates only "
            "source=%s and cannot see %s: %s",
            SOURCE_LICENCES_SUBJECT,
            blind_id,
            note,
        )

    actionable = [r for r in snap.rows if r.series_class == CLASS_VIOLATION]
    for row in actionable:
        logger.error(
            "tenant_is_a_subject: tenants.id=%s (name=%r) is ALSO a Keycloak principal "
            "id via subject.%s (subject.id=%s, realm=%s). An identity that "
            "authenticates is registered as the thing that pays, and is being billed "
            "as one. Existing row — reported, not fatal; new registrations of a "
            "principal id are refused by ensure_tenant.",
            row.tenant_id,
            row.tenant_name,
            row.match_kind,
            row.subject_id,
            row.keycloak_realm,
        )
    sentinels = sum(n for (cls, _), n in snap.counts.items() if cls == CLASS_SENTINEL)
    # A recorded exception is still SAID on every boot, with its reason — routing
    # it off the alerting series must not make it disappear from the log.
    for row in snap.rows:
        if row.series_class == CLASS_RETIRED_PRINCIPAL:
            logger.warning(
                "tenant_subject_invariant: RECORDED EXCEPTION (class=%s, not "
                "actionable) tenant %s (%r) is a subject.keycloak_sub: %s",
                CLASS_RETIRED_PRINCIPAL,
                row.tenant_id,
                row.tenant_name,
                RETIRED_PRINCIPAL_TENANT_IDS.get(row.tenant_id.strip().lower(), ""),
            )
    if actionable:
        logger.error(
            "tenant_subject_invariant: %d tenant row(s) are also subjects "
            "(%d via keycloak_sub, %d via subject.id; +%d deliberate sentinel). Watch "
            'nonagentic_license_tenant_is_subject_rows{class="violation"}.',
            len(actionable),
            sum(1 for r in actionable if r.match_kind == MATCH_KEYCLOAK_SUB),
            sum(1 for r in actionable if r.match_kind == MATCH_SUBJECT_ID),
            sentinels,
        )
    else:
        logger.info(
            "tenant_subject_invariant: no tenant id is a subject identity "
            "(%d deliberate sentinel exempt)",
            sentinels,
        )
    return len(actionable)


class TenantSubjectCollector:
    """Custom collector: a FRESH snapshot each scrape, never stale state.

    On a database fault it emits `collector_up 0` and NOTHING ELSE, for the
    reason spelled out on `LicenseExpiryCollector`: an empty-but-confident
    scrape makes `…{class="violation"} == 0` read TRUE while the query is
    failing, which is the one way a zero-valued guard lies.
    """

    def __init__(self, session_factory, ttl: float = SNAPSHOT_TTL_SECONDS) -> None:
        self._session_factory = session_factory
        self._ttl = ttl
        self._lock = threading.Lock()
        self._cached: TenantSubjectSnapshot | None = None
        self._cached_at = 0.0

    def _snapshot(self) -> TenantSubjectSnapshot:
        now = time.monotonic()
        with self._lock:
            if self._cached is not None and (now - self._cached_at) < self._ttl:
                return self._cached
        try:
            session = self._session_factory()
            try:
                snap = collect_snapshot(session)
            finally:
                session.close()
        except Exception:
            logger.exception("tenant/subject collector: snapshot failed")
            snap = TenantSubjectSnapshot(collector_up=False)
        with self._lock:
            self._cached = snap
            self._cached_at = time.monotonic()
        return snap

    def collect(self):
        snap = self._snapshot()

        up = GaugeMetricFamily(
            "nonagentic_license_tenant_subject_collector_up",
            "1 when the last tenant/subject invariant snapshot succeeded, 0 when the "
            "query failed. Read it beside the violation gauge: a zero there is only "
            "zero while this is one.",
        )
        up.add_metric([], 1.0 if snap.collector_up else 0.0)
        yield up

        # ⛔ EMITTED BEFORE THE `collector_up` GATE, DELIBERATELY. This is a
        # process-lifetime fact recorded at boot by `models.seed_platform_tenants`,
        # not a reading from the snapshot — it needs no query and cannot be stale.
        # Putting it after the early return below would make a database fault hide
        # the one signal that says the SEED LIST still names a principal id.
        refusals = seed_refusals()
        refused_total = GaugeMetricFamily(
            "nonagentic_license_tenant_seed_principal_refusals",
            "PLATFORM_TENANTS entries this process declined to seed because the id is "
            "already a Keycloak principal (`subject.keycloak_sub` or `subject.id`). "
            "⛔ NOT the same as …_tenant_is_subject_rows: that counts existing "
            "violating ROWS, this counts seed INSERTs refused, and a refusal whose row "
            "already exists changes no data at all. Nonzero means the seed CONSTANT "
            "still names a principal id — fix it at the source; the guard only stops "
            "it being reborn. Refusing is never fatal: this runs at boot on the "
            "platform auth path.",
        )
        refused_total.add_metric([], float(len(refusals)))
        yield refused_total

        if refusals:
            refused_named = GaugeMetricFamily(
                "nonagentic_license_tenant_seed_principal_refused",
                "1 per refused seed entry, so an alert can NAME it rather than report a "
                "bare count nobody can action.",
                labels=["tenant_id", "tenant_name", "match"],
            )
            for (tenant_id, tenant_name), match_kind in sorted(refusals.items()):
                refused_named.add_metric([tenant_id, tenant_name, match_kind], 1.0)
            yield refused_named

        # ⛔ ALSO BEFORE THE `collector_up` GATE, and for a stronger reason than the
        # seed register above. This is the ONLY machine-readable signal the SECOND
        # door has ever had, and it is a process-lifetime fact needing no query —
        # so a database fault must not be able to hide it. Suppressing it on a
        # failed snapshot would silence the subject-side door at exactly the moment
        # the tenant-side detector has also gone quiet, and the two silences would
        # be indistinguishable from a clean estate.
        sub_refusals = subject_refusals()
        sub_refused_total = GaugeMetricFamily(
            "nonagentic_license_subject_principal_refusals",
            "Subject registrations this process declined because the supplied "
            "`keycloak_sub` is ALREADY a registered `tenants.id`. ⛔ NOT the same as "
            "…_tenant_seed_principal_refusals (which is about the PLATFORM_TENANTS "
            "constant) and NOT the same as …_tenant_is_subject_rows (which counts "
            "violating rows that EXIST). Every entry here is a violation that was "
            "PREVENTED FROM BEING BORN through the door the tenant-side guards "
            "cannot see: `create_or_get_subject`, reached from "
            "POST /licenses/user/{subject_id} and the provision_* scripts. Nonzero "
            "is this guard working — but it also means some upstream flow believes "
            "an ORGANISATION id is a PRINCIPAL id, and that flow is the generator "
            "to fix.",
        )
        sub_refused_total.add_metric([], float(len(sub_refusals)))
        yield sub_refused_total

        if sub_refusals:
            sub_refused_named = GaugeMetricFamily(
                "nonagentic_license_subject_principal_refused",
                "1 per refused subject registration, so an alert can NAME the "
                "identity and the tenant it collided with rather than report a bare "
                "count nobody can action.",
                labels=["keycloak_sub", "keycloak_realm", "tenant_name"],
            )
            for (sub, realm), tenant_name in sorted(sub_refusals.items()):
                sub_refused_named.add_metric([sub, realm, tenant_name], 1.0)
            yield sub_refused_named

        if not snap.collector_up:
            # Emit nothing else. Half a snapshot is worse than none here: a
            # violation count of 0 from a failed query is indistinguishable from
            # a clean estate.
            return

        measured = GaugeMetricFamily(
            "nonagentic_license_tenant_is_subject_source_measured",
            "1 when this scrape actually EVALUATED that source, 0 when it did not "
            "look at all. ⛔ Read it before trusting a zero above: this check "
            'implements only source="licences_subject". source="keycloak_principal" '
            "reports 0 here and is DELIBERATELY absent from the violation counts, "
            'because a seeded zero is right for "measured, found none" and wrong '
            'for "never looked" — and it is known non-zero (nonagentic-gtm is a live '
            "Keycloak principal registered as a tenant that was never written to "
            "`subject`, so no licences-DB predicate can see it).",
            labels=["source"],
        )
        for source in SOURCES_MEASURED:
            measured.add_metric([source], 1.0)
        for source in SOURCES_UNMEASURED:
            measured.add_metric([source], 0.0)
        yield measured

        rows = GaugeMetricFamily(
            "nonagentic_license_tenant_is_subject_rows",
            "tenants.id values that are ALSO a Keycloak PRINCIPAL id — an identity "
            "that authenticates, registered as the thing that pays. (Principal, not "
            "person: book-fulfillment and nonagentic-gtm are service accounts; only "
            "portal-render-e2e's subject is a human. The defect is identical for a "
            "machine.) "
            'match="keycloak_sub" is a Keycloak claim registered as a tenant; '
            'match="subject_id" is this registry\'s own subject PK registered as '
            "one. They are counted apart because they have different causes and "
            'different fixes. class="violation" is ACTIONABLE and must reach 0; '
            'class="sentinel" is the deliberate, permanent exemption register '
            "(default-anon) and is split off precisely so an alert on the violation "
            'series is not pinned above zero forever. class="retired-principal" is a '
            "RECORDED EXCEPTION, not a clean row: a tenant id that was a Keycloak "
            "principal which has since been deleted and whose re-key the owner "
            "rejected (none registered today) — still matched, still counted, not "
            "actionable. Every (class, match) pair of "
            "the MEASURED source is seeded, so 0 is an assertion and not an absent "
            "series — see …_source_measured for the source that is not.",
            labels=["class", "match", "source"],
        )
        for cls in CLASSES:
            for match_kind in MATCH_KINDS:
                rows.add_metric(
                    [cls, match_kind, SOURCE_LICENCES_SUBJECT],
                    float(snap.counts.get((cls, match_kind), 0)),
                )
        yield rows

        named = GaugeMetricFamily(
            "nonagentic_license_tenant_is_subject",
            "1 per offending identity, so an alert can NAME the tenant instead of "
            "reporting a bare count nobody can action. The `source` label says which "
            "door caught it.",
            labels=["tenant_id", "tenant_name", "subject_id", "class", "match", "source"],
        )
        for row in snap.rows:
            named.add_metric(
                [
                    row.tenant_id,
                    row.tenant_name,
                    row.subject_id,
                    row.series_class,
                    row.match_kind,
                    SOURCE_LICENCES_SUBJECT,
                ],
                1.0,
            )
        yield named

        truncated = GaugeMetricFamily(
            "nonagentic_license_tenant_is_subject_series_truncated",
            f"1 when the per-identity series hit the {MAX_ROW_SERIES}-row cap. The "
            "counts above are computed by a separate unbounded aggregate and stay "
            "exact regardless.",
        )
        truncated.add_metric([], float(snap.truncated))
        yield truncated
