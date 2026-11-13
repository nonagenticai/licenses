"""Budget coverage must be MEASURED against billing, not inferred from a stamp.

WHY THIS FILE EXISTS (measured in prod, 2026-08-09/10):

Migration 018 added ``license.metering_provisioned_at`` and, in the same guarded
branch, ran ``UPDATE license SET metering_provisioned_at = now()`` across the
whole table. The result, verified against the prod licences database:

    stamp (truncated to the minute)   licences
    2026-08-09 20:30+00                    205   <- the migration transaction
    2026-08-09 20:33+00                      1
    2026-08-09 20:40+00                      1
    2026-08-10 03:49+00                      1

So 205 of 208 stamps are BASELINED, not observed. The column answers "did we
ever stamp this row", not "does billing actually hold the config". Cross-diffing
the two prod databases showed what that costs:

    live licences on plans with token_budget_monthly IS NOT NULL     98
    of those, with an ACTIVE source='license_sync' budget in billing   4
    THE GAP                                                          94

All 98 were reported as provisioned by the column. The reconcile sweep keyed off
that column, so it scanned 0 and repaired 0, and would have done so forever.
(The quota half of the estate really is converged — 0 of the same 98 lack a
``request_quota_config`` row — which is precisely why a quota-only sweep looked
healthy while budgets were 96% uncovered.)

⚠️ THE PROSE THAT KEPT THE HOLE OPEN — corrected here because it was the actual
root cause, not a side note. ``_push_token_budget`` and the reconcile docstring
both claimed billing's budget POST is "NOT idempotent" and that re-pushing
"CREATES A DUPLICATE row", citing "191 rows for 186 distinct licences". Measured
in the prod billing database instead of asserted:

    source        rows   distinct license_id
    license_sync   185                   185
    manual          10                     8

Zero duplicate ``(tenant_id, license_id, period, source='license_sync')`` groups.
The duplication is entirely in ``source='manual'`` rows, which stack BY DESIGN
(top-ups). billing's ``upsert_license_sync_budget`` — present in the deployed
pod, not just its repo — is a genuine read-before-write upsert keyed on that
tuple. The "duplicate" hazard that justified excluding budgets from the sweep
was a misattribution of manual-source stacking to the license_sync path.

⚠️ AND the 185 license_sync rows are mostly SOFT-DELETED: 176 are
``active=False``, leaving 9 active, of which only 4 belong to a live
budget-owing licence. Coverage that ignored ``active`` would have called 185
licences covered. ``test_an_inactive_budget_does_not_cover_a_licence`` is the
guard for that.

That said, this repo does NOT get to depend on a remote invariant it cannot
enforce. The sweep reads billing's budget list FIRST and only creates when the
licence is genuinely absent. ``FakeBilling`` below therefore appends naively on
every POST — it deliberately models billing WITHOUT the upsert, so these tests
prove OUR read-before-write is what prevents duplication, independent of
billing's behaviour. A fake that mirrored the upsert could not fail.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import main
from models import Plan

RECONCILE_URL = "/licenses/admin/metering/reconcile"
TENANT = "01000000-0000-7000-8000-000000000001"
# billing's tenant-wide budget marker. NOT NULL — measured in prod, zero
# token_budgets rows have license_id IS NULL; three carry this zero UUID.
TENANT_WIDE_SENTINEL = "00000000-0000-0000-0000-000000000000"


class _Resp:
    """Minimal httpx.Response stand-in: the three members main.py touches."""

    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = repr(payload)

    def json(self) -> Any:
        return self._payload


class FakeBilling:
    """Stateful billing double: budgets are a LIST, quota configs are a DICT.

    The asymmetry is the real billing schema. ``request_quota_config`` has
    PRIMARY KEY (license_id) so a re-push overwrites; ``token_budgets`` has
    PRIMARY KEY (id) and no unique constraint, so a naive POST appends.

    Deliberately WITHOUT billing's ``upsert_license_sync_budget`` dedup — see the
    module docstring. If licences double-POSTs, ``budgets`` grows, and the
    idempotency assertions fail on a row count rather than on a mock call count.
    """

    def __init__(
        self,
        *,
        list_status: int = 200,
        budget_post_status: int = 201,
        list_fail_first: int = 0,
        quota_post_status: int = 201,
        quota_check_status: int = 200,
        quota_check_fail_first: int = 0,
    ) -> None:
        # Lets a test make the QUOTA push fail with a chosen code. Budgets
        # already had `budget_post_status`; quota was hardcoded to 201, so the
        # quota helper's failure arms were unreachable from this harness.
        self.quota_post_status = quota_post_status
        # The quota READ (GET /api/quota/check). Its failure arms are the whole
        # reason "unknown" exists as a state distinct from "absent".
        self.quota_check_status = quota_check_status
        self.quota_check_fail_first = quota_check_fail_first
        self.budgets: list[dict[str, Any]] = []
        self.quota_configs: dict[str, dict[str, Any]] = {}
        self.list_status = list_status
        self.budget_post_status = budget_post_status
        # Number of leading budget-list GETs to fail with 503 before answering
        # normally — models a transient blip rather than a hard outage.
        self.list_fail_first = list_fail_first
        self.get_urls: list[str] = []
        # Query params of every GET, in order. The quota check MUST NOT carry a
        # `quota_monthly` fallback — billing echoes that param back when it finds
        # no row, which would make "absent" read as "already equals the plan".
        self.get_params: list[dict[str, Any]] = []
        self.post_urls: list[str] = []

    # -- seeding helpers -------------------------------------------------
    def seed_budget(
        self,
        *,
        tenant_id: str,
        license_id: str | None,
        source: str = "license_sync",
        active: bool = True,
        budget_tokens: int = 10_000_000,
    ) -> None:
        self.budgets.append(
            {
                "id": f"budget-{len(self.budgets)}",
                "tenant_id": tenant_id,
                "license_id": license_id,
                "budget_tokens": budget_tokens,
                "period": "monthly",
                "active": active,
                "source": source,
            }
        )

    def seed_quota_config(self, *, tenant_id: str, license_id: str, quota_monthly: int) -> None:
        """A durable ``request_quota_config`` row billing already holds.

        Models the operator override: a value someone set in billing by hand,
        which the nightly sweep used to revert to the plan default.
        """
        self.quota_configs[str(license_id)] = {
            "tenant_id": tenant_id,
            "license_id": str(license_id),
            "quota_monthly": quota_monthly,
        }

    def quota_of(self, license_id: str) -> int | None:
        row = self.quota_configs.get(str(license_id))
        return None if row is None else int(row["quota_monthly"])

    def budgets_for(self, license_id: str) -> list[dict[str, Any]]:
        return [b for b in self.budgets if b["license_id"] == str(license_id)]

    # -- httpx.AsyncClient surface ---------------------------------------
    async def get(self, url: str, **kwargs: Any) -> _Resp:
        self.get_urls.append(url)
        params: dict[str, Any] = dict(kwargs.get("params") or {})
        self.get_params.append(params)
        if "/api/quota/check" in url:
            if self.quota_check_fail_first > 0:
                self.quota_check_fail_first -= 1
                return _Resp(503, {"detail": "transient"})
            if self.quota_check_status != 200:
                return _Resp(self.quota_check_status, {"detail": "boom"})
            lic = str(params.get("license_id"))
            row = self.quota_configs.get(lic)
            # billing's own rule (routers/quota.py::quota_check):
            #   limit = config.quota_monthly if config is not None else <param>
            # Reproduced faithfully, INCLUDING the fallback to the query param —
            # so a caller that wrongly sends the plan default as `quota_monthly`
            # gets it echoed back and reads an empty table as fully covered.
            # That is the trap; a fake that ignored the param could not catch it.
            limit = row["quota_monthly"] if row is not None else params.get("quota_monthly")
            return _Resp(
                200,
                {
                    "data": {
                        "license_id": lic,
                        "plan_id": str(params.get("plan_id")),
                        "quota_monthly": limit,
                        "used_this_month": 0,
                        "remaining": None,
                        "over_quota": False,
                    },
                    "meta": {},
                },
            )
        if "/api/budget/list/" in url:
            if self.list_fail_first > 0:
                self.list_fail_first -= 1
                return _Resp(503, {"detail": "transient"})
            if self.list_status != 200:
                return _Resp(self.list_status, {"detail": "boom"})
            tenant = url.rsplit("/", 1)[-1]
            rows = [b for b in self.budgets if b["tenant_id"] == tenant]
            return _Resp(200, {"data": rows, "meta": {"total": len(rows)}})
        return _Resp(404, {"detail": "no such route"})

    async def post(self, url: str, **kwargs: Any) -> _Resp:
        self.post_urls.append(url)
        body = kwargs.get("json") or {}
        if "/api/quota/config" in url:
            if self.quota_post_status >= 300:
                return _Resp(self.quota_post_status, {"detail": "rejected"})
            self.quota_configs[str(body.get("license_id"))] = dict(body)
            return _Resp(201, {"ok": True})
        if "/api/budget" in url:
            if self.budget_post_status >= 300:
                return _Resp(self.budget_post_status, {"detail": "rejected"})
            # NAIVE append. See the class docstring: no upsert here on purpose.
            self.seed_budget(
                tenant_id=str(body.get("tenant_id")),
                license_id=str(body.get("license_id")),
                budget_tokens=int(body.get("budget_tokens", 0)),
            )
            return _Resp(201, {"ok": True})
        return _Resp(404, {"detail": "no such route"})

    @property
    def budget_post_count(self) -> int:
        return len([u for u in self.post_urls if u.endswith("/api/budget")])

    @property
    def budget_list_get_count(self) -> int:
        return len([u for u in self.get_urls if "/api/budget/list/" in u])

    @property
    def quota_post_count(self) -> int:
        return len([u for u in self.post_urls if "/api/quota/config" in u])

    @property
    def quota_check_get_count(self) -> int:
        return len([u for u in self.get_urls if "/api/quota/check" in u])


def _no_sleep(monkeypatch) -> list[float]:
    """Replace ``main.asyncio.sleep`` with a recorder; returns the recorded delays.

    Lifted from ``tests/unit/test_offerings.py``, which already had it and states
    the reason this file needed it: *"the number of sleeps is a SECOND independent
    signal about the retry decision — a helper that stopped retrying but still
    slept would be caught here even if the call count somehow matched"*.

    That is not hypothetical here. On a 403/422 nothing is written to billing, so
    the ENTIRE harm being fixed is the wasted second. Counting requests alone
    leaves a live mutant: hoist the ``await asyncio.sleep(1)`` above the
    ``_is_retryable_status`` guard and the request count stays 1 while the full
    per-licence second — 2.8h across a 5000-licence sweep — comes straight back.
    Found by an adversarial reviewer, not by me.
    """
    import main

    slept: list[float] = []

    async def _record(seconds):
        slept.append(seconds)

    monkeypatch.setattr(main.asyncio, "sleep", _record)
    return slept


def _patch_billing(fake: FakeBilling):
    """Patch httpx.AsyncClient so main.py's ``async with`` yields ``fake``."""
    patcher = patch("httpx.AsyncClient")
    mock_cls = patcher.start()
    mock_cls.return_value.__aenter__ = AsyncMock(return_value=fake)
    mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
    return patcher


