"""Nothing has EVER transitioned a licence out of ``status='active'`` in production.

WHY THIS FILE EXISTS
--------------------
Measured in the prod licences database on 2026-08-10::

    SELECT status, count(*) FROM public.license GROUP BY 1;
    -> active | 144
       revoked |  64

Zero ``expired`` rows, ever, on a 208-row estate that has been minting
time-bounded licences for months. Two rows are stuck ``status='active'`` with a
PAST ``expires_at`` — one of them a real client's (``subhankar-kag``, app
``kag``, expired 2026-07-18). ``DatabaseManager.expire_stale_licenses`` is
correct but is lazy expire-on-WRITE scoped to ONE
``(tenant_id, app_id, subject_id)``: those rows only heal if somebody happens to
re-provision that exact key. Nothing schedules anything.

The consequence is not cosmetic. ``/licenses/active`` is time-aware so the
subject is told it has NO licence, while the dead row still occupies the
``uq_license_active_tenant_app_subject`` partial-unique slot
(``WHERE status='active'``) — which is what made renewal deadlock.

WHAT IS PINNED HERE
-------------------
``DatabaseManager.expire_all_stale_licenses`` (the unscoped twin) and
``scripts/expire_stale_licences.py`` (the CLI a CronJob runs over it).

⚠️ THE TRAP THIS FILE EXISTS FOR. The sweep's happy path is "found nothing,
exit 0" — which is also exactly what a sweep with a broken selection does, and
what a DRY RUN over a completely broken estate does (a dry run writes nothing by
definition, so judging the run on "did it change anything" makes it green while
every stale row remains). Both halves of every guard are pinned: a guard that
can only fire is noise, a guard that can only pass is decoration.

The audit line separates the two questions that trap conflates: ``selected`` is
what the sweep picked, ``transitioned`` is what it WROTE — 0 on a dry run.

⚠️ NO TEST HERE CAN PASS ON AN EMPTY TABLE. Every one seeds real rows and
asserts a licence with a specific shape was, or was not, selected out of a
population that also contains rows of the other shapes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest
from sqlalchemy.exc import IntegrityError

from models import DatabaseManager, License
from scripts import expire_stale_licences
from tests.conftest import SEED_OTHER_TENANT_ID, SEED_TENANT_ID

_EXPIRE_ENV = ("EXPIRE_DRY_RUN", "EXPIRE_LIMIT", "EXPIRE_ACTOR")


@pytest.fixture(autouse=True)
def _clean_expire_env(monkeypatch):
    """No EXPIRE_* leaks in from the shell or a previous test."""
    for name in _EXPIRE_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def cli_db(db_manager, monkeypatch):
    """Point the CLI's database seam at the test database."""
    monkeypatch.setattr(expire_stale_licences, "_db_manager", lambda: db_manager)
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

    ``expires_days=None`` is a PERPETUAL licence (``expires_at IS NULL``), which
    is a different thing from a licence that expired long ago — conflating the
    two would expire the whole estate.
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


def _status_of(db: DatabaseManager, license_id) -> str:
    lic = db.get_license(UUID(str(license_id)))
    assert lic is not None, f"licence {license_id} vanished"
    return str(lic.status)


def _one_json_line(capsys) -> dict[str, Any]:
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one JSON line on stdout, got {lines!r}"
    return json.loads(lines[0])


# ---------------------------------------------------------------------------
# The unscoped sweep itself
# ---------------------------------------------------------------------------


