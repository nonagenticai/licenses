"""The expiry collector must stay correct in the two ways that silently rot.

⚠️ Neither test here is about "does it emit a number". Both encode a decision
that a future edit could reverse without any test going red:

  1. A NULL `expires_at` means PERPETUAL (models.py), so it must produce NO
     expiry series -- not 0, not +Inf. Emitting 0 would put every perpetual row
     in the past and fire the 7-day alert forever; +Inf would pad min() with
     values that can never cross a threshold. Both look "instrumented".
  2. A collector that cannot reach the database must emit `collector_up 0` and
     NOTHING ELSE. The dangerous failure is an empty-but-confident scrape:
     min() over an absent series is empty, so every expiry alert stops firing
     with nothing going red. That is why the fault path is asserted, not just
     the happy path.

⚠️ A third decision joined them on 2026-10-06, and it lives in SQL, so the fake
session below cannot pin it: `lifetime_class`. Hourly bundle/operator tokens
(owner ruling 2026-09-28, "short-lived tokens, no pasting") are stored as plain
`token_type='api'` rows; unlabelled, they false-fired both critical token alerts.
The classification, the per-class series caps and the per-class expired count are
therefore asserted against a REAL Postgres in the second half of this file. The
fake-session half only pins how classified rows map onto series.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import CheckConstraint, text

from models import Token
from services import expiry_metrics
from services.expiry_metrics import (
    LIFETIME_CLASS_SQL,
    LIFETIME_DAY_BUCKETS,
    SHORT_TOKEN_MAX_LIFETIME_MINUTES,
    LicenseExpiryCollector,
    collect_snapshot,
)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def one(self):
        return self._rows[0]


class _FakeSession:
    """Replays a scripted answer per SQL shape. Deliberately not a real DB: the
    SQL itself is exercised against production in the PR body; what needs
    pinning HERE is the mapping from rows to series."""

    def __init__(
        self, token_rows, license_rows, token_cov, license_cov, token_exp, license_exp, fail=False
    ):
        self._script = dict(
            token_rows=token_rows,
            license_rows=license_rows,
            token_cov=token_cov,
            license_cov=license_cov,
            token_exp=token_exp,
            license_exp=license_exp,
        )
        self._fail = fail

    def execute(self, stmt, params=None):
        if self._fail:
            raise RuntimeError("connection refused")
        sql = str(stmt)
        if "count(expires_at)" in sql:
            return _Result([self._script["token_cov" if "token" in sql else "license_cov"]])
        if "<= now()" in sql:
            return _Result(self._script["token_exp" if "FROM token" in sql else "license_exp"])
        if "FROM token" in sql:
            # The token fetch runs once per lifetime class; answer each run with
            # only that class's rows, as the SQL filter would.
            wanted = (params or {})["lifetime_class"]
            return _Result([r for r in self._script["token_rows"] if r["lifetime_class"] == wanted])
        return _Result(self._script["license_rows"])

    def close(self):
        pass


def _families(collector):
    return {f.name: f for f in collector.collect()}


def _samples(family):
    return {tuple(s.labels.values()): s.value for s in family.samples}


@pytest.fixture
def scripted():
    def build(**kw):
        base = dict(
            token_rows=[
                {
                    "row_id": "t1",
                    "subtype": "api",
                    "app": "Licenses Module",
                    "tenant": "acme",
                    "expiry_epoch": 1_800_000_000.0,
                    "lifetime_class": "standard",
                }
            ],
            license_rows=[],
            token_cov={"total": 221, "with_expiry": 37},
            license_cov={"total": 164, "with_expiry": 0},
            token_exp=[{"revoked": True, "lifetime_class": "standard", "n": 2}],
            license_exp=[],
        )
        base.update(kw)
        return LicenseExpiryCollector(lambda: _FakeSession(**base), ttl=0.0)

    return build


def test_a_null_expiry_row_produces_no_series_because_null_means_perpetual(scripted):
    """256 of 258 licences have expires_at NULL. They must be INVISIBLE to the
    expiry gauge and VISIBLE in the coverage gauge -- that split is the whole
    design. A regression that starts emitting them would fire the 7-day alert
    on 99% of the estate forever."""
    c = scripted()
    fams = _families(c)

    # Not one licence has an expiry -> not one licence expiry series.
    assert _samples(fams["nonagentic_license_expiry_timestamp_seconds"]) == {}

    # ...but the 164 perpetual licences are COUNTED, so a quiet dashboard is
    # distinguishable from a blind one.
    cov = _samples(fams["nonagentic_license_expiry_coverage_rows"])
    assert cov[("license", "false")] == 164.0
    assert cov[("license", "true")] == 0.0
    assert cov[("token", "true")] == 37.0
    assert cov[("token", "false")] == 184.0  # 221 live tokens - 37 with expiry


def test_already_expired_rows_are_split_by_revoked_and_seeded_at_zero(scripted):
    """The actionable series must EXIST at zero. An absent series and a healthy
    one read identically to `== 0`, so seeding is what makes the today-true
    reading (`revoked="true"` = 2) provable rather than inferred."""
    exp = _samples(_families(scripted())["nonagentic_license_expired_rows"])
    assert exp[("token", "true", "standard")] == 2.0
    assert exp[("token", "false", "standard")] == 0.0  # seeded, not absent
    assert exp[("license", "false", "standard")] == 0.0


def test_a_database_fault_emits_collector_up_zero_and_no_other_series(scripted):
    """The empty-but-confident scrape is the failure this guards. If a fault
    produced no series at all, min() would go empty and every expiry alert would
    stop firing without anything going red."""
    fams = _families(scripted(fail=True))
    assert _samples(fams["nonagentic_license_expiry_collector_up"]) == {(): 0.0}
    assert "nonagentic_license_token_expiry_timestamp_seconds" not in fams
    assert "nonagentic_license_expiry_coverage_rows" not in fams


def test_the_expiry_gauge_is_an_absolute_timestamp_not_a_countdown(scripted):
    """A pre-computed 'days left' goes stale between scrapes and cannot be
    re-thresholded without a code change. Pin the absolute epoch."""
    tok = _samples(_families(scripted())["nonagentic_license_token_expiry_timestamp_seconds"])
    assert tok[("t1", "api", "Licenses Module", "acme", "standard")] == 1_800_000_000.0


def test_the_alerted_expired_series_stays_exported_at_zero_when_only_hourly_tokens_lapsed(
    scripted,
):
    """`LicenseTokenAlreadyExpired` selects
    `{kind="token",revoked="false",lifetime_class!="short"}`. When the only lapsed
    tokens are hourly ones that series must read 0, NOT go absent: `max(absent) > 0`
    is silently false for ever, which is the alert dying rather than clearing."""
    c = scripted(token_exp=[{"revoked": False, "lifetime_class": "short", "n": 7}])
    exp = _samples(_families(c)["nonagentic_license_expired_rows"])
    assert exp[("token", "false", "short")] == 7.0
    assert exp[("token", "false", "standard")] == 0.0  # exported, not absent
    # Every series of one metric name carries the same label-name set (the
    # Prometheus client rejects anything else); licences are always standard.
    token_classes = ("short", "lt7d", "lt30d", "lt60d", "standard")
    assert set(exp) == {
        *{("token", r, c) for r in ("false", "true") for c in token_classes},
        ("license", "false", "standard"),
        ("license", "true", "standard"),
    }


def test_each_lifetime_class_reports_its_own_truncation_flag(scripted, monkeypatch):
    """One shared flag could not say WHICH budget ran out, and the short budget
    running out is routine while the standard one running out is a finding."""
    monkeypatch.setattr(expiry_metrics, "MAX_SHORT_TOKEN_SERIES", 1)
    row = {"subtype": "api", "app": "a", "tenant": "t", "expiry_epoch": 1.0}
    c = scripted(
        token_rows=[
            {**row, "row_id": "s1", "lifetime_class": "short"},
            {**row, "row_id": "l1", "lifetime_class": "standard"},
        ]
    )
    fams = _families(c)
    trunc = _samples(fams["nonagentic_license_expiry_series_truncated"])
    assert trunc == {
        ("token", "lt7d"): 0.0,
        ("token", "lt30d"): 0.0,
        ("token", "lt60d"): 0.0,
        ("token", "short"): 1.0,
        ("token", "standard"): 0.0,
        ("license", "standard"): 0.0,
    }
    tok = _samples(fams["nonagentic_license_token_expiry_timestamp_seconds"])
    assert set(tok) == {("s1", "api", "a", "t", "short"), ("l1", "api", "a", "t", "standard")}


# ══════════════════════════════════════════════════════════════════════════════
# REAL POSTGRES: the classification is a SQL predicate, so a fake cannot pin it.
# ══════════════════════════════════════════════════════════════════════════════


def _mk_token(
    db_manager,
    license_id,
    *,
    lifetime: timedelta,
    expires_in: timedelta,
    revoked: bool = False,
) -> str:
    """Insert ONE token with an explicit lifetime, returning its id.

    Inserted directly rather than through `create_token` because the thing under
    test is `expires_at - created_at`, and `create_token` owns `created_at`."""
    now = datetime.now(UTC)
    expires_at = now + expires_in
    session = db_manager.get_session()
    try:
        tok = Token(
            license_id=UUID(str(license_id)),
            token_hash=Token.hash_token(Token.generate_token()),
            token_type="api",
            created_at=expires_at - lifetime,
            expires_at=expires_at,
            revoked=revoked,
        )
        session.add(tok)
        session.commit()
        return str(tok.id)
    finally:
        session.close()


def _snapshot(db_manager):
    session = db_manager.get_session()
    try:
        return collect_snapshot(session)
    finally:
        session.close()


def _token_classes(snap) -> dict[str, str]:
    return {r.row_id: r.lifetime_class for r in snap.rows if r.kind == "token"}


def test_the_short_window_is_the_same_65_minutes_the_schema_already_enforces():
    """Two definitions of "short-lived token" in one service is how they drift.
    `ck_token_granted_roles_short_lived` is the older one; this module must agree
    with it, and cannot import the mint caps from main.py (main imports it)."""
    check = next(
        c
        for c in Token.__table_args__
        if isinstance(c, CheckConstraint) and c.name == "ck_token_granted_roles_short_lived"
    )
    assert f"interval '{SHORT_TOKEN_MAX_LIFETIME_MINUTES} minutes'" in str(check.sqltext)
    assert f"interval '{SHORT_TOKEN_MAX_LIFETIME_MINUTES} minutes'" in LIFETIME_CLASS_SQL


@pytest.mark.parametrize(
    ("created_at", "expected"),
    [
        ("now() - interval '1 minute'", "short"),
        ("now() - interval '60 minutes'", "short"),  # the mint cap
        ("now() - interval '65 minutes'", "short"),  # inclusive boundary (cap + skew)
        ("now() - interval '65 minutes 1 second'", "lt7d"),
        ("now() - interval '66 minutes'", "lt7d"),
        ("now() - interval '24 hours'", "lt7d"),  # the 2026-10-19 false page
        ("now() - interval '6 days 23 hours'", "lt7d"),
        # Each bucket edge == a band window plus the 5-minute mint-order
        # tolerance: a lifetime of N days (a real N-day mint is a fraction of a
        # second short) is inside band N from birth, so it lands in ltN, which
        # band N excludes. Only a lifetime clearly LONGER than N reaches band N.
        ("now() - interval '7 days' + interval '400 milliseconds'", "lt7d"),
        ("now() - interval '7 days'", "lt7d"),
        ("now() - interval '7 days 4 minutes 59 seconds'", "lt7d"),
        ("now() - interval '7 days 5 minutes'", "lt30d"),
        ("now() - interval '30 days' + interval '16 milliseconds'", "lt30d"),
        ("now() - interval '30 days'", "lt30d"),
        ("now() - interval '30 days 4 minutes 59 seconds'", "lt30d"),
        ("now() - interval '30 days 5 minutes'", "lt60d"),
        ("now() - interval '60 days'", "lt60d"),
        ("now() - interval '60 days 4 minutes 59 seconds'", "lt60d"),
        ("now() - interval '60 days 5 minutes'", "standard"),
        ("now() - interval '365 days'", "standard"),
        # ⚠️ Ambiguity resolves to the ALERTED class, never the hidden one.
        ("NULL::timestamptz", "standard"),  # created_at IS NULL
        ("now() - interval '0 minutes'", "standard"),  # zero lifetime
        ("now() - interval '-1 day'", "standard"),  # born already expired
    ],
)
def test_the_lifetime_class_predicate_boundaries(db_manager, created_at, expected):
    """The predicate itself, over literal rows. `token.created_at` is NOT NULL in
    the schema, so a NULL can only be exercised here, not through a seeded row --
    which is exactly why the module must not be left to "can't happen"."""
    sql = text(
        f"SELECT {LIFETIME_CLASS_SQL} AS lifetime_class "
        f"FROM (SELECT now() AS expires_at, {created_at} AS created_at) t"
    )
    session = db_manager.get_session()
    try:
        assert session.execute(sql).scalar_one() == expected
    finally:
        session.close()


def test_seeded_tokens_are_classified_by_their_own_lifetime(db_manager, seed_license):
    """A 60-minute token is `short`; 66-minute and 24-hour tokens are `lt7d`;
    a 10-day and a 30-day token are `lt30d`; 365 days is `standard`.

    ⚠️ THIS IS THE ASSERTION THAT FAILS IF THE PREDICATE IS REMOVED. Replace
    `LIFETIME_CLASS_SQL` with a constant 'standard' and `hourly` is no longer
    `short` (it is absent from the short fetch entirely); replace it with a
    constant 'short' and the two long-lived rows flip. All three token_type
    values are 'api' -- nothing but the lifetime distinguishes them."""
    lic = seed_license.id
    hourly = _mk_token(
        db_manager, lic, lifetime=timedelta(minutes=60), expires_in=timedelta(minutes=30)
    )
    just_over = _mk_token(
        db_manager, lic, lifetime=timedelta(minutes=66), expires_in=timedelta(minutes=30)
    )
    daily = _mk_token(db_manager, lic, lifetime=timedelta(hours=24), expires_in=timedelta(hours=12))
    tenday = _mk_token(db_manager, lic, lifetime=timedelta(days=10), expires_in=timedelta(days=5))
    monthly = _mk_token(db_manager, lic, lifetime=timedelta(days=30), expires_in=timedelta(days=3))
    yearly = _mk_token(
        db_manager, lic, lifetime=timedelta(days=365), expires_in=timedelta(days=200)
    )

    expected = {
        hourly: "short",
        just_over: "lt7d",
        daily: "lt7d",
        tenday: "lt30d",
        monthly: "lt30d",
        yearly: "standard",
    }
    assert _token_classes(_snapshot(db_manager)) == expected

    # ...and the label reaches the exported series, on every token series.
    collector = LicenseExpiryCollector(db_manager.get_session, ttl=0.0)
    tok = _families(collector)["nonagentic_license_token_expiry_timestamp_seconds"]
    assert {s.labels["token_id"]: s.labels["lifetime_class"] for s in tok.samples} == expected


def test_the_lifetime_buckets_are_the_reissue_band_windows():
    """Each lt* bucket exists because a token shorter-lived than a band's window
    is inside that window from birth. The edges must equal the band windows in
    gitops rules-b.yaml (LicenseTokenExpiringWithin{7,30,60}Days); move a band and
    this must move with it."""
    assert LIFETIME_DAY_BUCKETS == (("lt7d", 7), ("lt30d", 30), ("lt60d", 60))
    for _, days in LIFETIME_DAY_BUCKETS:
        assert f"interval '{days} days'" in LIFETIME_CLASS_SQL


# band -> classes it EXCLUDES (mirrors the rule selectors in gitops rules-b.yaml)
_BAND_EXCLUDES = {
    7: {"short", "lt7d"},
    30: {"short", "lt7d", "lt30d"},
    60: {"short", "lt7d", "lt30d", "lt60d"},
}


@pytest.mark.parametrize(
    ("lifetime", "band", "in_band_population"),
    [
        (timedelta(hours=24), 7, False),  # 2026-10-19 false page
        (timedelta(days=7), 7, False),  # lifetime == window: inside it from birth
        (timedelta(days=6, hours=23), 7, False),
        (timedelta(days=7) - timedelta(milliseconds=400), 7, False),  # real mint drift
        (timedelta(days=8), 7, True),
        (timedelta(days=29), 30, False),  # 7-30d token no longer pages 30d at birth
        (timedelta(days=30), 30, False),  # 2026-10-31 false page (expires_days=30)
        (timedelta(days=31), 30, True),
        (timedelta(days=59), 60, False),
        (timedelta(days=60), 60, False),
        (timedelta(days=61), 60, True),
        (timedelta(days=10), 7, True),  # longer than 7d: still pages the 7d band
        (timedelta(days=45), 30, True),
    ],
)
def test_a_token_is_in_a_band_iff_its_lifetime_reaches_the_window(
    db_manager, seed_license, lifetime, band, in_band_population
):
    """The invariant the buckets exist for, against real Postgres: a token sits in
    a band's alerted population exactly when its whole lifetime > that window."""
    tok = _mk_token(db_manager, seed_license.id, lifetime=lifetime, expires_in=timedelta(hours=1))
    cls = _token_classes(_snapshot(db_manager))[tok]
    assert (cls not in _BAND_EXCLUDES[band]) is in_band_population, cls


