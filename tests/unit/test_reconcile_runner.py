"""The reconcile sweep must be RUNNABLE BY THE SERVICE ACCOUNT, and never green when vacuous.

WHY THIS FILE EXISTS
--------------------
#105 shipped ``POST /licenses/admin/metering/reconcile`` and nothing called it.
Measured in the prod licences database on 2026-08-10::

    SELECT count(*), count(metering_verified_at) FROM public.license;
    -> 208 | 0

``metering_verified_at`` is written ONLY by this sweep, so NULL on 208 of 208
rows is proof it has never run in production. Merging #105 changed nothing.

The reason a CronJob could not simply curl the route: it is
``Depends(require_any_role("license-admin"))``, ``require_any_role`` tests REALM
roles only, and the licences service account does not have that role in
``secure-apps`` (its live token decodes to ``billing-internal``,
``default-roles-secure-apps``, ``offline_access``, ``service-internal``,
``uma_authorization``). Hence ``main.run_metering_reconcile`` — the same code,
callable with no HTTP and no ``license-admin`` — and
``scripts/reconcile_metering.py`` over it.

⚠️ THE TRAP THESE TESTS EXIST FOR. Once a repairing run has stamped every
licence, the next ``revalidate=false`` run scans 0, repairs 0 and succeeds —
forever, while measuring nothing. That is #105's own bug (a stamp read as a
measurement) reintroduced in the scheduler. ``TestTheAntiVacuityGuard`` pins
BOTH halves: a vacuous sweep must exit 1, and a genuinely converged estate must
still exit 0. A guard with only the first half is unfalsifiable; a guard with
only the second is not a guard.

Every test here runs against the real Postgres schema and real seeded rows. None
of them can pass on an empty database: the guard tests assert a NON-ZERO
independent count, and the filter tests assert one licence is selected out of
two that both exist.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import main
from scripts import reconcile_metering
from tests.unit.test_budget_coverage import (
    FakeBilling,
    _patch_billing,
    _set_plan_allowances,
    _set_stamps,
)

RECONCILE_URL = "/licenses/admin/metering/reconcile"
TENANT = "01000000-0000-7000-8000-000000000001"

_RECONCILE_ENV = (
    "RECONCILE_DRY_RUN",
    "RECONCILE_REVALIDATE",
    "RECONCILE_LIMIT",
    "RECONCILE_LICENSE_ID",
    "RECONCILE_ACTOR",
)


@pytest.fixture
def main_db(db_manager):
    """Point ``main``'s module-global DatabaseManager at the test database.

    Both the runner and ``scripts/reconcile_metering.py`` read ``main.db_manager``
    at call time, so this one patch covers both.
    """
    original = main.db_manager
    main.db_manager = db_manager
    try:
        yield db_manager
    finally:
        main.db_manager = original


@pytest.fixture(autouse=True)
def _clean_reconcile_env(monkeypatch):
    """No RECONCILE_* leaks in from the shell or a previous test."""
    for name in _RECONCILE_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def budget_gap(main_db, seed_license, seed_plan):
    """One live licence on a plan that owes billing a token budget, unverified."""
    _set_plan_allowances(main_db, seed_plan.id, quota=None, budget=10_000_000)
    _set_stamps(main_db, seed_license.id, provisioned=None, verified=None)
    return str(seed_license.id)


@pytest.fixture
def two_budget_gaps(main_db, seed_license, seed_second_license, seed_plan):
    """TWO live licences that both owe a budget — the canary's control group.

    Both must be genuinely selectable, otherwise a ``license_id`` filter that did
    nothing at all would still look like it picked one.
    """
    _set_plan_allowances(main_db, seed_plan.id, quota=None, budget=10_000_000)
    _set_stamps(main_db, seed_license.id, provisioned=None, verified=None)
    _set_stamps(main_db, seed_second_license.id, provisioned=None, verified=None)
    return str(seed_license.id), str(seed_second_license.id)


def _result(**overrides: Any) -> main.MeteringReconcileResult:
    """A minimal converged result, for tests that stub the runner out."""
    base: dict[str, Any] = {
        "scanned": 1,
        "repaired": 1,
        "failed": 0,
        "repaired_license_ids": [str(uuid4())],
        "failed_license_ids": [],
    }
    base.update(overrides)
    return main.MeteringReconcileResult(**base)


class TestTheRunnerIsCallableWithoutHTTP:
    """The whole point: the scheduled identity cannot reach the route."""

    async def test_the_runner_repairs_with_no_http_layer_and_no_license_admin(
        self, main_db, budget_gap
    ):
        """No TestClient, no token, no ``license-admin`` — just the function."""
        fake = FakeBilling()

        patcher = _patch_billing(fake)
        try:
            result = await main.run_metering_reconcile(actor="cronjob:test")
        finally:
            patcher.stop()

        assert result.scanned == 1, result
        assert result.repaired == 1, result
        assert result.budgets_created == 1, result
        assert len(fake.budgets_for(budget_gap)) == 1, fake.budgets

    async def test_the_runner_returns_what_the_route_returns(
        self, auth_client_admin: TestClient, main_db, budget_gap
    ):
        """Same estate, same billing, two entry points — one answer.

        Dry runs on both sides so neither pass mutates what the other measures.
        """
        fake = FakeBilling()
        patcher = _patch_billing(fake)
        try:
            via_http = auth_client_admin.post(f"{RECONCILE_URL}?dry_run=true").json()
            via_runner = await main.run_metering_reconcile(actor="cronjob:test", dry_run=True)
        finally:
            patcher.stop()

        assert via_runner.model_dump() == via_http, (via_runner.model_dump(), via_http)
        assert via_http["scanned"] == 1, via_http
        assert via_http["uncovered_budget_license_ids"] == [budget_gap], via_http


class TestTheRouteIsAThinWrapper:
    """Pin the route to forwarding ONLY, so the two paths cannot drift apart."""

    def test_the_route_forwards_every_argument_to_the_runner(
        self, auth_client_admin: TestClient, main_db
    ):
        lic = str(uuid4())
        stub = AsyncMock(return_value=_result())

        with patch.object(main, "run_metering_reconcile", stub):
            response = auth_client_admin.post(
                f"{RECONCILE_URL}?limit=7&revalidate=true&dry_run=true"
                f"&force_plan_quota=true&license_id={lic}"
            )

        assert response.status_code == 200, response.text
        stub.assert_awaited_once()
        assert _awaited_args(stub) == (), "the runner is keyword-only"
        assert _awaited_kwargs(stub) == {
            "limit": 7,
            "revalidate": True,
            "dry_run": True,
            "force_plan_quota": True,
            "actor": "admin",
            "license_ids": [lic],
        }, _awaited_kwargs(stub)

    def test_the_route_defaults_match_the_runner_defaults(
        self, auth_client_admin: TestClient, main_db
    ):
        """An unspecified query param must not invent a different default."""
        stub = AsyncMock(return_value=_result())

        with patch.object(main, "run_metering_reconcile", stub):
            auth_client_admin.post(RECONCILE_URL)

        kwargs = _awaited_kwargs(stub)
        assert kwargs["limit"] == 500, kwargs
        assert kwargs["revalidate"] is False, kwargs
        assert kwargs["dry_run"] is False, kwargs
        # DEFAULT OFF. force_plan_quota is the destructive opt-in that pushes the
        # plan value over an operator's quota; a query param defaulting to True
        # would silently restore the nightly revert this route was fixed to stop.
        assert kwargs["force_plan_quota"] is False, kwargs
        assert kwargs["license_ids"] is None, (
            f"no license_id must mean NO id filter, not an empty one: {kwargs}"
        )

    def test_the_route_returns_the_runners_result_unchanged(
        self, auth_client_admin: TestClient, main_db
    ):
        canned = _result(scanned=3, repaired=2, failed=1, failed_license_ids=["x"])
        stub = AsyncMock(return_value=canned)

        with patch.object(main, "run_metering_reconcile", stub):
            body = auth_client_admin.post(RECONCILE_URL).json()

        assert body == canned.model_dump(), body

    def test_the_actor_is_the_operator_not_a_constant(self, auth_client_admin, main_db):
        stub = AsyncMock(return_value=_result())

        with patch.object(main, "run_metering_reconcile", stub):
            auth_client_admin.post(RECONCILE_URL)

        assert _awaited_kwargs(stub)["actor"] == "admin", _awaited_kwargs(stub)


class TestTheLicenceIdFilter:
    """A single-licence canary. ``limit=1`` cannot do this job — it picks whichever
    licence sharing the earliest ``created_at`` happens to sort first."""

    async def test_it_selects_exactly_that_licence_and_nothing_else(self, main_db, two_budget_gaps):
        first, second = two_budget_gaps
        fake = FakeBilling()

        patcher = _patch_billing(fake)
        try:
            result = await main.run_metering_reconcile(actor="cronjob:test", license_ids=[second])
        finally:
            patcher.stop()

        assert result.scanned == 1, (
            f"both licences are selectable, so scanning 2 means the filter did nothing: {result}"
        )
        assert result.repaired_license_ids == [second], result
        assert fake.budgets_for(second), fake.budgets
        assert not fake.budgets_for(first), (
            f"the unnamed licence must not be touched: {fake.budgets}"
        )
        assert _verified(main_db, second) is not None, "the named licence must be stamped"
        assert _verified(main_db, first) is None, "the unnamed licence must NOT be stamped"

    async def test_it_narrows_and_never_widens(self, main_db, seed_revoked_license, seed_plan):
        """Naming a licence does not exempt it from the live/owing rules."""
        _set_plan_allowances(main_db, seed_plan.id, quota=None, budget=10_000_000)
        fake = FakeBilling()

        patcher = _patch_billing(fake)
        try:
            result = await main.run_metering_reconcile(
                actor="cronjob:test", license_ids=[str(seed_revoked_license.id)]
            )
        finally:
            patcher.stop()

        assert result.scanned == 0, f"a revoked licence is not live: {result}"
        assert fake.budget_post_count == 0, fake.post_urls

    async def test_an_empty_list_selects_nothing(self, main_db, two_budget_gaps):
        """``[]`` is not silently promoted to "no filter"."""
        fake = FakeBilling()

        patcher = _patch_billing(fake)
        try:
            result = await main.run_metering_reconcile(actor="cronjob:test", license_ids=[])
        finally:
            patcher.stop()

        assert result.scanned == 0, result
        assert fake.budget_post_count == 0, fake.post_urls

    async def test_a_malformed_id_is_rejected_before_the_scan(self, main_db, two_budget_gaps):
        """A typo'd canary id must never read as "that licence is fine"."""
        fake = FakeBilling()

        patcher = _patch_billing(fake)
        try:
            with pytest.raises(main.HTTPException) as excinfo:
                await main.run_metering_reconcile(actor="cronjob:test", license_ids=["not-a-uuid"])
        finally:
            patcher.stop()

        assert excinfo.value.status_code == 400, excinfo.value
        assert fake.budget_post_count == 0, fake.post_urls

    def test_the_route_exposes_it_as_a_query_param(
        self, auth_client_admin: TestClient, main_db, two_budget_gaps
    ):
        _first, second = two_budget_gaps
        fake = FakeBilling()

        patcher = _patch_billing(fake)
        try:
            body = auth_client_admin.post(f"{RECONCILE_URL}?license_id={second}").json()
        finally:
            patcher.stop()

        assert body["scanned"] == 1, body
        assert body["repaired_license_ids"] == [second], body