class TestTheSweepSelectsOnlyDeadRows:
    """One call, a mixed estate. Each shape is present so a selection that
    ignored the predicate entirely would be caught by the SAME test."""

    @pytest.fixture
    def estate(self, db_manager, seed_app, seed_plan):
        """Five licences, one per lifecycle shape. Only ``stale`` may be touched."""
        return {
            "stale": _mk_license(
                db_manager, seed_app, seed_plan, subject_key="stale", expires_days=-30
            ),
            "perpetual": _mk_license(
                db_manager, seed_app, seed_plan, subject_key="perpetual", expires_days=None
            ),
            "future": _mk_license(
                db_manager, seed_app, seed_plan, subject_key="future", expires_days=+30
            ),
            "revoked": _mk_license(
                db_manager,
                seed_app,
                seed_plan,
                subject_key="revoked",
                status="revoked",
                expires_days=-30,
            ),
            "pending": _mk_license(
                db_manager,
                seed_app,
                seed_plan,
                subject_key="pending",
                starts_days=+7,
                expires_days=+30,
            ),
        }

    def test_a_stale_row_is_transitioned(self, db_manager, estate):
        ids = db_manager.expire_all_stale_licenses()

        assert ids == [str(estate["stale"].id)], ids
        assert _status_of(db_manager, estate["stale"].id) == "expired"

    def test_a_perpetual_licence_is_never_touched(self, db_manager, estate):
        """``expires_at IS NULL`` means perpetual. Dropping that leg would expire
        the whole estate — a far worse outage than the bug this fixes."""
        db_manager.expire_all_stale_licenses()

        assert _status_of(db_manager, estate["perpetual"].id) == "active"

    def test_a_future_expiry_is_never_touched(self, db_manager, estate):
        db_manager.expire_all_stale_licenses()

        assert _status_of(db_manager, estate["future"].id) == "active"

    def test_a_revoked_licence_is_never_touched(self, db_manager, estate):
        """``revoked`` is a terminal state a human chose. Rewriting it to
        ``expired`` would destroy the reason the licence is dead."""
        db_manager.expire_all_stale_licenses()

        assert _status_of(db_manager, estate["revoked"].id) == "revoked"

    def test_a_pending_licence_is_never_touched(self, db_manager, estate):
        """🔴 ``starts_at`` MUST NOT be in the predicate.

        A future-dated row is the licence this key WILL have and it already owns
        the unique-index slot; judging it dead would retire it and send the
        renewal that follows straight into a 409. The upstream scoped variant
        excludes ``starts_at`` for exactly this reason.
        """
        db_manager.expire_all_stale_licenses()

        assert _status_of(db_manager, estate["pending"].id) == "active"

    def test_exactly_one_of_the_five_is_selected(self, db_manager, estate):
        """The whole estate in one assertion: four survivors, one corpse."""
        ids = db_manager.expire_all_stale_licenses()

        assert ids == [str(estate["stale"].id)], ids
        assert [
            _status_of(db_manager, estate[k].id) for k in ("perpetual", "future", "pending")
        ] == [
            "active",
            "active",
            "active",
        ]
        assert _status_of(db_manager, estate["revoked"].id) == "revoked"


class TestTheSweepIsUNSCOPED:
    """The entire delta from ``expire_stale_licenses``: one call, the whole estate.

    The scoped variant needs the caller to already know the
    ``(tenant, app, subject)`` of the corpse — which is precisely what nobody
    knows, which is why the prod rows have sat there for three weeks.
    """

    def test_it_crosses_tenants_and_subjects_in_one_call(self, db_manager, seed_app, seed_plan):
        a = _mk_license(db_manager, seed_app, seed_plan, subject_key="tenant-a", expires_days=-10)
        b = _mk_license(
            db_manager,
            seed_app,
            seed_plan,
            subject_key="tenant-b",
            tenant_id=SEED_OTHER_TENANT_ID,
            expires_days=-5,
        )

        ids = db_manager.expire_all_stale_licenses()

        assert sorted(ids) == sorted([str(a.id), str(b.id)]), ids
        assert _status_of(db_manager, a.id) == "expired"
        assert _status_of(db_manager, b.id) == "expired"

    def test_the_scoped_variant_would_have_missed_them(self, db_manager, seed_app, seed_plan):
        """A control: the reason a sweep is needed at all."""
        a = _mk_license(db_manager, seed_app, seed_plan, subject_key="scoped-a", expires_days=-10)
        b = _mk_license(
            db_manager,
            seed_app,
            seed_plan,
            subject_key="scoped-b",
            tenant_id=SEED_OTHER_TENANT_ID,
            expires_days=-5,
        )

        transitioned = db_manager.expire_stale_licenses(
            tenant_id=SEED_TENANT_ID,
            app_id=UUID(str(seed_app.id)),
            subject_id=UUID(str(a.subject_id)),
        )

        assert transitioned == 1, transitioned
        assert _status_of(db_manager, b.id) == "active", (
            "the scoped call reaches only the key it was given — that IS the defect"
        )

    def test_it_is_idempotent(self, db_manager, seed_app, seed_plan):
        _mk_license(db_manager, seed_app, seed_plan, subject_key="once", expires_days=-10)

        first = db_manager.expire_all_stale_licenses()
        second = db_manager.expire_all_stale_licenses()

        assert len(first) == 1, first
        assert second == [], second

    def test_the_limit_caps_one_sweep(self, db_manager, seed_app, seed_plan):
        for i in range(3):
            _mk_license(
                db_manager, seed_app, seed_plan, subject_key=f"many-{i}", expires_days=-10 - i
            )

        ids = db_manager.expire_all_stale_licenses(limit=2)

        assert len(ids) == 2, ids
        assert len(db_manager.expire_all_stale_licenses(limit=2)) == 1, "the tail must remain"


