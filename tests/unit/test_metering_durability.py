"""Durability of the licence → billing metering hand-off.

WHY THIS FILE EXISTS (measured in prod, 2026-08-08):

``create_user_license`` provisioned metering with a bare
``asyncio.create_task(_provision_budget(...))`` whose return value was thrown
away. Three independent defects rode on that one line:

1. CPython keeps only a WEAK reference to a running task, so an unreferenced
   task can be garbage-collected mid-flight.
2. The coroutine swallows every failure by design ("never block the license
   creation response"), so a transient billing error — or the pod being
   terminated during a rollout — permanently loses metering for that licence
   while the licence itself is created anyway.
3. Nothing recorded the outcome, so the gap was invisible from inside the
   licences database. Finding it required hand-diffing two separate databases:
   100 active licences sat on quota-bearing plans, 99 had a
   ``billing.request_quota_config`` row and one did not — and the unmetered one
   (``019fd7a0-888f-749e-86c2-3b935a196ebf``, ``svc-ingestion``/kag) was the
   NEWEST quota-bearing licence in the estate. An ongoing leak, not a historical
   one. Driving the deployed ``_provision_budget`` by hand against that licence
   returned 201/201 — the code path is correct; only its durability was not.

⚠️ CONSTRAINT ON ANY REPAIR (billing schema, verified in prod):
   ``request_quota_config`` has PRIMARY KEY (license_id) → re-pushing quota is
   safe, at most one row per licence.
   ``token_budgets`` has PRIMARY KEY (id) and no unique constraint on
   license_id → nothing in the schema stops a second row, so a budget must only
   ever be created when billing has been ASKED and reported none.

⚠️ RETRACTED 2026-08-10 — this file used to conclude the above with "So the
   reconciler pushes QUOTA ONLY", citing "191 rows for 186 distinct licences" as
   proof that re-pushing duplicates. That inference was wrong and it kept a
   94-licence hole open for as long as it stood:

   * The 191/186 spread is ``source='manual'`` rows, which STACK BY DESIGN.
     Across ``source='license_sync'`` prod holds 185 rows for 185 distinct
     licences — zero duplicates — because billing's
     ``upsert_license_sync_budget`` is a real read-before-write upsert on
     (tenant, licence, period, source).
   * Meanwhile the quota estate was fully converged (0 of 98 live quota-owing
     licences missing a config row) and 94 of those same 98 had NO active budget
     in billing. A quota-only sweep repaired only what was already correct.

   The sweep now repairs budgets too, read-before-write. See
   tests/unit/test_budget_coverage.py, which owns the coverage semantics, and
   ``test_reconcile_repairs_a_synthetic_gap_and_pushes_quota_and_budget`` below.
"""

from __future__ import annotations

import asyncio
import gc
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.exc import ProgrammingError

import main
from models import DatabaseManager, License, Plan

TENANT = "01000000-0000-7000-8000-000000000001"

# Sentinel returned by _read_stamp when the COLUMN itself is absent. Without it
# the pre-fix run dies with a psycopg UndefinedColumn error, which is a crash,
# not a falsified assertion — and a crash is not proof that a behaviour is
# missing (it could equally be a broken fixture). With it, the RED is a plain
# assertion naming exactly what is missing.
NO_COLUMN = "NO-SUCH-COLUMN"


def _read_stamp(db_manager, license_id) -> Any:
    """Read ``license.metering_provisioned_at`` straight from the DB.

    Deliberately raw SQL, not the ORM attribute: the point of the column is that
    an operator can answer "which licences never got metered?" with psql against
    the licences database alone.
    """
    session = db_manager.get_session()
    try:
        row = session.execute(
            text("SELECT metering_provisioned_at FROM license WHERE id = :i"),
            {"i": str(license_id)},
        ).first()
        return row[0] if row is not None else None
    except ProgrammingError:
        session.rollback()
        return NO_COLUMN
    finally:
        session.close()


def _set_stamp(db_manager, license_id, value) -> None:
    """Set BOTH metering stamps.

    The reconcile sweep selects on ``metering_verified_at`` (migration 019), not
    ``metering_provisioned_at`` — 018 baselined the latter across the whole table
    so it cannot mean "covered". These tests set the pair together because they
    are about the sweep's gap/no-gap behaviour, and a licence that is a gap is a
    gap on both columns. ``tests/unit/test_budget_coverage.py`` is where the two
    columns are driven APART, which is the case that matters.
    """
    session = db_manager.get_session()
    try:
        session.execute(
            text(
                "UPDATE license SET metering_provisioned_at = :v, metering_verified_at = :v "
                "WHERE id = :i"
            ),
            {"v": value, "i": str(license_id)},
        )
        session.commit()
    finally:
        session.close()


def _read_verified(db_manager, license_id) -> Any:
    """Read ``license.metering_verified_at`` (migration 019) straight from the DB."""
    session = db_manager.get_session()
    try:
        row = session.execute(
            text("SELECT metering_verified_at FROM license WHERE id = :i"),
            {"i": str(license_id)},
        ).first()
        return row[0] if row is not None else None
    except ProgrammingError:
        session.rollback()
        return NO_COLUMN
    finally:
        session.close()


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


def _mock_httpx(
    status_by_path: dict[str, int] | None = None,
    default: int = 201,
    covered_license_ids: list[str] | None = None,
    configured_quotas: dict[str, int] | None = None,
):
    """Patch httpx.AsyncClient; return (patcher, mock_client).

    ``status_by_path`` maps a URL fragment ('/api/budget') to the status code to
    return for it, so a test can fail ONE of the two pushes and prove the stamp
    is withheld — a mock that always succeeds cannot discriminate.

    ``covered_license_ids`` seeds what ``GET /api/budget/list/{tenant}`` reports
    billing already holds. The reconcile sweep reads that BEFORE deciding whether
    to create a budget (``_fetch_license_sync_budget_ids``), so a GET stub is not
    optional decoration: without it the sweep cannot tell "billing has nothing"
    from "billing did not answer", and correctly refuses to create anything.
    Default is the empty list = billing holds nothing.

    ``configured_quotas`` maps a licence id to the ``quota_monthly`` billing
    already holds in ``request_quota_config``, reported by
    ``GET /api/quota/check``. Since 2026-08-24 the sweep reads that before
    pushing — an unconditional push OVERWRITES the value and was reverting every
    operator-set quota nightly. Default empty = billing holds no quota row, so
    the push is owed. ``None`` in the response is how billing says "no row"
    (``routers/quota.py::quota_check`` falls back to the query param, which this
    sweep deliberately does not send).
    """
    status_by_path = status_by_path or {}
    covered = list(covered_license_ids or [])
    quotas = dict(configured_quotas or {})

    async def _post(url, **kwargs):
        code = default
        for fragment, value in status_by_path.items():
            if fragment in url:
                code = value
                break
        return MagicMock(status_code=code, text="mocked")

    async def _get(url, **kwargs):
        code = 200
        for fragment, value in status_by_path.items():
            if fragment in url:
                code = value
                break
        response = MagicMock(status_code=code, text="mocked")
        if "/api/quota/check" in url:
            lic = str((kwargs.get("params") or {}).get("license_id"))
            response.json = MagicMock(
                return_value={"data": {"license_id": lic, "quota_monthly": quotas.get(lic)}}
            )
            return response
        rows = [{"license_id": lid, "source": "license_sync", "active": True} for lid in covered]
        response.json = MagicMock(return_value={"data": rows})
        return response

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=_post)
    mock_client.get = AsyncMock(side_effect=_get)
    patcher = patch("httpx.AsyncClient")
    mock_cls = patcher.start()
    mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
    mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
    return patcher, mock_client


