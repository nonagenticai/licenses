"""The licence-row reaper, and the negative controls that stop it being too greedy.

WHY THIS FILE EXISTS
--------------------
``services/expiry_metrics.py`` buckets the licence expiry gauge on
``(l.status = 'revoked')``, so a row with ``status='expired'`` and a past ``expires_at``
counts under ``revoked="false"`` for ever — the series ``LicenseAlreadyExpired`` reads.
``expire_stale_licences.py`` moves rows INTO ``'expired'``; ``revoke_expired_tokens.py``
reaps TOKENS. Nothing moved a licence row OUT of ``'expired'``, so the gauge LATCHED:
measured live it sat at a constant 2 from the metric's first sample, with zero serving
impact, and every future expiry would have accumulated permanently.

WHAT IS PINNED HERE
-------------------
``DatabaseManager.reap_all_expired_licenses`` / ``count_reapable_licenses`` and
``scripts/reap_expired_licences.py`` (the CLI a CronJob runs over it).

⚠️ A REAPER WITHOUT A NEGATIVE CONTROL IS HOW YOU FIND OUT IT WAS TOO GREEDY IN
PRODUCTION. Every test here seeds an estate of EIGHT lifecycle shapes and only ONE of
them may ever be touched. Two of the seven survivors are the controls the requirement
names explicitly — an ACTIVE licence and a NOT-YET-EXPIRED licence — and the rest cover
perpetual (``expires_at IS NULL``), already-revoked, and the ``status='active'`` +
past-expiry population that belongs to ``expire_stale_licences.py``, not to this job.
A sweep that ignored its predicate entirely is caught by the SAME test that proves it
works. No test here can pass on an empty table.

⚠️ THE GRACE WINDOW IS PART OF THE PREDICATE, not a cosmetic knob, so both halves are
pinned: a row inside the window is NOT reaped at the default, and IS reaped at
``REAP_GRACE_DAYS=0``. A guard that can only fire is noise; one that can only pass is
decoration.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest

from models import DatabaseManager, License
from scripts import reap_expired_licences
from services.expiry_metrics import collect_snapshot
from tests.conftest import SEED_OTHER_TENANT_ID, SEED_TENANT_ID

# ⚠️ `pinned_service_token` / `billing_configured` / `fake_billing` come from
# `tests/unit/conftest.py`, SHARED with `test_the_reaper_tears_down_billing.py` rather
# than re-implemented here — a second copy would be free to drift from the one that file
# measures against, and then "converged" below would stop meaning what it proves.

_REAP_ENV = ("REAP_DRY_RUN", "REAP_LIMIT", "REAP_GRACE_DAYS", "REAP_ACTOR")

#: The cutoff a caller passes when it wants EVERY terminally-expired row regardless of
#: age (grace = 0). Used by the DAL-level tests that are not exercising the window.
_ALL = datetime.now(UTC)


def _shipped_cutoff() -> datetime:
    """The cutoff the CronJob actually computes: ``now - REAP_GRACE_DAYS`` at the
    shipped default. Tests that assert on the SELECTION SIZE must use this, not
    ``_ALL`` — under ``_ALL`` the recently-expired control is legitimately in scope,
    and an assertion written against the wrong cutoff would be testing the wrong job.
    """
    return datetime.now(UTC) - timedelta(days=reap_expired_licences.DEFAULT_GRACE_DAYS)


@pytest.fixture(autouse=True)
def _clean_reap_env(monkeypatch):
    """No REAP_* leaks in from the shell or a previous test."""
    for name in _REAP_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def cli_db(db_manager, monkeypatch):
    """Point the CLI's database seam at the test database."""
    monkeypatch.setattr(reap_expired_licences, "_db_manager", lambda: db_manager)
    return db_manager