class TestADryRunWritesNothing:
    def test_it_reports_the_ids_it_would_transition(self, db_manager, seed_app, seed_plan):
        stale = _mk_license(db_manager, seed_app, seed_plan, subject_key="dry", expires_days=-10)

        ids = db_manager.expire_all_stale_licenses(dry_run=True)

        assert ids == [str(stale.id)], ids

    def test_the_row_is_still_active_afterwards(self, db_manager, seed_app, seed_plan):
        """🔴 The measurement must not BE the mutation."""
        stale = _mk_license(db_manager, seed_app, seed_plan, subject_key="dry", expires_days=-10)

        db_manager.expire_all_stale_licenses(dry_run=True)

        assert _status_of(db_manager, stale.id) == "active"
        assert db_manager.count_stale_licenses() == 1, "a dry run must leave the estate as it was"


class TestTheUniqueIndexSlotIsFreed:
    """The POINT of the whole exercise, asserted as a behaviour and not as a
    status string: after the sweep, the tenant can be sold a new licence."""

    def test_the_slot_is_occupied_before_the_sweep(self, db_manager, seed_app, seed_plan):
        """🔴 The control. Without this the GREEN test below could pass because
        the unique index does not exist in the test schema at all."""
        stale = _mk_license(db_manager, seed_app, seed_plan, subject_key="renew", expires_days=-10)

        with pytest.raises(IntegrityError):
            db_manager.create_license(
                subject_id=UUID(str(stale.subject_id)),
                app_id=UUID(str(seed_app.id)),
                plan_id=UUID(str(seed_plan.id)),
                tenant_id=SEED_TENANT_ID,
                status="active",
                starts_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(days=365),
            )

    def test_a_renewal_can_be_minted_after_the_sweep(self, db_manager, seed_app, seed_plan):
        stale = _mk_license(db_manager, seed_app, seed_plan, subject_key="renew", expires_days=-10)

        db_manager.expire_all_stale_licenses()

        renewal = db_manager.create_license(
            subject_id=UUID(str(stale.subject_id)),
            app_id=UUID(str(seed_app.id)),
            plan_id=UUID(str(seed_plan.id)),
            tenant_id=SEED_TENANT_ID,
            status="active",
            starts_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(days=365),
        )

        assert renewal.id != stale.id
        found = db_manager.find_active_license(
            tenant_id=SEED_TENANT_ID,
            app_id=UUID(str(seed_app.id)),
            subject_id=UUID(str(stale.subject_id)),
        )
        assert found is not None, "the renewal must be the key's active licence"
        assert found.id == renewal.id


class TestTheIndependentStaleCount:
    """The guard's second opinion — hand-written SQL, so it can DISAGREE with the
    ORM sweep. If it reused the sweep's own query the guard would be a tautology."""

    def test_it_counts_only_structurally_active_time_expired_rows(
        self, db_manager, seed_app, seed_plan
    ):
        _mk_license(db_manager, seed_app, seed_plan, subject_key="c-stale", expires_days=-10)
        _mk_license(db_manager, seed_app, seed_plan, subject_key="c-perpetual", expires_days=None)
        _mk_license(db_manager, seed_app, seed_plan, subject_key="c-future", expires_days=+10)
        _mk_license(
            db_manager,
            seed_app,
            seed_plan,
            subject_key="c-revoked",
            status="revoked",
            expires_days=-10,
        )
        _mk_license(
            db_manager,
            seed_app,
            seed_plan,
            subject_key="c-pending",
            starts_days=+7,
            expires_days=+30,
        )

        assert db_manager.count_stale_licenses() == 1

    def test_it_drops_to_zero_once_the_sweep_has_run(self, db_manager, seed_app, seed_plan):
        _mk_license(db_manager, seed_app, seed_plan, subject_key="c-drop", expires_days=-10)

        assert db_manager.count_stale_licenses() == 1
        db_manager.expire_all_stale_licenses()
        assert db_manager.count_stale_licenses() == 0


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------