@pytest.fixture
def main_db(db_manager):
    """Point the module-level ``main.db_manager`` at the test database."""
    original = main.db_manager
    main.db_manager = db_manager
    try:
        yield db_manager
    finally:
        main.db_manager = original


class TestBackgroundTaskRegistry:
    """Defect 1: the provisioning task was not referenced anywhere."""

    async def test_spawned_task_is_strongly_referenced_while_it_runs(self):
        registry = getattr(main, "_BACKGROUND_TASKS", None)
        assert registry is not None, (
            "main must keep a module-level strong-reference registry for "
            "fire-and-forget tasks; CPython only weakly references a running task"
        )
        registry.clear()

        started = asyncio.Event()
        release = asyncio.Event()

        async def _work() -> None:
            started.set()
            await release.wait()

        task = main._spawn_background_task(_work())
        await started.wait()
        assert task in registry

        # Drop OUR reference and force a collection: the registry must be the
        # thing keeping the task alive.
        del task
        gc.collect()
        assert len(registry) == 1

        release.set()
        for _ in range(10):
            await asyncio.sleep(0)
            if not registry:
                break
        assert registry == set()

    async def test_completed_task_is_discarded_so_the_registry_cannot_grow(self):
        registry = main._BACKGROUND_TASKS
        registry.clear()

        async def _noop() -> None:
            return None

        for _ in range(5):
            main._spawn_background_task(_noop())
        for _ in range(20):
            await asyncio.sleep(0)
            if not registry:
                break
        assert registry == set(), "done-callback must discard the task or the set leaks"

    async def test_a_failing_background_task_is_logged_loudly(
        self, caplog: pytest.LogCaptureFixture
    ):
        """A task that dies must not vanish silently — the log is the only
        channel that reaches an operator (this service has no Prometheus
        scrape annotation, so a counter would be unobservable in prod)."""
        registry = main._BACKGROUND_TASKS
        registry.clear()

        async def _boom() -> None:
            raise RuntimeError("kaboom")

        with caplog.at_level(logging.ERROR, logger="main"):
            main._spawn_background_task(_boom())
            for _ in range(20):
                await asyncio.sleep(0)
                if not registry:
                    break

        loud = [r for r in caplog.records if "METERING_PROVISIONING_FAILED" in r.getMessage()]
        assert loud, f"expected a loud ERROR; got {[r.getMessage() for r in caplog.records]}"
        assert loud[0].levelno == logging.ERROR

    def test_create_license_spawns_provisioning_through_the_registry(
        self, auth_client_admin: TestClient, db_manager, seed_app, seed_plan
    ):
        """The real handler must route its fire-and-forget through the tracked
        spawn helper, not a bare ``asyncio.create_task``."""
        db_manager.ensure_tenant(TENANT, name="acme-durability", owner_key="platform")
        subject = "019e00aa-0000-7000-8000-0000000000f1"

        with (
            patch.object(
                main, "_spawn_background_task", wraps=main._spawn_background_task
            ) as spy_spawn,
            patch("main._provision_budget", new_callable=AsyncMock),
        ):
            response = auth_client_admin.post(
                f"/licenses/user/{subject}",
                json={"app_id": str(seed_app.id), "plan_id": str(seed_plan.id)},
                headers={"X-Tenant-Id": TENANT},
            )

        assert response.status_code == 201
        spy_spawn.assert_called_once()