def _mk_license(
    db: DatabaseManager,
    app,
    plan,
    *,
    subject_key: str,
    tenant_id: str = SEED_TENANT_ID,
    status: str = "active",
    starts_days: float = -1,
    expires_days: float | None = -1,
) -> License:
    """Seed ONE licence in an explicit lifecycle shape.

    ``expires_days=None`` is a PERPETUAL licence (``expires_at IS NULL``), which is a
    different thing from one that expired long ago — conflating the two is how a reaper
    archives an entire estate.
    """
    subject = db.create_or_get_subject(keycloak_sub=subject_key, keycloak_realm="secure-apps")
    now = datetime.now(UTC)
    return db.create_license(
        subject_id=UUID(str(subject.id)),
        app_id=UUID(str(app.id)),
        plan_id=UUID(str(plan.id)),
        tenant_id=tenant_id,
        status=status,
        starts_at=now + timedelta(days=starts_days),
        expires_at=None if expires_days is None else now + timedelta(days=expires_days),
    )


def _row(db: DatabaseManager, license_id) -> License:
    lic = db.get_license(UUID(str(license_id)))
    assert lic is not None, (
        f"licence {license_id} vanished from the estate — this job DELETES nothing"
    )
    return lic


def _status_of(db: DatabaseManager, license_id) -> str:
    return str(_row(db, license_id).status)


def _one_json_line(capsys) -> dict[str, Any]:
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one JSON line on stdout, got {lines!r}"
    return json.loads(lines[0])


@pytest.fixture
def estate(db_manager, seed_app, seed_plan):
    """EIGHT lifecycle shapes. Only ``ancient_expired`` may ever be touched.

    The two shapes the negative-control requirement names by hand are
    ``active_perpetual`` / ``active_future`` (ACTIVE) and ``active_future`` /
    ``expired_future_window`` (NOT YET EXPIRED).
    """
    return {
        # ── the ONE row this job exists for ────────────────────────────────────
        # terminal, and expired 60 days ago — outside any sane grace window.
        "ancient_expired": _mk_license(
            db_manager,
            seed_app,
            seed_plan,
            subject_key="r-ancient",
            status="expired",
            expires_days=-60,
        ),
        # ── NEGATIVE CONTROLS ─────────────────────────────────────────────────
        # terminal but expired only 5 days ago: inside the 30-day grace window, so
        # 'expired' remains observable for the renewal conversation.
        "recent_expired": _mk_license(
            db_manager,
            seed_app,
            seed_plan,
            subject_key="r-recent",
            status="expired",
            expires_days=-5,
        ),
        # terminal with a FUTURE expires_at — genuinely not yet expired.
        "expired_future_window": _mk_license(
            db_manager,
            seed_app,
            seed_plan,
            subject_key="r-futwin",
            status="expired",
            expires_days=+30,
        ),
        # terminal and PERPETUAL. `expires_at IS NULL` means never-expiring, not
        # unpopulated; dropping that leg of the predicate archives the estate.
        "expired_perpetual": _mk_license(
            db_manager,
            seed_app,
            seed_plan,
            subject_key="r-perp-exp",
            status="expired",
            expires_days=None,
        ),
        # ACTIVE + perpetual: the 256-of-258 shape in production.
        "active_perpetual": _mk_license(
            db_manager, seed_app, seed_plan, subject_key="r-active-perp", expires_days=None
        ),
        # ACTIVE and NOT YET EXPIRED: a live, paid-for entitlement.
        "active_future": _mk_license(
            db_manager, seed_app, seed_plan, subject_key="r-active-fut", expires_days=+30
        ),
        # ACTIVE with a PAST expiry. This is expire_stale_licences.py's population,
        # NOT this job's: reaping it would skip 'expired' entirely and take a live
        # entitlement straight to 'revoked'.
        "active_past": _mk_license(
            db_manager, seed_app, seed_plan, subject_key="r-active-past", expires_days=-60
        ),
        # already where this job would put it — the idempotency shape.
        "already_revoked": _mk_license(
            db_manager,
            seed_app,
            seed_plan,
            subject_key="r-revoked",
            status="revoked",
            expires_days=-60,
        ),
    }


# ---------------------------------------------------------------------------
# The sweep itself
# ---------------------------------------------------------------------------


