"""Revoking a licence must actively tear down billing's mirrored quota config.

Measured on `origin/main` before this fix: `revoke_license` (models.py) set
`status`/`revoked_at` on the licence row and on its tokens, and **nothing else**.
Billing keeps a mirrored `request_quota_config` row per licence, and its
`DELETE /api/quota/config/{license_id}` had **zero outbound call sites in this
repo** — no revocation path called it, and no other path did either.

The only thing that removed an orphaned mirror was billing's
`delete_absent_quota_configs`, reachable exclusively through
`POST /api/budget/sync-from-licenses`.

⚠️ CORRECTED 2026-09-06 — an earlier version of this docstring said that route is
called "by two n8n workflows on a SCHEDULE", and that the exposure was therefore
bounded by "the sync interval". MEASURED, and both halves are wrong: both callers
are WEBHOOK-triggered (`onboard-customer`, `change-plan`), and the only cron
workflow makes no billing call at all.

So the backstop has NO TIME BOUND. It fires when somebody onboards a customer or
changes a plan, and otherwise never — meaning a revoked, possibly compromised,
licence could keep a live billing mirror INDEFINITELY, not "until the next sync".
The original framing made the defect sound self-healing on a timer; it was not,
and that absence of a time bound is exactly why the fast path is worth having.

The production data looked clean only because it had been repaired by hand that
day; a clean table is not evidence that the write path works.

What this file pins, and what it deliberately does not
------------------------------------------------------
The fix is a FAST PATH in front of the sweep, never a replacement for it — the
sweep remains the only thing that can clear residue, at unbounded latency. So:

* the teardown is CALLED on revoke (and routed through the tracked spawn helper,
  not a bare `asyncio.create_task`);
* a teardown FAILURE still leaves the licence revoked — that is the actual
  contract. Revocation is the security-critical half and the mirror is derived
  state, so a billing outage must never be able to veto revoking a compromised
  licence;
* a failure is COUNTED: a loud, greppable `QUOTA_CONFIG_TEARDOWN_FAILED` ERROR
  naming the licence and the repair route, because this service has no
  Prometheus scrape annotation and the work happens after the 200 has gone.

Neither marker is `METERING_PROVISIONING_FAILED`: that one means "a live licence
is unmetered" and prints a repair path that PROVISIONS, which can never remove
this residue. Pinned by
`test_the_failure_is_not_reported_under_the_provisioning_marker`.

Cancellation gets its OWN marker, `QUOTA_CONFIG_TEARDOWN_ABANDONED`, because the
residue is identical but the diagnosis is opposite: a rising FAILED rate means
billing is refusing us or unreachable, while ABANDONED means billing is fine and
the pod simply went away mid-call. Under one shared marker a routine rollout
would have read as a billing outage. Pinned by `TestPodTerminationIsItsOwnStory`.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, patch
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient

import main
import service_auth
from models import License
from services import metering_teardown

TENANT = "01000000-0000-7000-8000-000000000001"


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


class TestRevokeCallsTheTeardown:
    """The fast path exists and the revoke route actually takes it.

    MUTATION CHECK: deleting the `_spawn_background_task(_teardown_quota_config(...))`
    call from `revoke_token_or_license` turns every test in this class red.
    """

    def test_revoking_a_license_tears_down_its_quota_config(
        self, auth_client_admin: TestClient, main_db, full_seed_data
    ):
        """The defect itself: on origin/main this assertion measured 0 calls."""
        license_id = str(full_seed_data["license"].id)

        with patch("main._teardown_quota_config", new_callable=AsyncMock) as teardown:
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

    def test_the_teardown_goes_through_the_tracked_spawn_helper(
        self, auth_client_admin: TestClient, main_db, full_seed_data
    ):
        """A bare `asyncio.create_task` is only WEAKLY referenced by the loop and
        can be garbage-collected mid-flight — losing the teardown for a licence
        already reported to the caller as revoked. Same constraint the mint path's
        `_provision_budget` spawn carries.

        ⚠️ UPDATED: revoke now spawns TWO teardowns — the quota config (request
        RATE) and the token budget (SPEND). This used to assert `called_once`.
        Both must go through the tracked helper, and they are spawned separately
        on purpose so that one billing refusal cannot suppress the other's
        attempt or its marker. Asserting the count pins that second spawn is
        really there; asserting it is still the tracked helper pins that neither
        was quietly downgraded to a bare `asyncio.create_task`.
        """
        with (
            patch.object(
                main, "_spawn_background_task", wraps=main._spawn_background_task
            ) as spy_spawn,
            patch("main._teardown_quota_config", new_callable=AsyncMock),
            patch("main._teardown_token_budget", new_callable=AsyncMock),
        ):
            response = auth_client_admin.post(
                "/license/revoke",
                json={"license_id": str(full_seed_data["license"].id)},
            )

        assert response.status_code == 200, response.text
        assert spy_spawn.call_count == 2, (
            "both the quota-config and the token-budget teardown must be spawned "
            f"through the tracked helper; got {spy_spawn.call_count}"
        )

    def test_revoking_by_token_does_not_tear_down_the_mirror(
        self, auth_client_admin: TestClient, main_db, full_seed_data
    ):
        """Scope control, and the reason this is not "revoke ⇒ delete" everywhere.

        `/license/revoke` with a `token` kills ONE credential; the LICENCE stays
        active and still owes billing a quota mirror. Tearing it down here would
        stop metering a live licence — the opposite defect. Without this test the
        obvious over-fix (hooking the teardown into both arms) passes everything
        else in this file.
        """
        with patch("main._teardown_quota_config", new_callable=AsyncMock) as teardown:
            response = auth_client_admin.post(
                "/license/revoke",
                json={"token": full_seed_data["raw_token"]},
            )

        assert response.status_code == 200, response.text
        assert response.json()["revoked"] is True, response.text
        teardown.assert_not_awaited()

    def test_a_license_that_was_not_found_tears_down_nothing(
        self, auth_client_admin: TestClient, main_db, seed_other_tenant_license
    ):
        """Another tenant's licence answers "License not found" and writes nothing,
        so it must not send billing a delete for a licence this caller does not
        own."""
        with patch("main._teardown_quota_config", new_callable=AsyncMock) as teardown:
            response = auth_client_admin.post(
                "/license/revoke",
                json={"license_id": str(seed_other_tenant_license.id)},
            )

        assert response.status_code == 200, response.text
        assert response.json()["revoked"] is False, response.text
        teardown.assert_not_awaited()


class TestATeardownFailureStillLeavesTheLicenceRevoked:
    """THE CONTRACT. Revoking is the security-critical half; the mirror is derived.

    If a billing outage could roll back or block a revoke, a compromised licence
    would stay live for exactly as long as billing is down. Every test here drives
    a REAL failure through the real code path and then asserts the licence row.
    """

    def test_a_teardown_that_raises_does_not_unrevoke_the_licence(
        self, auth_client_admin: TestClient, main_db, full_seed_data
    ):
        license_id = str(full_seed_data["license"].id)

        with patch(
            "main._teardown_quota_config",
            new_callable=AsyncMock,
            side_effect=RuntimeError("billing is on fire"),
        ):
            response = auth_client_admin.post(
                "/license/revoke",
                json={"license_id": license_id},
            )

        assert response.status_code == 200, response.text
        assert response.json()["revoked"] is True, (
            "a derived-state failure must not be reported to the caller as a failed revoke"
        )
        row = _license_row(main_db, license_id)
        assert row is not None
        assert row.status == "revoked", "the teardown failure rolled back the revocation"
        assert row.revoked_at is not None, "the revocation audit stamp was lost"

    def test_billing_being_unreachable_does_not_unrevoke_the_licence(
        self, auth_client_admin: TestClient, main_db, full_seed_data, pinned_service_token
    ):
        """End-to-end through the REAL `_teardown_quota_config`, with billing's
        socket refusing. This is the outage shape the contract is written for.

        `httpx.ConnectError`, never the builtin `ConnectionError`: production
        catches `httpx.HTTPError` and the two trees are disjoint, so the builtin
        sails past the retry handler and measures nothing (the trap pinned by
        `test_endpoints.py::test_the_builtin_connectionerror_is_not_what_production_catches`).
        """
        license_id = str(full_seed_data["license"].id)
        mock_client = AsyncMock()
        mock_client.delete = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

        with (
            patch.object(metering_teardown, "METERING_SERVICE_URL", "http://billing.invalid"),
            patch("httpx.AsyncClient") as mock_cls,
            patch("main.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            response = auth_client_admin.post(
                "/license/revoke",
                json={"license_id": license_id},
            )

        assert response.status_code == 200, response.text
        assert response.json()["revoked"] is True, response.text
        row = _license_row(main_db, license_id)
        assert row is not None and row.status == "revoked"
        assert mock_client.delete.call_count == 2, (
            "a transport error must still get its second attempt — a DELETE is "
            f"idempotent, so re-sending is safe; got {mock_client.delete.call_count}"
        )


class TestTheTeardownItself:
    """`_teardown_quota_config` in isolation: what it sends, and what it says when
    it fails. Driven directly rather than through the route so the request can be
    inspected."""

    @staticmethod
    def _resp(status_code: int):
        resp = AsyncMock()
        resp.status_code = status_code
        resp.text = "body"
        return resp

    async def test_it_deletes_the_right_url_with_the_service_headers(self, pinned_service_token):
        mock_client = AsyncMock()
        mock_client.delete = AsyncMock(return_value=self._resp(200))

        with (
            patch.object(metering_teardown, "METERING_SERVICE_URL", "http://billing.invalid"),
            patch("httpx.AsyncClient") as mock_cls,
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            ok = await main._teardown_quota_config(
                license_id="019e00aa-0000-7000-8000-0000000000aa", tenant_id=TENANT
            )

        assert ok is True
        mock_client.delete.assert_awaited_once()
        await_args = mock_client.delete.await_args
        assert await_args is not None
        url = await_args.args[0]
        assert url == (
            "http://billing.invalid/api/quota/config/019e00aa-0000-7000-8000-0000000000aa"
        ), f"the licence id must be in the PATH, not a query or a body; got {url}"
        headers = await_args.kwargs["headers"]
        assert headers["X-Tenant-Id"] == TENANT
        assert headers["Authorization"] == "Bearer fake-sa-jwt", (
            "billing gates this route with require_internal; an unauthenticated "
            "delete is a 401 and leaves the mirror in place"
        )

    async def test_a_forbidden_answer_is_not_retried(self, pinned_service_token, caplog):
        """A 403 is a verdict about the licences SA's realm roles. No amount of
        sleeping turns it into a 200, and re-sending burns a second in a
        background task for nothing."""
        mock_client = AsyncMock()
        mock_client.delete = AsyncMock(return_value=self._resp(403))
        sleeps = []

        async def _record_sleep(delay):
            sleeps.append(delay)

        with (
            patch.object(metering_teardown, "METERING_SERVICE_URL", "http://billing.invalid"),
            patch("httpx.AsyncClient") as mock_cls,
            patch("main.asyncio.sleep", side_effect=_record_sleep),
            caplog.at_level(logging.ERROR),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            ok = await main._teardown_quota_config(license_id="lic-403", tenant_id=TENANT)

        assert ok is False
        assert mock_client.delete.call_count == 1, (
            f"a 403 must cost exactly one attempt; got {mock_client.delete.call_count}"
        )
        assert sleeps == [], f"a verdict must not be slept on; got {sleeps}"

    async def test_a_retryable_answer_is_retried_then_counted(self, pinned_service_token, caplog):
        mock_client = AsyncMock()
        mock_client.delete = AsyncMock(return_value=self._resp(503))

        with (
            patch.object(metering_teardown, "METERING_SERVICE_URL", "http://billing.invalid"),
            patch("httpx.AsyncClient") as mock_cls,
            patch("main.asyncio.sleep", new_callable=AsyncMock),
            caplog.at_level(logging.ERROR),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            ok = await main._teardown_quota_config(license_id="lic-503", tenant_id=TENANT)

        assert ok is False
        assert mock_client.delete.call_count == 2, (
            f"a 503 is worth exactly one retry; got {mock_client.delete.call_count}"
        )
        loud = [
            r
            for r in caplog.records
            if metering_teardown.QUOTA_TEARDOWN_FAILURE_MARKER in r.getMessage()
        ]
        assert loud, (
            "residue must be COUNTED — best-effort cannot mean invisible, which is "
            f"what let the original defect survive; got {[r.getMessage() for r in caplog.records]}"
        )

    async def test_the_failure_line_names_the_licence_and_the_repair_route(
        self, pinned_service_token, caplog
    ):
        """An operator grepping the marker must be able to act without looking
        anything up: WHICH licence, and WHICH sweep clears it."""
        mock_client = AsyncMock()
        mock_client.delete = AsyncMock(return_value=self._resp(500))

        with (
            patch.object(metering_teardown, "METERING_SERVICE_URL", "http://billing.invalid"),
            patch("httpx.AsyncClient") as mock_cls,
            patch("main.asyncio.sleep", new_callable=AsyncMock),
            caplog.at_level(logging.ERROR),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            await main._teardown_quota_config(license_id="lic-500", tenant_id=TENANT)

        loud = [
            r.getMessage()
            for r in caplog.records
            if metering_teardown.QUOTA_TEARDOWN_FAILURE_MARKER in r.getMessage()
        ]
        assert loud, "no marker line at all"
        message = loud[0]
        assert "lic-500" in message, message
        assert metering_teardown.QUOTA_TEARDOWN_RECONCILE_PATH in message, message

    async def test_the_failure_is_not_reported_under_the_provisioning_marker(
        self, pinned_service_token, caplog
    ):
        """The two markers name OPPOSITE residues with OPPOSITE repairs.

        `METERING_PROVISIONING_FAILED` means "a live licence is unmetered" and its
        printed repair, `/licenses/admin/metering/reconcile`, PUSHES rows. Pointing
        this failure at it would publish a remedy that cannot possibly clear the
        residue — the sweep that removes an orphaned mirror is
        `delete_absent_quota_configs`, behind `/api/budget/sync-from-licenses`.
        """
        mock_client = AsyncMock()
        mock_client.delete = AsyncMock(return_value=self._resp(500))

        with (
            patch.object(metering_teardown, "METERING_SERVICE_URL", "http://billing.invalid"),
            patch("httpx.AsyncClient") as mock_cls,
            patch("main.asyncio.sleep", new_callable=AsyncMock),
            caplog.at_level(logging.ERROR),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            await main._teardown_quota_config(license_id="lic-marker", tenant_id=TENANT)

        wrong = [
            r.getMessage() for r in caplog.records if main.METERING_FAILURE_MARKER in r.getMessage()
        ]
        assert not wrong, (
            "a teardown failure reported under the PROVISIONING marker tells the "
            f"operator to run a sweep that only ever creates rows: {wrong}"
        )
        assert metering_teardown.QUOTA_TEARDOWN_FAILURE_MARKER != main.METERING_FAILURE_MARKER
        assert metering_teardown.QUOTA_TEARDOWN_RECONCILE_PATH != main.METERING_RECONCILE_PATH

    async def test_it_never_raises_into_the_background_task_callback(
        self, pinned_service_token, caplog
    ):
        """`_spawn_background_task`'s done-callback logs an ESCAPING exception under
        `METERING_FAILURE_MARKER` — the wrong marker with the wrong remedy. So this
        coroutine must swallow everything and report under its own marker."""
        with (
            patch.object(metering_teardown, "METERING_SERVICE_URL", "http://billing.invalid"),
            patch(
                "services.metering_teardown._metering_auth_headers",
                new_callable=AsyncMock,
                side_effect=RuntimeError("keycloak exploded"),
            ),
            caplog.at_level(logging.ERROR),
        ):
            ok = await main._teardown_quota_config(license_id="lic-boom", tenant_id=TENANT)

        assert ok is False, "an internal fault must be reported as a failed teardown, not raised"
        loud = [
            r
            for r in caplog.records
            if metering_teardown.QUOTA_TEARDOWN_FAILURE_MARKER in r.getMessage()
        ]
        assert loud, "the swallow must still count the residue"


class TestPodTerminationIsItsOwnStory:
    """A rollout that interrupts a teardown must not read as a billing outage.

    `CancelledError` derives from `BaseException`, not `Exception`, so before this
    handling it sailed past the swallow entirely and `_background_task_done`
    reported it as `METERING_PROVISIONING_FAILED ... repair with POST
    /licenses/admin/metering/reconcile` — the wrong marker, and a remedy that only
    ever CREATES rows and so cannot clear this residue. An operator debugging a
    deploy was being sent to provision something.

    The residue is the same as a failed teardown's; the DIAGNOSIS is the opposite,
    which is why it gets its own marker rather than sharing one.
    """

    async def test_cancellation_is_reported_under_the_abandoned_marker(
        self, pinned_service_token, caplog
    ):
        mock_client = AsyncMock()
        mock_client.delete = AsyncMock(side_effect=asyncio.CancelledError())

        with (
            patch.object(metering_teardown, "METERING_SERVICE_URL", "http://billing.invalid"),
            patch("httpx.AsyncClient") as mock_cls,
            caplog.at_level(logging.ERROR),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            ok = await main._teardown_quota_config(license_id="lic-sigterm", tenant_id=TENANT)

        assert ok is False
        abandoned = [
            r.getMessage()
            for r in caplog.records
            if metering_teardown.QUOTA_TEARDOWN_ABANDONED_MARKER in r.getMessage()
        ]
        assert abandoned, (
            "a cancelled teardown must be counted, or a rollout silently leaves "
            f"residue; got {[r.getMessage() for r in caplog.records]}"
        )
        assert "lic-sigterm" in abandoned[0]
        assert metering_teardown.QUOTA_TEARDOWN_RECONCILE_PATH in abandoned[0], (
            "the residue is real, so the line must still name what clears it"
        )

    async def test_cancellation_is_not_reported_as_a_billing_failure(
        self, pinned_service_token, caplog
    ):
        """The whole point of the separate marker: a deploy must be
        distinguishable from billing refusing us. Sharing one marker made a
        rollout indistinguishable from an outage."""
        mock_client = AsyncMock()
        mock_client.delete = AsyncMock(side_effect=asyncio.CancelledError())

        with (
            patch.object(metering_teardown, "METERING_SERVICE_URL", "http://billing.invalid"),
            patch("httpx.AsyncClient") as mock_cls,
            caplog.at_level(logging.ERROR),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            await main._teardown_quota_config(license_id="lic-sigterm", tenant_id=TENANT)

        for wrong_marker in (
            metering_teardown.QUOTA_TEARDOWN_FAILURE_MARKER,
            main.METERING_FAILURE_MARKER,
        ):
            assert not [r for r in caplog.records if wrong_marker in r.getMessage()], (
                f"a cancelled teardown must not be reported as {wrong_marker}"
            )

    async def test_cancellation_does_not_escape_into_the_done_callback(
        self, pinned_service_token, caplog
    ):
        """Asserted through the REAL `_spawn_background_task`, because the false
        remedy was published by its done-callback, not by the teardown.

        This is also the test that pins the deliberate non-re-raise: if the clause
        re-raised, the task would finish CANCELLED and `_background_task_done`
        would print `METERING_PROVISIONING_FAILED ... reconcile` right after the
        accurate line.
        """
        registry = main._BACKGROUND_TASKS
        mock_client = AsyncMock()
        mock_client.delete = AsyncMock(side_effect=asyncio.CancelledError())

        with (
            patch.object(metering_teardown, "METERING_SERVICE_URL", "http://billing.invalid"),
            patch("httpx.AsyncClient") as mock_cls,
            caplog.at_level(logging.ERROR),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            task = main._spawn_background_task(
                main._teardown_quota_config(license_id="lic-sigterm", tenant_id=TENANT)
            )
            for _ in range(10):
                await asyncio.sleep(0)
                if task not in registry:
                    break

        assert task.done()
        assert not task.cancelled(), (
            "the task must finish normally — a CANCELLED task makes "
            "_background_task_done publish the reconcile remedy"
        )
        assert task.exception() is None
        assert not [r for r in caplog.records if main.METERING_FAILURE_MARKER in r.getMessage()], (
            "the done-callback published the false remedy after all"
        )


class TestTheSweepIsStillTheBackstop:
    """A fix that suppresses a symptom must not delete the detector.

    This fast path narrows the window; it does not close it (a failed teardown
    leaves real residue, which is exactly why the failure is counted). Billing's
    `delete_absent_quota_configs`, behind `sync-from-licenses`, remains the only
    thing that actually converges the mirror, so nothing here may quietly retire
    it.

    ⚠️ And it is NOT periodic — both its callers are customer-lifecycle webhooks
    (`onboard-customer`, `change-plan`), so its latency is unbounded. That is the
    reason a failed teardown is worth a loud line rather than a shrug.
    """

    def test_the_repair_route_named_in_the_failure_is_the_sweep(self):
        assert (
            metering_teardown.QUOTA_TEARDOWN_RECONCILE_PATH == "/api/budget/sync-from-licenses"
        ), (
            "the marker must point at the reconciler that DELETES absent configs; "
            "any other route cannot clear this residue"
        )

    def test_the_three_markers_are_distinct(self):
        """Two of these were merged once already. A shared string makes the
        operator's grep unable to separate "billing refused us", "the pod went
        away", and "a live licence is unmetered" — three different next actions."""
        markers = {
            main.METERING_FAILURE_MARKER,
            metering_teardown.QUOTA_TEARDOWN_FAILURE_MARKER,
            metering_teardown.QUOTA_TEARDOWN_ABANDONED_MARKER,
        }
        assert len(markers) == 3, markers