@pytest.mark.parametrize("days", [7, 30, 60])
def test_a_token_minted_through_the_real_route_is_excluded_from_its_own_band(
    auth_client_admin, full_seed_data, db_manager, days
):
    """An `expires_days=N` token is under N days from expiry the instant it is
    minted, so band N would page for it from birth (2026-10-31: an
    `expires_days=30` MCP token paged Within30Days critical for its whole life).
    Mint through the real route and assert band N excludes it while every
    narrower band still covers it."""
    license_id = str(full_seed_data["license"].id)
    response = auth_client_admin.post(
        f"/licenses/{license_id}/tokens?token_type=internal&expires_days={days}"
    )
    assert response.status_code == 201, response.text
    token_id = str(response.json()["token_id"])
    session = db_manager.get_session()
    try:
        lifetime = session.execute(
            text("SELECT expires_at - created_at FROM token WHERE id = :i"), {"i": token_id}
        ).scalar_one()
    finally:
        session.close()
    cls = _token_classes(_snapshot(db_manager))[token_id]
    for band in _BAND_EXCLUDES:
        assert (cls in _BAND_EXCLUDES[band]) is (band >= days), (band, days, lifetime, cls)


def test_a_token_just_under_a_week_is_lt7d(full_seed_data, db_manager):
    """Control for the tolerance: a 6d23h token is genuinely shorter than the
    7-day window and must NOT be pulled up into it. (Inserted directly: the mint
    route only takes whole days, so 6d23h cannot be minted through it.)"""
    tok = _mk_token(
        db_manager,
        full_seed_data["license"].id,
        lifetime=timedelta(days=6, hours=23),
        expires_in=timedelta(days=6),
    )
    assert _token_classes(_snapshot(db_manager))[tok] == "lt7d"