def _set_plan_allowances(db_manager, plan_id, *, quota, budget) -> None:
    session = db_manager.get_session()
    try:
        plan = session.query(Plan).filter(Plan.id == plan_id).first()
        assert plan is not None
        plan.quota_monthly = quota
        plan.token_budget_monthly = budget
        session.commit()
    finally:
        session.close()


def _set_stamps(db_manager, license_id, *, provisioned=..., verified=...) -> None:
    """Set either stamp column directly. Raw SQL: these columns are the operator
    surface, so the test drives them the way psql would."""
    sets, params = [], {"i": str(license_id)}
    if provisioned is not ...:
        sets.append("metering_provisioned_at = :p")
        params["p"] = provisioned
    if verified is not ...:
        sets.append("metering_verified_at = :v")
        params["v"] = verified
    if not sets:
        return
    session = db_manager.get_session()
    try:
        session.execute(text(f"UPDATE license SET {', '.join(sets)} WHERE id = :i"), params)
        session.commit()
    finally:
        session.close()


@pytest.fixture
def main_db(db_manager):
    original = main.db_manager
    main.db_manager = db_manager
    try:
        yield db_manager
    finally:
        main.db_manager = original


@pytest.fixture
def budget_only_gap(main_db, seed_license, seed_plan):
    """A live licence on a plan that owes billing a BUDGET and no quota.

    Isolating budget from quota matters: on the pre-fix code the sweep selected
    on ``plan.quota_monthly IS NOT NULL``, so a budget-only plan was invisible to
    it no matter what the stamps said.
    """
    _set_plan_allowances(main_db, seed_plan.id, quota=None, budget=10_000_000)
    return seed_license.id


@pytest.fixture
def quota_only_gap(main_db, seed_license, seed_plan):
    """A live licence on a plan that owes billing a QUOTA and no budget.

    The mirror of ``budget_only_gap``, and it was missing. Without it every test
    that exercised quota also set a budget, so the ``Plan.quota_monthly IS NOT
    NULL`` leg of the selection filter was never the reason anything was
    selected — deleting that leg killed no test.
    """
    _set_plan_allowances(main_db, seed_plan.id, quota=1_000_000, budget=None)
    return seed_license.id