class TestTheAntiVacuityGuard:
    """BOTH halves. This lane has shipped green-about-nothing twice."""

    def test_a_dry_run_that_found_stale_rows_exits_1(self, cli_db, seed_app, seed_plan, capsys):
        """🔴 A dry run writes nothing, so "did it write anything" is empty BY
        CONSTRUCTION. Judged that way a dry run over an entirely broken estate
        exits 0 — the loudest way to be green about nothing."""
        stale = _mk_license(cli_db, seed_app, seed_plan, subject_key="v1", expires_days=-10)

        code = expire_stale_licences.main_cli([])

        payload = _one_json_line(capsys)
        assert code == 1, payload
        assert payload["dry_run"] is True, payload
        assert payload["stale_before"] == 1, payload
        assert payload["selected_license_ids"] == [str(stale.id)], payload
        assert payload["transitioned"] == 0, (
            f"a dry run WROTE nothing; the audit line must not say otherwise: {payload}"
        )
        assert "NOT CONVERGED" in payload["reason"], payload
        assert _status_of(cli_db, stale.id) == "active", "a dry run must not write"

    def test_a_genuinely_clean_estate_exits_0(self, cli_db, seed_app, seed_plan, capsys):
        """The green half. Live licences exist; none of them is stale."""
        live = _mk_license(cli_db, seed_app, seed_plan, subject_key="v2", expires_days=+30)

        code = expire_stale_licences.main_cli([])

        payload = _one_json_line(capsys)
        assert code == 0, payload
        assert payload["stale_before"] == 0 and payload["stale_after"] == 0, payload
        assert payload["selected"] == 0 and payload["transitioned"] == 0, payload
        assert _status_of(cli_db, live.id) == "active"

    def test_a_sweep_that_transitioned_nothing_over_a_stale_estate_exits_1(
        self, cli_db, seed_app, seed_plan, capsys
    ):
        """An ARMED run that selected nothing while stale rows exist is a broken
        selection wearing a green tick."""
        _mk_license(cli_db, seed_app, seed_plan, subject_key="v3", expires_days=-10)

        with patch.object(cli_db, "expire_all_stale_licenses", lambda **kw: []):
            with patch.dict(os.environ, {"EXPIRE_DRY_RUN": "false"}):
                code = expire_stale_licences.main_cli([])

        payload = _one_json_line(capsys)
        assert code == 1, payload
        assert "VACUOUS" in payload["reason"], payload
        assert payload["stale_before"] == 1, payload
        assert payload["selected"] == 0 and payload["transitioned"] == 0, payload

    def test_a_sweep_that_expired_more_than_the_independent_rule_allows_exits_1(
        self, cli_db, seed_app, seed_plan, capsys
    ):
        """🔴 The over-fire half. A predicate that widened (``starts_at`` added,
        ``expires_at`` bound dropped) mass-expires a healthy estate and every
        row it touched looks like a successful transition."""
        healthy = _mk_license(cli_db, seed_app, seed_plan, subject_key="v4", expires_days=+30)

        with patch.object(cli_db, "expire_all_stale_licenses", lambda **kw: [str(healthy.id)]):
            with patch.dict(os.environ, {"EXPIRE_DRY_RUN": "false"}):
                code = expire_stale_licences.main_cli([])

        payload = _one_json_line(capsys)
        assert code == 1, payload
        assert "OVER-FIRED" in payload["reason"], payload

    def test_a_dry_run_that_wrote_anyway_exits_1(self, cli_db, seed_app, seed_plan, capsys):
        """🔴 ``dry_run`` that mutates is the single most dangerous defect this
        script can have — the operator asked it not to touch production."""
        stale = _mk_license(cli_db, seed_app, seed_plan, subject_key="v5", expires_days=-10)
        real = cli_db.expire_all_stale_licenses

        def _writes_anyway(**kw):
            kw.pop("dry_run", None)
            return real(dry_run=False, **kw)

        with patch.object(cli_db, "expire_all_stale_licenses", _writes_anyway):
            code = expire_stale_licences.main_cli([])

        payload = _one_json_line(capsys)
        assert code == 1, payload
        assert "DRY RUN WROTE" in payload["reason"], payload
        assert _status_of(cli_db, stale.id) == "expired", "the mutant did write — that is the point"


