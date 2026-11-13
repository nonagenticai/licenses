"""The nightly reaper must do the WHOLE of a revoke, not just the row write.

`scripts/reap_expired_licences.py` archives terminally-expired licences to
`status='revoked'` on a schedule (`licenses-reap-expired-licences`, `11 5 * * *`).
Until licences#195 that was a BARE ORM write — `status`, `revoked_at`,
`updated_at` — and nothing else. `POST /license/revoke` does three things: the
row write, `_teardown_quota_config` (billing's request-RATE mirror) and
`_teardown_token_budget` (billing's SPEND mirror). The CronJob did one of the
three.

⭐ WHY THAT MATTERS AND IS NOT THEORETICAL. The identical gap on the HTTP path was
measured on production 2026-09-09: of 190 active `token_budgets` rows, **19 backed
a licence whose status is `revoked`** — 430,000,000 tokens of allowance behind
licences that no longer exist. Billing's own reconciler
(`deactivate_orphan_license_sync_budgets`) is reachable ONLY through
`POST /api/budget/sync-from-licenses`, which runs on **no timer at all**. So an
armed reaper would have been a DAILY, UNATTENDED producer of orphaned,
still-SPENDABLE budgets with no backstop that would ever notice.

What this file pins
-------------------
* A DRY RUN puts NOTHING on the wire and still REPORTS what it would tear down.
  A dry run that issued a DELETE would not be a dry run; a dry run that reported
  nothing would leave an operator unable to read the blast radius before arming.
* An armed run tears down BOTH halves for EVERY licence it archived, keyed on the
  tenant stored on the licence's own row (a CronJob has no `X-Tenant-Id` header).
* ⛔ THE SCOPE CONTROL: only `source='license_sync'` budgets die. `manual` and
  `topup` rows are operator grants that STACK by design and are frequently
  tenant-wide with `license_id: null`; destroying one on a revoke is unrecoverable.
  This is the property most at risk from a copy-paste re-implementation, and it is
  why the reaper calls `services.metering_teardown` rather than owning its own
  copy — `test_the_reaper_calls_the_same_functions_the_http_path_calls` is the
  test that fails if anyone forks the logic again.
* A failure for ONE licence emits the loud marker and does NOT abort the licences
  after it, and the sweep's exit code goes non-zero so the residue is not green.
* `METERING_SERVICE_URL` unset is reported as SKIPPED, never as success and never
  as a billing failure: nothing was attempted, so there is no billing verdict.
  ⭐ And a skipped teardown over rows that WERE archived exits 1, not 0 — the
  audit line is not scraped and a CronJob's only visible signal is its exit code,
  so exit 0 there means a nightly green Job producing the very orphaned budgets
  this file exists to prevent. Controlled both ways: an empty estate still
  converges (skipping a teardown with no work is correct), and a dry run keeps
  its own measure-only verdict.

⛔ NOT PINNED, DELIBERATELY: any Redis invalidation. The HTTP revoke path
invalidates no Redis key, so requiring one of the reaper would be a NEW divergence
in the opposite direction.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import UUID

import pytest

from models import DatabaseManager, License
from scripts import reap_expired_licences
from services import metering_teardown
from tests.conftest import SEED_TENANT_ID

# The billing fake and its fixtures live in `tests/unit/conftest.py`: the exit-code
# contract file needs the SAME one, and a second private copy would be free to drift
# from this one. `BILLING` is imported because the URLs asserted below are built from it.
from tests.unit.conftest import BILLING

_REAP_ENV = ("REAP_DRY_RUN", "REAP_LIMIT", "REAP_GRACE_DAYS", "REAP_ACTOR")


@pytest.fixture(autouse=True)
def _clean_reap_env(monkeypatch):
    for name in _REAP_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def cli_db(db_manager, monkeypatch):
    """Point the CLI's database seam at the test database."""
    monkeypatch.setattr(reap_expired_licences, "_db_manager", lambda: db_manager)
    return db_manager