class TestTheIndependentCount:
    """The guard's second opinion. Its own SQL, so it can disagree with the sweep."""

    def test_it_counts_live_budget_owing_licences_only(
        self, main_db, seed_license, seed_second_license, seed_revoked_license, seed_plan
    ):
        _set_plan_allowances(main_db, seed_plan.id, quota=None, budget=10_000_000)

        assert reconcile_metering.count_live_budget_owing_licenses() == 2, (
            "two live licences owe a budget; the revoked one does not"
        )

    def test_a_plan_that_owes_nothing_is_not_counted(
        self, main_db, seed_license, seed_second_license, seed_plan
    ):
        _set_plan_allowances(main_db, seed_plan.id, quota=None, budget=None)

        assert reconcile_metering.count_live_budget_owing_licenses() == 0, (
            "NULL quota and NULL budget = unlimited; billing is owed nothing"
        )

    def test_an_expired_licence_is_not_counted(self, main_db, seed_plan, expired_license):
        _set_plan_allowances(main_db, seed_plan.id, quota=None, budget=10_000_000)

        assert reconcile_metering.count_live_budget_owing_licenses() == 0, (
            "status='active' past expires_at is the renewal-deadlock shape, not a live licence"
        )

    def test_an_already_stamped_licence_is_still_counted(self, main_db, budget_gap):
        """The whole point. If the count honoured ``metering_verified_at`` it would
        agree with the sweep by construction and the guard could never fire."""
        _set_stamps(main_db, budget_gap, verified=datetime.now(UTC))

        assert reconcile_metering.count_live_budget_owing_licenses() == 1, (
            "the independent count must NOT read the sweep's own stamp"
        )

    def test_it_can_be_scoped_to_one_licence(self, main_db, two_budget_gaps):
        first, _second = two_budget_gaps

        assert reconcile_metering.count_live_budget_owing_licenses() == 2
        assert reconcile_metering.count_live_budget_owing_licenses(first) == 1


