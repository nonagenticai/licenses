"""Per-licence budget / quota overrides are DURABLE (migration 041, licenses #239).

THE AUDIT FINDING this file pins (221b895): ``provision_tenant_bundle.py --token-budget
/ --quota`` handed the override to the one-shot billing push and stored it nowhere. If
billing rejected that push (exit 6), the documented repair — the nightly
``licenses-metering-reconcile`` sweep (``main.run_metering_reconcile``) — re-pushed the
PLAN's values, silently RAISING the tenant's pooled spend cap the override existed to
hold. Now the override lives on ``license.token_budget_override`` /
``license.quota_override``, and every push and every drift comparison goes through
``models.effective_metering_values``.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

import main
from models import effective_metering, effective_metering_values
from tests.conftest import SEED_TENANT_ID
from tests.unit.test_budget_coverage import FakeBilling, _no_sleep, _patch_billing
from tests.unit.test_bundle_licence import BUNDLE_KEY, _seed_bundle, provision_tenant_bundle

PLAN_BUDGET = 7000
PLAN_QUOTA = 70


def _overrides(db, license_id: str) -> tuple[Any, Any]:
    with db.engine.connect() as conn:
        row = conn.execute(
            text("SELECT token_budget_override, quota_override FROM license WHERE id = :i"),
            {"i": license_id},
        ).one()
    return row[0], row[1]


def _kw(spy: Any) -> dict[str, Any]:
    call = spy.await_args
    assert call is not None, "the push was never awaited"
    return call.kwargs


def _reconcile(**kw: Any) -> Any:
    return asyncio.run(main.run_metering_reconcile(actor="test-041", **kw))


class TestEffectiveMeteringRule:
    def test_override_wins_and_null_falls_back_to_the_plan(self):
        assert effective_metering_values(None, None, 50, 5) == (50, 5)
        assert effective_metering_values(9, None, 50, 5) == (9, 5)
        assert effective_metering_values(None, 3, 50, 5) == (50, 3)

    def test_zero_is_a_value_not_unset(self):
        # 0 = BLOCK-ALL; collapsing it into "unset" would hand back the plan's cap.
        assert effective_metering_values(0, 0, 50, 5) == (0, 0)

    def test_an_override_on_an_unlimited_plan_owes_that_leg(self):
        assert effective_metering_values(9, None, None, None) == (9, None)

    def test_orm_form_delegates(self):
        class _L:
            token_budget_override, quota_override = 1, None

        class _P:
            token_budget_monthly, quota_monthly = 50, 5

        assert effective_metering(_L(), _P()) == (1, 5)


class TestTheOverrideSurvivesABillingOutage:
    """The audit's scenario end to end: insert with override, billing 5xx (exit 6),
    then the reconcile sweep repairs with the OVERRIDE, never the plan value."""

    @pytest.fixture(autouse=True)
    def _setup(self, db_manager, pinned_service_token, monkeypatch):
        _no_sleep(monkeypatch)  # the 5xx pushes retry; do not spend real seconds
        monkeypatch.setattr(main, "db_manager", db_manager)
        _bundle_id, self.apps = _seed_bundle(db_manager)
        with db_manager.engine.begin() as conn:
            conn.execute(
                text("UPDATE plan SET token_budget_monthly = :b, quota_monthly = :q"),
                {"b": PLAN_BUDGET, "q": PLAN_QUOTA},
            )
        self.db = db_manager

    def _provision(self, fake: FakeBilling, *extra: str) -> int:
        patcher = _patch_billing(fake)
        try:
            return provision_tenant_bundle.main(
                ["--tenant", SEED_TENANT_ID, "--bundle", BUNDLE_KEY, "--apply", *extra],
                db=self.db,
            )
        finally:
            patcher.stop()

    def _alpha(self) -> str:
        (lic,) = self.db.list_tenant_app_licences(SEED_TENANT_ID, self.apps["alpha-app"])
        return str(lic.id)

    def _sweep(self, fake: FakeBilling, **kw: Any) -> Any:
        patcher = _patch_billing(fake)
        try:
            return _reconcile(license_ids=[self._alpha()], **kw)
        finally:
            patcher.stop()

    def test_the_repair_pushes_the_override_not_the_plan(self):
        fake = FakeBilling(budget_post_status=503, quota_post_status=503)
        rc = self._provision(fake, "--token-budget", "123", "--quota", "45")
        assert rc == provision_tenant_bundle.EXIT_METERING_PENDING
        alpha = self._alpha()
        assert _overrides(self.db, alpha) == (123, 45), "stored in the licence INSERT"
        assert fake.budgets_for(alpha) == [] and fake.quota_of(alpha) is None, (
            "precondition: billing holds NOTHING for the licence after the outage"
        )

        fake.budget_post_status, fake.quota_post_status = 201, 201
        result = self._sweep(fake)

        assert [b["budget_tokens"] for b in fake.budgets_for(alpha)] == [123], (
            "the sweep re-pushed the PLAN budget — the pooled spend cap was raised"
        )
        assert fake.quota_of(alpha) == 45
        assert result.repaired_license_ids == [alpha]
        assert result.quota_drift_license_ids == []

    def test_a_zero_override_is_stored_pushed_and_repaired_as_zero(self):
        fake = FakeBilling(budget_post_status=503, quota_post_status=503)
        rc = self._provision(fake, "--token-budget", "0", "--quota", "0")
        assert rc == provision_tenant_bundle.EXIT_METERING_PENDING
        alpha = self._alpha()
        assert _overrides(self.db, alpha) == (0, 0)
        # The one-shot push carried 0 too (it was attempted, and rejected).
        assert fake.budget_post_count >= 1 and fake.quota_post_count >= 1

        fake.budget_post_status, fake.quota_post_status = 201, 201
        self._sweep(fake)
        assert [b["budget_tokens"] for b in fake.budgets_for(alpha)] == [0]
        assert fake.quota_of(alpha) == 0

    def test_an_overridden_quota_in_billing_is_not_drift(self):
        fake = FakeBilling()
        assert self._provision(fake, "--quota", "45") == 0
        alpha = self._alpha()
        assert fake.quota_of(alpha) == 45
        result = self._sweep(fake, revalidate=True)
        assert result.quota_drift_license_ids == [], (
            "billing holds exactly the licence's override; that is not divergence"
        )
        assert result.quotas_already_configured == 1
        assert fake.quota_of(alpha) == 45

    def test_force_plan_quota_pushes_the_override_not_the_plan(self):
        fake = FakeBilling()
        assert self._provision(fake, "--quota", "45") == 0
        alpha = self._alpha()
        fake.seed_quota_config(tenant_id=SEED_TENANT_ID, license_id=alpha, quota_monthly=999)
        self._sweep(fake, revalidate=True, force_plan_quota=True)
        assert fake.quota_of(alpha) == 45

    def test_no_override_keeps_plan_behaviour(self):
        fake = FakeBilling(budget_post_status=503, quota_post_status=503)
        assert self._provision(fake) == provision_tenant_bundle.EXIT_METERING_PENDING
        alpha = self._alpha()
        assert _overrides(self.db, alpha) == (None, None)
        fake.budget_post_status, fake.quota_post_status = 201, 201
        self._sweep(fake)
        assert [b["budget_tokens"] for b in fake.budgets_for(alpha)] == [PLAN_BUDGET]
        assert fake.quota_of(alpha) == PLAN_QUOTA

    def test_a_plan_quota_mismatch_without_override_is_still_drift(self):
        """Negative twin: the override must not have blinded the drift report."""
        fake = FakeBilling()
        assert self._provision(fake) == 0
        alpha = self._alpha()
        fake.seed_quota_config(tenant_id=SEED_TENANT_ID, license_id=alpha, quota_monthly=999)
        result = self._sweep(fake, revalidate=True)
        assert result.quota_drift_license_ids == [alpha]
        assert fake.quota_of(alpha) == 999


class TestAnOverrideOnAnUnlimitedPlanIsOwed:
    """An override makes the licence owe that leg even on a plan that owes nothing,
    so both the sweep's selection AND its independent count must include it."""

    def test_sweep_and_independent_count_both_select_it(
        self, db_manager, seed_license, seed_plan, pinned_service_token, monkeypatch
    ):
        from scripts import reconcile_metering

        monkeypatch.setattr(main, "db_manager", db_manager)

        # seed_plan owes nothing (NULL budget, NULL quota).
        assert (seed_plan.token_budget_monthly, seed_plan.quota_monthly) == (None, None)
        lic = str(seed_license.id)
        with db_manager.engine.begin() as conn:
            conn.execute(
                text("UPDATE license SET token_budget_override = 11 WHERE id = :i"), {"i": lic}
            )
            owing = conn.execute(
                text(reconcile_metering._LIVE_BUDGET_OWING_ONE_SQL), {"license_id": lic}
            ).scalar()
        assert owing == 1

        fake = FakeBilling()
        patcher = _patch_billing(fake)
        try:
            result = _reconcile(license_ids=[lic])
        finally:
            patcher.stop()
        assert [b["budget_tokens"] for b in fake.budgets_for(lic)] == [11]
        assert fake.quota_post_count == 0, "the quota leg is still unlimited"
        assert result.repaired_license_ids == [lic]