class TestTheQuotaLegIsLoadBearing:
    """Both halves of the OR in the selection filter must be able to select alone."""

    def test_a_quota_only_plan_is_selected_and_repaired(
        self, auth_client_admin: TestClient, main_db, quota_only_gap
    ):
        """Drop ``Plan.quota_monthly.isnot(None)`` from the filter and this is
        the test that notices: with no budget on the plan, the budget leg cannot
        select this licence."""
        _set_stamps(main_db, quota_only_gap, provisioned=None)
        fake = FakeBilling()

        patcher = _patch_billing(fake)
        try:
            body = auth_client_admin.post(RECONCILE_URL).json()
        finally:
            patcher.stop()

        assert body["scanned"] == 1, body
        assert body["repaired"] == 1, body
        assert body["quotas_pushed"] == 1, body
        assert str(quota_only_gap) in fake.quota_configs, fake.quota_configs
        assert fake.budget_post_count == 0, (
            f"the plan owes no budget (NULL = unlimited): {fake.post_urls}"
        )

    def test_dry_run_does_not_push_quota(
        self, auth_client_admin: TestClient, main_db, quota_only_gap
    ):
        """⚠️ THE GAP THAT SURVIVED A MUTANT. Every other dry-run test used a
        budget-only plan, so ``and not dry_run`` on the QUOTA push was held in
        place by nothing: deleting it passed the entire suite.

        A dry run that POSTs a real quota config to billing — and seeds a Redis
        key there — while reporting ``dry_run: true`` is precisely the
        instrumentation-lies-about-writes failure this change exists to fix.
        """
        _set_stamps(main_db, quota_only_gap, provisioned=None)
        fake = FakeBilling()

        patcher = _patch_billing(fake)
        try:
            body = auth_client_admin.post(f"{RECONCILE_URL}?dry_run=true").json()
        finally:
            patcher.stop()

        assert body["dry_run"] is True, body
        assert body["scanned"] == 1, body
        assert body["quotas_pushed"] == 0, body
        assert fake.quota_configs == {}, (
            f"a dry run must not write a quota config to billing: {fake.quota_configs}"
        )
        assert fake.post_urls == [], f"a dry run must POST nothing at all: {fake.post_urls}"
        assert _verified_stamp(main_db, quota_only_gap) is None


class TestBlockAllAllowancesAreNotSkipped:
    """``0`` is a BLOCK-ALL allowance, not "no allowance" — the falsy-zero trap.

    billing has been burned by this twice and names it in its own
    ``sync_from_licenses`` comment; ``_provision_budget`` carries the same note.
    The guards here are ``is not None``, and these tests are what stops someone
    "simplifying" them to a truthiness check, which would silently skip a
    zero-allowance plan AND then stamp it verified — a block-all licence
    behaving as unlimited.
    """

    def test_a_zero_budget_is_still_pushed(
        self, auth_client_admin: TestClient, main_db, seed_license, seed_plan
    ):
        _set_plan_allowances(main_db, seed_plan.id, quota=None, budget=0)
        _set_stamps(main_db, seed_license.id, provisioned=None)
        fake = FakeBilling()

        patcher = _patch_billing(fake)
        try:
            body = auth_client_admin.post(RECONCILE_URL).json()
        finally:
            patcher.stop()

        assert body["scanned"] == 1, body
        assert body["budgets_created"] == 1, body
        created = fake.budgets_for(seed_license.id)
        assert len(created) == 1 and created[0]["budget_tokens"] == 0, created

    def test_a_zero_quota_is_still_pushed(
        self, auth_client_admin: TestClient, main_db, seed_license, seed_plan
    ):
        _set_plan_allowances(main_db, seed_plan.id, quota=0, budget=None)
        _set_stamps(main_db, seed_license.id, provisioned=None)
        fake = FakeBilling()

        patcher = _patch_billing(fake)
        try:
            body = auth_client_admin.post(RECONCILE_URL).json()
        finally:
            patcher.stop()

        assert body["scanned"] == 1, body
        assert body["quotas_pushed"] == 1, body
        assert fake.quota_configs[str(seed_license.id)]["quota_monthly"] == 0, fake.quota_configs


class TestTheSweepDoesNotFabricateOrDestroyEvidence:
    """The sweep must not claim work it did not do, nor erase how we know."""

    def test_an_already_covered_licence_is_converged_but_not_repaired(
        self, auth_client_admin: TestClient, main_db, budget_only_gap
    ):
        """``repaired`` is a CONVERGED count and includes licences billing
        already covered. That is defensible, but only if it is stated and
        counted — otherwise the first prod sweep reads as "repaired 98" when it
        created 96 and touched 2 not at all."""
        fake = FakeBilling()
        fake.seed_budget(tenant_id=TENANT, license_id=str(budget_only_gap))
        _set_stamps(main_db, budget_only_gap, provisioned=None)

        patcher = _patch_billing(fake)
        try:
            body = auth_client_admin.post(RECONCILE_URL).json()
        finally:
            patcher.stop()

        assert body["repaired"] == 1, body
        assert body["already_covered"] == 1, body
        assert body["budgets_created"] == 0, (
            f"nothing was created — the licence was already covered: {body}"
        )
        assert body["uncovered_budget_license_ids"] == [], body

    def test_verifying_does_not_overwrite_the_018_baseline_stamp(
        self, auth_client_admin: TestClient, main_db, budget_only_gap
    ):
        """🔴 REGRESSION GUARD. ``_mark_metering_verified`` briefly stamped
        ``metering_provisioned_at`` too.

        Two things were wrong with that. It recorded provisioning this service
        never performed (the already-covered path does no push at all), and it
        would have overwritten migration 018's baseline histogram — 205 of 208
        prod rows share one transaction timestamp, which is the only way anyone
        can still separate a baselined stamp from an observed one. One sweep over
        the estate would have destroyed that evidence permanently.

        The columns are allowed to disagree. The disagreement is the finding.
        """
        baseline = datetime(2026, 8, 9, 20, 30, tzinfo=UTC)
        _set_stamps(main_db, budget_only_gap, provisioned=baseline, verified=None)
        fake = FakeBilling()

        patcher = _patch_billing(fake)
        try:
            body = auth_client_admin.post(RECONCILE_URL).json()
        finally:
            patcher.stop()

        assert body["repaired"] == 1, body
        assert _verified_stamp(main_db, budget_only_gap) is not None, "should now be verified"
        assert _provisioned_stamp(main_db, budget_only_gap) == baseline, (
            "the 018 baseline stamp must survive a sweep untouched — it is the "
            "evidence that distinguishes baselined rows from observed ones"
        )


class TestCoverageIsMeasuredNotAssumed:
    """The headline defect: a stamp is not evidence that billing holds a budget."""

    def test_a_baselined_stamp_does_not_prove_billing_has_a_budget(
        self, auth_client_admin: TestClient, main_db, budget_only_gap
    ):
        """THE prod shape: stamped by migration 018's blanket UPDATE, yet billing
        holds nothing for this licence.

        Pre-fix this returns scanned=0/repaired=0 — the sweep believes the stamp
        and reports the estate clean. That is the false instrumentation.
        """
        _set_stamps(main_db, budget_only_gap, provisioned=datetime(2026, 8, 9, 20, 30, tzinfo=UTC))
        fake = FakeBilling()
        assert fake.budgets_for(budget_only_gap) == []

        patcher = _patch_billing(fake)
        try:
            response = auth_client_admin.post(RECONCILE_URL)
        finally:
            patcher.stop()

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["scanned"] == 1, f"a stamped-but-uncovered licence must be scanned: {body}"
        assert body["repaired"] == 1, body
        assert len(fake.budgets_for(budget_only_gap)) == 1, (
            f"the sweep must CREATE the missing budget in billing: {fake.budgets}"
        )

    def test_reconcile_repairs_a_budget_only_plan(
        self, auth_client_admin: TestClient, main_db, budget_only_gap
    ):
        """A plan owing only a budget is a gap. Pre-fix the sweep filtered on
        ``quota_monthly IS NOT NULL`` and never saw it."""
        _set_stamps(main_db, budget_only_gap, provisioned=None)
        fake = FakeBilling()

        patcher = _patch_billing(fake)
        try:
            body = auth_client_admin.post(RECONCILE_URL).json()
        finally:
            patcher.stop()

        assert body["scanned"] == 1, body
        assert body["repaired"] == 1, body
        assert len(fake.budgets_for(budget_only_gap)) == 1, fake.budgets
        created = fake.budgets_for(budget_only_gap)[0]
        assert created["budget_tokens"] == 10_000_000, created
        assert created["tenant_id"] == TENANT, created