class TestTheAntiVacuityGuard:
    """BOTH halves. A guard that can only fire, or only pass, is not a guard."""

    def test_a_sweep_that_scanned_nothing_over_a_live_estate_exits_1(
        self, main_db, budget_gap, capsys
    ):
        """The exact steady state a naive CronJob reaches on its SECOND run: every
        licence already stamped, so ``revalidate=false`` selects nothing."""
        _set_stamps(main_db, budget_gap, verified=datetime.now(UTC))
        fake = FakeBilling()

        patcher = _patch_billing(fake)
        try:
            code = reconcile_metering.main_cli([])
        finally:
            patcher.stop()

        payload = _one_json_line(capsys)
        assert code == 1, payload
        assert payload["scanned"] == 0, payload
        assert payload["live_budget_owing_licenses"] == 1, payload
        assert "VACUOUS" in payload["reason"], payload
        assert fake.budget_post_count == 0, fake.post_urls

    def test_a_genuinely_converged_estate_exits_0(self, main_db, seed_license, seed_plan, capsys):
        """The green half. Nothing live owes billing anything, so scanning 0 is the
        truth — the guard must not fire on it."""
        _set_plan_allowances(main_db, seed_plan.id, quota=None, budget=None)
        fake = FakeBilling()

        patcher = _patch_billing(fake)
        try:
            code = reconcile_metering.main_cli([])
        finally:
            patcher.stop()

        payload = _one_json_line(capsys)
        assert code == 0, payload
        assert payload["scanned"] == 0, payload
        assert payload["live_budget_owing_licenses"] == 0, payload

    def test_revalidate_true_over_a_stamped_estate_is_not_vacuous(
        self, main_db, budget_gap, capsys
    ):
        """The escape hatch the guard's message points at must actually work."""
        _set_stamps(main_db, budget_gap, verified=datetime.now(UTC))
        fake = FakeBilling()

        patcher = _patch_billing(fake)
        try:
            with patch.dict("os.environ", {"RECONCILE_REVALIDATE": "true"}):
                code = reconcile_metering.main_cli([])
        finally:
            patcher.stop()

        payload = _one_json_line(capsys)
        assert code == 0, payload
        assert payload["scanned"] == 1, payload
        assert payload["budgets_created"] == 1, payload


