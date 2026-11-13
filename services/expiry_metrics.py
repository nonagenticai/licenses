"""Prometheus instrumentation for licence and service-token EXPIRY.

WHY THIS EXISTS. Reissue of a licence token is a MANUAL act, and until this
module there was no metric of any kind behind it. Measured live against
Prometheus on 2026-09-07, immediately before this change, each zero with a
positive control beside it:

    count({__name__=~".*licen.*|.*expir.*"})            EMPTY
    count({__name__=~".*token.*"})                      aa_prompt_tokens_total
        only -- an LLM token counter from worker_management, semantically
        unrelated. So the query SHAPE is fine; licence data genuinely is absent.
    label_values(job)                                   18 jobs, none of them
        `licenses` -- see the scrape note below.

The blast radius of a lapsed service token is downstream auth across w2v, kag,
karma, marketing and sales. The earliest live token expires 2027-02-17, 163 days
out at the time of writing, which is precisely the window in which an instrument
is cheap to build and worthless to build later.

⚠️ THE SCRAPE PATH WAS THE REAL GAP, NOT THE INSTRUMENTATION. main.py carried
three separate comments reading "this service has NO Prometheus scrape
annotation", and they were correct: the `licenses` Deployment's pod template
had only a `secret-rev` annotation. Prometheus's `kubernetes-pods` job keeps a
target ONLY when `prometheus.io/scrape=true` is present on the POD. So a
`/metrics` route added here alone would have emitted into the void, exactly like
the three counters that already do. The paired gitops change that adds the
annotation is what makes this module observable; neither half works alone.

────────────────────────────────────────────────────────────────────────────────
NULL `expires_at` MEANS PERPETUAL. THIS IS THE LOAD-BEARING DECISION.
────────────────────────────────────────────────────────────────────────────────
256 of 258 licence rows have `expires_at IS NULL`, and it is tempting to read
that as "nobody ever populated the column". It is not. `models.py` declares

    expires_at = Column(DateTime(timezone=True), nullable=True)  # NULL = perpetual

and `License.effective_status` resolves status against that window, treating a
NULL as never-expiring rather than as unknown. NULL is an ENCODED SEMANTIC, not
missing data.

So this module emits NO expiry series for a NULL row. The alternatives are both
wrong in a way that is hard to see later:
  * emitting 0 would put every perpetual row permanently in the past and make
    the 7-day alert fire forever on rows that cannot expire;
  * emitting +Inf makes min() correct but silently pads the series with values
    that can never cross a threshold, so a reader cannot distinguish "nothing is
    due" from "we are not looking".

⚠️ But emitting nothing has its own failure mode, and it is the whole reason for
the coverage gauge below: `min()` over the expiry series is then computed over
14% of live tokens and 0% of active licences. A future reader looking at a quiet
dashboard MUST be able to tell "nothing is expiring soon" from "we are not
looking at 99% of the estate". `nonagentic_license_expiry_coverage_rows` carries
both the with-expiry and without-expiry counts so that ratio is derivable at
query time, and is deliberately NOT alerted on -- a perpetual licence is the
normal, correct state here, so an alert on coverage would fire forever and be
silenced within a week.

⚠️ REVOKED ROWS ARE EXCLUDED FROM THE EXPIRY SERIES ON PURPOSE. A revoked token
is already dead; including it would let min() latch onto a credential nobody
needs to act on and make the earliest-wins alert unactionable. Revoked rows are
still COUNTED in `nonagentic_license_expired_rows{revoked="true"}` so their
existence stays visible -- today that is where the 2 expired tokens live.

⚠️ THE TRUNCATION BOUND IS IN SQL, ORDERED BY `expires_at ASC`, AND BOTH HALVES
MATTER. A bound applied after the fetch is not a bound -- it protects neither
the database nor memory. And an UNORDERED bound would be worse than none: it
could drop precisely the earliest-expiring row, which is the one value every
alert in this feature depends on. ORDER BY expires_at ASC makes truncation
provably safe for min(): the rows that survive are always the soonest.
`nonagentic_license_expiry_series_truncated` reports when the cap bites.

────────────────────────────────────────────────────────────────────────────────
`lifetime_class` -- HOURLY TOKENS ARE NOT A REISSUE DEADLINE
────────────────────────────────────────────────────────────────────────────────
Everything above was written when every token with an expiry was a credential a
human had to reissue by hand. That stopped being true on 2026-10-01: per the
owner ruling of 2026-09-28 ("short-lived tokens, no pasting"), services mint
hourly BUNDLE tokens (`POST /internal/tenants/{tenant_id}/bundle-token`, ttl
<= 60 min) and operators mint hourly operator tokens. Both are stored as plain
`token_type='api'` rows, so `token_type` cannot tell them from a year-long
service credential. Left unlabelled they false-fired two critical alerts:

  * `LicenseTokenExpiringWithin7Days` -- min() over the token expiry series is
    ALWAYS under an hour once anything mints hourly;
  * `LicenseTokenAlreadyExpired` -- an expired hourly token sits unrevoked until
    the hourly sweep reaps it, so `expired_rows{kind="token",revoked="false"}`
    is > 0 most of the time.

So every token series carries `lifetime_class`:

    short     0 < lifetime <= 65 minutes
    lt7d      65 minutes < lifetime <= 7 days
    lt30d     7 days < lifetime <= 30 days
    lt60d     30 days < lifetime <= 60 days
    standard  lifetime > 60 days, INCLUDING a NULL `created_at`
    (lifetime = expires_at - created_at; for the day buckets, lifetime - 5 min --
    see LIFETIME_BUCKET_TOLERANCE_MINUTES: a nominal N-day token is N days give
    or take a fraction of a second, and must land in ltN, the class band N
    EXCLUDES, because its expiry is under N days away from the moment it is minted)

and each reissue band excludes exactly the classes SHORTER than its own window:

    LicenseTokenExpiringWithin7Days   {lifetime_class!~"short|lt7d"}
    LicenseTokenExpiringWithin30Days  {lifetime_class!~"short|lt7d|lt30d"}
    LicenseTokenExpiringWithin60Days  {lifetime_class!~"short|lt7d|lt30d|lt60d"}

Decisions worth knowing:

  * The lt* buckets (added 2026-10-19): a token whose WHOLE lifetime is shorter
    than a band's window is inside that window from the moment it is minted, so
    it paged that band for its entire life (24h token 01a14f1b-..., tenant
    fr-test-user, paged Within7Days critical on 2026-10-19; 7-30d tokens paged
    Within30Days the same way). The bucket edges ARE the band windows, so a token
    whose lifetime is LONGER than a band's window is in that band's population.
    ⚠️ 2026-10-31: a lifetime EQUAL to the window (`expires_days=30`) used to land
    in the class above and paged the 30-day band critical from birth (MCP Module
    token 01a172bd-e9d4-..., tenant mcp-platform, since 2026-10-25). Fixed buckets, not a lifetime-seconds label:
    the label stays a 5-value enum, so cardinality does not grow.
  * `short` keeps its exact 65-minute meaning (pinned to the schema CHECK
    below). `LicenseTokenAlreadyExpired` still selects only `!="short"`, so it
    COUNTS every lt* token -- one expired and unrevoked across two hourly sweeps
    is a genuine sweep failure.

  * It is derived from the row's own LIFETIME, in SQL, not from the mint route.
    A token is "short" because it cannot outlive an hour, whoever minted it.
  * 65, not 60: the mint routes cap at 60 and `ck_token_granted_roles_short_lived`
    (models.py, migration 038) allows 5 minutes of clock skew. This module
    reuses that boundary so the two definitions of "short-lived" cannot diverge;
    a unit test pins `SHORT_TOKEN_MAX_LIFETIME_MINUTES` to the CHECK's text.
  * ⚠️ Every ambiguity resolves to `standard`, the class that IS alerted on.
    NULL `created_at` and a non-positive lifetime (a token born already expired)
    are both `standard`: a misclassification must make an alert louder, never
    hide a credential from it.
  * ⚠️ `short` has its OWN, much smaller series cap. Under one shared
    `ORDER BY expires_at ASC LIMIT 2000` hourly tokens always sort first, so a
    few hundred of them would evict exactly the long-lived rows the 7-day alert
    exists for. Each class is fetched under its own LIMIT and reports its own
    truncation flag.
  * The expired COUNT is exact per class (a GROUP BY, not the capped row fetch)
    and every (revoked, lifetime_class) pair is seeded at zero, so
    `expired_rows{kind="token",revoked="false",lifetime_class="standard"}` is
    exported as 0 rather than going absent when nothing standard has lapsed.
  * Licences have no short class; their series carry `lifetime_class="standard"`
    on the two metrics shared with tokens (`expired_rows`, `series_truncated`)
    because one metric name must have one label-name set.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from prometheus_client.core import GaugeMetricFamily
from sqlalchemy import text

logger = logging.getLogger(__name__)

# Cap on per-row series emitted per kind. 250 tokens exist today; 2000 leaves
# room for an order of magnitude of growth while bounding a scrape at a few
# hundred KB. See the truncation note in the module docstring: the cap is
# applied in SQL under ORDER BY expires_at ASC, so min() is never affected.
MAX_SERIES_PER_KIND = 2000

LIFETIME_SHORT = "short"
LIFETIME_LT7D = "lt7d"
LIFETIME_LT30D = "lt30d"
LIFETIME_LT60D = "lt60d"
LIFETIME_STANDARD = "standard"

# A token whose whole lifetime fits in this window is `lifetime_class="short"`.
# 65 = the 60-minute mint cap (`OPERATOR_TOKEN_MAX_TTL_MINUTES` /
# `BUNDLE_TOKEN_MAX_TTL_MINUTES` in main.py) + the 5 minutes of clock skew that
# `ck_token_granted_roles_short_lived` (models.py) already allows. Not imported
# from main.py: main imports this module. tests/unit/test_expiry_metrics.py pins
# it against the CHECK constraint's own text instead.
SHORT_TOKEN_MAX_LIFETIME_MINUTES = 65

# Lifetime buckets above `short`, in ascending order: (class, inclusive upper
# bound in days). Each bound equals the window of one reissue band in gitops
# rules-b.yaml (LicenseTokenExpiringWithin{7,30,60}Days): a token that cannot
# outlive a band's window is inside it from birth. A lifetime of EXACTLY a window
# is such a token (its expiry is < N days away the instant it exists), so it
# lands in ltN, which band N excludes. Anything above the last bound is `standard`.
#
# MINT-ORDER TOLERANCE. The mint path computes `expires_at = now() + N days`
# BEFORE the row's `created_at` is stamped, so a real N-day token is a fraction
# of a second short of N days (measured 2026-10-31: 29d 23:59:59.984). A day
# bucket is chosen on `lifetime - LIFETIME_BUCKET_TOLERANCE_MINUTES`, so a token
# within 5 minutes of a window still counts as "no longer than" it; 5 minutes
# matches the clock skew `ck_token_granted_roles_short_lived` already allows.
LIFETIME_BUCKET_TOLERANCE_MINUTES = 5
LIFETIME_DAY_BUCKETS: tuple[tuple[str, int], ...] = (
    (LIFETIME_LT7D, 7),
    (LIFETIME_LT30D, 30),
    (LIFETIME_LT60D, 60),
)
TOKEN_LIFETIME_CLASSES: tuple[str, ...] = (
    LIFETIME_SHORT,
    *(c for c, _ in LIFETIME_DAY_BUCKETS),
    LIFETIME_STANDARD,
)

# Separate, deliberately small cap for short-class token series. They are
# exported for visibility only -- no alert reads them -- and they always sort
# FIRST under ORDER BY expires_at ASC, so sharing MAX_SERIES_PER_KIND would let
# hourly tokens crowd out the long-lived rows the 7-day alert depends on.
MAX_SHORT_TOKEN_SERIES = 200

# Every token class gets its OWN fetch and budget, so a populous short-lived
# class can never evict a longer-lived one. lt7d is alerted by no band (visibility
# only) so it gets the small budget; lt30d/lt60d are alerted by the shorter bands
# and get the full one. Each fetch is ORDER BY expires_at ASC, so min() within a
# class is always exact even when its cap bites.
MAX_LT7D_TOKEN_SERIES = 500

# The snapshot is cached so that scrape frequency does not drive database load.
# The engine uses NullPool (every session is a fresh connect), so an uncached
# collector would open a new connection on every scrape of every replica.
# 60s against a 15s scrape interval means expiry data is at most one minute
# stale, which is irrelevant for a value measured in days.
SNAPSHOT_TTL_SECONDS = 60.0


@dataclass
class ExpiryRow:
    row_id: str
    kind: str
    subtype: str
    app: str
    tenant: str
    expiry_epoch: float
    # Tokens only; a licence row is always "standard".
    lifetime_class: str = LIFETIME_STANDARD


@dataclass
class ExpirySnapshot:
    rows: list[ExpiryRow] = field(default_factory=list)
    # (kind, has_expiry) -> count, over the LIVE (non-revoked / active) scope.
    coverage: dict[tuple[str, str], int] = field(default_factory=dict)
    # (kind, revoked, lifetime_class) -> count of rows already past their expiry.
    expired: dict[tuple[str, str, str], int] = field(default_factory=dict)
    # (kind, lifetime_class) -> 1 when that class hit its own series cap.
    truncated: dict[tuple[str, str], int] = field(default_factory=dict)
    collector_up: bool = True


# ── SQL ───────────────────────────────────────────────────────────────────────
# ⛔ SELECT-only. This module must never write to the licences database.

# The ONE definition of `lifetime_class`, shared by the row fetch and the expired
# count so the two can never classify the same token differently. Expects the
# token table aliased `t`. Every non-matching case -- NULL created_at (the
# subtraction is NULL), a non-positive lifetime -- falls to ELSE 'standard',
# the alerted class; see the module docstring. The interpolated value is a
# module int constant, not input.
_VALID_LIFETIME = "t.created_at IS NOT NULL AND t.expires_at > t.created_at"
LIFETIME_CLASS_SQL = (
    "CASE"
    f" WHEN {_VALID_LIFETIME} AND t.expires_at - t.created_at"
    f" <= interval '{SHORT_TOKEN_MAX_LIFETIME_MINUTES} minutes' THEN '{LIFETIME_SHORT}'"
    + "".join(
        f" WHEN {_VALID_LIFETIME} AND t.expires_at - t.created_at"
        f" - interval '{LIFETIME_BUCKET_TOLERANCE_MINUTES} minutes'"
        f" < interval '{days} days' THEN '{cls}'"
        for cls, days in LIFETIME_DAY_BUCKETS
    )
    + f" ELSE '{LIFETIME_STANDARD}' END"
)

# Run once per lifetime class, each under its own :cap. The class filter sits
# INSIDE the query, before ORDER BY/LIMIT -- classifying after a shared LIMIT
# would be the crowding-out bug with extra steps.
_TOKEN_ROWS = text(
    f"""
    SELECT c.row_id, c.subtype, c.app, c.tenant, c.expiry_epoch, c.lifetime_class
      FROM (
        SELECT t.id::text                        AS row_id,
               COALESCE(t.token_type, 'unknown') AS subtype,
               COALESCE(a.name, 'unknown')       AS app,
               COALESCE(tn.name, 'unknown')      AS tenant,
               EXTRACT(EPOCH FROM t.expires_at)  AS expiry_epoch,
               t.expires_at                      AS expires_at,
               {LIFETIME_CLASS_SQL}              AS lifetime_class
          FROM token t
          JOIN license l  ON l.id = t.license_id
          LEFT JOIN app a ON a.id = l.app_id
          LEFT JOIN tenants tn ON tn.id = l.tenant_id
         WHERE t.revoked = false
           AND t.expires_at IS NOT NULL
      ) c
     WHERE c.lifetime_class = :lifetime_class
     ORDER BY c.expires_at ASC
     LIMIT :cap
    """
)

_LICENSE_ROWS = text(
    """
    SELECT l.id::text                        AS row_id,
           COALESCE(l.status, 'unknown')     AS subtype,
           COALESCE(a.name, 'unknown')       AS app,
           COALESCE(tn.name, 'unknown')      AS tenant,
           EXTRACT(EPOCH FROM l.expires_at)  AS expiry_epoch
      FROM license l
      LEFT JOIN app a ON a.id = l.app_id
      LEFT JOIN tenants tn ON tn.id = l.tenant_id
     WHERE l.status = 'active'
       AND l.expires_at IS NOT NULL
     ORDER BY l.expires_at ASC
     LIMIT :cap
    """
)

# Coverage is measured over the LIVE scope only -- the rows a lapse could
# actually hurt. Counting revoked/expired rows in the denominator would make the
# ratio drift with history rather than with the thing being watched.
_TOKEN_COVERAGE = text(
    "SELECT count(*) AS total, count(expires_at) AS with_expiry FROM token WHERE revoked = false"
)
_LICENSE_COVERAGE = text(
    "SELECT count(*) AS total, count(expires_at) AS with_expiry "
    "FROM license WHERE status = 'active'"
)

# An exact count per class -- NOT derived from the capped row fetch above.
_TOKEN_EXPIRED = text(
    f"SELECT t.revoked AS revoked, {LIFETIME_CLASS_SQL} AS lifetime_class, count(*) AS n "
    "FROM token t "
    "WHERE t.expires_at IS NOT NULL AND t.expires_at <= now() GROUP BY 1, 2"
)
_LICENSE_EXPIRED = text(
    "SELECT (l.status = 'revoked') AS revoked, count(*) AS n FROM license l "
    "WHERE l.expires_at IS NOT NULL AND l.expires_at <= now() GROUP BY 1"
)


def collect_snapshot(session) -> ExpirySnapshot:
    """Read every expiry fact this module exposes. SELECT-only."""
    snap = ExpirySnapshot()

    # (kind, lifetime_class, statement, cap). Each fetch has its OWN cap, so no
    # class can consume another's series budget.
    fetches = (
        ("token", LIFETIME_STANDARD, _TOKEN_ROWS, MAX_SERIES_PER_KIND),
        ("token", LIFETIME_SHORT, _TOKEN_ROWS, MAX_SHORT_TOKEN_SERIES),
        ("token", LIFETIME_LT7D, _TOKEN_ROWS, MAX_LT7D_TOKEN_SERIES),
        ("token", LIFETIME_LT30D, _TOKEN_ROWS, MAX_SERIES_PER_KIND),
        ("token", LIFETIME_LT60D, _TOKEN_ROWS, MAX_SERIES_PER_KIND),
        ("license", LIFETIME_STANDARD, _LICENSE_ROWS, MAX_SERIES_PER_KIND),
    )
    for kind, lifetime_class, stmt, cap in fetches:
        # `lifetime_class` is unused by the licence statement; a surplus bind
        # parameter is ignored for a textual statement.
        params = {"cap": cap, "lifetime_class": lifetime_class}
        fetched = session.execute(stmt, params).mappings().all()
        for r in fetched:
            if r["expiry_epoch"] is None:
                continue
            snap.rows.append(
                ExpiryRow(
                    row_id=r["row_id"],
                    kind=kind,
                    subtype=str(r["subtype"]),
                    app=str(r["app"]),
                    tenant=str(r["tenant"]),
                    expiry_epoch=float(r["expiry_epoch"]),
                    lifetime_class=lifetime_class,
                )
            )
        snap.truncated[(kind, lifetime_class)] = 1 if len(fetched) >= cap else 0

    for kind, stmt in (("token", _TOKEN_COVERAGE), ("license", _LICENSE_COVERAGE)):
        row = session.execute(stmt).mappings().one()
        total = int(row["total"])
        with_expiry = int(row["with_expiry"])
        snap.coverage[(kind, "true")] = with_expiry
        # Explicitly emitted rather than left implicit: a reader must be able to
        # see the 99% we are NOT watching without knowing the total by heart.
        snap.coverage[(kind, "false")] = total - with_expiry

    for kind, stmt, classes in (
        ("token", _TOKEN_EXPIRED, TOKEN_LIFETIME_CLASSES),
        ("license", _LICENSE_EXPIRED, (LIFETIME_STANDARD,)),
    ):
        # Seed EVERY (revoked, lifetime_class) pair so the series EXISTS at
        # zero. An absent series cannot be alerted on with `== 0` and reads
        # identically to a collector that never ran -- see
        # `nonagentic_license_expiry_collector_up`. This matters most for
        # token/false/standard: it is the series LicenseTokenAlreadyExpired
        # selects, and it is 0 precisely when only hourly tokens have lapsed.
        for lifetime_class in classes:
            snap.expired[(kind, "false", lifetime_class)] = 0
            snap.expired[(kind, "true", lifetime_class)] = 0
        for row in session.execute(stmt).mappings().all():
            # The licence query has no lifetime_class column: always standard.
            lifetime_class = str(row.get("lifetime_class") or LIFETIME_STANDARD)
            revoked = "true" if row["revoked"] else "false"
            snap.expired[(kind, revoked, lifetime_class)] = int(row["n"])

    return snap


class LicenseExpiryCollector:
    """Custom collector: yields a FRESH snapshot each scrape, never stale state.

    ⚠️ On a database fault this deliberately emits `collector_up 0` and NO
    expiry series, rather than emitting a stale or empty-but-confident set. A
    guard reading zero is zero only inside its own scope: if a failed query
    silently produced no series, `min()` would go empty and every expiry alert
    would stop firing WITHOUT anything going red. `LicenseExpiryCollectorDown`
    is the alert that closes that hole, and it is the reason this class does not
    simply swallow the exception.
    """

    def __init__(self, session_factory, ttl: float = SNAPSHOT_TTL_SECONDS) -> None:
        self._session_factory = session_factory
        self._ttl = ttl
        self._lock = threading.Lock()
        self._cached: ExpirySnapshot | None = None
        self._cached_at = 0.0

    def _snapshot(self) -> ExpirySnapshot:
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
            logger.exception("license expiry collector: snapshot failed")
            snap = ExpirySnapshot(collector_up=False)
        with self._lock:
            self._cached = snap
            self._cached_at = time.monotonic()
        return snap

    def collect(self):
        snap = self._snapshot()

        up = GaugeMetricFamily(
            "nonagentic_license_expiry_collector_up",
            "1 when the last licence-expiry snapshot succeeded, 0 when the query failed.",
        )
        up.add_metric([], 1.0 if snap.collector_up else 0.0)
        yield up

        if not snap.collector_up:
            # Emit nothing else. Half a snapshot is worse than none: a partial
            # coverage count would understate what is unwatched.
            return

        token_expiry = GaugeMetricFamily(
            "nonagentic_license_token_expiry_timestamp_seconds",
            "Unix timestamp at which a non-revoked licence TOKEN expires. "
            "Absolute, never a countdown: alert rules compute remaining time at "
            "query time so thresholds can be changed without a code change. "
            f'lifetime_class="short" = whole lifetime <= {SHORT_TOKEN_MAX_LIFETIME_MINUTES} min '
            "(hourly bundle/operator tokens, re-minted automatically); "
            "lt7d/lt30d/lt60d = whole lifetime under 7/30/60 days; each reissue band "
            "must exclude every class shorter than its own window.",
            labels=["token_id", "token_type", "app", "tenant", "lifetime_class"],
        )
        license_expiry = GaugeMetricFamily(
            "nonagentic_license_expiry_timestamp_seconds",
            "Unix timestamp at which an ACTIVE licence expires. NULL expires_at "
            "means perpetual and is deliberately not represented here.",
            labels=["license_id", "status", "app", "tenant"],
        )
        for row in snap.rows:
            labels = [row.row_id, row.subtype, row.app, row.tenant]
            if row.kind == "token":
                token_expiry.add_metric([*labels, row.lifetime_class], row.expiry_epoch)
            else:
                license_expiry.add_metric(labels, row.expiry_epoch)
        yield token_expiry
        yield license_expiry

        coverage = GaugeMetricFamily(
            "nonagentic_license_expiry_coverage_rows",
            "Live rows split by whether they carry an expiry at all. "
            'has_expiry="false" is the population every expiry alert is BLIND to '
            "(NULL expires_at = perpetual); read it beside a quiet dashboard to "
            'tell "nothing is due" from "we are not looking".',
            labels=["kind", "has_expiry"],
        )
        for (kind, has_expiry), n in sorted(snap.coverage.items()):
            coverage.add_metric([kind, has_expiry], float(n))
        yield coverage

        expired = GaugeMetricFamily(
            "nonagentic_license_expired_rows",
            "Rows whose expiry is already in the past, split by revoked. "
            'revoked="false" is actionable; revoked="true" is benign history and '
            'is surfaced only so it stays visible. For kind="token", '
            'lifetime_class="short" counts lapsed hourly tokens awaiting the sweep '
            '(expected, not actionable); alert on lifetime_class!="short". '
            'Licences are always "standard".',
            labels=["kind", "revoked", "lifetime_class"],
        )
        for (kind, revoked, lifetime_class), n in sorted(snap.expired.items()):
            expired.add_metric([kind, revoked, lifetime_class], float(n))
        yield expired

        truncated = GaugeMetricFamily(
            "nonagentic_license_expiry_series_truncated",
            f"1 when per-row series hit their cap ({MAX_SERIES_PER_KIND} rows; "
            f'{MAX_SHORT_TOKEN_SERIES} for kind="token",lifetime_class="short"). '
            "Each class is capped separately and ordered by expires_at ASC, so "
            "min() within a class stays correct even at the cap.",
            labels=["kind", "lifetime_class"],
        )
        for (kind, lifetime_class), flag in sorted(snap.truncated.items()):
            truncated.add_metric([kind, lifetime_class], float(flag))
        yield truncated