def _mk_expired_license(
    db: DatabaseManager, app, plan, *, subject_key: str, tenant_id: str = SEED_TENANT_ID
) -> License:
    """One terminally-expired licence, 60 days past its expiry — this job's population."""
    subject = db.create_or_get_subject(keycloak_sub=subject_key, keycloak_realm="secure-apps")
    now = datetime.now(UTC)
    return db.create_license(
        subject_id=UUID(str(subject.id)),
        app_id=UUID(str(app.id)),
        plan_id=UUID(str(plan.id)),
        tenant_id=tenant_id,
        status="expired",
        starts_at=now - timedelta(days=365),
        expires_at=now - timedelta(days=60),
    )


def _budget_row(row_id: str, *, source: str, active: bool, license_id: str | None, tenant: str):
    return {
        "id": row_id,
        "source": source,
        "active": active,
        "license_id": license_id,
        "tenant_id": tenant,
    }


# ---------------------------------------------------------------------------


class TestADryRunTearsDownNothingAndSaysWhatItWould:
    """⛔ The default. `REAP_DRY_RUN` ships True and arming is a separate decision."""

    def test_a_dry_run_puts_nothing_on_the_wire(
        self, cli_db, seed_app, seed_plan, pinned_service_token, billing_configured, fake_billing
    ):
        """A dry run that issued a DELETE would not be a dry run.

        MUTATION CHECK: dropping the `if dry_run` branch from
        `_teardown_billing_mirrors` turns this red and only this — the armed tests
        below stay green, which is what makes it a control rather than a duplicate.
        """
        lic = _mk_expired_license(cli_db, seed_app, seed_plan, subject_key="reap-dry-1")
        billing = fake_billing({SEED_TENANT_ID: []})

        code, payload = reap_expired_licences.run(
            limit=500, grace_days=30, dry_run=True, actor="test"
        )

        assert billing.gets == [], f"a dry run read billing: {billing.gets}"
        assert billing.quota_deletes == [], billing.quota_deletes
        assert billing.budget_deletes == [], billing.budget_deletes
        assert payload["teardown_mode"] == "dry_run", payload
        assert payload["teardown_attempted"] == 0, payload
        assert cli_db.get_license(UUID(str(lic.id))).status == "expired", (
            "a dry run must not write the row either"
        )
        assert code == reap_expired_licences.EXIT_NOT_CONVERGED, payload

    def test_a_dry_run_reports_the_pairs_it_would_call_billing_about(
        self, cli_db, seed_app, seed_plan
    ):
        """REPORTING is the whole value of the dry run: an operator has to be able
        to read the blast radius on billing BEFORE arming the job. A dry run that
        silently skipped the teardown would be indistinguishable from one that had
        nothing to tear down."""
        lic = _mk_expired_license(cli_db, seed_app, seed_plan, subject_key="reap-dry-2")

        _code, payload = reap_expired_licences.run(
            limit=500, grace_days=30, dry_run=True, actor="test"
        )

        assert payload["teardown_would_call"] == [
            {"license_id": str(lic.id), "tenant_id": SEED_TENANT_ID}
        ], payload