class TestTheExitCodeContract:
    """0 converged / 1 unconverged / 2 transport — each against a real condition."""

    def test_a_repairing_run_that_converged_exits_0(self, main_db, budget_gap, capsys):
        fake = FakeBilling()

        patcher = _patch_billing(fake)
        try:
            code = reconcile_metering.main_cli([])
        finally:
            patcher.stop()

        payload = _one_json_line(capsys)
        assert code == 0, payload
        assert payload["scanned"] == 1 and payload["repaired"] == 1, payload
        assert payload["failed"] == 0 and payload["unknown_budget_license_ids"] == [], payload
        assert _verified(main_db, budget_gap) is not None

    def test_a_licence_billing_refused_exits_1(self, main_db, budget_gap, capsys):
        """billing answers the coverage read but REJECTS the budget POST."""
        fake = FakeBilling(budget_post_status=500)

        patcher = _patch_billing(fake)
        try:
            code = reconcile_metering.main_cli([])
        finally:
            patcher.stop()

        payload = _one_json_line(capsys)
        assert code == 1, payload
        assert payload["failed_license_ids"] == [budget_gap], payload
        assert _verified(main_db, budget_gap) is None, "a failed licence must not be stamped"

    def test_coverage_billing_would_not_disclose_exits_2(self, main_db, budget_gap, capsys):
        """billing will not answer the list read at all — transport/auth, not a
        licence-level failure, and NOTHING may be created on an unknown state."""
        fake = FakeBilling(list_status=503)

        patcher = _patch_billing(fake)
        try:
            code = reconcile_metering.main_cli([])
        finally:
            patcher.stop()

        payload = _one_json_line(capsys)
        assert code == 2, payload
        assert payload["unknown_budget_license_ids"] == [budget_gap], payload
        assert fake.budget_post_count == 0, fake.post_urls

    def test_a_database_failure_exits_2_without_leaking_the_exception(
        self, main_db, budget_gap, capsys
    ):
        """The audit line names the failure CLASS, never the exception text.

        🔴 REGRESSION GUARD. This printed ``repr(e)``, so a typo'd
        ``RECONCILE_LICENSE_ID`` put ``invalid input syntax for type uuid …
        unnamed portal parameter $1 = '…'`` on stdout — SQL context plus bound
        parameters, the exact shape #104 removed from the HTTP surface. A
        psycopg connection failure stringifies to the DSN the same way.
        """
        secret_shaped = "SELECT ... WHERE dsn=postgresql://licenses_user:hunter2@host/db"  # pragma: allowlist secret
        boom = AsyncMock(side_effect=RuntimeError(secret_shaped))

        with patch.object(main, "run_metering_reconcile", boom):
            code = reconcile_metering.main_cli([])

        payload = _one_json_line(capsys)
        assert code == 2, payload
        assert payload["error_kind"] == "RuntimeError", payload
        assert secret_shaped not in json.dumps(payload), (
            f"the caught exception must not reach the audit line: {payload}"
        )
        assert "hunter2" not in json.dumps(payload), payload

    def test_a_malformed_env_flag_refuses_to_run_and_exits_2(self, main_db, budget_gap, capsys):
        """``RECONCILE_DRY_RUN=ture`` must not quietly mean "write to billing"."""
        stub = AsyncMock(return_value=_result())

        with patch.object(main, "run_metering_reconcile", stub):
            with patch.dict("os.environ", {"RECONCILE_DRY_RUN": "ture"}):
                code = reconcile_metering.main_cli([])

        payload = _one_json_line(capsys)
        assert code == 2, payload
        stub.assert_not_awaited()