class TestTheReaperSelectsOnlyTerminallyExpiredRows:
    """One call, a mixed estate. ⛔ The negative controls live HERE, beside the
    positive case, so a predicate that widened is caught by the same test."""

    def test_the_ancient_expired_row_is_archived(self, db_manager, estate):
        """POSITIVE CASE."""
        ids = db_manager.reap_all_expired_licenses(cutoff=_ALL)
        assert str(estate["ancient_expired"].id) in ids, ids
        assert _status_of(db_manager, estate["ancient_expired"].id) == "revoked"

    def test_an_active_licence_is_never_touched(self, db_manager, estate):
        """🔴 NEGATIVE CONTROL — ACTIVE. Archiving one of these is a revenue
        incident: it revokes an entitlement somebody is paying for."""
        db_manager.reap_all_expired_licenses(cutoff=_ALL)
        for key in ("active_perpetual", "active_future", "active_past"):
            assert _status_of(db_manager, estate[key].id) == "active", (
                f"the reaper archived an ACTIVE licence ({key})"
            )

    def test_a_not_yet_expired_licence_is_never_touched(self, db_manager, estate):
        """🔴 NEGATIVE CONTROL — NOT YET EXPIRED, in both shapes: a future
        ``expires_at``, and ``expires_at IS NULL`` (perpetual, which is an encoded
        semantic and not missing data)."""
        db_manager.reap_all_expired_licenses(cutoff=_ALL)
        assert _status_of(db_manager, estate["active_future"].id) == "active"
        assert _status_of(db_manager, estate["expired_future_window"].id) == "expired", (
            "a row whose expires_at is in the FUTURE has not expired yet"
        )
        assert _status_of(db_manager, estate["expired_perpetual"].id) == "expired", (
            "expires_at IS NULL means PERPETUAL — this row can never be reapable"
        )
        assert _status_of(db_manager, estate["active_perpetual"].id) == "active"

    def test_exactly_one_of_the_eight_is_selected(self, db_manager, estate):
        """The whole-population assertion. A sweep that ignored its predicate would
        return eight ids here, and the per-shape tests above would each fail — but this
        is the one that fails on a shape nobody thought to name.

        Evaluated at the SHIPPED cutoff, so this pins what the CronJob as configured
        would actually touch: one row out of eight."""
        ids = db_manager.reap_all_expired_licenses(cutoff=_shipped_cutoff(), dry_run=True)
        assert ids == [str(estate["ancient_expired"].id)], (
            f"expected exactly the ancient expired row, got {ids}"
        )

    def test_it_deletes_nothing(self, db_manager, estate):
        """⛔ A status transition, not a delete. Every row, and every ``expires_at``,
        survives — which is what makes the archive decision reversible by hand."""
        before = {k: (str(v.id), v.expires_at) for k, v in estate.items()}
        db_manager.reap_all_expired_licenses(cutoff=_ALL)
        for key, (lid, expires_at) in before.items():
            row = _row(db_manager, lid)  # asserts the row still exists
            assert row.expires_at == expires_at, f"{key}: expires_at was rewritten"


class TestTheGraceWindow:
    """⚠️ Both halves. The grace window is the one deliberate difference from the
    token reaper, and a window that never excludes anything is not a window."""

    def test_a_recently_expired_row_is_left_alone_at_the_default(self, db_manager, estate):
        """'expired' must survive long enough to be a state a human and an API client
        can observe — otherwise this job quietly deletes the distinction that
        ``expire_stale_licences.py`` exists to create."""
        cutoff = datetime.now(UTC) - timedelta(days=reap_expired_licences.DEFAULT_GRACE_DAYS)
        ids = db_manager.reap_all_expired_licenses(cutoff=cutoff, dry_run=True)
        assert str(estate["recent_expired"].id) not in ids, ids
        assert str(estate["ancient_expired"].id) in ids, ids

    def test_a_zero_grace_reduces_it_to_the_token_reapers_behaviour(self, db_manager, estate):
        """The other half: with the window closed, the recent row IS in scope. If this
        passed under both settings the window would be decoration."""
        ids = db_manager.reap_all_expired_licenses(cutoff=datetime.now(UTC), dry_run=True)
        assert {str(estate["ancient_expired"].id), str(estate["recent_expired"].id)} == set(ids)

    def test_the_cli_default_is_thirty_days(self, cli_db, estate, capsys):
        code = reap_expired_licences.main_cli([])
        payload = _one_json_line(capsys)
        assert payload["grace_days"] == 30, payload
        assert payload["selected_license_ids"] == [str(estate["ancient_expired"].id)], payload
        assert code == 1, "a dry run that found a reapable row is NOT converged"