class TestProvisionBudgetReadsTheStoredOverride:
    """``_provision_budget`` with NO override argument uses the licence's stored one."""

    @pytest.fixture(autouse=True)
    def _setup(self, db_manager, pinned_service_token, monkeypatch):
        from unittest.mock import AsyncMock

        self.budget = AsyncMock(return_value=True)
        self.quota = AsyncMock(return_value=True)
        monkeypatch.setattr(main, "_push_token_budget", self.budget)
        monkeypatch.setattr(main, "_push_quota_config", self.quota)
        monkeypatch.setattr(main, "db_manager", db_manager)
        app = db_manager.create_app(key="ov-stored", name="ov-stored", issuers=("platform",))
        self.app_id = UUID(str(app.id))
        self.plan = db_manager.create_plan(
            app_id=self.app_id,
            code="standard",
            name="Standard",
            token_budget_monthly=PLAN_BUDGET,
            quota_monthly=PLAN_QUOTA,
        )
        subject = db_manager.create_or_get_subject(
            keycloak_sub="svc-ov-stored", keycloak_realm="service"
        )
        self.subject_id = UUID(str(subject.id))
        self.db = db_manager

    def _licence(self, **kw: Any) -> str:
        lic = self.db.create_license(
            subject_id=self.subject_id,
            app_id=self.app_id,
            plan_id=UUID(str(self.plan.id)),
            tenant_id=SEED_TENANT_ID,
            **kw,
        )
        return str(lic.id)

    def _provision(self, license_id: str, **kw: Any) -> bool:
        return asyncio.run(
            main.provision_license_metering(
                license_id=license_id,
                tenant_id=SEED_TENANT_ID,
                plan_id=str(self.plan.id),
                **kw,
            )
        )

    def test_stored_override_is_pushed(self):
        lic = self._licence(token_budget_override=5, quota_override=0)
        assert self._provision(lic) is True
        assert _kw(self.budget)["budget_tokens"] == 5
        assert _kw(self.quota)["quota_monthly"] == 0

    def test_no_stored_override_pushes_the_plan(self):
        lic = self._licence()
        assert self._provision(lic) is True
        assert _kw(self.budget)["budget_tokens"] == PLAN_BUDGET
        assert _kw(self.quota)["quota_monthly"] == PLAN_QUOTA

    def test_an_explicit_argument_beats_the_stored_value(self):
        lic = self._licence(token_budget_override=5)
        assert self._provision(lic, token_budget_override=6) is True
        assert _kw(self.budget)["budget_tokens"] == 6