class TestTheJobIsConfiguredFromEnv:
    def test_every_reconcile_env_var_reaches_the_runner(self, main_db, capsys):
        lic = str(uuid4())
        stub = AsyncMock(return_value=_result(scanned=1))

        env = {
            "RECONCILE_LIMIT": "42",
            "RECONCILE_REVALIDATE": "true",
            "RECONCILE_DRY_RUN": "1",
            "RECONCILE_FORCE_PLAN_QUOTA": "true",
            "RECONCILE_LICENSE_ID": lic,
            "RECONCILE_ACTOR": "cronjob:nightly",
        }
        with patch.object(main, "run_metering_reconcile", stub):
            with patch.dict("os.environ", env):
                reconcile_metering.main_cli([])

        assert _awaited_kwargs(stub) == {
            "limit": 42,
            "revalidate": True,
            "dry_run": True,
            "force_plan_quota": True,
            "actor": "cronjob:nightly",
            "license_ids": [lic],
        }, _awaited_kwargs(stub)

    def test_force_plan_quota_defaults_off_when_the_manifest_omits_it(self, main_db, capsys):
        """⚠️ The scheduled path is the one that did the damage — the CronJob runs
        every night with ``RECONCILE_DRY_RUN=false RECONCILE_REVALIDATE=true``.
        An unset ``RECONCILE_FORCE_PLAN_QUOTA`` must mean "leave operator quotas
        alone", never "propagate the plan"."""
        stub = AsyncMock(return_value=_result(scanned=1))

        with patch.object(main, "run_metering_reconcile", stub):
            with patch.dict("os.environ", {"RECONCILE_ACTOR": "cronjob:nightly"}):
                reconcile_metering.main_cli([])

        assert _awaited_kwargs(stub)["force_plan_quota"] is False, _awaited_kwargs(stub)

    def test_a_dry_run_writes_nothing(self, main_db, budget_gap, capsys):
        fake = FakeBilling()

        patcher = _patch_billing(fake)
        try:
            with patch.dict("os.environ", {"RECONCILE_DRY_RUN": "true"}):
                code = reconcile_metering.main_cli([])
        finally:
            patcher.stop()

        payload = _one_json_line(capsys)
        assert payload["dry_run"] is True, payload
        assert fake.budget_post_count == 0, fake.post_urls
        assert fake.quota_configs == {}, fake.quota_configs
        assert _verified(main_db, budget_gap) is None, "a dry run must not stamp"
        # The licence is uncovered, so this dry run is NOT converged — see
        # TestMeasureOnlyRunsAreJudgedOnCoverage for why that is exit 1.
        assert code == 1, payload

    def test_a_dry_run_over_a_covered_estate_exits_0(self, main_db, budget_gap, capsys):
        """The green half of the dry-run rule: measured everything, found it fine."""
        fake = FakeBilling()
        fake.seed_budget(tenant_id=TENANT, license_id=budget_gap)

        patcher = _patch_billing(fake)
        try:
            with patch.dict("os.environ", {"RECONCILE_DRY_RUN": "true"}):
                code = reconcile_metering.main_cli([])
        finally:
            patcher.stop()

        payload = _one_json_line(capsys)
        assert code == 0, payload
        assert payload["scanned"] == 1, payload
        assert payload["already_covered"] == 1, payload
        assert payload["uncovered_budget_license_ids"] == [], payload

    def test_the_output_is_exactly_one_json_line(self, main_db, budget_gap, capsys):
        """``kubectl logs`` is the audit trail — it has to be parseable."""
        fake = FakeBilling()

        patcher = _patch_billing(fake)
        try:
            reconcile_metering.main_cli([])
        finally:
            patcher.stop()

        out = capsys.readouterr().out
        lines = [line for line in out.splitlines() if line.strip()]
        assert len(lines) == 1, out
        payload = json.loads(lines[0])
        assert payload["event"] == "metering_reconcile", payload
        assert payload["actor"] == "cronjob:reconcile-metering", payload
        for key in ("scanned", "repaired", "failed", "exit_code", "live_budget_owing_licenses"):
            assert key in payload, payload