class TestProvisioningOutcomeIsRecorded:
    """Defect 3: the outcome was not queryable inside the licences database."""

    async def test_successful_provisioning_records_metering_provisioned_at(
        self, main_db, full_seed_data
    ):
        plan = full_seed_data["plan"]
        license_id = full_seed_data["license"].id
        _set_plan_allowances(main_db, plan.id, quota=1_000_000, budget=10_000_000)
        assert _read_stamp(main_db, license_id) in (None, NO_COLUMN)

        patcher, mock_client = _mock_httpx()
        try:
            await main._provision_budget(
                license_id=str(license_id), tenant_id=TENANT, plan_id=str(plan.id)
            )
        finally:
            patcher.stop()

        assert mock_client.post.await_count == 2  # positive control: both pushes happened
        stamp = _read_stamp(main_db, license_id)
        assert stamp not in (None, NO_COLUMN), (
            "a licence whose budget AND quota both landed must record "
            "metering_provisioned_at so the gap is queryable without a cross-database diff"
        )
        assert isinstance(stamp, datetime)

    async def test_quota_failure_leaves_the_stamp_null_and_logs_loudly(
        self, main_db, full_seed_data, caplog: pytest.LogCaptureFixture
    ):
        """Discriminating half #1: budget succeeds, quota 503s."""
        plan = full_seed_data["plan"]
        license_id = full_seed_data["license"].id
        _set_plan_allowances(main_db, plan.id, quota=1_000_000, budget=10_000_000)

        patcher, mock_client = _mock_httpx({"/api/quota/config": 503})
        try:
            with caplog.at_level(logging.ERROR, logger="main"):
                await main._provision_budget(
                    license_id=str(license_id), tenant_id=TENANT, plan_id=str(plan.id)
                )
        finally:
            patcher.stop()

        assert _read_stamp(main_db, license_id) in (None, NO_COLUMN)
        loud = [r for r in caplog.records if "METERING_PROVISIONING_FAILED" in r.getMessage()]
        assert loud, f"expected a loud ERROR; got {[r.getMessage() for r in caplog.records]}"
        assert str(license_id) in loud[0].getMessage()

    async def test_budget_failure_leaves_the_stamp_null_and_logs_loudly(
        self, main_db, full_seed_data, caplog: pytest.LogCaptureFixture
    ):
        """Discriminating half #2: quota succeeds, budget 503s. A stamp written
        on quota alone would mark a licence with NO token budget as complete."""
        plan = full_seed_data["plan"]
        license_id = full_seed_data["license"].id
        _set_plan_allowances(main_db, plan.id, quota=1_000_000, budget=10_000_000)

        patcher, _ = _mock_httpx({"/api/budget": 503})
        try:
            with caplog.at_level(logging.ERROR, logger="main"):
                await main._provision_budget(
                    license_id=str(license_id), tenant_id=TENANT, plan_id=str(plan.id)
                )
        finally:
            patcher.stop()

        assert _read_stamp(main_db, license_id) in (None, NO_COLUMN)
        assert [r for r in caplog.records if "METERING_PROVISIONING_FAILED" in r.getMessage()]

    async def test_a_plan_that_owes_nothing_is_still_recorded_as_complete(
        self, main_db, full_seed_data
    ):
        """NULL quota AND NULL budget = unlimited; nothing is owed to billing, so
        provisioning IS complete. Recording it keeps
        ``metering_provisioned_at IS NULL`` a clean 'unfinished' signal rather
        than a mix of 'failed' and 'nothing to do'."""
        plan = full_seed_data["plan"]
        license_id = full_seed_data["license"].id
        _set_plan_allowances(main_db, plan.id, quota=None, budget=None)

        patcher, mock_client = _mock_httpx()
        try:
            await main._provision_budget(
                license_id=str(license_id), tenant_id=TENANT, plan_id=str(plan.id)
            )
        finally:
            patcher.stop()

        assert mock_client.post.await_count == 0  # positive control: nothing pushed
        assert _read_stamp(main_db, license_id) not in (None, NO_COLUMN)

    async def test_sentinel_tenant_skips_the_budget_push_and_still_records(
        self, main_db, full_seed_data, caplog: pytest.LogCaptureFixture
    ):
        """#234: billing 422s budgets for its sentinel tenant BY DESIGN. The
        budget is skipped (not pushed, not failed); the quota still goes. The
        422 stub makes a regression that pushes anyway fail loudly here.
        Negative control: ``test_budget_failure_leaves_the_stamp_null...``."""
        sentinel = "00000000-0000-0000-0000-000000000001"
        assert sentinel in main.BILLING_SENTINEL_TENANT_IDS
        plan = full_seed_data["plan"]
        license_id = full_seed_data["license"].id
        _set_plan_allowances(main_db, plan.id, quota=1_000_000, budget=10_000_000)

        patcher, mock_client = _mock_httpx({"/api/budget": 422})
        try:
            with caplog.at_level(logging.INFO, logger="main"):
                await main._provision_budget(
                    license_id=str(license_id), tenant_id=sentinel, plan_id=str(plan.id)
                )
        finally:
            patcher.stop()

        posted = [c.args[0] for c in mock_client.post.await_args_list]
        assert not [u for u in posted if u.endswith("/api/budget")], posted
        assert [u for u in posted if "/api/quota/config" in u], posted
        assert not [r for r in caplog.records if "METERING_PROVISIONING_FAILED" in r.getMessage()]
        assert _read_stamp(main_db, license_id) not in (None, NO_COLUMN)

    async def test_missing_plan_does_not_record_and_logs_loudly(
        self, main_db, full_seed_data, caplog: pytest.LogCaptureFixture
    ):
        license_id = full_seed_data["license"].id
        with caplog.at_level(logging.ERROR, logger="main"):
            await main._provision_budget(
                license_id=str(license_id),
                tenant_id=TENANT,
                plan_id="019e00ff-0000-7000-8000-00000000dead",
            )
        assert _read_stamp(main_db, license_id) in (None, NO_COLUMN)
        assert [r for r in caplog.records if "METERING_PROVISIONING_FAILED" in r.getMessage()]