def test_a_short_lived_token_does_not_hide_a_long_lived_one_in_the_window(db_manager, seed_license):
    """The 2026-10-19 false page, and its control. A 24h token is `lt7d`, so the
    7-day band's population holds only the 30-day token that genuinely expires in
    3 days -- min() over that population is that token, not the 24h one."""
    lic = seed_license.id
    daily = _mk_token(db_manager, lic, lifetime=timedelta(hours=24), expires_in=timedelta(hours=1))
    monthly = _mk_token(db_manager, lic, lifetime=timedelta(days=30), expires_in=timedelta(days=3))
    snap = _snapshot(db_manager)
    in_7d_band = [
        r for r in snap.rows if r.kind == "token" and r.lifetime_class not in _BAND_EXCLUDES[7]
    ]
    assert [r.row_id for r in in_7d_band] == [monthly]
    assert _token_classes(snap)[daily] == "lt7d"


def test_short_tokens_beyond_their_cap_do_not_evict_standard_rows(
    db_manager, seed_license, monkeypatch
):
    """The crowding-out bug, at a size a test can afford.

    Five hourly tokens all expire BEFORE both long-lived ones, so under a single
    shared `ORDER BY expires_at ASC LIMIT 4` they would take every slot and the
    long-lived rows -- the only ones the 7-day alert reads -- would vanish. With
    per-class caps the short class is cut to its own 2 and BOTH standard rows
    survive."""
    monkeypatch.setattr(expiry_metrics, "MAX_SERIES_PER_KIND", 4)
    monkeypatch.setattr(expiry_metrics, "MAX_SHORT_TOKEN_SERIES", 2)
    lic = seed_license.id
    shorts = [
        _mk_token(db_manager, lic, lifetime=timedelta(minutes=60), expires_in=timedelta(minutes=m))
        for m in (10, 20, 30, 40, 50)
    ]
    standards = {
        _mk_token(db_manager, lic, lifetime=timedelta(days=365), expires_in=timedelta(days=d))
        for d in (3, 90)
    }

    snap = _snapshot(db_manager)
    classes = _token_classes(snap)

    assert {i for i, c in classes.items() if c == "standard"} == standards
    # The short class keeps its SOONEST rows, so min() within it is still right.
    assert [i for i, c in classes.items() if c == "short"] == shorts[:2]
    assert snap.truncated[("token", "short")] == 1
    assert snap.truncated[("token", "standard")] == 0  # 2 rows under a cap of 4