class TestMeasureOnlyRunsAreJudgedOnCoverage:
    """A dry run writes nothing, so ``failed`` is empty BY CONSTRUCTION.

    Judged on ``failed`` alone, a dry run measuring 94 of 98 prod licences
    uncovered exits 0 — "measured everything, all of it broken", the sibling of
    the vacuity failure one column over. And the route's own docs send operators
    to ``dry_run=true`` to ask what is still broken, so this is the mode most
    likely to be believed.
    """

    def test_a_dry_run_with_uncovered_licences_is_not_converged(
        self, main_db, two_budget_gaps, capsys
    ):
        first, second = two_budget_gaps
        fake = FakeBilling()

        patcher = _patch_billing(fake)
        try:
            with patch.dict("os.environ", {"RECONCILE_DRY_RUN": "true"}):
                code = reconcile_metering.main_cli([])
        finally:
            patcher.stop()

        payload = _one_json_line(capsys)
        assert code == 1, payload
        assert sorted(payload["uncovered_budget_license_ids"]) == sorted([first, second]), payload
        assert "NOT CONVERGED" in payload["reason"], payload
        assert payload["failed"] == 0, (
            f"a dry run never populates `failed` — that is the whole trap: {payload}"
        )

    def test_a_repairing_run_that_covered_them_still_exits_0(
        self, main_db, two_budget_gaps, capsys
    ):
        """``uncovered`` is populated on a REPAIRING run too, where it lists what
        was attempted, not what remains. That must not be read as a failure."""
        fake = FakeBilling()

        patcher = _patch_billing(fake)
        try:
            code = reconcile_metering.main_cli([])
        finally:
            patcher.stop()

        payload = _one_json_line(capsys)
        assert code == 0, payload
        assert len(payload["uncovered_budget_license_ids"]) == 2, payload
        assert payload["budgets_created"] == 2, payload


class TestTruncatedSweepsAreNotConverged:
    """``ORDER BY created_at`` + ``LIMIT`` means a schedule can re-measure the same
    oldest N forever and never reach the tail — green indefinitely."""

    def test_scanning_exactly_the_limit_with_more_behind_it_exits_1(
        self, main_db, two_budget_gaps, capsys
    ):
        fake = FakeBilling()
        env = {"RECONCILE_LIMIT": "1", "RECONCILE_REVALIDATE": "true"}

        patcher = _patch_billing(fake)
        try:
            with patch.dict("os.environ", env):
                code = reconcile_metering.main_cli([])
        finally:
            patcher.stop()

        payload = _one_json_line(capsys)
        assert code == 1, payload
        assert payload["scanned"] == 1, payload
        assert payload["live_budget_owing_licenses"] == 2, payload
        assert "TRUNCATED" in payload["reason"], payload

    def test_a_sweep_whose_limit_covers_the_estate_exits_0(self, main_db, budget_gap, capsys):
        """The green half. ``scanned == limit`` is only truncation when there is
        actually more estate behind it — one licence with limit=1 is complete."""
        fake = FakeBilling()
        env = {"RECONCILE_LIMIT": "1", "RECONCILE_REVALIDATE": "true"}

        patcher = _patch_billing(fake)
        try:
            with patch.dict("os.environ", env):
                code = reconcile_metering.main_cli([])
        finally:
            patcher.stop()

        payload = _one_json_line(capsys)
        assert code == 0, payload
        assert payload["scanned"] == 1 and payload["live_budget_owing_licenses"] == 1, payload

    def test_a_canary_is_not_reported_truncated(self, main_db, two_budget_gaps, capsys):
        """The truncation count is scoped to the same licence the sweep was, so a
        one-licence canary is not judged against the whole estate."""
        _first, second = two_budget_gaps
        fake = FakeBilling()
        env = {"RECONCILE_LIMIT": "1", "RECONCILE_LICENSE_ID": second}

        patcher = _patch_billing(fake)
        try:
            with patch.dict("os.environ", env):
                code = reconcile_metering.main_cli([])
        finally:
            patcher.stop()

        payload = _one_json_line(capsys)
        assert code == 0, payload
        assert payload["live_budget_owing_licenses"] == 1, (
            f"the count must be scoped to the canary, not the estate: {payload}"
        )