class TestReadBeforeWrite:
    """The repair must ASK billing first, because token_budgets has no unique key."""

    def test_an_existing_budget_is_not_re_pushed(
        self, auth_client_admin: TestClient, main_db, budget_only_gap
    ):
        fake = FakeBilling()
        fake.seed_budget(tenant_id=TENANT, license_id=str(budget_only_gap))
        _set_stamps(main_db, budget_only_gap, provisioned=None)

        patcher = _patch_billing(fake)
        try:
            body = auth_client_admin.post(RECONCILE_URL).json()
        finally:
            patcher.stop()

        assert body["scanned"] == 1, body
        assert fake.budget_post_count == 0, (
            f"billing already holds a budget; re-posting would duplicate it: {fake.post_urls}"
        )
        assert len(fake.budgets_for(budget_only_gap)) == 1, fake.budgets

    def test_two_runs_do_not_duplicate_the_budget_row(
        self, auth_client_admin: TestClient, main_db, budget_only_gap
    ):
        """Idempotency proven on billing's ROW COUNT, not on a mock call count,
        and with ``revalidate`` on the second run so the sweep genuinely
        re-examines the licence instead of being short-circuited by its own
        stamp. The short-circuit is real idempotency too, but it cannot show
        that the read-before-write works."""
        _set_stamps(main_db, budget_only_gap, provisioned=None)
        fake = FakeBilling()

        patcher = _patch_billing(fake)
        try:
            first = auth_client_admin.post(RECONCILE_URL).json()
            rows_after_first = len(fake.budgets_for(budget_only_gap))
            second = auth_client_admin.post(f"{RECONCILE_URL}?revalidate=true").json()
        finally:
            patcher.stop()

        assert first["repaired"] == 1, first
        assert rows_after_first == 1, fake.budgets
        assert second["scanned"] == 1, f"revalidate must re-examine the licence: {second}"
        assert len(fake.budgets_for(budget_only_gap)) == 1, (
            f"second run duplicated the budget row: {fake.budgets}"
        )

    @pytest.mark.parametrize(
        "sentinel",
        [
            pytest.param(TENANT_WIDE_SENTINEL, id="zero-uuid-sentinel"),
            pytest.param(None, id="null-license-id"),
        ],
    )
    def test_a_tenant_wide_manual_budget_does_not_cover_a_licence(
        self, auth_client_admin: TestClient, main_db, budget_only_gap, sentinel
    ):
        """Measured live in prod: tenant 019de842-c92a-… holds exactly ONE budget
        row, ``source='manual'`` with 40M tokens, while 21 of its licences each
        owe a licence-keyed budget. Matching on tenant alone would call all 21
        covered.

        Both sentinel shapes are exercised because the prod convention is NOT the
        one you would guess: ``license_id IS NULL`` occurs zero times in
        ``billing.token_budgets``; the tenant-wide marker is the zero UUID
        ``00000000-0000-0000-0000-000000000000`` (3 such rows, all manual+active,
        40M / 90M / 1M). A guard written only against the NULL shape would pass
        while missing every real row.
        """
        fake = FakeBilling()
        fake.seed_budget(
            tenant_id=TENANT, license_id=sentinel, source="manual", budget_tokens=40_000_000
        )
        _set_stamps(main_db, budget_only_gap, provisioned=None)

        patcher = _patch_billing(fake)
        try:
            body = auth_client_admin.post(RECONCILE_URL).json()
        finally:
            patcher.stop()

        assert body["repaired"] == 1, body
        assert len(fake.budgets_for(budget_only_gap)) == 1, fake.budgets

    def test_a_manual_topup_on_the_same_licence_does_not_cover_it(
        self, auth_client_admin: TestClient, main_db, budget_only_gap
    ):
        """ADDED after a surviving mutant. Deleting the ``source == 'license_sync'``
        check from ``_fetch_license_sync_budget_ids`` killed no test, because the
        other source-related guards here all use a tenant-wide sentinel
        ``license_id`` that never equals the licence's own id. The uncovered case
        is a ``manual`` row keyed to the licence ITSELF.

        It is a real prod shape, not a hypothetical: ``billing.token_budgets``
        holds manual rows against specific licence ids, two of them ACTIVE
        (``069c6c62-…`` and ``069c6d9f-…``, tenant ``n8n-dev``, 100k each).

        A top-up must not read as the plan's entitlement. It is additive and
        transient, and billing's own upsert keys on ``source='license_sync'`` —
        so the licence's plan budget is still genuinely absent and still owed.
        """
        fake = FakeBilling()
        fake.seed_budget(
            tenant_id=TENANT,
            license_id=str(budget_only_gap),
            source="manual",
            budget_tokens=100_000,
        )
        _set_stamps(main_db, budget_only_gap, provisioned=None)

        patcher = _patch_billing(fake)
        try:
            body = auth_client_admin.post(RECONCILE_URL).json()
        finally:
            patcher.stop()

        assert str(budget_only_gap) in body["uncovered_budget_license_ids"], (
            f"a manual top-up is not the plan's license_sync budget: {body}"
        )
        assert body["budgets_created"] == 1, body
        sync_rows = [b for b in fake.budgets_for(budget_only_gap) if b["source"] == "license_sync"]
        assert len(sync_rows) == 1, fake.budgets

    def test_a_budget_held_under_a_different_tenant_key_is_not_seen(
        self, auth_client_admin: TestClient, main_db, budget_only_gap
    ):
        """PINS A KNOWN LIMITATION rather than pretending it away.

        Coverage is keyed on (tenant, licence) because tenant-scoped list is the
        only budget read billing offers — and billing's own
        ``upsert_license_sync_budget`` keys on the same tuple, so this is its
        semantics too, not an approximation of them.

        The consequence is real and measured: two LIVE prod licences carry a
        stale legacy tenant on their billing row — ``a005bf37-…`` is ``portal``
        in billing but ``019de842-c92b-7564-…`` in licences, and ``9d96dc27-…``
        is ``n8n-test`` vs ``019de842-c92b-752e-…``. A sweep keyed on the
        licences-side tenant cannot see those budgets and will create a second
        active row, stacking the entitlement.

        That is a billing-side data defect (stale tenant_id), not something this
        repo can fix from here, so it is documented on the endpoint and pinned
        here. If someone later teaches the sweep to match on licence id alone,
        this test SHOULD fail — and its message says what to do about it.
        """
        fake = FakeBilling()
        fake.seed_budget(tenant_id="portal", license_id=str(budget_only_gap))
        _set_stamps(main_db, budget_only_gap, provisioned=None)

        patcher = _patch_billing(fake)
        try:
            body = auth_client_admin.post(RECONCILE_URL).json()
        finally:
            patcher.stop()

        assert str(budget_only_gap) in body["uncovered_budget_license_ids"], (
            "coverage is (tenant, licence)-keyed; if this now passes, the sweep "
            "matches on licence alone and the endpoint's KNOWN LIMITATION note "
            "plus the prod repair command in the PR body must be retired: "
            f"{body}"
        )
        assert len(fake.budgets_for(budget_only_gap)) == 2, (
            f"the stale-tenant row plus the new one — the stacking hazard: {fake.budgets}"
        )

    def test_an_inactive_budget_does_not_cover_a_licence(
        self, auth_client_admin: TestClient, main_db, budget_only_gap
    ):
        """billing soft-deletes with ``active=False`` (see
        ``deactivate_orphan_license_sync_budgets``). An inactive row enforces
        nothing, so it must not read as coverage."""
        fake = FakeBilling()
        fake.seed_budget(tenant_id=TENANT, license_id=str(budget_only_gap), active=False)
        _set_stamps(main_db, budget_only_gap, provisioned=None)

        patcher = _patch_billing(fake)
        try:
            body = auth_client_admin.post(RECONCILE_URL).json()
        finally:
            patcher.stop()

        assert body["repaired"] == 1, body
        assert fake.budget_post_count == 1, fake.post_urls

    def test_a_transient_billing_blip_is_retried_not_treated_as_unknown(
        self, auth_client_admin: TestClient, main_db, budget_only_gap
    ):
        """The budget read is per-TENANT, so giving up on the first error does
        not cost one licence — it sends every licence under that tenant to
        ``unknown`` and repairs none of them (21 licences for the largest prod
        tenant). Both push helpers already retry once; this read now matches
        them, and this test is what holds that retry in place.

        Distinct from ``test_unknown_billing_state_fails_closed``: there billing
        is down for good and failing closed is correct. Here it answers on the
        second attempt.
        """
        fake = FakeBilling(list_fail_first=1)
        _set_stamps(main_db, budget_only_gap, provisioned=None)

        patcher = _patch_billing(fake)
        try:
            body = auth_client_admin.post(RECONCILE_URL).json()
        finally:
            patcher.stop()

        assert body["unknown_budget_license_ids"] == [], (
            f"the retry should have recovered this tenant: {body}"
        )
        assert body["repaired"] == 1, body
        assert body["budgets_created"] == 1, body
        assert len(fake.budgets_for(budget_only_gap)) == 1, fake.budgets

    def test_unknown_billing_state_fails_closed(
        self, auth_client_admin: TestClient, main_db, budget_only_gap
    ):
        """If billing will not say what it holds, creating a budget might
        duplicate one. Push nothing, stamp nothing, count it failed."""
        fake = FakeBilling(list_status=503)
        _set_stamps(main_db, budget_only_gap, provisioned=None)

        patcher = _patch_billing(fake)
        try:
            body = auth_client_admin.post(RECONCILE_URL).json()
        finally:
            patcher.stop()

        assert body["scanned"] == 1, body
        assert body["repaired"] == 0, body
        assert body["failed"] == 1, body
        assert fake.budget_post_count == 0, fake.post_urls
        assert _verified_stamp(main_db, budget_only_gap) is None


