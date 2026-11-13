"""The nightly metering reconcile never (re)creates a TOKEN budget for a money tenant.

PLAN §2.2: ``licenses-metering-reconcile`` (03:41Z, ``scripts/reconcile_metering.py`` ->
``main.run_metering_reconcile``) used to push a ``license_sync`` token budget for every
live budget-owing licence billing did not cover. For a ``budget_currency='EUR'`` tenant
that would re-create the very token budget provisioning skipped, every night. Now the
token leg is skipped for such a tenant (counted in ``budgets_skipped_money_tenant``,
no budget READ either), the quota leg is unchanged, and the licence still converges.

Negative twin: a NULL-currency tenant is repaired exactly as before.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import text

import main
from tests.conftest import SEED_TENANT_ID
from tests.unit.test_budget_coverage import FakeBilling, _no_sleep, _patch_billing

BUDGET = 61_355_000
QUOTA = 500


@pytest.fixture
def licence(db_manager, seed_license, seed_plan, pinned_service_token, monkeypatch):
    _no_sleep(monkeypatch)
    monkeypatch.setattr(main, "db_manager", db_manager)
    with db_manager.engine.begin() as conn:
        conn.execute(
            text("UPDATE plan SET token_budget_monthly = :b, quota_monthly = :q WHERE id = :p"),
            {"b": BUDGET, "q": QUOTA, "p": str(seed_plan.id)},
        )
    return seed_license


def _set_currency(db, value: str | None) -> None:
    with db.engine.begin() as conn:
        conn.execute(
            text("UPDATE tenants SET budget_currency = :c WHERE id = :t"),
            {"c": value, "t": SEED_TENANT_ID},
        )


def _sweep(fake: FakeBilling, **kw: Any) -> Any:
    patcher = _patch_billing(fake)
    try:
        return asyncio.run(main.run_metering_reconcile(actor="test-043", **kw))
    finally:
        patcher.stop()


@pytest.mark.parametrize("ccy", ["EUR", "USD", "CAD"])
def test_money_tenant_gets_no_token_budget_and_converges(db_manager, licence, ccy):
    """ANY non-NULL currency skips the token leg (PLAN REV F), not only EUR."""
    _set_currency(db_manager, ccy)
    fake = FakeBilling()
    result = _sweep(fake, revalidate=True)
    lic = str(licence.id)
    assert fake.budget_post_count == 0, "reconcile re-created a token budget for an EUR tenant"
    assert fake.budget_list_get_count == 0, "no budget read either: the leg is not owed"
    assert result.budgets_skipped_money_tenant == 1
    assert result.budgets_created == 0
    assert result.uncovered_budget_license_ids == []
    assert fake.quota_of(lic) == QUOTA, "the quota leg is unchanged"
    assert result.repaired_license_ids == [lic]
    assert result.failed_license_ids == []


def test_money_tenant_dry_run_is_not_reported_uncovered(db_manager, licence):
    _set_currency(db_manager, "EUR")
    fake = FakeBilling()
    fake.seed_quota_config(
        tenant_id=SEED_TENANT_ID, license_id=str(licence.id), quota_monthly=QUOTA
    )
    result = _sweep(fake, revalidate=True, dry_run=True)
    assert result.uncovered_budget_license_ids == []
    assert result.budgets_skipped_money_tenant == 1


def test_null_currency_tenant_is_repaired_exactly_as_before(db_manager, licence):
    _set_currency(db_manager, None)
    fake = FakeBilling()
    result = _sweep(fake, revalidate=True)
    lic = str(licence.id)
    assert [b["budget_tokens"] for b in fake.budgets_for(lic)] == [BUDGET]
    assert result.budgets_created == 1
    assert result.budgets_skipped_money_tenant == 0
    assert result.repaired_license_ids == [lic]


def test_cronjob_runner_exits_converged_for_a_money_tenant(db_manager, licence):
    """The 03:41Z job's own guard (scripts/reconcile_metering.py) stays green."""
    from scripts import reconcile_metering

    _set_currency(db_manager, "EUR")
    fake = FakeBilling()
    patcher = _patch_billing(fake)
    try:
        code, payload = asyncio.run(
            reconcile_metering.run(
                limit=500,
                revalidate=True,
                dry_run=False,
                force_plan_quota=False,
                actor="test-043",
                license_id=None,
            )
        )
    finally:
        patcher.stop()
    assert code == reconcile_metering.EXIT_CONVERGED, payload
    assert payload["budgets_skipped_money_tenant"] == 1
    assert fake.budget_post_count == 0