class TestSessionIsNotHeldAcrossTheHttpPushes:
    """Defect 4 (2026-08-11): the DB session was pinned across ~25s of HTTP.

    ``_provision_budget`` did ``session = db_manager.get_session()``, ran the
    ``Plan`` SELECT — which under ``autocommit=False`` OPENS A TRANSACTION — and
    then held it across ``_metering_auth_headers`` and the entire
    ``httpx.AsyncClient`` block, closing only in a trailing ``finally``.

    Under pgbouncer TRANSACTION pooling an open transaction pins a server-side
    backend. The window is a cold-cache Keycloak token fetch
    (``service_auth._TOKEN_FETCH_TIMEOUT_SECONDS = 5.0``) plus two ``timeout=5.0``
    pushes that each retry once — ~25s worst case, per licence created. A
    provisioning burst exhausts the licences pool (40 server connections
    fleet-wide) and the damage lands on the AUTH path, which under ``NullPool``
    has no local pool to absorb a connect failure.

    ⚠️ WHY THIS IS ASSERTED AS AN ORDERED LEDGER AND NOT AS A ``close()`` SPY:
    a leaked session in this repo surfaces as a HUNG SUITE, not a failure — the
    conftest reset does ``pg_terminate_backend`` + ``DROP SCHEMA`` and blocks on
    a held lock. A test that only spies ``close()`` cannot distinguish "closed
    late" from "closed at all", and a test that only checks the end state passes
    for a session closed 25 seconds too late. Pool ``checkout``/``checkin``
    events give the ORDER, which is the actual invariant.

    MUTATION PROVEN RED: move ``session.close()`` back to a trailing
    ``finally`` on the outer try.
    """

    @staticmethod
    def _ledger(engine, events: list[str]):
        """Record pool checkout/checkin on ``engine``; returns a stop callable."""

        def _checkout(dbapi_conn, conn_record, conn_proxy):
            events.append("checkout")

        def _checkin(dbapi_conn, conn_record):
            events.append("checkin")

        event.listen(engine, "checkout", _checkout)
        event.listen(engine, "checkin", _checkin)

        def _stop():
            event.remove(engine, "checkout", _checkout)
            event.remove(engine, "checkin", _checkin)

        return _stop

    @staticmethod
    def _assert_no_connection_held_at(events: list[str], marker: str):
        """Every checkout before ``marker`` must already be matched by a checkin."""
        assert marker in events, f"marker {marker!r} never reached; ledger={events}"
        prefix = events[: events.index(marker)]
        assert prefix.count("checkout") >= 1, (
            f"positive control failed: nothing was ever checked out before {marker!r}, "
            f"so this assertion could not have failed. ledger={events}"
        )
        assert prefix.count("checkout") == prefix.count("checkin"), (
            f"a database connection was still checked out when {marker!r} began — "
            f"under pgbouncer transaction pooling that pins a server backend for the "
            f"whole HTTP round-trip. ledger={events}"
        )

    async def test_the_session_is_released_before_any_http_call(self, main_db, full_seed_data):
        plan = full_seed_data["plan"]
        license_id = full_seed_data["license"].id
        _set_plan_allowances(main_db, plan.id, quota=1_000_000, budget=10_000_000)

        events: list[str] = []

        async def _recording_headers(tenant_id: str, lid: str) -> dict[str, str]:
            events.append("auth_headers")
            return {"X-Tenant-Id": tenant_id}

        patcher, mock_client = _mock_httpx()
        inner_post = mock_client.post.side_effect

        async def _recording_post(url, **kwargs):
            events.append("post")
            return await inner_post(url, **kwargs)

        mock_client.post = AsyncMock(side_effect=_recording_post)

        stop = self._ledger(main_db.engine, events)
        try:
            with patch.object(main, "_metering_auth_headers", _recording_headers):
                await main._provision_budget(
                    license_id=str(license_id), tenant_id=TENANT, plan_id=str(plan.id)
                )
        finally:
            stop()
            patcher.stop()

        # Positive controls: the work this ordering is about actually happened.
        assert events.count("post") == 2, f"expected both pushes; ledger={events}"
        assert _read_stamp(main_db, license_id) not in (None, NO_COLUMN)

        # The token fetch is the FIRST network call and can be a cold 5s Keycloak
        # round-trip, so the session must already be gone by then — not merely
        # before the pushes.
        self._assert_no_connection_held_at(events, "auth_headers")
        self._assert_no_connection_held_at(events, "post")

    async def test_every_connection_it_opens_is_returned(self, main_db, full_seed_data):
        """Close-counting ledger: a leak here would later hang the suite, not fail it."""
        plan = full_seed_data["plan"]
        license_id = full_seed_data["license"].id
        _set_plan_allowances(main_db, plan.id, quota=1_000_000, budget=10_000_000)

        events: list[str] = []
        patcher, _ = _mock_httpx()
        stop = self._ledger(main_db.engine, events)
        try:
            await main._provision_budget(
                license_id=str(license_id), tenant_id=TENANT, plan_id=str(plan.id)
            )
        finally:
            stop()
            patcher.stop()

        assert events.count("checkout") >= 2, (
            "positive control: expected at least the plan SELECT and the stamp write; "
            f"ledger={events}"
        )
        assert events.count("checkout") == events.count("checkin"), (
            f"_provision_budget leaked a connection: {events}"
        )

    async def test_the_missing_plan_early_return_still_releases_the_session(
        self, main_db, full_seed_data, caplog: pytest.LogCaptureFixture
    ):
        """The ``if not plan: return`` path now returns from INSIDE the inner try.

        Its ``finally`` must still run. A leak on this path is the nastiest kind:
        it only happens for a licence whose plan row is missing, so it would be
        rare in prod and invisible in a suite that never asserts it.
        """
        license_id = full_seed_data["license"].id
        events: list[str] = []
        stop = self._ledger(main_db.engine, events)
        try:
            with caplog.at_level(logging.ERROR, logger="main"):
                await main._provision_budget(
                    license_id=str(license_id),
                    tenant_id=TENANT,
                    plan_id="019e00ff-0000-7000-8000-00000000dead",
                )
        finally:
            stop()

        assert [r for r in caplog.records if "METERING_PROVISIONING_FAILED" in r.getMessage()]
        assert events.count("checkout") == 1, f"expected exactly the plan SELECT; {events}"
        assert events.count("checkin") == 1, f"the early return leaked the session: {events}"

    async def test_provision_budget_swallows_a_session_open_failure(
        self, main_db, full_seed_data, caplog: pytest.LogCaptureFixture
    ):
        """``get_session()`` must be INSIDE the try, and nothing may mask its error.

        Two defects are pinned by this one test:

        * ``get_session()`` used to sit OUTSIDE the ``try``. Under ``NullPool``
          it opens a real connection and can raise — and that raise escaped this
          function's ``except Exception`` entirely, propagating into
          ``_background_task_done``. Fire-and-forget means fire-and-forget.
        * the trailing ``finally: session.close()`` had to go with it. Kept, it
          would raise ``UnboundLocalError`` on this exact path and MASK the real
          cause, which is why the assertion below checks the logged exception is
          the original ``RuntimeError`` and not a name error.

        MUTATIONS PROVEN RED: move ``get_session()`` back outside the ``try``;
        re-add a trailing ``finally: session.close()``.
        """
        plan = full_seed_data["plan"]
        license_id = full_seed_data["license"].id

        with patch.object(
            DatabaseManager,
            "get_session",
            side_effect=RuntimeError("NullPool connect refused by pgbouncer"),
        ):
            with caplog.at_level(logging.ERROR, logger="main"):
                result = await main._provision_budget(
                    license_id=str(license_id), tenant_id=TENANT, plan_id=str(plan.id)
                )

        # Swallowed (no raise) and reported as not provisioned.
        assert result is False
        loud = [r for r in caplog.records if "METERING_PROVISIONING_FAILED" in r.getMessage()]
        assert loud, f"expected the marker; got {[r.getMessage() for r in caplog.records]}"
        assert str(license_id) in loud[0].getMessage()

        raised = [r.exc_info for r in loud if r.exc_info is not None]
        assert raised, "the swallow must log the traceback, or the cause is unrecoverable"
        original = raised[0][1]
        assert isinstance(original, RuntimeError) and "NullPool connect refused" in str(original), (
            "the logged exception is not the one that was raised — something in the "
            f"cleanup path masked it: {original!r}"
        )