def test_an_expired_short_token_is_counted_under_short_not_standard(db_manager, seed_license):
    """The `LicenseTokenAlreadyExpired` false-fire, reproduced and cleared.

    An hourly token that lapsed 10 minutes ago and has not been swept yet is
    unrevoked and expired -- before `lifetime_class` that made the alerted count
    1. It must land under `short`, and the alerted `standard` series must still be
    PRESENT at 0. The revoked and year-long rows are the controls that stop this
    passing on a predicate that calls everything short."""
    lic = seed_license.id
    _mk_token(db_manager, lic, lifetime=timedelta(minutes=60), expires_in=timedelta(minutes=-10))
    _mk_token(
        db_manager,
        lic,
        lifetime=timedelta(minutes=60),
        expires_in=timedelta(minutes=-10),
        revoked=True,
    )
    _mk_token(
        db_manager,
        lic,
        lifetime=timedelta(minutes=15),
        expires_in=timedelta(hours=-3),
        revoked=True,
    )

    expired = _snapshot(db_manager).expired
    assert expired[("token", "false", "short")] == 1
    assert expired[("token", "true", "short")] == 2
    assert ("token", "false", "standard") in expired, "the alerted series went absent"
    assert expired[("token", "false", "standard")] == 0
    assert expired[("token", "true", "standard")] == 0

    # Positive control for the alert itself: a lapsed LONG-lived token still fires.
    _mk_token(db_manager, lic, lifetime=timedelta(days=365), expires_in=timedelta(days=-1))
    expired = _snapshot(db_manager).expired
    assert expired[("token", "false", "standard")] == 1
    assert expired[("token", "false", "short")] == 1