class TestItIsIdempotentAndUnscoped:
    def test_a_second_sweep_selects_nothing(self, db_manager, estate):
        """The predicate is scoped to ``status='expired'``, so a row this job already
        moved is out of scope by construction."""
        first = db_manager.reap_all_expired_licenses(cutoff=_ALL)
        second = db_manager.reap_all_expired_licenses(cutoff=_ALL)
        assert first and second == []

    def test_an_already_revoked_row_keeps_its_original_revoked_at(self, db_manager, estate):
        """``revoked_at`` is the audit record of when the credential was killed. A
        sweep must never rewrite it — the same rule ``revoke_license`` follows."""
        session = db_manager.get_session()
        try:
            row = session.get(License, UUID(str(estate["already_revoked"].id)))
            original = datetime(2020, 1, 1, tzinfo=UTC)
            row.revoked_at = original
            session.commit()
        finally:
            session.close()
        db_manager.reap_all_expired_licenses(cutoff=_ALL)
        assert _row(db_manager, estate["already_revoked"].id).revoked_at == original

    def test_it_stamps_revoked_at_on_the_transition(self, db_manager, estate):
        assert estate["ancient_expired"].revoked_at is None
        db_manager.reap_all_expired_licenses(cutoff=_ALL)
        assert _row(db_manager, estate["ancient_expired"].id).revoked_at is not None

    def test_it_crosses_tenants_in_one_call(self, db_manager, seed_app, seed_plan):
        """Estate-wide, with no tenant concept at all — the reason this is a DAL sweep
        and not a call to the tenant-scoped list endpoint."""
        a = _mk_license(
            db_manager,
            seed_app,
            seed_plan,
            subject_key="t-a",
            tenant_id=SEED_TENANT_ID,
            status="expired",
            expires_days=-60,
        )
        b = _mk_license(
            db_manager,
            seed_app,
            seed_plan,
            subject_key="t-b",
            tenant_id=SEED_OTHER_TENANT_ID,
            status="expired",
            expires_days=-60,
        )
        ids = db_manager.reap_all_expired_licenses(cutoff=_ALL)
        assert {str(a.id), str(b.id)} == set(ids)

    def test_the_limit_caps_one_sweep(self, db_manager, seed_app, seed_plan):
        for i in range(3):
            _mk_license(
                db_manager,
                seed_app,
                seed_plan,
                subject_key=f"lim-{i}",
                status="expired",
                expires_days=-60 - i,
            )
        assert len(db_manager.reap_all_expired_licenses(cutoff=_ALL, limit=2)) == 2
        assert db_manager.count_reapable_licenses(_ALL) == 1