class TestAnArmedSweepTearsDownBothHalves:
    """The defect itself. On origin/main every assertion in this class measured 0."""

    def test_every_archived_licence_loses_its_quota_config_and_its_budget(
        self, cli_db, seed_app, seed_plan, pinned_service_token, billing_configured, fake_billing
    ):
        """TWO licences, so "it tore one down" cannot pass for "it tore them all down"."""
        first = _mk_expired_license(cli_db, seed_app, seed_plan, subject_key="reap-armed-1")
        second = _mk_expired_license(cli_db, seed_app, seed_plan, subject_key="reap-armed-2")
        billing = fake_billing(
            {
                SEED_TENANT_ID: [
                    _budget_row(
                        "b-first",
                        source="license_sync",
                        active=True,
                        license_id=str(first.id),
                        tenant=SEED_TENANT_ID,
                    ),
                    _budget_row(
                        "b-second",
                        source="license_sync",
                        active=True,
                        license_id=str(second.id),
                        tenant=SEED_TENANT_ID,
                    ),
                ]
            }
        )

        code, payload = reap_expired_licences.run(
            limit=500, grace_days=30, dry_run=False, actor="test"
        )

        assert sorted(billing.quota_deletes) == sorted([str(first.id), str(second.id)]), (
            f"both licences owe billing a quota-config delete; got {billing.quota_deletes}"
        )
        assert sorted(billing.budget_deletes) == ["b-first", "b-second"], (
            f"both licences owe billing a budget deactivation; got {billing.budget_deletes}"
        )
        assert payload["teardown_mode"] == "performed", payload
        assert payload["teardown_attempted"] == 2, payload
        assert payload["teardown_quota_failed"] == [], payload
        assert payload["teardown_budget_failed"] == [], payload
        assert code == reap_expired_licences.EXIT_CONVERGED, payload
        for lic in (first, second):
            assert cli_db.get_license(UUID(str(lic.id))).status == "revoked"

    def test_the_tenant_comes_from_the_licence_row(
        self, cli_db, seed_app, seed_plan, pinned_service_token, billing_configured, fake_billing
    ):
        """A CronJob has no `X-Tenant-Id` header, so the row is the only source of
        truth — and it is the right one, because it is the column the create path
        mirrored into billing. Two tenants in one sweep, so a hardcoded or
        first-row-wins tenant cannot pass."""
        other_tenant = "01000000-0000-7000-8000-000000000002"
        mine = _mk_expired_license(cli_db, seed_app, seed_plan, subject_key="reap-t1")
        theirs = _mk_expired_license(
            cli_db, seed_app, seed_plan, subject_key="reap-t2", tenant_id=other_tenant
        )
        billing = fake_billing(
            {
                SEED_TENANT_ID: [
                    _budget_row(
                        "b-mine",
                        source="license_sync",
                        active=True,
                        license_id=str(mine.id),
                        tenant=SEED_TENANT_ID,
                    )
                ],
                other_tenant: [
                    _budget_row(
                        "b-theirs",
                        source="license_sync",
                        active=True,
                        license_id=str(theirs.id),
                        tenant=other_tenant,
                    )
                ],
            }
        )

        _code, payload = reap_expired_licences.run(
            limit=500, grace_days=30, dry_run=False, actor="test"
        )

        assert sorted(billing.gets) == sorted(
            [
                f"{BILLING}/api/budget/list/{SEED_TENANT_ID}",
                f"{BILLING}/api/budget/list/{other_tenant}",
            ]
        ), f"each licence must be looked up under ITS OWN tenant; got {billing.gets}"
        assert sorted(billing.budget_deletes) == ["b-mine", "b-theirs"], billing.budget_deletes
        assert payload["teardown_unresolved_tenant"] == [], payload


