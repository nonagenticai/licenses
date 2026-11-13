"""A licence's token budget must reach billing when it CHANGES, not only at creation.

WHY THIS FILE EXISTS (prod, 2026-10-15): a KAG licence's plan cap was raised, but
licences pushed a budget to billing only in ``_provision_budget`` (licence create).
``PATCH /plans/{id}`` pushed nothing and the nightly reconcile created missing
budgets but never updated an existing one, so license-authz (which reads the Redis
limit only billing writes) kept refusing until billing was restarted.

Billing's ``POST /api/budget`` with ``source='license_sync'`` is an upsert keyed on
the licence (updates ``budget_tokens``, keeps ``consumed_tokens``, rewrites Redis),
so the fix is entirely here:

* ``PATCH /plans/{id}`` re-pushes for every LIVE, plan-driven licence on that plan;
* ``push_effective_token_budgets`` pushes the effective value (override wins);
* the reconcile sweep re-pushes a differing billing value and leaves an equal one.

Billing is faked with ``FakeBilling`` (tests/unit/test_budget_coverage.py), which
records every POST body on the wire.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

import main
from tests.unit.test_budget_coverage import (
    FakeBilling,
    _patch_billing,
    _set_plan_allowances,
    _set_stamps,
)

TENANT = "01000000-0000-7000-8000-000000000001"


class RecordingBilling(FakeBilling):
    """FakeBilling that also keeps each budget POST body, in order."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.budget_bodies: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs: Any):
        if url.endswith("/api/budget"):
            self.budget_bodies.append(dict(kwargs.get("json") or {}))
        return await super().post(url, **kwargs)

    def pushed_for(self, license_id: Any) -> list[int]:
        return [
            int(b["budget_tokens"])
            for b in self.budget_bodies
            if b.get("license_id") == str(license_id)
        ]


@pytest.fixture
def main_db(db_manager):
    original = main.db_manager
    main.db_manager = db_manager
    try:
        yield db_manager
    finally:
        main.db_manager = original


def _exec(db_manager, sql: str, **params: Any) -> None:
    session = db_manager.get_session()
    try:
        session.execute(text(sql), params)
        session.commit()
    finally:
        session.close()


def _licence(db_manager, *, app_id, plan_id, sub: str, **kwargs: Any):
    subject = db_manager.create_or_get_subject(keycloak_sub=sub, keycloak_realm="portal")
    return db_manager.create_license(
        subject_id=UUID(str(subject.id)),
        app_id=UUID(str(app_id)),
        plan_id=UUID(str(plan_id)),
        tenant_id=TENANT,
        status="active",
        starts_at=datetime.now(UTC) - timedelta(days=1),
        expires_at=datetime.now(UTC) + timedelta(days=30),
        **kwargs,
    )


@pytest.fixture
def plan_estate(main_db, seed_app, seed_plan):
    """Licences on one budget plan in every state that matters, plus a control plan."""
    _set_plan_allowances(main_db, seed_plan.id, quota=None, budget=1_000_000)
    other_plan = main_db.create_plan(
        app_id=UUID(str(seed_app.id)),
        code="other-budget-plan",
        name="Other",
        duration_days=30,
        token_budget_monthly=1_000_000,
    )
    on_plan = {"app_id": seed_app.id, "plan_id": seed_plan.id}
    estate = {
        "active": _licence(main_db, sub="tb-active", **on_plan),
        "revoked": _licence(main_db, sub="tb-revoked", **on_plan),
        "archived": _licence(main_db, sub="tb-archived", **on_plan),
        "expired": _licence(main_db, sub="tb-expired", **on_plan),
        "override": _licence(main_db, sub="tb-override", token_budget_override=42, **on_plan),
        "other_plan": _licence(main_db, sub="tb-other", app_id=seed_app.id, plan_id=other_plan.id),
    }
    _exec(main_db, "UPDATE license SET status='revoked' WHERE id=:i", i=str(estate["revoked"].id))
    _exec(main_db, "UPDATE license SET deleted_at=now() WHERE id=:i", i=str(estate["archived"].id))
    _exec(
        main_db,
        "UPDATE license SET expires_at=now() - interval '1 day' WHERE id=:i",
        i=str(estate["expired"].id),
    )
    return estate