class TestADryRunWritesNothing:
    """Requirement 1: no-write mode, and it is the DEFAULT."""

    def test_it_reports_the_ids_it_would_change(self, db_manager, estate):
        ids = db_manager.reap_all_expired_licenses(cutoff=_shipped_cutoff(), dry_run=True)
        assert ids == [str(estate["ancient_expired"].id)]

    def test_the_row_is_untouched_afterwards(self, db_manager, estate):
        db_manager.reap_all_expired_licenses(cutoff=_ALL, dry_run=True)
        assert _status_of(db_manager, estate["ancient_expired"].id) == "expired"
        assert _row(db_manager, estate["ancient_expired"].id).revoked_at is None

    def test_the_cli_default_is_a_dry_run(self, cli_db, estate, capsys):
        """⛔ A prod licence table is not a place for an unguarded UPDATE. With no
        REAP_DRY_RUN set at all, nothing may be written."""
        reap_expired_licences.main_cli([])
        payload = _one_json_line(capsys)
        assert payload["dry_run"] is True, payload
        assert payload["archived"] == 0, payload
        assert _status_of(cli_db, estate["ancient_expired"].id) == "expired"

    def test_a_blank_value_falls_back_to_the_default(self, cli_db, estate, capsys):
        """``REAP_DRY_RUN: ""`` in a manifest must DISARM, not arm."""
        with patch.dict(os.environ, {"REAP_DRY_RUN": ""}):
            reap_expired_licences.main_cli([])
        payload = _one_json_line(capsys)
        assert payload["dry_run"] is True, payload
        assert _status_of(cli_db, estate["ancient_expired"].id) == "expired"


class TestTheIndependentReapableCount:
    """The hand-written second opinion behind every guard. If it agreed with the
    sweep's own ORM query by construction, no guard here could ever fail."""

    def test_it_counts_only_terminally_expired_rows_past_the_cutoff(self, db_manager, estate):
        assert db_manager.count_reapable_licenses(_ALL) == 2  # ancient + recent
        cutoff = datetime.now(UTC) - timedelta(days=30)
        assert db_manager.count_reapable_licenses(cutoff) == 1  # ancient only

    def test_it_drops_to_zero_once_the_sweep_has_run(self, db_manager, estate):
        db_manager.reap_all_expired_licenses(cutoff=_ALL)
        assert db_manager.count_reapable_licenses(_ALL) == 0


# ---------------------------------------------------------------------------
# The instrument this fix exists for
# ---------------------------------------------------------------------------


class TestTheLatchedGaugeActuallyClears:
    """⚠️ The end-to-end claim. Everything above proves the sweep is well-behaved;
    only this proves it fixes the thing that was broken — the collector's own
    bucketing, read through ``collect_snapshot``, moving off ``revoked="false"``."""

    def _expired_buckets(self, db_manager) -> dict[tuple[str, str, str], int]:
        session = db_manager.get_session()
        try:
            return collect_snapshot(session).expired
        finally:
            session.close()

    def test_the_gauge_latches_before_the_reaper_runs(self, db_manager, estate):
        """The control. Without it the test below could pass on an empty gauge.

        ⚠️ ASSERTED EXACTLY, not `>= 1`. A `>= 1` here is satisfied by any one of the
        three rows and so cannot distinguish the population it is a control FOR: the
        collector buckets on `expires_at <= now()` REGARDLESS of status, so the alerting
        bucket holds `ancient_expired` + `recent_expired` (both reapable-in-principle)
        AND `active_past`, which is `status='active'` and therefore permanently
        unreachable by this reaper. Naming all three is what makes the -2/+2 in the next
        test a statement about WHICH rows moved rather than merely that the number fell.
        """
        buckets = self._expired_buckets(db_manager)
        assert buckets[("license", "false", "standard")] == 3, (
            "expected exactly ancient_expired + recent_expired + active_past in the "
            f"alerting bucket; got {buckets}"
        )
        assert buckets[("license", "true", "standard")] == 1, (
            f"expected exactly already_revoked in the benign-history bucket: {buckets}"
        )

    def test_an_active_row_that_lapsed_stays_in_the_alerting_bucket(self, db_manager, estate):
        """⚠️ The grace window's real justification, pinned. A freshly-lapsed licence
        enters `revoked="false"` while still `status='active'` — so it is unreachable by
        this reaper no matter what the grace is, and LicenseAlreadyExpired keeps firing
        on it. The reaper removes the LATCH, not the SIGNAL."""
        db_manager.reap_all_expired_licenses(cutoff=_ALL)
        buckets = self._expired_buckets(db_manager)
        assert _status_of(db_manager, estate["active_past"].id) == "active"
        assert buckets[("license", "false", "standard")] == 1, (
            f"active_past must remain in the alerting bucket after a full sweep: {buckets}"
        )

    def test_it_clears_after_the_reaper_runs(self, db_manager, estate):
        before = self._expired_buckets(db_manager)
        db_manager.reap_all_expired_licenses(cutoff=_ALL)
        after = self._expired_buckets(db_manager)
        assert (
            after[("license", "false", "standard")] == before[("license", "false", "standard")] - 2
        ), f"the two reapable rows must leave the alerting bucket: {before} -> {after}"
        assert (
            after[("license", "true", "standard")] == before[("license", "true", "standard")] + 2
        ), "and land in the benign-history bucket rather than vanishing"