class TestOnlyLicenseSyncBudgetsDie:
    """⭐ THE SCOPE CONTROL — the most important class in this file.

    `manual` and `topup` rows are operator grants that STACK by design and are
    frequently tenant-wide with `license_id: null` (prod tenant
    019de842-c92a-7e37-8fdd-eb0d8c5396f9 holds exactly such a row, 40M tokens).
    A reaper that keyed on `license_id` alone, or on "active", would destroy a
    deliberate top-up that no licence ever backed and that no revoke can restore —
    residue is over-permission on a dead licence; this would be a dead cap on a
    live one, which is worse.
    """

    def test_a_manual_grant_survives_the_reaper(
        self, cli_db, seed_app, seed_plan, pinned_service_token, billing_configured, fake_billing
    ):
        lic = _mk_expired_license(cli_db, seed_app, seed_plan, subject_key="reap-scope")
        billing = fake_billing(
            {
                SEED_TENANT_ID: [
                    # (a) the ONLY legitimate target
                    _budget_row(
                        "b-license-sync-mine",
                        source="license_sync",
                        active=True,
                        license_id=str(lic.id),
                        tenant=SEED_TENANT_ID,
                    ),
                    # (b) an operator grant, tenant-wide
                    _budget_row(
                        "b-manual-tenantwide",
                        source="manual",
                        active=True,
                        license_id=None,
                        tenant=SEED_TENANT_ID,
                    ),
                    # (c) an operator top-up that NAMES this licence
                    _budget_row(
                        "b-topup-mine",
                        source="topup",
                        active=True,
                        license_id=str(lic.id),
                        tenant=SEED_TENANT_ID,
                    ),
                    # (d) someone else's cap
                    _budget_row(
                        "b-license-sync-theirs",
                        source="license_sync",
                        active=True,
                        license_id="019e00bb-0000-7000-8000-0000000000bb",
                        tenant=SEED_TENANT_ID,
                    ),
                    # (e) already soft-deleted
                    _budget_row(
                        "b-license-sync-inactive",
                        source="license_sync",
                        active=False,
                        license_id=str(lic.id),
                        tenant=SEED_TENANT_ID,
                    ),
                ]
            }
        )

        _code, payload = reap_expired_licences.run(
            limit=500, grace_days=30, dry_run=False, actor="test"
        )

        assert billing.budget_deletes == ["b-license-sync-mine"], (
            "the reaper must delete exactly the active license_sync row for THIS "
            f"licence — a manual or topup grant it destroyed cannot be restored; got "
            f"{billing.budget_deletes}"
        )
        assert payload["teardown_budget_failed"] == [], payload

    def test_the_reaper_calls_the_same_functions_the_http_path_calls(self):
        """⛔ THE ANTI-FORK CONTROL, and the reason licences#195 was a refactor.

        The scope above is a property of ONE implementation. If the reaper ever
        grows its own copy — which is what it would have to do if the logic stayed
        in `main`, a module a CronJob cannot import — the copy can drift and every
        behavioural test above would keep passing against the fork. So this asserts
        IDENTITY: the route and the job must reach the same function objects.
        """
        import main

        assert main._teardown_quota_config is metering_teardown._teardown_quota_config
        assert main._teardown_token_budget is metering_teardown._teardown_token_budget
        source = (
            __import__("pathlib").Path(reap_expired_licences.__file__).read_text(encoding="utf-8")
        )
        assert "metering_teardown" in source, (
            "the reaper must reach the shared teardowns, not re-implement them"
        )