class TestTheExitCodeContract:
    """0 converged / 1 could-not-converge / 2 config-or-DB — each against a real
    condition, none of them stubbed into existence."""

    def test_an_armed_run_that_converged_exits_0(self, cli_db, seed_app, seed_plan, capsys):
        stale = _mk_license(cli_db, seed_app, seed_plan, subject_key="e1", expires_days=-10)
        live = _mk_license(cli_db, seed_app, seed_plan, subject_key="e1-live", expires_days=+30)

        with patch.dict(os.environ, {"EXPIRE_DRY_RUN": "false"}):
            code = expire_stale_licences.main_cli([])

        payload = _one_json_line(capsys)
        assert code == 0, payload
        assert payload["stale_before"] == 1 and payload["stale_after"] == 0, payload
        assert payload["selected_license_ids"] == [str(stale.id)], payload
        assert payload["transitioned"] == 1, payload
        assert _status_of(cli_db, stale.id) == "expired"
        assert _status_of(cli_db, live.id) == "active"

    def test_a_truncated_sweep_exits_1(self, cli_db, seed_app, seed_plan, capsys):
        """``LIMIT`` with more of the estate behind it: the tail is never reached,
        and every run looks like a success."""
        for i in range(2):
            _mk_license(cli_db, seed_app, seed_plan, subject_key=f"e2-{i}", expires_days=-10 - i)

        with patch.dict(os.environ, {"EXPIRE_DRY_RUN": "false", "EXPIRE_LIMIT": "1"}):
            code = expire_stale_licences.main_cli([])

        payload = _one_json_line(capsys)
        assert code == 1, payload
        assert "TRUNCATED" in payload["reason"], payload
        assert payload["transitioned"] == 1 and payload["stale_after"] == 1, payload

    def test_a_sweep_whose_limit_covered_the_estate_exits_0(
        self, cli_db, seed_app, seed_plan, capsys
    ):
        """The green half of truncation: ``transitioned == limit`` is only a
        problem when there is actually more behind it."""
        _mk_license(cli_db, seed_app, seed_plan, subject_key="e3", expires_days=-10)

        with patch.dict(os.environ, {"EXPIRE_DRY_RUN": "false", "EXPIRE_LIMIT": "1"}):
            code = expire_stale_licences.main_cli([])

        payload = _one_json_line(capsys)
        assert code == 0, payload
        assert payload["transitioned"] == 1 and payload["stale_after"] == 0, payload

    def test_a_database_failure_exits_2_without_leaking_the_exception(self, cli_db, capsys):
        """The audit line names the failure CLASS, never the exception text: a
        psycopg error stringifies to the statement plus its bound parameters,
        and a connection error to the DSN (#104 removed exactly that shape)."""
        secret_shaped = (
            "SELECT ... dsn=postgresql://licenses_user:hunter2@host/db"  # pragma: allowlist secret
        )

        def _boom(*_args, **_kw):
            raise RuntimeError(secret_shaped)

        with patch.object(cli_db, "count_stale_licenses", _boom):
            code = expire_stale_licences.main_cli([])

        payload = _one_json_line(capsys)
        assert code == 2, payload
        assert payload["error_kind"] == "RuntimeError", payload
        assert "hunter2" not in json.dumps(payload), payload

    def test_a_malformed_env_flag_refuses_to_run_and_exits_2(
        self, cli_db, seed_app, seed_plan, capsys
    ):
        """``EXPIRE_DRY_RUN=ture`` must not quietly mean "write to production"."""
        stale = _mk_license(cli_db, seed_app, seed_plan, subject_key="e4", expires_days=-10)

        with patch.dict(os.environ, {"EXPIRE_DRY_RUN": "ture"}):
            code = expire_stale_licences.main_cli([])

        payload = _one_json_line(capsys)
        assert code == 2, payload
        assert payload["error_kind"] == "bad_expire_env", payload
        assert _status_of(cli_db, stale.id) == "active", "nothing may run on a bad flag"

    def test_an_out_of_range_limit_refuses_to_run(self, cli_db, capsys):
        with patch.dict(os.environ, {"EXPIRE_LIMIT": "0"}):
            code = expire_stale_licences.main_cli([])

        payload = _one_json_line(capsys)
        assert code == 2, payload
        assert payload["error_kind"] == "bad_expire_env", payload

    def test_it_takes_no_arguments(self, cli_db, capsys):
        code = expire_stale_licences.main_cli(["--all"])

        payload = _one_json_line(capsys)
        assert code == 2, payload