class TestTheCoverageQueryDiscriminates:
    """A coverage report that only ever runs against a clean corpus proves nothing."""

    def test_dry_run_reports_the_uncovered_licence_and_writes_nothing(
        self, auth_client_admin: TestClient, main_db, budget_only_gap
    ):
        _set_stamps(main_db, budget_only_gap, provisioned=None)
        fake = FakeBilling()

        patcher = _patch_billing(fake)
        try:
            body = auth_client_admin.post(f"{RECONCILE_URL}?dry_run=true").json()
        finally:
            patcher.stop()

        assert str(budget_only_gap) in body["uncovered_budget_license_ids"], body
        assert body["repaired"] == 0, body
        assert fake.budget_post_count == 0, fake.post_urls
        assert _verified_stamp(main_db, budget_only_gap) is None, (
            "a dry run must not stamp the licence as verified"
        )

    def test_dry_run_reports_nothing_against_a_covered_licence(
        self, auth_client_admin: TestClient, main_db, budget_only_gap
    ):
        """The other half of the discriminating pair: SAME licence, SAME sweep,
        but billing holds its budget — so the report must come back empty."""
        _set_stamps(main_db, budget_only_gap, provisioned=None)
        fake = FakeBilling()
        fake.seed_budget(tenant_id=TENANT, license_id=str(budget_only_gap))

        patcher = _patch_billing(fake)
        try:
            body = auth_client_admin.post(f"{RECONCILE_URL}?dry_run=true").json()
        finally:
            patcher.stop()

        assert body["uncovered_budget_license_ids"] == [], body

    def test_a_plan_owing_no_budget_is_never_reported_uncovered(
        self, auth_client_admin: TestClient, main_db, seed_license, seed_plan
    ):
        """NULL budget = unlimited: billing owes it nothing."""
        _set_plan_allowances(main_db, seed_plan.id, quota=None, budget=None)
        _set_stamps(main_db, seed_license.id, provisioned=None)
        fake = FakeBilling()

        patcher = _patch_billing(fake)
        try:
            body = auth_client_admin.post(f"{RECONCILE_URL}?dry_run=true").json()
        finally:
            patcher.stop()

        assert body["scanned"] == 0, body
        assert body["uncovered_budget_license_ids"] == [], body


def _verified_stamp(db_manager, license_id):
    return _read_column(db_manager, license_id, "metering_verified_at")


def _provisioned_stamp(db_manager, license_id):
    return _read_column(db_manager, license_id, "metering_provisioned_at")


def _read_column(db_manager, license_id, column: str):
    """Raw SQL on purpose: these columns are the operator surface, so the tests
    read them the way psql would rather than through the ORM."""
    session = db_manager.get_session()
    try:
        row = session.execute(
            text(f"SELECT {column} FROM license WHERE id = :i"),  # noqa: S608 - fixed literals
            {"i": str(license_id)},
        ).first()
        return row[0] if row else None
    finally:
        session.close()