class TestAFailureIsLoudAndDoesNotStopTheSweep:
    def test_one_refused_teardown_does_not_abort_the_others(
        self,
        cli_db,
        seed_app,
        seed_plan,
        pinned_service_token,
        billing_configured,
        fake_billing,
        caplog,
    ):
        """Billing answers 403 for the FIRST licence's quota config.

        403 is a verdict about the licences SA's realm roles, not a blip, so it is
        answered once and counted. What must NOT happen is the sweep giving up: the
        second licence is a different customer and its budget is just as spendable.
        """
        first = _mk_expired_license(cli_db, seed_app, seed_plan, subject_key="reap-fail-1")
        second = _mk_expired_license(cli_db, seed_app, seed_plan, subject_key="reap-fail-2")
        billing = fake_billing(
            {
                SEED_TENANT_ID: [
                    _budget_row(
                        "b-fail-1",
                        source="license_sync",
                        active=True,
                        license_id=str(first.id),
                        tenant=SEED_TENANT_ID,
                    ),
                    _budget_row(
                        "b-fail-2",
                        source="license_sync",
                        active=True,
                        license_id=str(second.id),
                        tenant=SEED_TENANT_ID,
                    ),
                ]
            },
            quota_status={str(first.id): 403},
        )

        with caplog.at_level(logging.ERROR):
            code, payload = reap_expired_licences.run(
                limit=500, grace_days=30, dry_run=False, actor="test"
            )

        assert sorted(billing.quota_deletes) == sorted([str(first.id), str(second.id)]), (
            f"a refusal for one licence must not skip the next; got {billing.quota_deletes}"
        )
        assert sorted(billing.budget_deletes) == ["b-fail-1", "b-fail-2"], (
            "and it must not suppress the SPEND half either — the two are graded "
            f"separately on purpose; got {billing.budget_deletes}"
        )
        assert payload["teardown_quota_failed"] == [str(first.id)], payload
        assert payload["teardown_budget_failed"] == [], payload

        loud = [
            r.getMessage()
            for r in caplog.records
            if metering_teardown.QUOTA_TEARDOWN_FAILURE_MARKER in r.getMessage()
        ]
        assert len(loud) == 1, (
            "a partial teardown must be greppable in Loki, not silent — that is what "
            f"let the original defect survive; got {[r.getMessage() for r in caplog.records]}"
        )
        assert str(first.id) in loud[0] and SEED_TENANT_ID in loud[0], loud[0]
        assert metering_teardown.QUOTA_TEARDOWN_RECONCILE_PATH in loud[0], loud[0]

        assert code == reap_expired_licences.EXIT_NOT_CONVERGED, (
            f"billing residue must not report a green Job; got {payload}"
        )
        assert "BILLING RESIDUE" in payload["reason"], payload
        # The ROW half still succeeded — residue is not a reason to un-revoke.
        assert cli_db.get_license(UUID(str(first.id))).status == "revoked"

    def test_a_teardown_that_cannot_run_at_all_still_reports_the_archive(
        self,
        cli_db,
        seed_app,
        seed_plan,
        billing_configured,
        monkeypatch,
        caplog,
    ):
        """The teardown blows up OUTSIDE any per-licence guard — the tenant lookup.

        ``_teardown_one`` isolates a per-licence failure, but it cannot cover the DB
        read that feeds it, the import that loads it, or the event loop that runs it.
        The rows are ALREADY COMMITTED by the time any of those run, so an escape here
        must not become a bare exit 2 from ``main_cli``'s catch-all: that would drop
        ``archived`` off the audit line and tell the operator nothing about the
        residue this sweep had just created. Losing the report is worse than the
        failure.
        """
        lic = _mk_expired_license(cli_db, seed_app, seed_plan, subject_key="reap-explode")

        def _boom(_ids):
            raise RuntimeError("connection reset by peer")

        monkeypatch.setattr(cli_db, "tenants_for_licenses", _boom)

        with caplog.at_level(logging.ERROR):
            code, payload = reap_expired_licences.run(
                limit=500, grace_days=30, dry_run=False, actor="test"
            )

        assert payload["archived"] == 1, (
            f"the row write succeeded and the audit line must still say so; got {payload}"
        )
        assert payload["teardown_mode"] == "error", payload
        # BOTH halves: an escape says nothing about which, if any, calls got out.
        assert payload["teardown_quota_failed"] == [str(lic.id)], payload
        assert payload["teardown_budget_failed"] == [str(lic.id)], payload
        assert code == reap_expired_licences.EXIT_NOT_CONVERGED, (
            f"unknown residue is residue, not a green Job; got {payload}"
        )
        assert "BILLING RESIDUE" in payload["reason"], payload
        assert cli_db.get_license(UUID(str(lic.id))).status == "revoked"
        assert any("only the teardown failed" in r.getMessage() for r in caplog.records), (
            f"the escape must be greppable; got {[r.getMessage() for r in caplog.records]}"
        )

    def test_an_unreadable_budget_list_is_reported_not_read_as_empty(
        self,
        cli_db,
        seed_app,
        seed_plan,
        pinned_service_token,
        billing_configured,
        fake_billing,
        caplog,
    ):
        """ "Unknown" is never "nothing to do".

        If billing does not answer the list, concluding there was no budget is how
        the residue becomes INVISIBLE instead of merely present.
        """
        lic = _mk_expired_license(cli_db, seed_app, seed_plan, subject_key="reap-unknown")
        # 500 twice: retryable, so the read is attempted twice and then gives up
        # "unknown" rather than "empty".
        billing = fake_billing({SEED_TENANT_ID: []}, list_status=500)

        with caplog.at_level(logging.ERROR):
            code, payload = reap_expired_licences.run(
                limit=500, grace_days=30, dry_run=False, actor="test"
            )

        assert billing.budget_deletes == [], billing.budget_deletes
        assert payload["teardown_budget_failed"] == [str(lic.id)], payload
        assert code == reap_expired_licences.EXIT_NOT_CONVERGED, payload
        assert [
            r
            for r in caplog.records
            if metering_teardown.TOKEN_BUDGET_TEARDOWN_FAILURE_MARKER in r.getMessage()
        ], [r.getMessage() for r in caplog.records]