class TestPlanBudgetChangeIsPushed:
    def test_patch_plan_budget_pushes_only_live_plan_driven_licences(
        self, auth_client_admin, main_db, seed_plan, plan_estate
    ):
        fake = RecordingBilling()
        patcher = _patch_billing(fake)
        try:
            resp = auth_client_admin.patch(
                f"/plans/{seed_plan.id}", json={"token_budget_monthly": 5_000_000}
            )
        finally:
            patcher.stop()

        assert resp.status_code == 200, resp.text
        # The one live, plan-driven licence on the plan gets the NEW value.
        assert fake.pushed_for(plan_estate["active"].id) == [5_000_000], fake.budget_bodies
        assert fake.budget_bodies[0]["source"] == "license_sync"
        # Nothing else: revoked, archived, expired, an override (its effective
        # budget did not move) and a licence on another plan.
        for name in ("revoked", "archived", "expired", "override", "other_plan"):
            assert fake.pushed_for(plan_estate[name].id) == [], (name, fake.budget_bodies)
        assert len(fake.budget_bodies) == 1, fake.budget_bodies

    def test_patch_plan_without_a_budget_change_pushes_nothing(
        self, auth_client_admin, main_db, seed_plan, plan_estate
    ):
        """Control: an unrelated field, and re-sending the same value, are no-ops."""
        fake = RecordingBilling()
        patcher = _patch_billing(fake)
        try:
            r1 = auth_client_admin.patch(f"/plans/{seed_plan.id}", json={"name": "Renamed"})
            r2 = auth_client_admin.patch(
                f"/plans/{seed_plan.id}", json={"token_budget_monthly": 1_000_000}
            )
        finally:
            patcher.stop()
        assert r1.status_code == 200 and r2.status_code == 200
        assert fake.budget_bodies == []

    def test_a_billing_failure_does_not_fail_the_plan_update(
        self, auth_client_admin, main_db, seed_plan, plan_estate, monkeypatch
    ):
        """Same contract as create: the request succeeds, the failure is recorded."""

        async def _no_sleep(_s):
            return None

        monkeypatch.setattr(main.asyncio, "sleep", _no_sleep)
        lic = str(plan_estate["active"].id)
        _set_stamps(main_db, lic, verified=datetime.now(UTC))
        fake = RecordingBilling(budget_post_status=503)
        patcher = _patch_billing(fake)
        try:
            resp = auth_client_admin.patch(
                f"/plans/{seed_plan.id}", json={"token_budget_monthly": 7}
            )
        finally:
            patcher.stop()

        assert resp.status_code == 200, resp.text
        assert resp.json()["token_budget_monthly"] == 7
        assert fake.pushed_for(lic) == [7, 7]  # one bounded retry on a 503
        # Verified stamp cleared, so a non-revalidating reconcile selects it.
        session = main_db.get_session()
        try:
            verified = session.execute(
                text("SELECT metering_verified_at FROM license WHERE id=:i"), {"i": lic}
            ).scalar()
        finally:
            session.close()
        assert verified is None


class TestOverrideChangeIsPushed:
    async def test_an_override_change_pushes_the_override(self, main_db, plan_estate):
        lic = plan_estate["override"]
        _exec(main_db, "UPDATE license SET token_budget_override=9000 WHERE id=:i", i=str(lic.id))
        fake = RecordingBilling()
        patcher = _patch_billing(fake)
        try:
            result = await main.push_effective_token_budgets(
                license_ids=[str(lic.id)], reason="test override change"
            )
        finally:
            patcher.stop()
        assert result.pushed == [str(lic.id)], result
        assert fake.pushed_for(lic.id) == [9000]

    async def test_a_revoked_licence_is_not_pushed_even_when_named(self, main_db, plan_estate):
        fake = RecordingBilling()
        patcher = _patch_billing(fake)
        try:
            result = await main.push_effective_token_budgets(
                license_ids=[str(plan_estate["revoked"].id), str(plan_estate["archived"].id)],
                reason="test",
            )
        finally:
            patcher.stop()
        assert result.pushed == [] and fake.budget_bodies == []


class TestReconcileUpdatesADifferingBudget:
    @pytest.fixture
    def two_live(self, main_db, seed_license, seed_second_license, seed_plan):
        _set_plan_allowances(main_db, seed_plan.id, quota=None, budget=5_000_000)
        _set_stamps(main_db, seed_license.id, verified=None)
        _set_stamps(main_db, seed_second_license.id, verified=None)
        return str(seed_license.id), str(seed_second_license.id)

    async def test_differing_value_is_upserted_equal_value_left_alone(self, main_db, two_live):
        differing, equal = two_live
        fake = RecordingBilling()
        fake.seed_budget(tenant_id=TENANT, license_id=differing, budget_tokens=1_000_000)
        fake.seed_budget(tenant_id=TENANT, license_id=equal, budget_tokens=5_000_000)
        patcher = _patch_billing(fake)
        try:
            result = await main.run_metering_reconcile(actor="test")
        finally:
            patcher.stop()

        assert fake.pushed_for(differing) == [5_000_000], fake.budget_bodies
        assert fake.pushed_for(equal) == [], fake.budget_bodies
        assert result.budget_drift_license_ids == [differing], result
        assert result.budgets_updated == 1, result
        assert result.budgets_created == 0, result
        assert result.already_covered == 1, result
        assert sorted(result.repaired_license_ids) == sorted(two_live), result

    async def test_dry_run_reports_drift_and_writes_nothing(self, main_db, two_live):
        differing, equal = two_live
        fake = RecordingBilling()
        fake.seed_budget(tenant_id=TENANT, license_id=differing, budget_tokens=1)
        fake.seed_budget(tenant_id=TENANT, license_id=equal, budget_tokens=5_000_000)
        patcher = _patch_billing(fake)
        try:
            result = await main.run_metering_reconcile(actor="test", dry_run=True)
        finally:
            patcher.stop()
        assert fake.budget_bodies == []
        assert result.budget_drift_license_ids == [differing], result
        assert result.budgets_updated == 0, result