class TestTheCanaryUnderDryRun:
    """``RECONCILE_LICENSE_ID`` + ``RECONCILE_DRY_RUN`` together — literally the
    first command a staged canary rollout issues.

    🔴 A mutant that made the id filter apply only when ``not dry_run`` passed
    the ENTIRE 946-test suite: nothing exercised the two together.
    """

    async def test_the_filter_still_narrows_under_dry_run(self, main_db, two_budget_gaps):
        first, second = two_budget_gaps
        fake = FakeBilling()

        patcher = _patch_billing(fake)
        try:
            result = await main.run_metering_reconcile(
                actor="cronjob:test", dry_run=True, license_ids=[second]
            )
        finally:
            patcher.stop()

        assert result.scanned == 1, f"the filter must narrow under dry_run too: {result}"
        assert result.uncovered_budget_license_ids == [second], result
        assert first not in result.uncovered_budget_license_ids, result
        assert fake.budget_post_count == 0, fake.post_urls

    def test_the_cli_canary_dry_run_measures_only_that_licence(
        self, main_db, two_budget_gaps, capsys
    ):
        _first, second = two_budget_gaps
        fake = FakeBilling()
        env = {"RECONCILE_DRY_RUN": "true", "RECONCILE_LICENSE_ID": second}

        patcher = _patch_billing(fake)
        try:
            with patch.dict("os.environ", env):
                code = reconcile_metering.main_cli([])
        finally:
            patcher.stop()

        payload = _one_json_line(capsys)
        assert payload["scanned"] == 1, payload
        assert payload["uncovered_budget_license_ids"] == [second], payload
        assert code == 1, payload
        assert fake.budget_post_count == 0, fake.post_urls


class TestEnvValueParsing:
    def test_a_padded_value_is_trimmed_before_use(self, main_db, capsys):
        """A YAML block scalar trailing a newline must not reach the UUID parser."""
        lic = str(uuid4())
        stub = AsyncMock(return_value=_result(scanned=1))

        with patch.object(main, "run_metering_reconcile", stub):
            with patch.dict(
                "os.environ",
                {"RECONCILE_LICENSE_ID": f"  {lic}\n", "RECONCILE_ACTOR": " cronjob:padded "},
            ):
                reconcile_metering.main_cli([])

        kwargs = _awaited_kwargs(stub)
        assert kwargs["license_ids"] == [lic], kwargs
        assert kwargs["actor"] == "cronjob:padded", kwargs

    def test_a_blank_value_means_unset_not_empty(self, main_db, capsys):
        """``RECONCILE_LICENSE_ID: ""`` in a manifest means "sweep the estate" — it
        must not become an id filter, and an empty id filter selects NOTHING."""
        stub = AsyncMock(return_value=_result(scanned=1))

        with patch.object(main, "run_metering_reconcile", stub):
            with patch.dict("os.environ", {"RECONCILE_LICENSE_ID": "   ", "RECONCILE_ACTOR": ""}):
                reconcile_metering.main_cli([])

        kwargs = _awaited_kwargs(stub)
        assert kwargs["license_ids"] is None, kwargs
        assert kwargs["actor"] == "cronjob:reconcile-metering", kwargs

    def test_a_blank_limit_falls_back_to_the_default(self, main_db, capsys):
        stub = AsyncMock(return_value=_result(scanned=1))

        with patch.object(main, "run_metering_reconcile", stub):
            with patch.dict("os.environ", {"RECONCILE_LIMIT": ""}):
                reconcile_metering.main_cli([])

        assert _awaited_kwargs(stub)["limit"] == 500, _awaited_kwargs(stub)

    def test_an_out_of_range_limit_refuses_to_run(self, main_db, capsys):
        """Bounds mirror the route's ``Query(ge=1, le=5000)``."""
        stub = AsyncMock(return_value=_result())

        with patch.object(main, "run_metering_reconcile", stub):
            with patch.dict("os.environ", {"RECONCILE_LIMIT": "0"}):
                code = reconcile_metering.main_cli([])

        payload = _one_json_line(capsys)
        assert code == 2, payload
        assert payload["error_kind"] == "bad_reconcile_env", payload
        stub.assert_not_awaited()