# ---------------------------------------------------------------------------
# The guards
# ---------------------------------------------------------------------------


class TestTheAntiVacuityGuards:
    """BOTH halves of each. A guard that can only fire is noise; one that can only
    pass is decoration."""

    def test_a_dry_run_that_found_reapable_rows_exits_1(self, cli_db, estate, capsys):
        """🔴 A dry run writes nothing, so "did it write anything" is empty BY
        CONSTRUCTION. Judged that way, a dry run over a fully latched estate exits 0."""
        code = reap_expired_licences.main_cli([])
        payload = _one_json_line(capsys)
        assert code == 1, payload
        assert payload["reapable_before"] == 1, payload
        assert payload["selected_license_ids"] == [str(estate["ancient_expired"].id)], payload
        assert payload["archived"] == 0, (
            f"a dry run WROTE nothing; the audit line must not say otherwise: {payload}"
        )
        assert "NOT CONVERGED" in payload["reason"], payload

    def test_a_genuinely_clean_estate_exits_0(self, cli_db, seed_app, seed_plan, capsys):
        """The green half. Live licences exist; none of them is reapable."""
        live = _mk_license(cli_db, seed_app, seed_plan, subject_key="g1", expires_days=+30)
        code = reap_expired_licences.main_cli([])
        payload = _one_json_line(capsys)
        assert code == 0, payload
        assert payload["reapable_before"] == 0 and payload["reapable_after"] == 0, payload
        assert _status_of(cli_db, live.id) == "active"

    def test_an_armed_sweep_that_archived_nothing_exits_1(self, cli_db, estate, capsys):
        """A broken selection wearing a green tick."""
        with patch.object(cli_db, "reap_all_expired_licenses", lambda **kw: []):
            with patch.dict(os.environ, {"REAP_DRY_RUN": "false"}):
                code = reap_expired_licences.main_cli([])
        payload = _one_json_line(capsys)
        assert code == 1, payload
        assert "VACUOUS" in payload["reason"], payload

    def test_a_sweep_that_archived_more_than_the_rule_allows_exits_1(self, cli_db, estate, capsys):
        """🔴 The over-fire half — the dangerous direction. A predicate that widened
        would archive live licences and every row it touched looks like a success."""
        healthy = estate["active_future"]
        with patch.object(cli_db, "reap_all_expired_licenses", lambda **kw: [str(healthy.id)] * 5):
            with patch.dict(os.environ, {"REAP_DRY_RUN": "false"}):
                code = reap_expired_licences.main_cli([])
        payload = _one_json_line(capsys)
        assert code == 1, payload
        assert "OVER-FIRED" in payload["reason"], payload

    def test_a_dry_run_that_wrote_anyway_exits_1(self, cli_db, estate, capsys):
        """🔴 The single most dangerous defect this script can have — the operator
        asked it not to touch production."""
        real = cli_db.reap_all_expired_licenses

        def _writes_anyway(**kw):
            kw.pop("dry_run", None)
            return real(dry_run=False, **kw)

        with patch.object(cli_db, "reap_all_expired_licenses", _writes_anyway):
            code = reap_expired_licences.main_cli([])
        payload = _one_json_line(capsys)
        assert code == 1, payload
        assert "DRY RUN WROTE" in payload["reason"], payload
        assert _status_of(cli_db, estate["ancient_expired"].id) == "revoked", (
            "the mutant did write — that is the point of this test"
        )

    def test_a_truncated_sweep_exits_1(self, cli_db, seed_app, seed_plan, capsys):
        for i in range(3):
            _mk_license(
                cli_db,
                seed_app,
                seed_plan,
                subject_key=f"tr-{i}",
                status="expired",
                expires_days=-60 - i,
            )
        with patch.dict(os.environ, {"REAP_DRY_RUN": "false", "REAP_LIMIT": "2"}):
            code = reap_expired_licences.main_cli([])
        payload = _one_json_line(capsys)
        assert code == 1, payload
        assert "TRUNCATED" in payload["reason"], payload
        assert payload["archived"] == 2 and payload["reapable_after"] == 1, payload