class TestTheJobIsConfiguredFromEnv:
    def test_the_default_is_a_dry_run(self, cli_db, seed_app, seed_plan, capsys):
        """🔴 SHIPS DRY-RUN. An unset ``EXPIRE_DRY_RUN`` must never mutate prod."""
        stale = _mk_license(cli_db, seed_app, seed_plan, subject_key="c1", expires_days=-10)

        expire_stale_licences.main_cli([])

        payload = _one_json_line(capsys)
        assert payload["dry_run"] is True, payload
        assert _status_of(cli_db, stale.id) == "active"

    def test_the_default_limit_is_500(self, cli_db, capsys):
        expire_stale_licences.main_cli([])

        assert _one_json_line(capsys)["limit"] == 500

    def test_a_blank_value_falls_back_to_the_default(self, cli_db, seed_app, seed_plan, capsys):
        """``EXPIRE_DRY_RUN: ""`` in a manifest must not read as "armed"."""
        stale = _mk_license(cli_db, seed_app, seed_plan, subject_key="c2", expires_days=-10)

        with patch.dict(os.environ, {"EXPIRE_DRY_RUN": "", "EXPIRE_LIMIT": ""}):
            expire_stale_licences.main_cli([])

        payload = _one_json_line(capsys)
        assert payload["dry_run"] is True, payload
        assert payload["limit"] == 500, payload
        assert _status_of(cli_db, stale.id) == "active"

    def test_the_audit_line_never_claims_a_dry_run_wrote(self, cli_db, seed_app, seed_plan, capsys):
        """🔴 ``kubectl logs`` IS the audit trail — alerts fire on this line and
        humans paste it. A dry run that reports ``transitioned: 2`` is a false
        record of a production mutation that never happened. SELECTED is what the
        sweep picked; TRANSITIONED is what it wrote, and on a dry run that is 0.
        """
        for i in range(2):
            _mk_license(cli_db, seed_app, seed_plan, subject_key=f"c4-{i}", expires_days=-10 - i)

        expire_stale_licences.main_cli([])

        payload = _one_json_line(capsys)
        assert payload["dry_run"] is True, payload
        assert payload["selected"] == 2, payload
        assert payload["transitioned"] == 0, (
            f"nothing was written, so nothing was transitioned: {payload}"
        )

    def test_an_armed_run_reports_selected_and_transitioned_alike(
        self, cli_db, seed_app, seed_plan, capsys
    ):
        """The green half: when the sweep DOES write, the two agree — otherwise
        ``transitioned`` could be hard-coded to 0 and the test above would pass."""
        _mk_license(cli_db, seed_app, seed_plan, subject_key="c5", expires_days=-10)

        with patch.dict(os.environ, {"EXPIRE_DRY_RUN": "false"}):
            expire_stale_licences.main_cli([])

        payload = _one_json_line(capsys)
        assert payload["selected"] == 1 and payload["transitioned"] == 1, payload

    def test_the_actor_reaches_the_audit_line(self, cli_db, capsys):
        with patch.dict(os.environ, {"EXPIRE_ACTOR": " cronjob:padded "}):
            expire_stale_licences.main_cli([])

        assert _one_json_line(capsys)["actor"] == "cronjob:padded"

    def test_the_output_is_exactly_one_json_line(self, cli_db, seed_app, seed_plan, capsys):
        """``kubectl logs`` IS the audit trail — this service has no Prometheus
        scrape annotation and no audit table."""
        _mk_license(cli_db, seed_app, seed_plan, subject_key="c3", expires_days=-10)

        expire_stale_licences.main_cli([])

        payload = _one_json_line(capsys)
        assert payload["event"] == "expire_stale_licenses", payload
        for key in (
            "actor",
            "dry_run",
            "limit",
            "stale_before",
            "stale_after",
            "selected",
            "selected_license_ids",
            "transitioned",
            "exit_code",
            "reason",
        ):
            assert key in payload, payload