class TestItStartsUnderTheCronJobsRealEnvironment:
    """Proof by EXECUTION, in a subprocess with a constructed env.

    🔴 ``import main`` runs ``_validate_production_config()`` and ``ENVIRONMENT``
    defaults to ``production``, so this script — the FIRST in the repo to import
    ``main`` rather than ``models`` — needs ``METERING_SERVICE_URL`` and
    ``CORS_ALLOWED_ORIGINS``, and NEITHER is in the ``licenses-env`` Secret. On
    the Secret alone it died at import: exit 1, no JSON line, a config failure
    wearing "a licence could not be converged"'s exit code.

    In-process tests cannot catch this: ``tests/conftest.py`` forces
    ``ENVIRONMENT=development`` and ``main`` is already in ``sys.modules`` by the
    time any test runs.
    """

    # Exactly the keys the live licenses-env Secret holds (2026-08-10).
    SECRET_KEYS = {
        "DATABASE_URL": "postgresql+psycopg://user:pass@127.0.0.1:1/licenses",  # pragma: allowlist secret
        "KEYCLOAK_REALM": "secure-apps",
        "KEYCLOAK_URL": "https://keycloak.invalid",
        "POSTGRES_SSLMODE": "disable",
        "SERVICE_CLIENT_ID": "licensing-service",
        "SERVICE_CLIENT_SECRET": "x" * 32,  # pragma: allowlist secret
        "SERVICE_TOKEN_URL": "https://keycloak.invalid/token",
    }

    def test_the_secret_alone_dies_but_reports_it_as_a_config_failure(self):
        proc = _run_script(self.SECRET_KEYS)

        assert proc.returncode == 2, (
            f"a config failure is not 'a licence could not be converged'\n"
            f"stdout={proc.stdout!r}\nstderr={proc.stderr[-2000:]!r}"
        )
        payload = _sole_json_line(proc.stdout)
        assert payload["error_kind"] == "startup_config", payload
        assert "METERING_SERVICE_URL" in payload["error"], payload
        assert "CORS_ALLOWED_ORIGINS" in payload["error"], payload

    def test_with_the_two_deployment_vars_it_starts_and_sweeps(self, main_db, db_manager):
        """The whole CLI end to end — real interpreter, real import, real DB."""
        from tests.conftest import TEST_DATABASE_URL

        env = {
            **self.SECRET_KEYS,
            "DATABASE_URL": TEST_DATABASE_URL,
            "METERING_SERVICE_URL": "http://billing.invalid",
            "CORS_ALLOWED_ORIGINS": "https://portal.invalid",
        }
        proc = _run_script(env)

        payload = _sole_json_line(proc.stdout)
        assert proc.returncode == 0, (
            f"an empty estate is converged\nstdout={proc.stdout!r}\nstderr={proc.stderr[-2000:]!r}"
        )
        assert payload["event"] == "metering_reconcile", payload
        assert payload["scanned"] == 0, payload
        assert payload["live_budget_owing_licenses"] == 0, payload


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _run_script(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run the CLI in a subprocess with ONLY ``env`` (plus PATH/HOME) set.

    A constructed env, not ``os.environ`` plus overrides: the point is to prove
    what happens with exactly the variables a CronJob would provide, and the
    pytest process carries ENVIRONMENT=development, DATABASE_URL and
    PYTEST_CURRENT_TEST, every one of which would hide the failure.
    """
    root = Path(__file__).resolve().parents[2]
    return subprocess.run(
        [sys.executable, str(root / "scripts" / "reconcile_metering.py")],
        cwd=str(root),
        env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""), **env},
        capture_output=True,
        text=True,
        timeout=120,
    )


def _sole_json_line(stdout: str) -> dict[str, Any]:
    lines = [line for line in stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one JSON line on stdout, got {lines!r}"
    return json.loads(lines[0])


@pytest.fixture
def expired_license(db_manager, seed_app, seed_plan):
    """``status='active'`` but past ``expires_at`` — the renewal-deadlock shape.

    Two prod licences are in exactly this state (one of them a real client's), so
    it is not a hypothetical.
    """
    subject = db_manager.create_or_get_subject(
        keycloak_sub="expired-subject", keycloak_realm="secure-apps"
    )
    return db_manager.create_license(
        subject_id=UUID(str(subject.id)),
        app_id=UUID(str(seed_app.id)),
        plan_id=UUID(str(seed_plan.id)),
        tenant_id=TENANT,
        status="active",
        starts_at=datetime.now(UTC) - timedelta(days=60),
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )


def _awaited_kwargs(stub: AsyncMock) -> dict[str, Any]:
    """The kwargs the runner was awaited with, asserting it was awaited at all.

    ``AsyncMock.await_args`` is ``_Call | None``; reading ``.kwargs`` off it
    without this check is both a type error and a test that would blow up with
    an AttributeError instead of saying "the route never called the runner".
    """
    call = stub.await_args
    assert call is not None, "the runner was never awaited"
    return dict(call.kwargs)


def _awaited_args(stub: AsyncMock) -> tuple[Any, ...]:
    call = stub.await_args
    assert call is not None, "the runner was never awaited"
    return tuple(call.args)


def _one_json_line(capsys) -> dict[str, Any]:
    """Parse the single JSON line the CLI must print, failing loudly if it is not one."""
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one JSON line, got {len(lines)}: {out!r}"
    return json.loads(lines[0])


def _verified(db_manager, license_id):
    session = db_manager.get_session()
    try:
        row = session.execute(
            text("SELECT metering_verified_at FROM license WHERE id = :i"),
            {"i": str(license_id)},
        ).first()
        return row[0] if row else None
    finally:
        session.close()