class TestMeteringReconcile:
    """Defect 2: no reconciliation. POST /licenses/admin/metering/reconcile."""

    URL = "/licenses/admin/metering/reconcile"

    def _quota_bearing_gap(self, db_manager, seed_license, seed_plan):
        """Make ``seed_license`` a genuine, currently-unrepaired gap."""
        _set_plan_allowances(db_manager, seed_plan.id, quota=1_000_000, budget=10_000_000)
        _set_stamp(db_manager, seed_license.id, None)
        return seed_license.id

    def test_reconcile_repairs_a_synthetic_gap_and_pushes_quota_and_budget(
        self, auth_client_admin: TestClient, db_manager, seed_license, seed_plan
    ):
        """⚠️ RETARGETED 2026-08-10. This test used to be named
        ``..._and_pushes_quota_only`` and asserted that the sweep must NEVER post
        to ``/api/budget``, on the stated grounds that billing has no unique
        constraint so a re-push "would duplicate rows".

        That assertion was the defect, not the guard. Measured in prod: the
        quota half of the estate is fully converged (0 of 98 live quota-owing
        licences lack a ``request_quota_config`` row) while 94 of the same 98
        had no active budget in billing — a sweep forbidden from touching budgets
        could only ever repair the half that was already fine.

        The real invariant is not "never push a budget", it is "never push a
        budget billing already has". That is now enforced by reading
        ``GET /api/budget/list/{tenant}`` first —
        ``TestReadBeforeWrite::test_an_existing_budget_is_not_re_pushed`` in
        tests/unit/test_budget_coverage.py is the other half of this pair, and it
        drives the same sweep with billing seeded as already-covered.

        Here billing reports it holds nothing (``covered_license_ids=[]``), so
        both pushes are owed.
        """
        license_id = self._quota_bearing_gap(db_manager, seed_license, seed_plan)

        patcher, mock_client = _mock_httpx()
        try:
            response = auth_client_admin.post(self.URL)
        finally:
            patcher.stop()

        assert response.status_code == 200
        body = response.json()
        assert body["repaired"] == 1, body
        assert body["failed"] == 0, body
        assert str(license_id) in body["repaired_license_ids"]

        posted = [c.args[0] for c in mock_client.post.await_args_list]
        assert [u for u in posted if "/api/quota/config" in u], posted
        assert [u for u in posted if u.endswith("/api/budget")], (
            f"billing holds no budget for this licence, so the sweep owes it one: {posted}"
        )
        assert body["budgets_created"] == 1, body
        # The sweep must have ASKED before creating — a create without a prior
        # read is the duplicate-row hazard, whatever the outcome happened to be.
        assert [c.args[0] for c in mock_client.get.await_args_list], (
            "no GET /api/budget/list — the sweep created a budget without first "
            "asking billing what it holds"
        )
        # The sweep records convergence on metering_verified_at, and ONLY there.
        # It must not write metering_provisioned_at: that column carries
        # migration 018's baseline, and overwriting it destroys the only way to
        # tell a baselined stamp from an observed one. See
        # tests/unit/test_budget_coverage.py
        # ::test_verifying_does_not_overwrite_the_018_baseline_stamp.
        assert _read_verified(db_manager, license_id) is not None
        assert _read_stamp(db_manager, license_id) is None, (
            "the sweep must leave metering_provisioned_at alone"
        )

    def test_reconcile_is_a_noop_against_a_clean_estate(
        self, auth_client_admin: TestClient, db_manager, seed_license, seed_plan
    ):
        """The discriminating half: the SAME licence that leg 1 repairs is here
        already stamped, so the sweep must find nothing and push nothing.

        A reconcile test that only ever runs against a clean estate proves
        nothing — that is exactly why the previous test exists."""
        _set_plan_allowances(db_manager, seed_plan.id, quota=1_000_000, budget=10_000_000)
        _set_stamp(db_manager, seed_license.id, datetime.now(UTC))

        patcher, mock_client = _mock_httpx()
        try:
            response = auth_client_admin.post(self.URL)
        finally:
            patcher.stop()

        assert response.status_code == 200
        body = response.json()
        assert body["scanned"] == 0, body
        assert body["repaired"] == 0, body
        assert mock_client.post.await_count == 0

    def test_reconcile_is_idempotent_on_a_second_run(
        self, auth_client_admin: TestClient, db_manager, seed_license, seed_plan
    ):
        self._quota_bearing_gap(db_manager, seed_license, seed_plan)

        patcher, mock_client = _mock_httpx()
        try:
            first = auth_client_admin.post(self.URL)
            calls_after_first = mock_client.post.await_count
            second = auth_client_admin.post(self.URL)
        finally:
            patcher.stop()

        assert first.json()["repaired"] == 1
        assert second.json()["repaired"] == 0
        assert second.json()["scanned"] == 0
        assert mock_client.post.await_count == calls_after_first  # no second push

    def test_reconcile_ignores_unlimited_quota_plans(
        self, auth_client_admin: TestClient, db_manager, seed_license, seed_plan
    ):
        """quota_monthly IS NULL = unlimited: billing owes it no quota row, so an
        unstamped licence on such a plan is NOT a gap."""
        _set_plan_allowances(db_manager, seed_plan.id, quota=None, budget=None)
        _set_stamp(db_manager, seed_license.id, None)

        patcher, mock_client = _mock_httpx()
        try:
            response = auth_client_admin.post(self.URL)
        finally:
            patcher.stop()

        assert response.json()["scanned"] == 0
        assert mock_client.post.await_count == 0

    def test_reconcile_ignores_revoked_licenses(
        self, auth_client_admin: TestClient, db_manager, seed_revoked_license, seed_plan
    ):
        _set_plan_allowances(db_manager, seed_plan.id, quota=1_000_000, budget=10_000_000)
        _set_stamp(db_manager, seed_revoked_license.id, None)

        patcher, mock_client = _mock_httpx()
        try:
            response = auth_client_admin.post(self.URL)
        finally:
            patcher.stop()

        assert response.json()["scanned"] == 0
        assert mock_client.post.await_count == 0

    def test_reconcile_ignores_expired_licenses(
        self, auth_client_admin: TestClient, db_manager, seed_license, seed_plan
    ):
        """status='active' with a past expires_at is the renewal-deadlock shape
        seen in prod; it is not a live entitlement and must not be re-pushed."""
        _set_plan_allowances(db_manager, seed_plan.id, quota=1_000_000, budget=10_000_000)
        _set_stamp(db_manager, seed_license.id, None)
        session = db_manager.get_session()
        try:
            lic = session.query(License).filter(License.id == seed_license.id).first()
            lic.expires_at = datetime.now(UTC) - timedelta(days=1)
            session.commit()
        finally:
            session.close()

        patcher, mock_client = _mock_httpx()
        try:
            response = auth_client_admin.post(self.URL)
        finally:
            patcher.stop()

        assert response.json()["scanned"] == 0
        assert mock_client.post.await_count == 0

    def test_reconcile_withholds_the_stamp_when_billing_rejects(
        self, auth_client_admin: TestClient, db_manager, seed_license, seed_plan
    ):
        license_id = self._quota_bearing_gap(db_manager, seed_license, seed_plan)

        patcher, _ = _mock_httpx({"/api/quota/config": 503})
        try:
            response = auth_client_admin.post(self.URL)
        finally:
            patcher.stop()

        body = response.json()
        assert body["scanned"] == 1
        assert body["repaired"] == 0
        assert body["failed"] == 1
        assert str(license_id) in body["failed_license_ids"]
        assert _read_stamp(db_manager, license_id) in (None, NO_COLUMN)

    def test_reconcile_requires_license_admin(
        self, auth_client_manager: TestClient, db_manager, seed_license, seed_plan
    ):
        self._quota_bearing_gap(db_manager, seed_license, seed_plan)
        patcher, mock_client = _mock_httpx()
        try:
            response = auth_client_manager.post(self.URL)
        finally:
            patcher.stop()
        assert response.status_code == 403
        assert mock_client.post.await_count == 0


