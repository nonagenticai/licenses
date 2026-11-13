"""scripts/provision_bundle_all_tenants.py — the fleet form of provision_tenant_bundle.py.

WHAT IS PINNED (licenses #239; George §I Q1/Q3 2026-10-01).
* Scope: archived tenants and default-anon are skipped; a kind='service' tenant IS
  included.
* Dry-run by default writes nothing.
* --only-tenant restricts (and refuses a tenant outside scope); --limit caps.
* Exit-code aggregation follows the documented precedence, including a crashing tenant
  (exit 5) that does not stop the walk.
* The receipt JSON shape.
* An existing ACTIVE, unbundled, tenant-level licence (realm 'service',
  `svc-inference-router-x`) is ADOPTED: exactly one active (tenant, app) licence after.
* `--plan` is REQUIRED (argparse exit 2 without it); a budget-bearing plan prints the
  pooled-cap WARNING and a plan without budget/quota does not.
* `--overrides` (George 2026-10-02): per-tenant token_budget/quota REPLACE the plan's
  value in the billing push of an INSERTED licence only (an adopted one is never
  pushed); an unlisted tenant keeps the plan value; a tenant outside scope or a
  malformed file is exit 2 before any tenant runs; the receipt records the effective
  overrides per tenant.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text

from models import BundleApp, DatabaseManager
from tests.conftest import SEED_OTHER_TENANT_ID, SEED_TENANT_ID

REPO = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "provision_bundle_all_tenants", REPO / "scripts" / "provision_bundle_all_tenants.py"
)
assert _spec is not None and _spec.loader is not None
fleet = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fleet)

BUNDLE_KEY = "inference-router"
APP_KEY = "inference-router"

SERVICE_TENANT_ID = "01000000-0000-7000-8000-0000000000a1"
ARCHIVED_TENANT_ID = "01000000-0000-7000-8000-0000000000a2"
DEFAULT_ANON = "00000000-0000-0000-0000-000000000000"


def _seed(db: DatabaseManager) -> UUID:
    """The inference-router app (plan `standard`, no budget/quota) in its own bundle,
    plus three extra tenants: a service actor, an archived one, and default-anon.
    Returns the app id."""
    app = db.create_app(key=APP_KEY, name="Inference Router", issuers=("platform",))
    app_id = UUID(str(app.id))
    db.create_plan(app_id=app_id, code="standard", name="Standard")
    with db.engine.begin() as conn:
        bundle_id = conn.execute(
            text("INSERT INTO bundle (key, name) VALUES (:k, 'Inference Router') RETURNING id"),
            {"k": BUNDLE_KEY},
        ).scalar_one()
    session = db.get_session()
    try:
        session.add(BundleApp(bundle_id=bundle_id, app_id=app_id))
        session.commit()
    finally:
        session.close()
    db.ensure_tenant(
        SERVICE_TENANT_ID,
        "svc-dormant-actor",
        kind="service",
        byo_mode="platform_only",
        owner_key="platform",
    )
    db.ensure_tenant(ARCHIVED_TENANT_ID, "retired-tenant", owner_key="platform")
    db.ensure_tenant(DEFAULT_ANON, "default-anon", byo_mode="platform_only", owner_key="platform")
    with db.engine.begin() as conn:
        conn.execute(
            text("UPDATE tenants SET archived_at = now() WHERE id = :t"), {"t": ARCHIVED_TENANT_ID}
        )
    return app_id


ELIGIBLE = sorted([SEED_TENANT_ID, SEED_OTHER_TENANT_ID, SERVICE_TENANT_ID])


def _counts(db: DatabaseManager) -> dict[str, int]:
    with db.engine.connect() as conn:
        return {
            t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one()
            for t in ("tenant_bundle", "license", "token", "subject")
        }


def _active_ir_licences(db: DatabaseManager, tenant_id: str) -> list[tuple[str, str | None]]:
    with db.engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT l.id, l.bundle_id FROM license l JOIN app a ON a.id = l.app_id "
                "WHERE l.tenant_id = :t AND a.key = :k AND l.status = 'active'"
            ),
            {"t": tenant_id, "k": APP_KEY},
        ).all()
    return [(str(i), str(b) if b else None) for i, b in rows]


@pytest.fixture
def metering(pinned_service_token, monkeypatch):
    """--apply provisions billing through ``main``; never reach a real billing."""
    from unittest.mock import AsyncMock

    import main

    budget = AsyncMock(return_value=True)
    quota = AsyncMock(return_value=True)
    monkeypatch.setattr(main, "_push_token_budget", budget)
    monkeypatch.setattr(main, "_push_quota_config", quota)
    budget.quota = quota  # the quota spy rides along for the override tests
    return budget


def _run(db, *extra: str) -> int:
    """``--plan standard`` unless the test names a plan: --plan is REQUIRED."""
    plan = [] if "--plan" in extra else ["--plan", "standard"]
    return fleet.main(["--bundle", BUNDLE_KEY, *plan, *extra], db=db)


class TestScope:
    def test_archived_and_default_anon_are_skipped_service_is_included(self, db_manager):
        _seed(db_manager)
        ids = [str(t.id) for t in fleet.eligible_tenants(db_manager)]
        assert ids == ELIGIBLE
        assert SERVICE_TENANT_ID in ids, "George §I Q1: service actors hold the licence too"
        assert ARCHIVED_TENANT_ID not in ids and DEFAULT_ANON not in ids

    def test_apply_licenses_exactly_the_eligible_tenants(self, db_manager, metering, tmp_path):
        _seed(db_manager)
        receipt = tmp_path / "r.json"
        assert _run(db_manager, "--apply", "--receipt", str(receipt)) == 0
        for t in ELIGIBLE:
            assert len(_active_ir_licences(db_manager, t)) == 1, t
        for t in (ARCHIVED_TENANT_ID, DEFAULT_ANON):
            assert _active_ir_licences(db_manager, t) == [], t
        body = json.loads(receipt.read_text())
        assert [e["tenant_id"] for e in body["tenants"]] == ELIGIBLE
        kinds = {e["tenant_id"]: e["kind"] for e in body["tenants"]}
        assert kinds[SERVICE_TENANT_ID] == "service"


class TestDryRun:
    def test_dry_run_is_the_default_and_writes_nothing(self, db_manager, capsys):
        _seed(db_manager)
        before = _counts(db_manager)
        assert _run(db_manager) == 0
        assert _counts(db_manager) == before
        out = capsys.readouterr().out
        assert "DRY-RUN" in out and "nothing written" in out
        for t in ELIGIBLE:
            assert f"svc:{t}:{APP_KEY}" in out

    def test_default_is_not_apply(self):
        assert fleet._parse_args(["--bundle", "x", "--plan", "standard"]).apply is False


class TestOnlyTenantAndLimit:
    def test_only_tenant_restricts(self, db_manager, metering, tmp_path):
        _seed(db_manager)
        receipt = tmp_path / "r.json"
        rc = _run(
            db_manager, "--apply", "--only-tenant", SERVICE_TENANT_ID, "--receipt", str(receipt)
        )
        assert rc == 0
        assert len(_active_ir_licences(db_manager, SERVICE_TENANT_ID)) == 1
        assert _active_ir_licences(db_manager, SEED_TENANT_ID) == []
        assert [e["tenant_id"] for e in json.loads(receipt.read_text())["tenants"]] == [
            SERVICE_TENANT_ID
        ]

    @pytest.mark.parametrize(
        "outside", [ARCHIVED_TENANT_ID, DEFAULT_ANON, "01000000-0000-7000-8000-00000000dead"]
    )
    def test_only_tenant_outside_scope_is_a_usage_error_and_runs_nothing(
        self, db_manager, outside, capsys
    ):
        _seed(db_manager)
        before = _counts(db_manager)
        assert _run(db_manager, "--apply", "--only-tenant", outside) == fleet.EXIT_USAGE
        assert _counts(db_manager) == before
        assert "outside scope" in capsys.readouterr().err

    def test_a_non_uuid_only_tenant_is_rejected(self):
        with pytest.raises(SystemExit):
            fleet._parse_args(
                ["--bundle", "x", "--plan", "standard", "--only-tenant", "seed-tenant-primary"]
            )

    def test_limit_caps_in_id_order(self, db_manager, tmp_path):
        _seed(db_manager)
        receipt = tmp_path / "r.json"
        assert _run(db_manager, "--limit", "2", "--receipt", str(receipt)) == 0
        assert [e["tenant_id"] for e in json.loads(receipt.read_text())["tenants"]] == ELIGIBLE[:2]


class TestExitAggregation:
    @pytest.mark.parametrize(
        ("codes", "expected"),
        [
            ([], 0),
            ([0, 0], 0),
            ([0, 6], 6),
            ([6, 3], 3),
            ([3, 7], 7),
            ([7, 4], 4),
            ([4, 5], 5),
            ([5, 2], 2),
            ([2, 1], 1),
            ([0, 6, 3, 7, 4, 5, 2, 1], 1),
            ([0, 5, 3], 5),
        ],
    )
    def test_precedence(self, codes, expected):
        assert fleet.aggregate_exit(codes) == expected

    def test_an_unknown_code_never_reads_as_success(self):
        assert fleet.aggregate_exit([0, 9]) == 9

    def test_a_crashing_tenant_is_5_and_the_walk_continues(
        self, db_manager, monkeypatch, tmp_path, capsys
    ):
        _seed(db_manager)
        real = fleet.provision_tenant_bundle.main

        def flaky(argv, db=None):
            if argv[argv.index("--tenant") + 1] == SEED_OTHER_TENANT_ID:
                raise RuntimeError(
                    "boom postgresql://user:secret@host/db"  # pragma: allowlist secret
                )
            return real(argv, db=db)

        monkeypatch.setattr(fleet.provision_tenant_bundle, "main", flaky)
        receipt = tmp_path / "r.json"
        assert _run(db_manager, "--receipt", str(receipt)) == fleet.EXIT_CRASHED
        body = json.loads(receipt.read_text())
        by_id = {e["tenant_id"]: e for e in body["tenants"]}
        assert set(by_id) == set(ELIGIBLE), "the walk continued past the crash"
        assert by_id[SEED_OTHER_TENANT_ID]["exit_code"] == 5
        assert by_id[SEED_OTHER_TENANT_ID]["error"] == "RuntimeError"
        assert "secret" not in receipt.read_text() and "secret" not in capsys.readouterr().err
        assert body["totals"] == {"0": 2, "5": 1}

    def test_a_refusal_aggregates_to_3(self, db_manager):
        app_id = _seed(db_manager)
        for sub in ("svc-ir-a", "svc-ir-b"):  # two tenant-level candidates -> REFUSE
            subject = db_manager.create_or_get_subject(keycloak_sub=sub, keycloak_realm="service")
            plan = [p for p in db_manager.list_plans(app_id) if p.code == "standard"][0]
            db_manager.create_license(
                subject_id=UUID(str(subject.id)),
                app_id=app_id,
                plan_id=UUID(str(plan.id)),
                tenant_id=SEED_TENANT_ID,
            )
        assert _run(db_manager) == fleet.EXIT_REFUSED

    def test_an_unknown_bundle_is_1_and_runs_nothing(self, db_manager, tmp_path, monkeypatch):
        _seed(db_manager)
        called: list[object] = []
        monkeypatch.setattr(fleet.provision_tenant_bundle, "main", lambda *a, **k: called.append(a))
        receipt = tmp_path / "r.json"
        rc = fleet.main(
            ["--bundle", "no-such-bundle", "--plan", "standard", "--receipt", str(receipt)],
            db=db_manager,
        )
        assert rc == fleet.EXIT_BAD_BUNDLE and called == []
        assert json.loads(receipt.read_text())["exit_code"] == 1


class TestReceipt:
    def test_shape(self, db_manager, tmp_path):
        _seed(db_manager)
        receipt = tmp_path / "r.json"
        assert _run(db_manager, "--plan", "standard", "--receipt", str(receipt)) == 0
        body = json.loads(receipt.read_text())
        assert set(body) == {
            "started_at",
            "finished_at",
            "status",
            "abort_reason",
            "bundle",
            "plan",
            "apply",
            "only_tenant",
            "limit",
            "overrides_file",
            "tenants",
            "totals",
            "exit_code",
        }
        assert body["bundle"] == BUNDLE_KEY and body["plan"] == "standard"
        assert body["apply"] is False and body["exit_code"] == 0
        assert body["status"] == "completed" and body["abort_reason"] is None
        assert body["totals"] == {"0": len(ELIGIBLE)}
        assert body["started_at"] <= body["finished_at"]
        assert body["started_at"].endswith("+00:00")
        for e in body["tenants"]:
            assert set(e) == {"tenant_id", "name", "kind", "overrides", "exit_code"}

    def test_a_completed_run_says_completed(self, db_manager, tmp_path):
        _seed(db_manager)
        receipt = tmp_path / "r.json"
        assert _run(db_manager, "--receipt", str(receipt)) == 0
        body = json.loads(receipt.read_text())
        assert body["status"] == "completed"
        assert body["abort_reason"] is None and body["exit_code"] == 0

    def test_an_existing_receipt_is_refused_untouched_and_nothing_runs(
        self, db_manager, tmp_path, monkeypatch, capsys
    ):
        _seed(db_manager)
        called: list[object] = []
        monkeypatch.setattr(fleet.provision_tenant_bundle, "main", lambda *a, **k: called.append(a))
        receipt = tmp_path / "r.json"
        receipt.write_text("PREVIOUS RUN EVIDENCE\n")
        before = _counts(db_manager)
        assert _run(db_manager, "--apply", "--receipt", str(receipt)) == fleet.EXIT_USAGE
        assert receipt.read_text() == "PREVIOUS RUN EVIDENCE\n"
        assert called == [] and _counts(db_manager) == before
        assert str(receipt) in capsys.readouterr().err
        assert sorted(p.name for p in tmp_path.iterdir()) == ["r.json"], "no temp file left"

    def test_enumeration_raising_writes_an_aborted_receipt_and_exits_8(
        self, db_manager, tmp_path, monkeypatch, capsys
    ):
        _seed(db_manager)

        def broken(*_a, **_k):
            raise RuntimeError(
                "connection to postgresql://user:secret@host/db lost"  # pragma: allowlist secret
            )

        monkeypatch.setattr(db_manager, "list_tenants", broken)
        receipt = tmp_path / "r.json"
        assert _run(db_manager, "--receipt", str(receipt)) == fleet.EXIT_ABORTED == 8
        body = json.loads(receipt.read_text())
        assert body["status"] == "aborted" and body["abort_reason"] == "RuntimeError"
        assert body["exit_code"] == 8 and body["tenants"] == []
        assert "secret" not in receipt.read_text() and "secret" not in capsys.readouterr().err

    def test_ctrl_c_mid_walk_keeps_the_results_so_far_and_exits_8(
        self, db_manager, tmp_path, monkeypatch
    ):
        _seed(db_manager)
        real = fleet.provision_tenant_bundle.main

        def interrupted(argv, db=None):
            if argv[argv.index("--tenant") + 1] == ELIGIBLE[1]:
                raise KeyboardInterrupt
            return real(argv, db=db)

        monkeypatch.setattr(fleet.provision_tenant_bundle, "main", interrupted)
        receipt = tmp_path / "r.json"
        assert _run(db_manager, "--receipt", str(receipt)) == fleet.EXIT_ABORTED
        body = json.loads(receipt.read_text())
        assert body["status"] == "aborted" and body["abort_reason"] == "KeyboardInterrupt"
        assert [(e["tenant_id"], e["exit_code"]) for e in body["tenants"]] == [
            (ELIGIBLE[0], 0),
            (ELIGIBLE[1], 8),
        ]
        assert body["totals"] == {"0": 1, "8": 1}

    def test_aborted_outranks_everything(self):
        assert fleet.PRECEDENCE[0] == fleet.EXIT_ABORTED
        assert fleet.aggregate_exit([1, 8]) == 8


class TestAdoptsTheExistingServiceLicence:
    def test_an_unbundled_tenant_level_licence_is_adopted_not_duplicated(
        self, db_manager, metering
    ):
        app_id = _seed(db_manager)
        subject = db_manager.create_or_get_subject(
            keycloak_sub="svc-inference-router-x", keycloak_realm="service"
        )
        plan = [p for p in db_manager.list_plans(app_id) if p.code == "standard"][0]
        existing = db_manager.create_license(
            subject_id=UUID(str(subject.id)),
            app_id=app_id,
            plan_id=UUID(str(plan.id)),
            tenant_id=SEED_TENANT_ID,
            status="active",
        )
        bundle = db_manager.get_bundle_by_key(BUNDLE_KEY)
        assert _run(db_manager, "--apply", "--only-tenant", SEED_TENANT_ID) == 0
        assert _active_ir_licences(db_manager, SEED_TENANT_ID) == [
            (str(existing.id), str(bundle.id))
        ]
        # idempotent: a second apply changes nothing
        before = _counts(db_manager)
        assert _run(db_manager, "--apply", "--only-tenant", SEED_TENANT_ID) == 0
        assert _counts(db_manager) == before
        assert len(_active_ir_licences(db_manager, SEED_TENANT_ID)) == 1
        metering.assert_not_awaited()  # an adopted licence is never pushed to


class TestPlanIsRequiredAndSpendIsReported:
    def test_missing_plan_is_an_argparse_usage_error(self, capsys):
        with pytest.raises(SystemExit) as exc:
            fleet._parse_args(["--bundle", BUNDLE_KEY])
        assert exc.value.code == 2
        assert "--plan" in capsys.readouterr().err

    def test_a_budget_bearing_plan_prints_the_pooled_cap_warning(self, db_manager, capsys):
        app_id = _seed(db_manager)
        _metered_plan(db_manager, app_id)
        assert _run(db_manager, "--plan", "metered") == 0  # a warning, not a refusal
        out = capsys.readouterr().out
        assert (
            f"WARNING: plan metered on app {APP_KEY} pushes a spend budget of 50000000 "
            "— this RAISES every inserted tenant's pooled cap"
        ) in out
        assert "pushes a request quota of 1000000" in out

    def test_a_plan_without_budget_or_quota_prints_no_warning(self, db_manager, capsys):
        _seed(db_manager)
        assert _run(db_manager, "--plan", "standard") == 0
        out = capsys.readouterr().out
        assert (
            f"plan standard on app {APP_KEY}: token_budget_monthly=None quota_monthly=None" in out
        )
        assert "WARNING" not in out


def _metered_plan(db: DatabaseManager, app_id: UUID) -> None:
    db.create_plan(
        app_id=app_id,
        code="metered",
        name="Metered",
        quota_monthly=1_000_000,
        token_budget_monthly=50_000_000,
    )


def _write_overrides(tmp_path: Path, body: object) -> str:
    path = tmp_path / "overrides.json"
    path.write_text(json.dumps(body))
    return str(path)


class TestOverrides:
    """Per-tenant token_budget / quota replace the plan's values for INSERTED licences."""

    def _pushed(self, metering) -> tuple[dict[str, int], dict[str, int]]:
        budgets = {
            c.kwargs["tenant_id"]: c.kwargs["budget_tokens"] for c in metering.await_args_list
        }
        quotas = {
            c.kwargs["tenant_id"]: c.kwargs["quota_monthly"] for c in metering.quota.await_args_list
        }
        return budgets, quotas

    def test_override_replaces_the_plan_value_in_the_push_and_plan_rows_are_untouched(
        self, db_manager, metering, tmp_path
    ):
        app_id = _seed(db_manager)
        _metered_plan(db_manager, app_id)
        ov = _write_overrides(tmp_path, {SEED_TENANT_ID: {"token_budget": 11_355_000, "quota": 42}})
        rc = _run(
            db_manager,
            "--apply",
            "--plan",
            "metered",
            "--overrides",
            ov,
            "--only-tenant",
            SEED_TENANT_ID,
            "--only-tenant",
            SEED_OTHER_TENANT_ID,
        )
        assert rc == 0
        budgets, quotas = self._pushed(metering)
        assert budgets == {SEED_TENANT_ID: 11_355_000, SEED_OTHER_TENANT_ID: 50_000_000}
        assert quotas == {SEED_TENANT_ID: 42, SEED_OTHER_TENANT_ID: 1_000_000}
        (plan,) = [p for p in db_manager.list_plans(app_id) if str(p.code) == "metered"]
        assert (plan.token_budget_monthly, plan.quota_monthly) == (50_000_000, 1_000_000)

    def test_one_key_only_overrides_that_key(self, db_manager, metering, tmp_path):
        app_id = _seed(db_manager)
        _metered_plan(db_manager, app_id)
        ov = _write_overrides(tmp_path, {SEED_TENANT_ID: {"quota": 0}})
        assert (
            _run(
                db_manager,
                "--apply",
                "--plan",
                "metered",
                "--overrides",
                ov,
                "--only-tenant",
                SEED_TENANT_ID,
            )
            == 0
        )
        budgets, quotas = self._pushed(metering)
        assert budgets == {SEED_TENANT_ID: 50_000_000}
        assert quotas == {SEED_TENANT_ID: 0}, "0 is a block-all quota, not 'no override'"

    def test_an_adopted_licence_is_never_pushed_even_with_overrides(
        self, db_manager, metering, tmp_path
    ):
        app_id = _seed(db_manager)
        _metered_plan(db_manager, app_id)
        (std,) = [p for p in db_manager.list_plans(app_id) if str(p.code) == "standard"]
        subject = db_manager.create_or_get_subject(
            keycloak_sub="svc-inference-router-x", keycloak_realm="service"
        )
        db_manager.create_license(
            subject_id=UUID(str(subject.id)),
            app_id=app_id,
            plan_id=UUID(str(std.id)),
            tenant_id=SEED_TENANT_ID,
            status="active",
        )
        ov = _write_overrides(tmp_path, {SEED_TENANT_ID: {"token_budget": 1, "quota": 1}})
        rc = _run(
            db_manager,
            "--apply",
            "--plan",
            "metered",
            "--overrides",
            ov,
            "--only-tenant",
            SEED_TENANT_ID,
        )
        assert rc == 0
        (lic,) = _active_ir_licences(db_manager, SEED_TENANT_ID)
        assert lic[1] is not None, "positive control: the licence WAS adopted into the bundle"
        metering.assert_not_awaited()
        metering.quota.assert_not_awaited()

    def test_dry_run_prints_the_effective_values(self, db_manager, metering, tmp_path, capsys):
        app_id = _seed(db_manager)
        _metered_plan(db_manager, app_id)
        ov = _write_overrides(tmp_path, {SEED_TENANT_ID: {"token_budget": 777}})
        assert (
            _run(
                db_manager, "--plan", "metered", "--overrides", ov, "--only-tenant", SEED_TENANT_ID
            )
            == 0
        )
        out = capsys.readouterr().out
        assert "metering: token_budget=777 (override; plan: 50000000), quota=1000000" in out
        metering.assert_not_awaited()

    def test_a_tenant_outside_scope_is_exit_2_before_any_tenant_runs(
        self, db_manager, tmp_path, monkeypatch
    ):
        _seed(db_manager)
        called: list[object] = []
        monkeypatch.setattr(fleet.provision_tenant_bundle, "main", lambda *a, **k: called.append(a))
        for outsider in (ARCHIVED_TENANT_ID, DEFAULT_ANON, "01000000-0000-7000-8000-0000000000ff"):
            ov = _write_overrides(tmp_path, {SEED_TENANT_ID: {"quota": 1}, outsider: {"quota": 1}})
            assert _run(db_manager, "--overrides", ov) == fleet.EXIT_USAGE, outsider
        assert called == []

    @pytest.mark.parametrize(
        "body",
        [
            [],
            {"not-a-uuid": {"quota": 1}},
            {SEED_TENANT_ID: {"quota": -1}},
            {SEED_TENANT_ID: {"token_budget": True}},
            {SEED_TENANT_ID: {"token_budget": 1.5}},
            {SEED_TENANT_ID: {"budget": 1}},
            {SEED_TENANT_ID: 5},
            # license.*_override are INTEGER (migration 041): refused here, not as a
            # DB error after earlier tenants in the same run were already written.
            {SEED_TENANT_ID: {"token_budget": 2_147_483_648}},
        ],
    )
    def test_a_malformed_file_is_exit_2(self, db_manager, tmp_path, monkeypatch, body):
        _seed(db_manager)
        called: list[object] = []
        monkeypatch.setattr(fleet.provision_tenant_bundle, "main", lambda *a, **k: called.append(a))
        assert _run(db_manager, "--overrides", _write_overrides(tmp_path, body)) == fleet.EXIT_USAGE
        assert called == []

    @pytest.mark.parametrize(
        "raw",
        [
            # The SAME tenant twice: json.loads silently keeps the second value.
            f'{{"{SEED_TENANT_ID}": {{"token_budget": 1}}, "{SEED_TENANT_ID}": {{"token_budget": 9}}}}',
            # A duplicate key INSIDE one tenant's object is just as ambiguous.
            f'{{"{SEED_TENANT_ID}": {{"quota": 1, "quota": 2}}}}',
        ],
    )
    def test_a_duplicate_json_key_is_exit_2(self, db_manager, tmp_path, monkeypatch, raw):
        _seed(db_manager)
        called: list[object] = []
        monkeypatch.setattr(fleet.provision_tenant_bundle, "main", lambda *a, **k: called.append(a))
        path = tmp_path / "dup.json"
        path.write_text(raw)
        assert json.loads(raw), "positive control: plain json.loads ACCEPTS this file"
        assert _run(db_manager, "--overrides", str(path)) == fleet.EXIT_USAGE
        assert called == []

    def test_the_receipt_records_the_effective_overrides(self, db_manager, tmp_path):
        _seed(db_manager)
        ov = _write_overrides(tmp_path, {SERVICE_TENANT_ID: {"token_budget": 5, "quota": 6}})
        receipt = tmp_path / "r.json"
        assert _run(db_manager, "--overrides", ov, "--receipt", str(receipt)) == 0
        data = json.loads(receipt.read_text())
        assert data["overrides_file"] == ov
        by_id = {t["tenant_id"]: t["overrides"] for t in data["tenants"]}
        assert by_id == {
            SEED_TENANT_ID: {},
            SEED_OTHER_TENANT_ID: {},
            SERVICE_TENANT_ID: {"token_budget": 5, "quota": 6},
        }

    def test_overrides_are_passed_through_as_per_tenant_flags(
        self, db_manager, tmp_path, monkeypatch
    ):
        _seed(db_manager)
        seen: dict[str, list[str]] = {}

        def fake(argv, db=None):
            seen[argv[1]] = argv
            return 0

        monkeypatch.setattr(fleet.provision_tenant_bundle, "main", fake)
        ov = _write_overrides(tmp_path, {SERVICE_TENANT_ID: {"token_budget": 5}})
        assert _run(db_manager, "--overrides", ov) == 0
        assert seen[SERVICE_TENANT_ID][-2:] == ["--token-budget", "5"]
        assert "--quota" not in seen[SERVICE_TENANT_ID]
        assert "--token-budget" not in seen[SEED_TENANT_ID]
