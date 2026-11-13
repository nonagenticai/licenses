"""Shared unit-test fixtures.

⚠️ THE BILLING FAKE LIVES HERE BECAUSE TWO FILES NEED THE SAME ONE, NOT FOR TIDINESS.
`test_the_reaper_tears_down_billing.py` proves WHAT an armed reaper sweep does to billing;
`test_reap_expired_licences.py` proves the exit-code contract, and since licences#195 its
`converged ⇒ exit 0` case is only reachable when the teardown actually RAN and SUCCEEDED
(an armed sweep that archives rows with `METERING_SERVICE_URL` unset is graded
`REAP_TEARDOWN_SKIPPED_UNCONFIGURED`, exit 1). A second, private copy of this fake in the
second file would be free to drift from the one the teardown tests measure against — and
then "converged" over there would quietly stop meaning what this one proves.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import service_auth
from services import metering_teardown

BILLING = "http://billing.invalid"


@pytest.fixture
def pinned_service_token():
    """Pin the service-token provider.

    With SERVICE_CLIENT_ID/SECRET exported these calls would take a DIFFERENT
    path, making the tests silently weaker on some machines than others.
    """
    original = service_auth._provider
    fake = AsyncMock()
    fake.get_token = AsyncMock(return_value="fake-sa-jwt")
    service_auth._provider = fake
    try:
        yield fake
    finally:
        service_auth._provider = original


@pytest.fixture
def billing_configured():
    """`METERING_SERVICE_URL` as the CronJob manifest would have to supply it."""
    with patch.object(metering_teardown, "METERING_SERVICE_URL", BILLING):
        yield BILLING


def _list_resp(rows, status_code: int = 200):
    """Billing's `GET /api/budget/list/{tenant}` answer.

    `MagicMock`, not `AsyncMock`: production calls `resp.json()` SYNCHRONOUSLY. An
    `AsyncMock` hands back a coroutine, the parse raises, and every test here would
    silently measure the "unknown" path instead of the one it names.
    """
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = "body"
    resp.json = MagicMock(return_value={"data": rows})
    return resp


def _resp(status_code: int):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = "body"
    return resp


class FakeBilling:
    """One mock billing service shared by every teardown in a sweep.

    Records what actually went on the wire. `quota_status` / `budget_status` may be
    a dict keyed by the id in the URL, so one licence can be refused while its
    neighbours succeed — which is how the "does not abort the rest" claim is
    measured rather than asserted.
    """

    def __init__(
        self,
        budgets: dict[str, list[dict[str, Any]]],
        *,
        quota_status: Any = 200,
        budget_status: Any = 200,
        list_status: int = 200,
    ):
        self.budgets = budgets
        self.quota_status = quota_status
        self.budget_status = budget_status
        self.list_status = list_status
        self.gets: list[str] = []
        self.quota_deletes: list[str] = []
        self.budget_deletes: list[str] = []

    def _status(self, spec: Any, key: str) -> int:
        return spec.get(key, 200) if isinstance(spec, dict) else spec

    async def get(self, url: str, **_kwargs):
        self.gets.append(url)
        tenant = url.rsplit("/", 1)[-1]
        return _list_resp(self.budgets.get(tenant, []), status_code=self.list_status)

    async def delete(self, url: str, **_kwargs):
        tail = url.rsplit("/", 1)[-1]
        if "/api/quota/config/" in url:
            self.quota_deletes.append(tail)
            return _resp(self._status(self.quota_status, tail))
        self.budget_deletes.append(tail)
        return _resp(self._status(self.budget_status, tail))

    def install(self):
        """Patch `httpx.AsyncClient` so both teardowns talk to this object."""
        client = AsyncMock()
        client.get = AsyncMock(side_effect=self.get)
        client.delete = AsyncMock(side_effect=self.delete)
        cls = patch("httpx.AsyncClient")
        mock_cls = cls.start()
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        return cls


@pytest.fixture
def fake_billing():
    """Factory + teardown for `FakeBilling`, so the httpx patch is always undone."""
    started = []

    def _make(budgets, **kwargs) -> FakeBilling:
        billing = FakeBilling(budgets, **kwargs)
        started.append(billing.install())
        return billing

    try:
        yield _make
    finally:
        for patcher in started:
            patcher.stop()