class TestABillingVerdictIsNotRetried:
    """A 4xx from billing is an ANSWER; re-sending the same body cannot change it.

    ``_is_retryable_status`` (``main.py``) exists for exactly this, and its
    docstring names the live incident that produced it: the offering-provisioning
    helpers used to sleep a second and re-POST on ANY non-2xx, so billing #74
    gating two money-config writes behind a ``pricing-writer`` role the licences
    SA does not hold cost every fr "Provision" click two 403s and a wasted second.

    That fix was wired into the two OFFERING helpers only. The two METERING
    helpers -- ``_push_token_budget`` and ``_push_quota_config`` -- were left on
    the retry-anything shape, and they are the ones the reconcile sweep calls
    INLINE PER LICENCE with a route ceiling of ``le=5000``. A billing 403 or 422
    therefore cost 2 x (one wasted second + one doomed POST) for every licence in
    the sweep -- hours inside a single HTTP request, for a verdict that was known
    on the first response.

    ``_push_quota_config`` even carried a comment describing the defect in place:
    "a deterministic 422 that burns both retries". It described it and did not
    fix it.

    WHAT WAS ALREADY COVERED, AND WHY IT WAS NOT ENOUGH.
    ``test_a_transient_billing_blip_is_retried_not_treated_as_unknown`` pins that
    a retry HAPPENS. Nothing pinned that a verdict is answered ONCE -- the two
    are independent, and only the second one is about POST count.
    """

    def test_a_403_on_the_budget_push_is_posted_once(
        self, auth_client_admin: TestClient, main_db, budget_only_gap, monkeypatch
    ):
        """RED on the pre-fix tree: it posts twice, a second apart."""
        fake = FakeBilling(budget_post_status=403)
        _set_stamps(main_db, budget_only_gap, provisioned=None)
        slept = _no_sleep(monkeypatch)

        patcher = _patch_billing(fake)
        try:
            body = auth_client_admin.post(RECONCILE_URL).json()
        finally:
            patcher.stop()

        # VACUITY FLOOR first: the sweep must actually have reached the push.
        # Without this, a scan that selected nothing would satisfy "posted at
        # most once" trivially.
        assert fake.budget_post_count >= 1, (
            f"the sweep never pushed a budget, so the count below proves nothing: {body}"
        )
        assert fake.budget_post_count == 1, (
            f"a 403 is a verdict, not a blip — it was re-sent {fake.budget_post_count} "
            f"times: {fake.post_urls}"
        )
        # THE assertion that matters: nothing is written on a 403, so the wasted
        # second IS the harm. Without this, hoisting the sleep above the guard
        # keeps the count at 1 and restores the whole cost.
        assert slept == [], f"slept {slept} on a non-retryable status"

    def test_a_422_on_the_quota_push_is_posted_once(
        self, auth_client_admin: TestClient, main_db, quota_only_gap, monkeypatch
    ):
        """The shape the in-place comment described and did not fix."""
        fake = FakeBilling(quota_post_status=422)
        _set_stamps(main_db, quota_only_gap, provisioned=None)
        slept = _no_sleep(monkeypatch)

        patcher = _patch_billing(fake)
        try:
            body = auth_client_admin.post(RECONCILE_URL).json()
        finally:
            patcher.stop()

        assert fake.quota_post_count >= 1, (
            f"the sweep never pushed a quota config, so the count proves nothing: {body}"
        )
        assert fake.quota_post_count == 1, (
            f"a 422 is a verdict about the BODY — the identical body was re-sent "
            f"{fake.quota_post_count} times: {fake.post_urls}"
        )
        assert slept == [], f"slept {slept} on a non-retryable status"

    def test_a_503_on_the_budget_push_is_still_retried(
        self, auth_client_admin: TestClient, main_db, budget_only_gap, monkeypatch
    ):
        """The control, and the reason this change is a narrowing not a removal.

        Without it, deleting the retry entirely would pass both tests above. A
        503 is billing rolling out, which is exactly what the retry is for.
        """
        fake = FakeBilling(budget_post_status=503)
        _set_stamps(main_db, budget_only_gap, provisioned=None)
        slept = _no_sleep(monkeypatch)

        patcher = _patch_billing(fake)
        try:
            auth_client_admin.post(RECONCILE_URL).json()
        finally:
            patcher.stop()

        assert fake.budget_post_count == 2, (
            f"a 503 is retryable and must still be re-sent once, got "
            f"{fake.budget_post_count} POST(s): {fake.post_urls}"
        )
        # The converse of the two assertions above: a retryable status MUST sleep,
        # so "slept == []" cannot become a blanket property of the whole module.
        assert slept == [1], f"a retryable status must back off exactly once, got {slept}"

    def test_a_403_on_the_budget_list_read_is_fetched_once(
        self, auth_client_admin: TestClient, main_db, budget_only_gap, monkeypatch
    ):
        """The third loop, promoted in the same change.

        `_fetch_license_sync_budget_ids` had the identical defect and was the
        remaining member of the scoped-out set in
        `tests/unit/test_offerings.py::TestKnownUnfixedRetryLoops`. Its failure
        mode is milder — a failed read means "unknown" and the sweep correctly
        creates nothing — but the read is PER TENANT, so a 403 cost one wasted
        second per tenant for an answer that could not change.

        The retry itself is still proven by
        `test_a_transient_billing_blip_is_retried_not_treated_as_unknown`, which
        uses a 503 and must keep passing.
        """
        fake = FakeBilling(list_status=403)
        _set_stamps(main_db, budget_only_gap, provisioned=None)
        slept = _no_sleep(monkeypatch)

        patcher = _patch_billing(fake)
        try:
            body = auth_client_admin.post(RECONCILE_URL).json()
        finally:
            patcher.stop()

        assert fake.budget_list_get_count >= 1, (
            f"the sweep never read the budget list, so the count proves nothing: {body}"
        )
        assert fake.budget_list_get_count == 1, (
            f"a 403 on the list read is a verdict — it was re-read "
            f"{fake.budget_list_get_count} times: {fake.get_urls}"
        )
        assert slept == [], f"slept {slept} on a non-retryable status"


