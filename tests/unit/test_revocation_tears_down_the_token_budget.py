"""Revoking a licence must also tear down billing's TOKEN BUDGET, not just the quota mirror.

`_teardown_quota_config` closed one half of this. Revoking a licence removed
billing's mirrored `request_quota_config` — the row that caps request RATE — and
left the `token_budgets` row, the one that caps SPEND, fully live.

⭐ MEASURED on production 2026-09-09: of **190 active `token_budgets` rows, 19
backed a licence whose status is `revoked`** — **430,000,000 tokens** of
allowance standing behind licences that no longer exist. Tenant `ahtapol` reads
**310.0M remaining** on the operator's Budgets page while holding only **3** live
licences.

⚠️ The control that makes this a residue defect and not a duplicate-sync defect:
every active row carries a DISTINCT `license_id`. One budget per licence is the
intended shape, so the rows are not duplicates to be de-duplicated — they have
simply outlived their licences.

Billing's own reconciler, `deactivate_orphan_license_sync_budgets`, is reachable
ONLY through `POST /api/budget/sync-from-licenses`, and that route runs on **NO
timer** — no scheduler, no CronJob, no watcher loop, no startup hook. So the
residue had no time bound at all: nothing on the revoke path told billing, and
nothing on billing's side was ever going to notice. That absence of a time bound
is exactly why the fast path is worth having, and why a failed teardown is worth
a loud line rather than a shrug.

What this file pins, and what it deliberately does not
------------------------------------------------------
The teardown is scoped, permanently, to `source='license_sync'`. `manual` and
`topup` rows are operator grants that STACK by design and are frequently
tenant-wide with `license_id: null`; tearing one down on a revoke would destroy a
deliberate top-up that no licence ever backed. That scope is the most important
thing here, and `test_only_the_license_sync_row_for_this_licence_is_deleted`
is the test that can fail for it.

"Unknown" is never read as "nothing to do". If billing does not answer the budget
list, the teardown reports FAILURE rather than success-with-no-work — concluding
absence from an unanswered read is how residue becomes invisible instead of
merely present.

Cancellation gets its own marker for the same reason the quota half does: the
residue is identical but the diagnosis is opposite. A rising
`TOKEN_BUDGET_TEARDOWN_FAILED` rate means billing refused us or is unreachable;
`TOKEN_BUDGET_TEARDOWN_ABANDONED` means billing is fine and the pod went away
mid-call. Under one shared marker a routine rollout would read as a billing
outage. Pinned by `TestPodTerminationIsItsOwnStory`.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient

import main
import service_auth
from models import License
from services import metering_teardown

TENANT = "01000000-0000-7000-8000-000000000001"

BILLING = "http://billing.invalid"
LICENSE_ID = "019e00aa-0000-7000-8000-0000000000aa"
OTHER_LICENSE_ID = "019e00bb-0000-7000-8000-0000000000bb"


def _license_row(db_manager, license_id) -> License | None:
    """Read the licence straight from the table.

    Deliberately not `db_manager.get_license`: the claim under test is about
    STORED state, and going through a convenience method would only prove the
    method agrees with itself.
    """
    session = db_manager.get_session()
    try:
        return session.query(License).filter(License.id == UUID(str(license_id))).first()
    finally:
        session.close()


def _list_resp(rows, status_code: int = 200):
    """Billing's `GET /api/budget/list/{tenant}` answer.

    `MagicMock`, not `AsyncMock`: production calls `resp.json()` SYNCHRONOUSLY.
    An `AsyncMock` would hand back a coroutine, the parse would raise, and
    `_fetch_active_budget_ids_for_license` would return `None` — every test in
    this class would then measure the "unknown" path instead of the one it names.
    """
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = "body"
    resp.json = MagicMock(return_value={"data": rows})
    return resp


def _delete_resp(status_code: int):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = "body"
    return resp


def _budget_row(row_id: str, *, source: str, active: bool, license_id: str | None):
    return {
        "id": row_id,
        "source": source,
        "active": active,
        "license_id": license_id,
        "tenant_id": TENANT,
    }


@pytest.fixture
def main_db(db_manager):
    original = main.db_manager
    main.db_manager = db_manager
    try:
        yield db_manager
    finally:
        main.db_manager = original


@pytest.fixture
def pinned_service_token():
    """Pin the service-token provider.

    With SERVICE_CLIENT_ID/SECRET exported in the environment these calls would
    take a DIFFERENT path, making the tests silently weaker on some machines than
    others — the trap `test_endpoints.py` documents at its own billing tests.
    """
    original = service_auth._provider
    fake = AsyncMock()
    fake.get_token = AsyncMock(return_value="fake-sa-jwt")
    service_auth._provider = fake
    try:
        yield fake
    finally:
        service_auth._provider = original


class TestRevokeCallsTheBudgetTeardown:
    """The spend half exists and the revoke route actually takes it.

    MUTATION CHECK: deleting the `_spawn_background_task(_teardown_token_budget(...))`
    call from `revoke_token_or_license` turns the first test in this class red, and
    only that call — removing the quota-config spawn beside it leaves these green,
    which is the point of having both.
    """

    def test_revoking_a_license_tears_down_its_token_budget(
        self, auth_client_admin: TestClient, main_db, full_seed_data
    ):
        """The defect itself: on origin/main this assertion measured 0 calls, which
        is how 19 budgets and 430M tokens outlived their licences."""
        license_id = str(full_seed_data["license"].id)

        with (
            patch("main._teardown_quota_config", new_callable=AsyncMock),
            patch("main._teardown_token_budget", new_callable=AsyncMock) as teardown,
        ):
            response = auth_client_admin.post(
                "/license/revoke",
                json={"license_id": license_id},
            )

        assert response.status_code == 200, response.text
        assert response.json()["revoked"] is True, response.text
        teardown.assert_awaited_once()
        await_args = teardown.await_args
        assert await_args is not None
        assert await_args.kwargs == {
            "license_id": license_id,
            "tenant_id": TENANT,
        }, (
            "the teardown must name the licence just revoked and the tenant the "
            f"ownership check already proved owns it; got {await_args}"
        )

    def test_revoking_by_token_does_not_tear_down_the_budget(
        self, auth_client_admin: TestClient, main_db, full_seed_data
    ):
        """⭐ SCOPE CONTROL, and the reason this is not "revoke ⇒ delete" everywhere.

        `/license/revoke` with a `token` kills ONE credential; the LICENCE stays
        ACTIVE and still owes billing a budget. Tearing it down here would strip a
        live licence of its spending cap — the opposite defect, and a worse one:
        residue is over-permission on a dead licence, this would be a dead cap on
        a live one. Without this test the obvious over-fix (hooking the teardown
        into both arms) passes everything else in this file.
        """
        license_id = str(full_seed_data["license"].id)

        with (
            patch("main._teardown_quota_config", new_callable=AsyncMock),
            patch("main._teardown_token_budget", new_callable=AsyncMock) as teardown,
        ):
            response = auth_client_admin.post(
                "/license/revoke",
                json={"token": full_seed_data["raw_token"]},
            )

        assert response.status_code == 200, response.text
        assert response.json()["revoked"] is True, response.text
        teardown.assert_not_awaited()
        row = _license_row(main_db, license_id)
        assert row is not None
        assert row.status == "active", (
            "the token arm must leave the LICENCE alone — if it did not, the "
            f"budget teardown would be correct here and this control is void; got {row.status}"
        )

    def test_a_license_that_was_not_found_tears_down_nothing(
        self, auth_client_admin: TestClient, main_db, seed_other_tenant_license
    ):
        """Another tenant's licence answers "License not found" and writes nothing,
        so it must not send billing a delete for a budget this caller does not
        own."""
        with (
            patch("main._teardown_quota_config", new_callable=AsyncMock),
            patch("main._teardown_token_budget", new_callable=AsyncMock) as teardown,
        ):
            response = auth_client_admin.post(
                "/license/revoke",
                json={"license_id": str(seed_other_tenant_license.id)},
            )

        assert response.status_code == 200, response.text
        assert response.json()["revoked"] is False, response.text
        assert teardown.await_count == 0, (
            f"a licence that was never revoked must cost billing nothing; got {teardown.await_args_list}"
        )


class TestTheBudgetTeardownItself:
    """`_teardown_token_budget` in isolation: which rows it kills, and what it says
    when it cannot. Driven directly rather than through the route so both the
    budget-list GET and the DELETEs can be inspected."""

    @staticmethod
    def _urls_touched(mock_client) -> list[str]:
        """Every URL this teardown put on the wire, list read included."""
        calls = list(mock_client.get.await_args_list) + list(mock_client.delete.await_args_list)
        return [call.args[0] for call in calls]

    async def test_only_the_license_sync_row_for_this_licence_is_deleted(
        self, pinned_service_token
    ):
        """⭐ THE SCOPE CONTROL — the most important test in this file.

        `GET /api/budget/list/{tenant}` returns the tenant's budgets INCLUDING
        inactive ones and INCLUDING every source, so the teardown must pick out
        exactly one row from five candidates:

        (a) active `license_sync` for THIS licence  → the only legitimate target
        (b) active `manual`, tenant-wide            → an operator grant
        (c) active `topup` naming this licence      → an operator grant
        (d) active `license_sync`, OTHER licence    → someone else's cap
        (e) INACTIVE `license_sync`, this licence   → already soft-deleted

        `manual` and `topup` are the ones that matter: they STACK by design and are
        frequently tenant-wide with `license_id: null` (prod tenant
        019de842-c92a-7e37-8fdd-eb0d8c5396f9 holds exactly such a row, 40M tokens).
        A teardown that keyed on `license_id` alone, or on "active", would destroy
        a deliberate top-up that no licence ever backed and that no revoke can
        restore.
        """
        rows = [
            _budget_row(
                "budget-license-sync-mine",
                source="license_sync",
                active=True,
                license_id=LICENSE_ID,
            ),
            _budget_row("budget-manual-tenantwide", source="manual", active=True, license_id=None),
            _budget_row("budget-topup-mine", source="topup", active=True, license_id=LICENSE_ID),
            _budget_row(
                "budget-license-sync-theirs",
                source="license_sync",
                active=True,
                license_id=OTHER_LICENSE_ID,
            ),
            _budget_row(
                "budget-license-sync-inactive",
                source="license_sync",
                active=False,
                license_id=LICENSE_ID,
            ),
        ]
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=_list_resp(rows))
        mock_client.delete = AsyncMock(return_value=_delete_resp(200))

        with (
            patch.object(metering_teardown, "METERING_SERVICE_URL", BILLING),
            patch("httpx.AsyncClient") as mock_cls,
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            ok = await main._teardown_token_budget(license_id=LICENSE_ID, tenant_id=TENANT)

        assert ok is True
        assert mock_client.delete.await_count == 1, (
            "exactly one row backed this licence; anything else means the filter "
            f"widened; got {mock_client.delete.await_args_list}"
        )
        # Bind it once and narrow it: `await_args` is `_Call | None`, and
        # reaching through the Optional is both a type error and a worse failure
        # message than this assert gives.
        delete_call = mock_client.delete.await_args
        assert delete_call is not None
        delete_url = delete_call.args[0]
        assert delete_url == f"{BILLING}/api/budget/budget-license-sync-mine", (
            f"the DELETE must name the BUDGET row id, in the path; got {delete_url}"
        )
        touched = self._urls_touched(mock_client)
        for spared in (
            "budget-manual-tenantwide",
            "budget-topup-mine",
            "budget-license-sync-theirs",
            "budget-license-sync-inactive",
        ):
            assert not any(spared in url for url in touched), (
                f"{spared} was touched — a revoke must never reach an operator grant, "
                f"another licence's cap, or an already-inactive row; urls={touched}"
            )
        headers = delete_call.kwargs["headers"]
        assert headers["X-Tenant-Id"] == TENANT
        assert headers["Authorization"] == "Bearer fake-sa-jwt", (
            "billing gates this route with require_internal; an unauthenticated "
            "delete is a 401 and leaves the budget live"
        )

    async def test_an_unreadable_budget_list_is_a_failure_not_an_empty_list(
        self, pinned_service_token, caplog
    ):
        """⭐ "unknown" must never be read as "nothing to do".

        Billing answering 503 is billing NOT answering; a teardown that read that
        as "no matching rows" would return success and leave 22.6M tokens live
        while telling nobody. The residue is what made this defect survive, so the
        marker is the assertion — not the return value alone.
        """
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=_list_resp([], status_code=503))
        mock_client.delete = AsyncMock(return_value=_delete_resp(200))

        with (
            patch.object(metering_teardown, "METERING_SERVICE_URL", BILLING),
            patch("httpx.AsyncClient") as mock_cls,
            patch("main.asyncio.sleep", new_callable=AsyncMock),
            caplog.at_level(logging.ERROR),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            ok = await main._teardown_token_budget(license_id=LICENSE_ID, tenant_id=TENANT)

        assert ok is False
        assert mock_client.get.await_count == 2, (
            f"a 503 on the list read is worth exactly one retry; got {mock_client.get.await_count}"
        )
        assert mock_client.delete.await_count == 0, (
            "an unread list names no budget ids, so there is nothing safe to "
            f"delete; got {mock_client.delete.await_args_list}"
        )
        loud = [
            r.getMessage()
            for r in caplog.records
            if metering_teardown.TOKEN_BUDGET_TEARDOWN_FAILURE_MARKER in r.getMessage()
        ]
        assert loud, (
            "residue must be COUNTED — a silent 'nothing to do' is exactly how 19 "
            f"orphans accumulated unseen; got {[r.getMessage() for r in caplog.records]}"
        )
        assert LICENSE_ID in loud[0], loud[0]
        assert metering_teardown.TOKEN_BUDGET_TEARDOWN_RECONCILE_PATH in loud[0], (
            f"an operator grepping the marker must be told what clears it; got {loud[0]}"
        )

    async def test_an_unreachable_billing_is_a_failure_not_an_empty_list(
        self, pinned_service_token, caplog
    ):
        """Same claim, transport shape.

        `httpx.ConnectError`, never the builtin `ConnectionError`: production
        catches `httpx.HTTPError` and the two trees are disjoint, so the builtin
        would sail straight past the retry handler into the generic `except
        Exception` and measure a different code path than the one named here.
        """
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
        mock_client.delete = AsyncMock(return_value=_delete_resp(200))

        with (
            patch.object(metering_teardown, "METERING_SERVICE_URL", BILLING),
            patch("httpx.AsyncClient") as mock_cls,
            patch("main.asyncio.sleep", new_callable=AsyncMock),
            caplog.at_level(logging.ERROR),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            ok = await main._teardown_token_budget(license_id=LICENSE_ID, tenant_id=TENANT)

        assert ok is False
        assert mock_client.get.await_count == 2, (
            f"a transport error is retryable and must get its second attempt; "
            f"got {mock_client.get.await_count}"
        )
        assert mock_client.delete.await_count == 0
        assert [
            r
            for r in caplog.records
            if metering_teardown.TOKEN_BUDGET_TEARDOWN_FAILURE_MARKER in r.getMessage()
        ], (
            f"billing being unreachable must be counted; got {[r.getMessage() for r in caplog.records]}"
        )

    async def test_a_genuine_empty_list_is_success_with_no_delete(
        self, pinned_service_token, caplog
    ):
        """The other side of the tri-state: billing DID answer, and this licence
        has no active `license_sync` budget. That is not residue, so it must not
        burn a DELETE and must not print a marker — a marker here would make the
        alert rate meaningless, since most revokes are of licences billing never
        budgeted."""
        rows = [
            _budget_row("budget-manual-tenantwide", source="manual", active=True, license_id=None),
            _budget_row(
                "budget-license-sync-theirs",
                source="license_sync",
                active=True,
                license_id=OTHER_LICENSE_ID,
            ),
        ]
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=_list_resp(rows))
        mock_client.delete = AsyncMock(return_value=_delete_resp(200))

        with (
            patch.object(metering_teardown, "METERING_SERVICE_URL", BILLING),
            patch("httpx.AsyncClient") as mock_cls,
            caplog.at_level(logging.ERROR),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            ok = await main._teardown_token_budget(license_id=LICENSE_ID, tenant_id=TENANT)

        assert ok is True
        assert mock_client.delete.await_count == 0, (
            f"nothing matched, so nothing may be deleted; got {mock_client.delete.await_args_list}"
        )
        noisy = [
            r.getMessage()
            for r in caplog.records
            if metering_teardown.TOKEN_BUDGET_TEARDOWN_FAILURE_MARKER in r.getMessage()
        ]
        assert not noisy, f"a clean no-op must not be counted as residue; got {noisy}"

    async def test_a_404_from_the_soft_delete_is_treated_as_success(self, pinned_service_token):
        """Billing's `DELETE /api/budget/{id}` is a SOFT delete (`active=False`)
        and idempotent: a 404 means the row is already inactive, which is the
        state this teardown exists to produce. Counting it as a failure would make
        every retry of a SUCCESSFUL teardown look like fresh residue and pin the
        alert rate above zero permanently."""
        rows = [
            _budget_row(
                "budget-already-gone", source="license_sync", active=True, license_id=LICENSE_ID
            ),
        ]
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=_list_resp(rows))
        mock_client.delete = AsyncMock(return_value=_delete_resp(404))

        with (
            patch.object(metering_teardown, "METERING_SERVICE_URL", BILLING),
            patch("httpx.AsyncClient") as mock_cls,
            patch("main.asyncio.sleep", new_callable=AsyncMock) as slept,
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            ok = await main._teardown_token_budget(license_id=LICENSE_ID, tenant_id=TENANT)

        assert ok is True
        assert mock_client.delete.await_count == 1, (
            f"a 404 is a terminal success; re-sending it buys nothing; "
            f"got {mock_client.delete.await_count}"
        )
        assert slept.await_count == 0, "a 404 must not be slept on"

    async def test_a_forbidden_delete_is_counted_as_a_failure(self, pinned_service_token, caplog):
        """A 403 is a verdict about the licences SA's realm roles. No amount of
        sleeping turns it into a 200, and the budget really is still live — so it
        costs exactly one attempt and gets the marker."""
        rows = [
            _budget_row(
                "budget-forbidden", source="license_sync", active=True, license_id=LICENSE_ID
            ),
        ]
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=_list_resp(rows))
        mock_client.delete = AsyncMock(return_value=_delete_resp(403))

        with (
            patch.object(metering_teardown, "METERING_SERVICE_URL", BILLING),
            patch("httpx.AsyncClient") as mock_cls,
            patch("main.asyncio.sleep", new_callable=AsyncMock) as slept,
            caplog.at_level(logging.ERROR),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            ok = await main._teardown_token_budget(license_id=LICENSE_ID, tenant_id=TENANT)

        assert ok is False
        assert mock_client.delete.await_count == 1, (
            f"a 403 must cost exactly one attempt; got {mock_client.delete.await_count}"
        )
        assert slept.await_count == 0, "a verdict must not be slept on"
        loud = [
            r.getMessage()
            for r in caplog.records
            if metering_teardown.TOKEN_BUDGET_TEARDOWN_FAILURE_MARKER in r.getMessage()
        ]
        assert loud, f"a refused delete is residue; got {[r.getMessage() for r in caplog.records]}"
        assert "403" in loud[0], (
            f"the line must carry the status an operator needs to triage it; got {loud[0]}"
        )
        assert metering_teardown.TOKEN_BUDGET_TEARDOWN_RECONCILE_PATH in loud[0], loud[0]

    async def test_an_unset_metering_url_makes_no_call_at_all(self, pinned_service_token):
        """`METERING_SERVICE_URL` defaults to `""`. Unguarded, `f"{''}/api/budget/..."`
        has no scheme, so httpx raises `UnsupportedProtocol` BEFORE any I/O — the
        shape `_is_retryable_transport_error` was narrowed for. Guarding here keeps
        it from ever reaching that path, and the verdict is FAILURE, not success:
        an unconfigured service has torn down nothing."""
        with (
            patch.object(metering_teardown, "METERING_SERVICE_URL", ""),
            patch("httpx.AsyncClient") as mock_cls,
        ):
            ok = await main._teardown_token_budget(license_id=LICENSE_ID, tenant_id=TENANT)

        assert ok is False, "no billing URL means no teardown happened; that is not a success"
        mock_cls.assert_not_called()


class TestPodTerminationIsItsOwnStory:
    """A rollout that interrupts a teardown must not read as a billing outage.

    The residue is identical to a failed teardown's; the DIAGNOSIS is the
    opposite. `TOKEN_BUDGET_TEARDOWN_FAILED` means billing refused us or is
    unreachable — go look at billing. `TOKEN_BUDGET_TEARDOWN_ABANDONED` means
    billing is fine and the pod went away mid-call — go look at the rollout.
    Under one shared marker a routine deploy would be indistinguishable from an
    outage, and the on-call response to each is different.

    `CancelledError` derives from `BaseException`, not `Exception`, so without its
    own clause it sails past the swallow entirely and `_background_task_done`
    reports it under `METERING_PROVISIONING_FAILED`, whose printed remedy only
    ever CREATES rows and so cannot clear this residue.
    """

    async def test_cancellation_is_abandoned_and_never_failed(self, pinned_service_token, caplog):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=asyncio.CancelledError())
        mock_client.delete = AsyncMock(return_value=_delete_resp(200))

        with (
            patch.object(metering_teardown, "METERING_SERVICE_URL", BILLING),
            patch("httpx.AsyncClient") as mock_cls,
            caplog.at_level(logging.ERROR),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            ok = await main._teardown_token_budget(license_id=LICENSE_ID, tenant_id=TENANT)

        assert ok is False
        abandoned = [
            r.getMessage()
            for r in caplog.records
            if metering_teardown.TOKEN_BUDGET_TEARDOWN_ABANDONED_MARKER in r.getMessage()
        ]
        assert abandoned, (
            "a cancelled teardown must still be counted, or a rollout silently "
            f"leaves budgets live; got {[r.getMessage() for r in caplog.records]}"
        )
        assert LICENSE_ID in abandoned[0], abandoned[0]
        assert metering_teardown.TOKEN_BUDGET_TEARDOWN_RECONCILE_PATH in abandoned[0], (
            f"the residue is real, so the line must still name what clears it; got {abandoned[0]}"
        )
        for wrong_marker in (
            metering_teardown.TOKEN_BUDGET_TEARDOWN_FAILURE_MARKER,
            main.METERING_FAILURE_MARKER,
        ):
            assert not [r for r in caplog.records if wrong_marker in r.getMessage()], (
                f"a cancelled teardown reported as {wrong_marker} sends the operator "
                "to debug a billing outage that is not happening"
            )
        assert metering_teardown.TOKEN_BUDGET_TEARDOWN_ABANDONED_MARKER != (
            metering_teardown.TOKEN_BUDGET_TEARDOWN_FAILURE_MARKER
        ), "a shared string makes the two grep identically and the distinction is lost"