class TestTheExitCodeContract:
    def test_an_armed_run_that_converged_exits_0(
        self,
        cli_db,
        estate,
        capsys,
        pinned_service_token,
        billing_configured,
        fake_billing,
    ):
        """CONVERGED means the WHOLE of a revoke ran, not just the row write.

        ⚠️ Billing is configured here DELIBERATELY; it is the contract, not scaffolding.
        Since licences#195 an armed sweep that archives rows and never tells billing exits
        1 under ``REAP_TEARDOWN_SKIPPED_UNCONFIGURED``, because a revoked licence with a
        live ``license_sync`` token budget is still SPENDABLE. So an UNCONFIGURED run can
        no longer reach exit 0 at all, and re-pointing this test at 1 would not have been
        a smaller fix — it would have deleted the ``converged ⇒ 0`` guarantee this test is
        named for and left nothing pinning it. The teardown is therefore made to genuinely
        RUN and SUCCEED, and ``teardown_mode == "performed"`` plus the recorded DELETE are
        asserted so this can never go green by SKIPPING the half it calls converged.
        """
        billing = fake_billing({SEED_TENANT_ID: []})
        with patch.dict(os.environ, {"REAP_DRY_RUN": "false"}):
            code = reap_expired_licences.main_cli([])
        payload = _one_json_line(capsys)
        assert code == 0, payload
        assert payload["archived"] == 1 and payload["reapable_after"] == 0, payload
        assert payload["teardown_mode"] == "performed", (
            f"exit 0 must not be reachable by skipping the teardown: {payload}"
        )
        assert billing.quota_deletes == [str(estate["ancient_expired"].id)], (
            f"the archived licence owes billing a quota-config delete; got {billing.quota_deletes}"
        )
        assert payload["teardown_quota_failed"] == [], payload
        assert payload["teardown_budget_failed"] == [], payload
        assert payload["teardown_unresolved_tenant"] == [], payload
        assert _status_of(cli_db, estate["ancient_expired"].id) == "revoked"
        # and the controls survived the ARMED path too, not just the dry run
        assert _status_of(cli_db, estate["active_future"].id) == "active"
        assert _status_of(cli_db, estate["active_perpetual"].id) == "active"
        assert _status_of(cli_db, estate["recent_expired"].id) == "expired"

    def test_a_database_failure_exits_2_without_leaking_the_exception(self, cli_db, capsys):
        """The audit line names the failure CLASS, never the exception text: a psycopg
        error stringifies to the statement plus its bound parameters, and a connection
        error to the DSN (licenses#104 removed exactly that shape)."""
        secret_shaped = (
            "SELECT ... dsn=postgresql://licenses_user:hunter2@host/db"  # pragma: allowlist secret
        )

        def _boom(*_args, **_kw):
            raise RuntimeError(secret_shaped)

        with patch.object(cli_db, "count_reapable_licenses", _boom):
            code = reap_expired_licences.main_cli([])
        payload = _one_json_line(capsys)
        assert code == 2, payload
        assert payload["error_kind"] == "RuntimeError", payload
        assert "hunter2" not in json.dumps(payload), payload

    def test_a_malformed_flag_refuses_to_run_and_exits_2(self, cli_db, estate, capsys):
        """``REAP_DRY_RUN=ture`` must not quietly mean "write to production"."""
        with patch.dict(os.environ, {"REAP_DRY_RUN": "ture"}):
            code = reap_expired_licences.main_cli([])
        payload = _one_json_line(capsys)
        assert code == 2, payload
        assert payload["error_kind"] == "bad_reap_env", payload
        assert _status_of(cli_db, estate["ancient_expired"].id) == "expired"

    def test_an_out_of_range_limit_refuses_to_run(self, cli_db, capsys):
        with patch.dict(os.environ, {"REAP_LIMIT": "0"}):
            code = reap_expired_licences.main_cli([])
        payload = _one_json_line(capsys)
        assert code == 2 and payload["error_kind"] == "bad_reap_env", payload

    def test_a_negative_grace_refuses_to_run(self, cli_db, estate, capsys):
        """A grace that went negative would reap rows that have not expired yet."""
        with patch.dict(os.environ, {"REAP_GRACE_DAYS": "-1"}):
            code = reap_expired_licences.main_cli([])
        payload = _one_json_line(capsys)
        assert code == 2 and payload["error_kind"] == "bad_reap_env", payload
        assert _status_of(cli_db, estate["ancient_expired"].id) == "expired"

    def test_it_takes_no_arguments(self, cli_db, capsys):
        code = reap_expired_licences.main_cli(["--all"])
        assert code == 2, _one_json_line(capsys)