class TestItStartsUnderTheCronJobsRealEnvironment:
    """Proof by EXECUTION, in a subprocess with a constructed env.

    ``scripts/reconcile_metering.py`` — the sibling this job is modelled on —
    imports ``main``, which runs ``_validate_production_config()`` at module
    scope, so it needs ``METERING_SERVICE_URL`` and ``CORS_ALLOWED_ORIGINS`` on
    TOP of the ``licenses-env`` Secret and dies at import without them. This job
    deliberately imports ``models`` ONLY, so ``envFrom: licenses-env`` alone is
    enough. That is a claim about a manifest nobody has written yet, so it is
    proven here by running the real interpreter with exactly the Secret's keys.

    In-process tests cannot make this claim: ``tests/conftest.py`` forces
    ``ENVIRONMENT=development`` and both modules are already in ``sys.modules``.
    """

    # Exactly the keys the live licenses-env Secret holds (2026-08-10).
    SECRET_KEYS = {
        "KEYCLOAK_REALM": "secure-apps",
        "KEYCLOAK_URL": "https://keycloak.invalid",
        "POSTGRES_SSLMODE": "disable",
        "SERVICE_CLIENT_ID": "licensing-service",
        "SERVICE_CLIENT_SECRET": "x" * 32,  # pragma: allowlist secret
        "SERVICE_TOKEN_URL": "https://keycloak.invalid/token",
    }

    def test_the_licenses_env_secret_alone_is_enough(self, cli_db, seed_app, seed_plan):
        """The whole CLI end to end — real interpreter, real import, real DB, and
        NOT the two extra vars the metering job needs."""
        from tests.conftest import TEST_DATABASE_URL

        stale = _mk_license(cli_db, seed_app, seed_plan, subject_key="sub1", expires_days=-10)

        proc = _run_script({**self.SECRET_KEYS, "DATABASE_URL": TEST_DATABASE_URL})

        payload = _sole_json_line(proc.stdout)
        assert proc.returncode == 1, (
            f"a dry run over a stale estate is NOT converged\n"
            f"stdout={proc.stdout!r}\nstderr={proc.stderr[-2000:]!r}"
        )
        assert payload["event"] == "expire_stale_licenses", payload
        assert payload["dry_run"] is True, payload
        assert payload["selected_license_ids"] == [str(stale.id)], payload
        assert payload["transitioned"] == 0, payload
        assert _status_of(cli_db, stale.id) == "active", "the shipped default must not write"

    def test_without_a_database_url_it_reports_a_config_failure(self):
        proc = _run_script(self.SECRET_KEYS)

        assert proc.returncode == 2, (
            f"a config failure is not 'could not converge'\n"
            f"stdout={proc.stdout!r}\nstderr={proc.stderr[-2000:]!r}"
        )
        payload = _sole_json_line(proc.stdout)
        assert payload["error_kind"] == "bad_expire_env", payload
        assert "DATABASE_URL" in payload["error"], payload


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _run_script(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run the CLI in a subprocess with ONLY ``env`` (plus PATH/HOME) set.

    A constructed env, not ``os.environ`` plus overrides: the pytest process
    carries ENVIRONMENT=development, DATABASE_URL and PYTEST_CURRENT_TEST, and
    each of those would hide a real CronJob failure.
    """
    root = Path(__file__).resolve().parents[2]
    return subprocess.run(
        [sys.executable, str(root / "scripts" / "expire_stale_licences.py")],
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