class TestAnOperatorQuotaSurvivesTheSweep:
    """The quota leg must be read-before-write, exactly like the budget leg.

    ⚠️ WHAT THIS CLASS IS FOR, measured in the prod billing database on
    2026-08-24. ``_push_quota_config`` was called UNCONDITIONALLY for every
    licence on a quota-owing plan, guarded only by ``quota_monthly is not None
    and not dry_run``. Since the push overwrites (``upsert_request_quota_config``
    finds the row by its ``license_id`` primary key and sets ``quota_monthly``),
    every quota an operator tuned by hand was reverted to the plan default by the
    next ``41 3 * * *`` CronJob run — at HTTP 201, counted in ``quotas_pushed``
    as work done. The table showed it plainly: 181 rows, 155 of them rewritten
    inside 25 hours, newest ``updated_at`` 2026-08-24 03:41:56, against exactly
    155 active licences. Every row, every night.

    The docstring that let it stand said the push is "SAFE to re-push" because
    ``request_quota_config`` has ``PRIMARY KEY (license_id)``, "so repeated
    pushes leave at most one row per licence". True, and about the wrong thing:
    the key bounds the ROW COUNT and the damage is to the VALUE.

    ``FakeBilling`` above now serves ``GET /api/quota/check`` and reproduces
    billing's fallback-to-query-param rule verbatim, so these tests can fail for
    the real reasons rather than for the fake's convenience.
    """

    def test_an_existing_differing_quota_is_not_overwritten(
        self, auth_client_admin: TestClient, main_db, quota_only_gap
    ):
        """THE DEFECT, stated as a test. Fails on the pre-fix code, which POSTs
        the plan default over the operator's 42 and reports success."""
        _set_stamps(main_db, quota_only_gap, provisioned=None)
        fake = FakeBilling()
        fake.seed_quota_config(tenant_id=TENANT, license_id=str(quota_only_gap), quota_monthly=42)

        patcher = _patch_billing(fake)
        try:
            body = auth_client_admin.post(RECONCILE_URL).json()
        finally:
            patcher.stop()

        assert fake.quota_of(quota_only_gap) == 42, (
            "the operator's quota was overwritten with the plan default — this is the "
            "nightly revert, reproduced"
        )
        assert fake.quota_post_count == 0, f"nothing may be POSTed here: {fake.post_urls}"
        assert body["quotas_pushed"] == 0, body
        assert body["quota_drift_license_ids"] == [str(quota_only_gap)], body
        # Divergence is NOT a failure: a deliberate override must still converge.
        assert body["repaired"] == 1, body
        assert body["failed"] == 0, body

    def test_an_absent_quota_is_pushed(
        self, auth_client_admin: TestClient, main_db, quota_only_gap
    ):
        """THE NEGATIVE TWIN of the guard above. A read-before-write that never
        writes is not a fix, it is an outage — this is the test that would catch
        a guard hardened into "never push"."""
        _set_stamps(main_db, quota_only_gap, provisioned=None)
        fake = FakeBilling()

        patcher = _patch_billing(fake)
        try:
            body = auth_client_admin.post(RECONCILE_URL).json()
        finally:
            patcher.stop()

        assert fake.quota_of(quota_only_gap) == 1_000_000, fake.quota_configs
        assert fake.quota_post_count == 1, fake.post_urls
        assert body["quotas_pushed"] == 1, body
        assert body["uncovered_quota_license_ids"] == [str(quota_only_gap)], body
        assert body["quota_drift_license_ids"] == [], body
        assert body["repaired"] == 1, body

    def test_force_plan_quota_does_overwrite(
        self, auth_client_admin: TestClient, main_db, quota_only_gap
    ):
        """THE SECOND NEGATIVE TWIN — the one that lets the guard fail.

        Without it, deleting the ``force_plan_quota`` branch entirely (or wiring
        the flag to nothing) breaks no test, and an opt-in nobody can prove works
        is indistinguishable from a dead flag.
        """
        _set_stamps(main_db, quota_only_gap, provisioned=None)
        fake = FakeBilling()
        fake.seed_quota_config(tenant_id=TENANT, license_id=str(quota_only_gap), quota_monthly=42)

        patcher = _patch_billing(fake)
        try:
            body = auth_client_admin.post(f"{RECONCILE_URL}?force_plan_quota=true").json()
        finally:
            patcher.stop()

        assert fake.quota_of(quota_only_gap) == 1_000_000, (
            "the explicit operator opt-in did NOT propagate the plan value"
        )
        assert fake.quota_post_count == 1, fake.post_urls
        # Counted apart from quotas_pushed on purpose: this number is overrides
        # DESTROYED, and it must never hide inside a "work done" total.
        assert body["quotas_overwritten"] == 1, body
        assert body["quotas_pushed"] == 0, body

    def test_force_is_off_by_default_on_the_scheduled_path(self, main_db, quota_only_gap):
        """The runner's default, not the route's. The CronJob calls the runner
        directly, so a default that lived only in the ``Query()`` would leave the
        scheduled path overwriting exactly as before."""
        import inspect

        assert (
            inspect.signature(main.run_metering_reconcile).parameters["force_plan_quota"].default
            is False
        )

    def test_a_matching_quota_is_not_repushed(
        self, auth_client_admin: TestClient, main_db, quota_only_gap
    ):
        """A row already equal to the plan default is left alone — no write, and
        not reported as drift."""
        _set_stamps(main_db, quota_only_gap, provisioned=None)
        fake = FakeBilling()
        fake.seed_quota_config(
            tenant_id=TENANT, license_id=str(quota_only_gap), quota_monthly=1_000_000
        )

        patcher = _patch_billing(fake)
        try:
            body = auth_client_admin.post(RECONCILE_URL).json()
        finally:
            patcher.stop()

        assert fake.quota_post_count == 0, fake.post_urls
        assert body["quotas_already_configured"] == 1, body
        assert body["quota_drift_license_ids"] == [], body
        assert body["repaired"] == 1, body

    def test_an_unreadable_quota_pushes_nothing_and_fails_closed(
        self, auth_client_admin: TestClient, main_db, quota_only_gap, monkeypatch
    ):
        """UNKNOWN IS NOT ABSENT. billing 404s a config row whose tenant differs
        from the ``X-Tenant-Id`` we send (the stale-legacy-tenant shape), and a
        row we may not read is still a row. Pushing over it is the exact harm.
        """
        _no_sleep(monkeypatch)
        _set_stamps(main_db, quota_only_gap, provisioned=None)
        fake = FakeBilling(quota_check_status=404)

        patcher = _patch_billing(fake)
        try:
            body = auth_client_admin.post(RECONCILE_URL).json()
        finally:
            patcher.stop()

        assert fake.quota_post_count == 0, f"nothing may be pushed on unknown: {fake.post_urls}"
        assert body["unknown_quota_license_ids"] == [str(quota_only_gap)], body
        assert body["quotas_pushed"] == 0, body
        assert body["failed"] == 1, body
        assert body["repaired"] == 0, body

    def test_a_dry_run_reads_the_quota_and_still_writes_nothing(
        self, auth_client_admin: TestClient, main_db, quota_only_gap
    ):
        """Measuring is what a dry run is FOR: the read happens, the write does
        not, and the divergence is reported."""
        _set_stamps(main_db, quota_only_gap, provisioned=None)
        fake = FakeBilling()
        fake.seed_quota_config(tenant_id=TENANT, license_id=str(quota_only_gap), quota_monthly=42)

        patcher = _patch_billing(fake)
        try:
            body = auth_client_admin.post(f"{RECONCILE_URL}?dry_run=true").json()
        finally:
            patcher.stop()

        assert fake.quota_check_get_count == 1, fake.get_urls
        assert fake.quota_post_count == 0, fake.post_urls
        assert fake.quota_of(quota_only_gap) == 42
        assert body["quota_drift_license_ids"] == [str(quota_only_gap)], body

    def test_the_plan_default_is_never_sent_as_the_check_fallback(
        self, auth_client_admin: TestClient, main_db, quota_only_gap
    ):
        """⚠️ THE TRAP THAT WOULD MAKE THE READ USELESS.

        billing's ``quota_check`` falls back to the ``quota_monthly`` QUERY PARAM
        when it finds no durable row. Send the plan default there and billing
        echoes it straight back, so "absent" becomes indistinguishable from
        "present and already equal to the plan" — the sweep would report the
        whole estate configured and create nothing, forever. The fake reproduces
        that fallback, so this assertion is about a real behaviour, not a shape.
        """
        _set_stamps(main_db, quota_only_gap, provisioned=None)
        fake = FakeBilling()

        patcher = _patch_billing(fake)
        try:
            auth_client_admin.post(RECONCILE_URL)
        finally:
            patcher.stop()

        checks = [
            p
            for u, p in zip(fake.get_urls, fake.get_params, strict=True)
            if "/api/quota/check" in u
        ]
        assert checks, fake.get_urls
        for params in checks:
            assert "quota_monthly" not in params, (
                f"the plan default was sent as billing's fallback param: {params}"
            )
            assert params.get("plan_id"), f"plan_id is mandatory on this route: {params}"