class TestAnUnconfiguredJobSaysSoInsteadOfPretending:
    def test_an_unset_metering_url_is_skipped_and_loud(
        self, cli_db, seed_app, seed_plan, pinned_service_token, fake_billing, caplog
    ):
        """`METERING_SERVICE_URL` is NOT in the `licenses-env` Secret this job was
        written to need, so this is the realistic first armed run.

        It must not be reported as success (the budgets ARE still live) and it must
        not be reported under a `*_TEARDOWN_FAILED` marker (nothing was attempted,
        so there is no billing verdict — an operator alerting on that rate must not
        have it moved by a missing env var).
        """
        lic = _mk_expired_license(cli_db, seed_app, seed_plan, subject_key="reap-unset")
        billing = fake_billing({SEED_TENANT_ID: []})

        with (
            patch.object(metering_teardown, "METERING_SERVICE_URL", ""),
            caplog.at_level(logging.ERROR),
        ):
            _code, payload = reap_expired_licences.run(
                limit=500, grace_days=30, dry_run=False, actor="test"
            )

        assert (
            billing.gets == [] and billing.quota_deletes == [] and billing.budget_deletes == []
        ), (
            "an unset URL must put nothing on the wire — `f'{\"\"}/api/...'` is a "
            "relative path httpx cannot send"
        )
        assert payload["teardown_mode"] == "skipped", payload
        assert payload["teardown_attempted"] == 0, payload
        assert payload["teardown_quota_failed"] == [], (
            "nothing was attempted, so there is no billing verdict to report"
        )
        skipped = [
            r.getMessage()
            for r in caplog.records
            if reap_expired_licences.TEARDOWN_SKIPPED_MARKER in r.getMessage()
        ]
        assert len(skipped) == 1, [r.getMessage() for r in caplog.records]
        assert str(lic.id) in skipped[0], skipped[0]
        assert not [
            r
            for r in caplog.records
            if metering_teardown.TOKEN_BUDGET_TEARDOWN_FAILURE_MARKER in r.getMessage()
        ], "a missing env var must not read as a billing failure"

    def test_a_skipped_teardown_is_not_convergence(
        self, cli_db, seed_app, seed_plan, pinned_service_token, fake_billing, caplog
    ):
        """⭐ THE EXIT CODE, which is the only part of this a Job status can see.

        Reporting SKIPPED on the audit line is necessary and not sufficient: a CronJob
        pod is not scraped, there is no audit table, and the `reason`/stderr lines are
        read only by someone who already went looking. The Job's success/failure signal
        is the exit code, so a run that archived rows and never told billing must be
        exit 1 — otherwise, armed on a pod without `METERING_SERVICE_URL`, this job is
        GREEN every night while manufacturing exactly the orphaned, still-spendable
        budgets it exists to prevent (19 such rows / 430M tokens measured on production
        2026-09-09), and the only trace is an unscraped stderr line.

        Graded as NOT CONVERGED rather than as a failure (exit 2) because the estate IS
        knowable: the rows are archived and the outstanding work is named. Same code as
        BILLING RESIDUE, because it is the same fact — a dead licence with a live
        budget — reached by not asking rather than by being refused.
        """
        lic = _mk_expired_license(cli_db, seed_app, seed_plan, subject_key="reap-unset-exit")
        fake_billing({SEED_TENANT_ID: []})

        with (
            patch.object(metering_teardown, "METERING_SERVICE_URL", ""),
            caplog.at_level(logging.ERROR),
        ):
            code, payload = reap_expired_licences.run(
                limit=500, grace_days=30, dry_run=False, actor="test"
            )

        assert payload["archived"] == 1, payload
        assert payload["teardown_mode"] == "skipped", payload
        assert code == reap_expired_licences.EXIT_NOT_CONVERGED, (
            f"a sweep that archived {payload['archived']} licence(s) without telling "
            f"billing reported exit {code}; reason={payload['reason']!r}"
        )
        assert payload["exit_code"] == code, payload
        assert reap_expired_licences.TEARDOWN_SKIPPED_MARKER in payload["reason"], payload
        assert str(lic.id) in payload["reason"], payload
        # The cause must be named as the CONFIG defect it is, not laundered into the
        # residue reason — an operator alerting on billing-residue rates must not have
        # that rate moved by a missing env var.
        assert "BILLING RESIDUE" not in payload["reason"], payload

    def test_a_sweep_that_archived_nothing_is_still_converged(
        self, cli_db, pinned_service_token, fake_billing
    ):
        """⛔ THE CONTROL for the leg above, and the reason it is not simply
        `if teardown_mode == "skipped"`.

        An unconfigured job over a CLEAN estate has nothing to tear down, and skipping a
        teardown that had no work is correct convergence. Without this, arming the exit
        code would red the Job on every night the estate is empty — which is most of
        them — and a check that is red when nothing is wrong stops being read.
        """
        fake_billing({SEED_TENANT_ID: []})

        with patch.object(metering_teardown, "METERING_SERVICE_URL", ""):
            code, payload = reap_expired_licences.run(
                limit=500, grace_days=30, dry_run=False, actor="test"
            )

        assert payload["archived"] == 0 and payload["reapable_before"] == 0, payload
        assert code == reap_expired_licences.EXIT_CONVERGED, payload
        assert reap_expired_licences.TEARDOWN_SKIPPED_MARKER not in payload["reason"], payload

    def test_the_dry_run_verdict_is_unchanged_by_the_missing_url(
        self, cli_db, seed_app, seed_plan, pinned_service_token, fake_billing
    ):
        """A dry run's exit code answers about the ESTATE, and that must not move.

        It already exits 1 over reapable rows (measure-only), so the exit code alone
        cannot distinguish "unchanged" from "newly broken" — hence the assertion on the
        REASON. A dry run archived nothing, so there is no skipped teardown to grade;
        blaming it for the unset variable would make the pre-arm dry run unreadable.
        """
        _mk_expired_license(cli_db, seed_app, seed_plan, subject_key="reap-unset-dry")
        fake_billing({SEED_TENANT_ID: []})

        with patch.object(metering_teardown, "METERING_SERVICE_URL", ""):
            code, payload = reap_expired_licences.run(
                limit=500, grace_days=30, dry_run=True, actor="test"
            )

        assert code == reap_expired_licences.EXIT_NOT_CONVERGED, payload
        assert payload["teardown_mode"] == "dry_run", payload
        assert "NOT CONVERGED (measure-only)" in payload["reason"], payload
        assert reap_expired_licences.TEARDOWN_SKIPPED_MARKER not in payload["reason"], payload


class TestTheTenantLookup:
    def test_it_reads_the_tenant_off_each_row(self, db_manager, seed_app, seed_plan):
        other = "01000000-0000-7000-8000-000000000002"
        mine = _mk_expired_license(db_manager, seed_app, seed_plan, subject_key="tl-1")
        theirs = _mk_expired_license(
            db_manager, seed_app, seed_plan, subject_key="tl-2", tenant_id=other
        )
        assert db_manager.tenants_for_licenses([str(mine.id), str(theirs.id)]) == {
            str(mine.id): SEED_TENANT_ID,
            str(theirs.id): other,
        }

    def test_an_unknown_or_malformed_id_is_absent_not_guessed(self, db_manager):
        """⛔ Absent, never defaulted. Sending billing a DELETE under a guessed
        tenant is worse than leaving the mirror up."""
        assert db_manager.tenants_for_licenses([]) == {}
        assert db_manager.tenants_for_licenses(["not-a-uuid"]) == {}
        assert db_manager.tenants_for_licenses(["019e00aa-0000-7000-8000-0000000000aa"]) == {}