class TestLicenseResponseExposesTheStamp:
    """Operator visibility: the stamp must reach the API, not only the DB."""

    def test_license_response_reflects_the_stamp_in_both_directions(
        self, auth_client_admin: TestClient, db_manager, seed_license
    ):
        _set_stamp(db_manager, seed_license.id, datetime.now(UTC))
        stamped = auth_client_admin.get("/licenses/admin/all")
        assert stamped.status_code == 200
        row = next(r for r in stamped.json() if r["id"] == str(seed_license.id))
        assert row["metering_provisioned_at"] is not None

        # The other half: a hardcoded timestamp would pass the assertion above.
        _set_stamp(db_manager, seed_license.id, None)
        row = next(
            r
            for r in auth_client_admin.get("/licenses/admin/all").json()
            if r["id"] == str(seed_license.id)
        )
        assert row["metering_provisioned_at"] is None


class TestMeteringProvisionedAtMigration:
    """018_add_license_metering_provisioned_at.sql.

    Prod has 17/17 stamped, so a new 018 runs normally — the baseline
    short-circuit in scripts/apply_migrations.py only fires when the applied set
    is EMPTY. These tests drive the file against a schema with the column
    REMOVED, i.e. the actual prod pre-state.
    """

    FILE = "018_add_license_metering_provisioned_at.sql"

    def _statements(self):
        from scripts.apply_migrations import MIGRATIONS_DIR, split_statements

        return split_statements((MIGRATIONS_DIR / self.FILE).read_text())

    def _run(self, db_engine):
        for stmt in self._statements():
            with db_engine.begin() as c:
                c.execute(text(stmt))

    def _columns(self, db_engine) -> set[str]:
        from sqlalchemy import inspect

        return {c["name"] for c in inspect(db_engine).get_columns("license")}

    def test_adds_the_column_and_baselines_pre_existing_rows(
        self, db_engine, db_manager, seed_license
    ):
        """018 stamps every row that predates the column. That is what the file
        does, so it is what this pins.

        ⚠️ The RATIONALE it shipped with was wrong, and this test is not evidence
        for it. 018 argued the blanket stamp was safe because "the estate was
        hand-converged to 100/100 on 2026-08-08". That convergence covered QUOTA
        only: measured on 2026-08-10, 94 of 98 live budget-owing licences had no
        active budget in billing while every one of them carried this stamp. The
        blanket UPDATE is therefore a baseline, not a coverage claim, and nothing
        may key coverage off this column — see TestMeteringVerifiedAtMigration
        and migration 019, which deliberately does NOT backfill."""
        license_id = seed_license.id
        with db_engine.begin() as c:
            c.execute(text("ALTER TABLE license DROP COLUMN IF EXISTS metering_provisioned_at"))
        try:
            assert "metering_provisioned_at" not in self._columns(db_engine)

            self._run(db_engine)

            assert "metering_provisioned_at" in self._columns(db_engine)
            assert _read_stamp(db_manager, license_id) not in (None, NO_COLUMN)
        finally:
            with db_engine.begin() as c:
                c.execute(
                    text(
                        "ALTER TABLE license ADD COLUMN IF NOT EXISTS "
                        "metering_provisioned_at TIMESTAMPTZ"
                    )
                )

    def test_is_idempotent_and_does_not_restamp_a_null_written_after_it_ran(
        self, db_engine, db_manager, seed_license
    ):
        """Re-running must not clobber state: a licence deliberately reset to
        NULL (a real, post-migration gap) stays NULL on a second apply, or the
        migration would silently mask exactly the leak it exists to expose.

        ⚠️ This is why the backfill is written as a one-shot inside the same
        guarded block as the ADD COLUMN, not a bare UPDATE."""
        self._run(db_engine)  # already applied by create_all + this call
        _set_stamp(db_manager, seed_license.id, None)

        self._run(db_engine)  # re-apply

        assert _read_stamp(db_manager, seed_license.id) is None


class TestMeteringVerifiedAtMigration:
    """019_add_license_metering_verified_at.sql — the ABSENCE of a backfill.

    018's blanket ``UPDATE license SET metering_provisioned_at = now()`` is what
    made the column unusable as a coverage signal: in prod 205 of 208 rows carry
    that single migration timestamp, so non-NULL cannot distinguish "we observed
    a push" from "the migration stamped it sight unseen". 019 exists to give the
    reconcile sweep a column with no prior opinion, so its ONE load-bearing
    property is that existing rows come out NULL.
    """

    FILE = "019_add_license_metering_verified_at.sql"

    def _run(self, db_engine):
        from scripts.apply_migrations import MIGRATIONS_DIR, split_statements

        for stmt in split_statements((MIGRATIONS_DIR / self.FILE).read_text()):
            with db_engine.begin() as c:
                c.execute(text(stmt))

    def _columns(self, db_engine) -> set[str]:
        from sqlalchemy import inspect

        return {c["name"] for c in inspect(db_engine).get_columns("license")}

    def _read_verified(self, db_manager, license_id):
        session = db_manager.get_session()
        try:
            row = session.execute(
                text("SELECT metering_verified_at FROM license WHERE id = :i"),
                {"i": str(license_id)},
            ).first()
            return row[0] if row else None
        finally:
            session.close()

    def test_adds_the_column_and_does_not_backfill(self, db_engine, db_manager, seed_license):
        """The discriminating assertion against 018: same shape of migration,
        opposite treatment of pre-existing rows.

        A licence carrying 018's baseline stamp must still come out with
        ``metering_verified_at IS NULL`` — otherwise 019 would inherit exactly
        the false "already provisioned" claim it exists to escape, and the first
        sweep would again scan 0 against a 94-licence gap.
        """
        with db_engine.begin() as c:
            c.execute(text("ALTER TABLE license DROP COLUMN IF EXISTS metering_verified_at"))
        try:
            assert "metering_verified_at" not in self._columns(db_engine)
            # The prod pre-state: 018 has stamped this row. Set the 018 column
            # directly — _set_stamp writes both, and one of them does not exist
            # at this point in the test.
            with db_engine.begin() as c:
                c.execute(
                    text("UPDATE license SET metering_provisioned_at = :v WHERE id = :i"),
                    {"v": datetime(2026, 8, 9, 20, 30, tzinfo=UTC), "i": str(seed_license.id)},
                )

            self._run(db_engine)

            assert "metering_verified_at" in self._columns(db_engine)
            assert self._read_verified(db_manager, seed_license.id) is None, (
                "019 must not backfill — a baselined row is not a verified one"
            )
            assert _read_stamp(db_manager, seed_license.id) is not None, (
                "019 must leave metering_provisioned_at alone"
            )
        finally:
            with db_engine.begin() as c:
                c.execute(
                    text(
                        "ALTER TABLE license ADD COLUMN IF NOT EXISTS "
                        "metering_verified_at TIMESTAMPTZ"
                    )
                )

    def test_is_idempotent_and_does_not_clobber_a_verified_stamp(
        self, db_engine, db_manager, seed_license
    ):
        """Re-applying must not reset a genuine verification to NULL, or every
        re-deploy would make the whole estate look unverified again."""
        self._run(db_engine)
        session = db_manager.get_session()
        try:
            session.execute(
                text("UPDATE license SET metering_verified_at = now() WHERE id = :i"),
                {"i": str(seed_license.id)},
            )
            session.commit()
        finally:
            session.close()

        self._run(db_engine)  # re-apply

        assert self._read_verified(db_manager, seed_license.id) is not None


