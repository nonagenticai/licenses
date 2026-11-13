"""``_provision_budget`` skips the TOKEN leg for a money tenant (PLAN §2.2).

A tenant with ``budget_currency='EUR'`` holds its budget as money in billing. Pushing a
``license_sync`` TOKEN budget as well would put a second, unrelated cap on it (billing
pools token budgets per tenant), so the token push is skipped and logged as
``token_budget_skipped_money_tenant``. The QUOTA is still pushed, and the licence is
stamped provisioned when the quota is accepted.

Negative twin: a tenant with ``budget_currency`` NULL (every tenant today) still gets
its token budget pushed, byte-for-byte as before.
"""

from __future__ import annotations

import asyncio
import logging

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


def _stamped(db, license_id: str) -> bool:
    with db.engine.connect() as conn:
        return (
            conn.execute(
                text("SELECT metering_provisioned_at FROM license WHERE id = :i"),
                {"i": license_id},
            ).scalar_one()
            is not None
        )


def _provision(lic) -> bool:
    return asyncio.run(
        main._provision_budget(
            license_id=str(lic.id),
            tenant_id=str(lic.tenant_id),
            plan_id=str(lic.plan_id),
            app_id=str(lic.app_id),
        )
    )


@pytest.mark.parametrize("ccy", ["EUR", "USD", "CAD"])
def test_money_tenant_pushes_quota_only_and_is_stamped(db_manager, licence, caplog, ccy):
    """ANY non-NULL currency skips the token leg (PLAN REV F), not only EUR."""
    _set_currency(db_manager, ccy)
    fake = FakeBilling()
    patcher = _patch_billing(fake)
    try:
        with caplog.at_level(logging.INFO, logger="main"):
            ok = _provision(licence)
    finally:
        patcher.stop()
    assert ok is True
    assert fake.budget_post_count == 0, "a money tenant must never get a token budget"
    assert fake.budgets_for(str(licence.id)) == []
    assert fake.quota_of(str(licence.id)) == QUOTA
    assert _stamped(db_manager, str(licence.id))
    assert any("token_budget_skipped_money_tenant" in r.getMessage() for r in caplog.records)


def test_money_tenant_with_explicit_override_still_skips_the_token_leg(db_manager, licence):
    _set_currency(db_manager, "EUR")
    fake = FakeBilling()
    patcher = _patch_billing(fake)
    try:
        ok = asyncio.run(
            main._provision_budget(
                license_id=str(licence.id),
                tenant_id=str(licence.tenant_id),
                plan_id=str(licence.plan_id),
                app_id=str(licence.app_id),
                token_budget_override=123,
            )
        )
    finally:
        patcher.stop()
    assert ok is True
    assert fake.budget_post_count == 0


def test_money_tenant_quota_failure_is_still_a_failure(db_manager, licence):
    _set_currency(db_manager, "EUR")
    fake = FakeBilling(quota_post_status=503)
    patcher = _patch_billing(fake)
    try:
        ok = _provision(licence)
    finally:
        patcher.stop()
    assert ok is False
    assert not _stamped(db_manager, str(licence.id))


def test_null_currency_tenant_still_gets_its_token_budget(db_manager, licence):
    """Zero behaviour change for every existing tenant (all have NULL)."""
    _set_currency(db_manager, None)
    fake = FakeBilling()
    patcher = _patch_billing(fake)
    try:
        ok = _provision(licence)
    finally:
        patcher.stop()
    assert ok is True
    assert [b["budget_tokens"] for b in fake.budgets_for(str(licence.id))] == [BUDGET]
    assert fake.quota_of(str(licence.id)) == QUOTA