class TestTheQuotaPushCarriesTheApp:
    """The quota push must name the APP, or billing's resolver cannot see the row.

    ⚠️ WHY THIS EXISTS, measured in the prod billing database on 2026-08-28.
    ``license_resolver._build_map`` selects attribution candidates with
    ``where(RequestQuotaConfig.app_id.is_not(None))``. A ``request_quota_config``
    row written with a NULL ``app_id`` is therefore **not a candidate** and can
    never resolve, so every cost event billed to that licence's tenant stays
    unattributed — permanently, because billing resolves only at flush time and
    nothing re-resolves a row already written NULL.

    26 of 189 rows were in that state. The cause was a split between two writers:
    ``POST /api/budget/sync-from-licenses`` sets ``app_id`` but nothing schedules
    it, while THIS push — the one the ``41 3 * * *`` CronJob actually makes —
    carried only ``tenant_id``/``license_id``/``quota_monthly``. The scheduled
    writer structurally could not carry the one field the resolver needs.

    The reconcile repairs a MISSING quota row, so a repair without the app closes
    the gap it was called for and reopens it one field lower down.
    """

    def test_the_reconcile_push_names_the_app(
        self, auth_client_admin: TestClient, main_db, seed_license, seed_plan
    ):
        """RED on the pre-fix tree: the body has no ``app_id`` key at all."""
        _set_plan_allowances(main_db, seed_plan.id, quota=500, budget=None)
        _set_stamps(main_db, seed_license.id, provisioned=None)
        fake = FakeBilling()

        patcher = _patch_billing(fake)
        try:
            body = auth_client_admin.post(RECONCILE_URL).json()
        finally:
            patcher.stop()

        assert body["quotas_pushed"] == 1, body
        pushed = fake.quota_configs[str(seed_license.id)]
        assert "app_id" in pushed, f"the push carries no app at all: {pushed}"
        assert pushed["app_id"] is not None, (
            "app_id is None — billing's resolver ignores this row and the licence "
            f"can never be attributed: {pushed}"
        )
        assert str(pushed["app_id"]) == str(seed_license.app_id), pushed

    def test_the_pushed_app_is_the_licence_own_app_not_a_constant(
        self, auth_client_admin: TestClient, main_db, seed_license, seed_plan
    ):
        """The control that makes the test above mean something.

        An implementation that hardcoded any non-null app id would satisfy
        "app_id is not None" while attributing every licence in the estate to one
        app — a worse defect than the NULL it replaced, because it resolves
        CONFIDENTLY and wrongly. Billing groups candidates by
        ``(tenant_id, app_name)``, so a constant app collapses distinct licences
        into one bucket and the resolver then abstains on ambiguity or, worse,
        picks the survivor.
        """
        _set_plan_allowances(main_db, seed_plan.id, quota=500, budget=None)
        _set_stamps(main_db, seed_license.id, provisioned=None)
        fake = FakeBilling()

        patcher = _patch_billing(fake)
        try:
            auth_client_admin.post(RECONCILE_URL).json()
        finally:
            patcher.stop()

        pushed = fake.quota_configs[str(seed_license.id)]
        assert str(pushed["app_id"]) != "00000000-0000-0000-0000-000000000000", pushed
        assert pushed.get("app_name"), f"app_name is half the resolver's grouping key: {pushed}"


BILLING_SENTINEL_TENANT = "00000000-0000-0000-0000-000000000001"


def _move_licence_to_tenant(db_manager, license_id, tenant_id: str) -> None:
    """Re-home a seeded licence. The tenant row must exist first (FK)."""
    db_manager.ensure_tenant(tenant_id, name="billing-sentinel", owner_key="platform")
    session = db_manager.get_session()
    try:
        session.execute(
            text("UPDATE license SET tenant_id = :t WHERE id = :i"),
            {"t": tenant_id, "i": str(license_id)},
        )
        session.commit()
    finally:
        session.close()


class TestTheBillingSentinelTenantIsSkippedNotFailed:
    """#234: billing refuses budgets for its sentinel tenant BY DESIGN (422
    "not a registered tenant"). The sweep must skip the budget leg for exactly
    that tenant — and ONLY that tenant, which the negative control pins.

    ``budget_post_status=422`` in both tests models billing's real refusal, so
    a regression that pushes anyway turns the sentinel test red rather than
    passing on a fake that would have accepted the write.
    """

    def test_a_sentinel_tenant_budget_is_skipped_and_the_licence_converges(
        self, auth_client_admin: TestClient, main_db, seed_license, seed_plan, monkeypatch
    ):
        _no_sleep(monkeypatch)
        assert BILLING_SENTINEL_TENANT in main.BILLING_SENTINEL_TENANT_IDS
        _set_plan_allowances(main_db, seed_plan.id, quota=1_000_000, budget=10_000_000)
        _move_licence_to_tenant(main_db, seed_license.id, BILLING_SENTINEL_TENANT)
        _set_stamps(main_db, seed_license.id, provisioned=None)
        fake = FakeBilling(budget_post_status=422)

        patcher = _patch_billing(fake)
        try:
            body = auth_client_admin.post(RECONCILE_URL).json()
        finally:
            patcher.stop()

        assert body["scanned"] == 1, body
        assert body["budgets_skipped_sentinel"] == 1, body
        assert body["failed"] == 0 and body["failed_license_ids"] == [], body
        assert body["repaired"] == 1, body
        assert body["budgets_created"] == 0, body
        assert body["uncovered_budget_license_ids"] == [], body
        assert fake.budget_post_count == 0, fake.post_urls
        assert fake.budget_list_get_count == 0, fake.get_urls
        # Quota handling is unchanged for the sentinel: billing accepts quotas.
        assert body["quotas_pushed"] == 1, body
        assert str(seed_license.id) in fake.quota_configs, fake.quota_configs
        assert _verified_stamp(main_db, seed_license.id) is not None

    def test_a_non_sentinel_budget_refusal_is_still_a_failure(
        self, auth_client_admin: TestClient, main_db, budget_only_gap, monkeypatch
    ):
        """Negative control: the skip is keyed on the tenant id, never on the
        422. A real tenant whose budget push is refused must still fail."""
        _no_sleep(monkeypatch)
        _set_stamps(main_db, budget_only_gap, provisioned=None)
        fake = FakeBilling(budget_post_status=422)

        patcher = _patch_billing(fake)
        try:
            body = auth_client_admin.post(RECONCILE_URL).json()
        finally:
            patcher.stop()

        assert body["scanned"] == 1, body
        assert body["budgets_skipped_sentinel"] == 0, body
        assert body["failed"] == 1, body
        assert body["failed_license_ids"] == [str(budget_only_gap)], body
        assert fake.budget_post_count >= 1, fake.post_urls
        assert _verified_stamp(main_db, budget_only_gap) is None