class TestAnUnknownQuotaNamesItsCause:
    """``unknown_quotas=1`` must say WHY. Added 2026-09-04, from an incident.

    ``licenses-metering-reconcile`` failed on 2026-09-04 03:41Z — BOTH attempts,
    ``exit=2``, three firing alerts — and its entire operator-facing output was::

        scanned=157 repaired(converged)=156 failed=1 unknown_quotas=1
        METERING_PROVISIONING_FAILED license=019edca0-… tenant=019edc1e-…
          budget_ok=True quota_ok=False

    The cause took ~30 minutes of Loki archaeology to recover: billing answered
    ``GET /api/quota/check`` with **404**, and billing's ``quota_check`` has
    exactly ONE 404 path — the foreign-owner rule at
    ``billing_service/src/routers/quota.py:132``
    (``config.tenant_id != x-tenant-id``). So the 404 was not "no row": it was
    PROOF that a ``request_quota_config`` row exists under a different tenant,
    i.e. the licence's tenant of record had diverged between the licences and
    billing databases. Three unrelated causes — non-retryable status, transport
    failure, unreadable payload — all reached that one counter, and only this
    one has a remediation.

    ⚠️ WHAT THIS CLASS DOES **NOT** ASSERT, deliberately. The reason is a LABEL.
    It must change no decision: ``unknown`` still pushes nothing, for every
    reason alike. ``_fetch_quota_config_monthly``'s own docstring states the rule
    the fail-closed behaviour exists for — *"a row we are not allowed to see is
    still a row, and pushing over it is the exact harm this function exists to
    stop"* — so every case below pairs its reason assertion with a
    **push-count-zero** assertion. A reason breakdown bought by making the 404
    push would be a regression wearing an improvement's clothes; the pairing is
    what makes that impossible to ship green.
    """

    URL = "/licenses/admin/metering/reconcile"

    # -- direct unit level: the helper's own return contract ----------------

    @staticmethod
    def _client(*, status: int | None = None, payload: Any = None, raises: Exception | None = None):
        """A minimal ``httpx.AsyncClient`` stand-in that records its GETs."""
        calls: list[dict[str, Any]] = []

        async def _get(url, **kwargs):
            calls.append({"url": url, **kwargs})
            if raises is not None:
                raise raises
            resp = MagicMock(status_code=status, text="mocked")
            resp.json = MagicMock(return_value=payload)
            return resp

        client = AsyncMock()
        client.get = AsyncMock(side_effect=_get)
        client.calls = calls  # type: ignore[attr-defined]
        return client

    @staticmethod
    def _no_sleep(monkeypatch) -> list[float]:
        """Record ``asyncio.sleep`` delays instead of serving them.

        The retry costs a real second per licence; without this a 500 case is a
        one-second test, and the sleep count is also a second independent signal
        that the retry decision itself did not move.
        """
        slept: list[float] = []

        async def _fake(delay):
            slept.append(delay)

        monkeypatch.setattr(main.asyncio, "sleep", _fake)
        return slept

    async def _read(self, client) -> tuple[Any, Any, Any]:
        return await main._fetch_quota_config_monthly(
            client,
            license_id="019edca0-b1c5-75bc-8f00-79d9b5aef2da",
            plan_id="019edc1e-0000-7000-8000-000000000001",
            headers={"X-Tenant-Id": TENANT},
        )

    async def test_a_404_is_unknown_and_names_the_foreign_tenant(self, monkeypatch):
        """The incident's own shape. 404 is billing's foreign-owner rule and
        nothing else, so it must not share a label with the codes that mean
        nothing in particular."""
        slept = self._no_sleep(monkeypatch)
        client = self._client(status=404, payload={"detail": "not found"})

        assert await self._read(client) == ("unknown", None, "foreign_tenant")
        # A 404 is a VERDICT: re-asking answers identically, so no retry and no
        # wasted second. This also pins that the new branch sits BELOW the
        # `_is_retryable_status` guard, not above it.
        assert len(client.calls) == 1, client.calls
        assert slept == [], f"a 404 must not be retried: {slept}"

    async def test_another_non_retryable_status_is_not_labelled_foreign_tenant(self, monkeypatch):
        """403 is billing refusing us, not billing telling us a row exists
        elsewhere. Collapsing it into ``foreign_tenant`` would send an operator
        hunting a tenant divergence that is not there — the exact failure this
        whole change exists to stop, one column over."""
        self._no_sleep(monkeypatch)
        client = self._client(status=403, payload={"detail": "denied"})

        state, quota, reason = await self._read(client)
        assert (state, quota) == ("unknown", None)
        assert reason != "foreign_tenant", reason
        assert reason == "http_403", reason

    async def test_a_retried_server_error_reports_its_status_not_a_tenant_divergence(
        self, monkeypatch
    ):
        """500 is retryable, so it reaches the reason through the SECOND pass of
        the loop. A reason computed only on the first pass would be lost here."""
        slept = self._no_sleep(monkeypatch)
        client = self._client(status=500, payload={"detail": "boom"})

        assert await self._read(client) == ("unknown", None, "http_500")
        assert len(client.calls) == 2, client.calls
        assert slept == [1], slept

    async def test_a_transport_failure_is_reported_as_transport(self, monkeypatch):
        """billing never answered at all. Nothing about a tenant is known, and
        the remediation is 're-run', not 'go compare two tables'."""
        import httpx

        slept = self._no_sleep(monkeypatch)
        client = self._client(raises=httpx.ConnectError("connection refused"))

        assert await self._read(client) == ("unknown", None, "transport")
        assert len(client.calls) == 2, "a connect error is retryable exactly once"
        assert slept == [1], slept

    async def test_an_unreadable_payload_is_reported_as_such(self, monkeypatch):
        """billing answered 200 in a shape this cannot read. Distinct from both
        of the above: the route is reachable and we are allowed to call it."""
        self._no_sleep(monkeypatch)
        client = self._client(status=200, payload=["not", "an", "object"])

        assert await self._read(client) == ("unknown", None, "unreadable_payload")

    async def test_a_readable_answer_carries_no_reason(self, monkeypatch):
        """The reason is non-None on EXACTLY the unknown state. A helper that
        always returned a string would make the breakdown count healthy reads."""
        self._no_sleep(monkeypatch)

        absent = self._client(status=200, payload={"data": {"quota_monthly": None}})
        assert await self._read(absent) == ("absent", None, None)

        present = self._client(status=200, payload={"data": {"quota_monthly": 1_000_000}})
        assert await self._read(present) == ("present", 1_000_000, None)

    # -- sweep level: the breakdown reaches the operator, nothing is pushed --

    def _quota_bearing_gap(self, db_manager, seed_license, seed_plan):
        _set_plan_allowances(db_manager, seed_plan.id, quota=1_000_000, budget=10_000_000)
        _set_stamp(db_manager, seed_license.id, None)
        return seed_license.id

    def test_the_sweep_reports_the_foreign_tenant_breakdown_and_pushes_nothing(
        self, auth_client_admin: TestClient, main_db, seed_license, seed_plan, monkeypatch
    ):
        """The incident, end to end. ``unknown_quotas=1`` becomes
        ``unknown_quota_reasons={'foreign_tenant': 1}`` — and the licence still
        fails closed with ZERO quota writes."""
        self._no_sleep(monkeypatch)
        license_id = self._quota_bearing_gap(main_db, seed_license, seed_plan)

        patcher, mock_client = _mock_httpx({"/api/quota/check": 404})
        try:
            body = auth_client_admin.post(self.URL).json()
        finally:
            patcher.stop()

        posted = [c.args[0] for c in mock_client.post.await_args_list]
        assert [u for u in posted if "/api/quota/config" in u] == [], (
            f"FAIL-CLOSED: nothing may be pushed over a row we could not read: {posted}"
        )
        assert body["quotas_pushed"] == 0, body
        assert body["unknown_quota_license_ids"] == [str(license_id)], body
        assert body["unknown_quota_reasons"] == {"foreign_tenant": 1}, body
        assert body["failed"] == 1, body
        assert body["repaired"] == 0, body

    def test_the_breakdown_distinguishes_a_plain_refusal_from_a_divergence(
        self, auth_client_admin: TestClient, main_db, seed_license, seed_plan, monkeypatch
    ):
        """The discriminating half. Without it a breakdown hardcoded to
        ``{'foreign_tenant': N}`` passes the test above and is worse than the
        bare count it replaced — it would name a cause that is not there."""
        self._no_sleep(monkeypatch)
        license_id = self._quota_bearing_gap(main_db, seed_license, seed_plan)

        patcher, mock_client = _mock_httpx({"/api/quota/check": 403})
        try:
            body = auth_client_admin.post(self.URL).json()
        finally:
            patcher.stop()

        posted = [c.args[0] for c in mock_client.post.await_args_list]
        assert [u for u in posted if "/api/quota/config" in u] == [], posted
        assert body["unknown_quota_license_ids"] == [str(license_id)], body
        assert body["unknown_quota_reasons"] == {"http_403": 1}, body
        assert "foreign_tenant" not in body["unknown_quota_reasons"], body
        assert body["failed"] == 1, body

    def test_a_readable_sweep_reports_an_empty_breakdown(
        self, auth_client_admin: TestClient, main_db, seed_license, seed_plan
    ):
        """The vacuity floor. A field that is always ``{}`` would pass nothing
        above; a field that is always populated would pass nothing here."""
        self._quota_bearing_gap(main_db, seed_license, seed_plan)

        patcher, _ = _mock_httpx()
        try:
            body = auth_client_admin.post(self.URL).json()
        finally:
            patcher.stop()

        assert body["unknown_quota_reasons"] == {}, body
        assert body["unknown_quota_license_ids"] == [], body
        assert body["repaired"] == 1, body

    def test_the_runners_exit_reason_names_the_cause_and_the_check(self):
        """``scripts/reconcile_metering.py::_classify`` builds the string the
        FAILING CronJob prints. That is the surface the 2026-09-04 operator
        actually read, so the breakdown has to reach it — not just the API body.
        """
        from scripts import reconcile_metering

        result = main.MeteringReconcileResult(
            scanned=1,
            repaired=0,
            failed=1,
            repaired_license_ids=[],
            failed_license_ids=["019edca0-b1c5-75bc-8f00-79d9b5aef2da"],
            unknown_quota_license_ids=["019edca0-b1c5-75bc-8f00-79d9b5aef2da"],
            unknown_quota_reasons={"foreign_tenant": 1},
        )
        code, reason = reconcile_metering._classify(result, live_budget_owing=1, limit=100)

        assert code == reconcile_metering.EXIT_TRANSPORT, (code, reason)
        assert "foreign_tenant" in reason, reason
        # The remediation, not just the label: an operator who has never read
        # this file must be able to act on the line alone.
        assert "request_quota_config.tenant_id" in reason, reason

    async def test_the_404_log_line_names_the_hypothesis_and_the_check(self, monkeypatch, caplog):
        """The reason label lands in a summary; the LOG LINE is what an operator
        greps first, and on 2026-09-04 it said only "billing returned 404".

        A reason nobody can act on is the same defect one layer up, so the line
        must carry the hypothesis (tenant of record diverged) AND the check (the
        two columns to compare) — not a code to go look up.
        """
        import logging

        self._no_sleep(monkeypatch)
        client = self._client(status=404, payload={"detail": "not found"})

        with caplog.at_level(logging.WARNING, logger="main"):
            await self._read(client)

        line = "\n".join(r.getMessage() for r in caplog.records)
        assert "tenant" in line.lower(), line
        assert "licenses.license.tenant_id" in line, line
        assert "billing.request_quota_config.tenant_id" in line, line
        # The licence it is about, so the line is self-contained in a log search.
        assert "019edca0-b1c5-75bc-8f00-79d9b5aef2da" in line, line

    async def test_a_generic_status_does_not_claim_a_tenant_divergence(self, monkeypatch, caplog):
        """Discriminating half of the line above: the actionable text must be on
        the 404 arm ONLY. Hoisted above the status check it would accuse a
        divergence on every failed read, which is worse than the bare line."""
        import logging

        self._no_sleep(monkeypatch)
        client = self._client(status=403, payload={"detail": "denied"})

        with caplog.at_level(logging.WARNING, logger="main"):
            await self._read(client)

        line = "\n".join(r.getMessage() for r in caplog.records)
        assert "billing.request_quota_config.tenant_id" not in line, line