class TestTheAuditLine:
    """``kubectl logs`` IS the audit trail — a CronJob pod carries no scrape
    annotation — so the one-JSON-line contract is the observability."""

    def test_the_output_is_exactly_one_json_line_on_every_path(self, cli_db, estate, capsys):
        for env in ({}, {"REAP_DRY_RUN": "false"}, {"REAP_DRY_RUN": "ture"}, {"REAP_LIMIT": "0"}):
            with patch.dict(os.environ, env):
                reap_expired_licences.main_cli([])
            _one_json_line(capsys)  # asserts exactly one line

    def test_it_never_claims_a_dry_run_wrote(self, cli_db, estate, capsys):
        reap_expired_licences.main_cli([])
        payload = _one_json_line(capsys)
        assert payload["selected"] == 1 and payload["archived"] == 0, payload

    def test_the_actor_and_cutoff_reach_the_audit_line(self, cli_db, capsys):
        with patch.dict(os.environ, {"REAP_ACTOR": "test-actor"}):
            reap_expired_licences.main_cli([])
        payload = _one_json_line(capsys)
        assert payload["actor"] == "test-actor", payload
        assert payload["event"] == "reap_expired_licenses", payload
        # the cutoff is the predicate's boundary; without it on the line a reader
        # cannot tell WHICH population a past run measured.
        assert datetime.fromisoformat(payload["cutoff"]) < datetime.fromisoformat(payload["now"])

    def test_the_default_actor_names_the_cronjob(self, cli_db, capsys):
        reap_expired_licences.main_cli([])
        assert _one_json_line(capsys)["actor"] == "cronjob:reap-expired-licences"


class TestItStartsUnderTheCronJobsRealEnvironment:
    def test_without_a_database_url_it_reports_a_config_failure(self, monkeypatch, capsys):
        """⚠️ ``DatabaseManager()`` with no argument hard-defaults to localhost — a
        connection-refused to nothing inside a pod (licenses#162). Missing config must
        be a loud exit 2, not a silent one."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        code = reap_expired_licences.main_cli([])
        payload = _one_json_line(capsys)
        assert code == 2, payload
        assert "DATABASE_URL" in payload["error"], payload

    def test_it_does_not_import_main(self):
        """⚠️ ``main`` runs ``_validate_production_config()`` at module scope and would
        add METERING_SERVICE_URL + CORS_ALLOWED_ORIGINS as required env to every
        manifest that runs this. The script's docstring asks that this stay true; this
        is the check that keeps it true by construction."""
        source = (
            __import__("pathlib").Path(reap_expired_licences.__file__).read_text(encoding="utf-8")
        )
        assert "import main" not in source
