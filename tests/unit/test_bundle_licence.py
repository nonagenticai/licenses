"""Bundle licences (migration 039): tenant_bundle, license.bundle_id, introspect `apps`,
and scripts/provision_tenant_bundle.py.

WHAT IS PINNED HERE.
* 039 creates `tenant_bundle` and `license.bundle_id` (run for real, inside a
  transaction that is rolled back, after its own downgrade removed them).
* Ruling 2026-09-28: introspect NEVER widens — a licence with bundle_id answers only its
  own `app` and no `apps`, even when the caller asks for a sibling app.
* The provisioning script is DRY-RUN by default and writes nothing; it refuses a
  tenant id that is not in `tenants`; `--apply` ensures ONE per-app licence per bundle
  app (subject `svc:<tenant>:<app>`), ADOPTS a sole existing (tenant, app) licence,
  refuses an ambiguous app (listing candidates), mints no tokens, and is idempotent.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from uuid import UUID

import psycopg
import pytest
from sqlalchemy import text

from models import BundleApp, DatabaseManager
from tests.conftest import SEED_TENANT_ID, TEST_DATABASE_URL

REPO = Path(__file__).resolve().parents[2]
UP = REPO / "migrations" / "039_tenant_bundle_and_license_bundle.sql"
DOWN = REPO / "migrations" / "downgrades" / "039_tenant_bundle_and_license_bundle.sql"

_spec = importlib.util.spec_from_file_location(
    "provision_tenant_bundle", REPO / "scripts" / "provision_tenant_bundle.py"
)
assert _spec is not None and _spec.loader is not None
provision_tenant_bundle = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(provision_tenant_bundle)

BUNDLE_KEY = "test-bundle"
BUNDLE_APPS = ("alpha-app", "kanban-t", "zeta-app")


def _libpq_dsn() -> str:
    return TEST_DATABASE_URL.replace("postgresql+psycopg://", "postgresql://", 1)


def _seed_bundle(db: DatabaseManager) -> tuple[UUID, dict[str, UUID]]:
    """A bundle of three apps, each with a `standard` plan."""
    apps: dict[str, UUID] = {}
    for key in BUNDLE_APPS:
        app = db.create_app(key=key, name=key, issuers=("platform",))
        apps[key] = UUID(str(app.id))
        db.create_plan(app_id=apps[key], code="standard", name="Standard")
    with db.engine.begin() as conn:
        bundle_id = conn.execute(
            text("INSERT INTO bundle (key, name) VALUES (:k, 'Test Bundle') RETURNING id"),
            {"k": BUNDLE_KEY},
        ).scalar_one()
    session = db.get_session()
    try:
        for app_id in apps.values():
            session.add(BundleApp(bundle_id=bundle_id, app_id=app_id))
        session.commit()
    finally:
        session.close()
    return UUID(str(bundle_id)), apps


def _counts(db: DatabaseManager) -> dict[str, int]:
    with db.engine.connect() as conn:
        return {
            t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one()
            for t in ("tenant_bundle", "license", "token", "subject")
        }


class TestMigration039:
    def test_the_downgrade_removes_and_the_upgrade_creates_table_and_column(self, db_manager):
        import sys

        sys.path.insert(0, str(REPO / "scripts"))
        from apply_migrations import _run_file  # type: ignore[import-not-found]

        def _has(cur) -> tuple[bool, bool]:
            table = cur.execute("SELECT to_regclass('public.tenant_bundle')").fetchone()[0]
            col = cur.execute(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'license' AND column_name = 'bundle_id'"
            ).fetchone()[0]
            return table is not None, col == 1

        # NOT autocommit: every DDL here is rolled back, so the shared schema is untouched.
        conn = psycopg.connect(_libpq_dsn())
        try:
            assert _has(conn) == (True, True), "positive control: create_all built both"
            _run_file(conn, DOWN)
            assert _has(conn) == (False, False), "the downgrade did not remove them"
            _run_file(conn, UP)
            assert _has(conn) == (True, True)
            row = conn.execute(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'tenant_bundle_pkey'"
            ).fetchone()
            assert row is not None, "tenant_bundle_pkey is missing after the up-migration"
            pk = row[0]
            assert pk == "PRIMARY KEY (tenant_id, bundle_id)"
            # Idempotent: a second run is a no-op, never an error.
            _run_file(conn, UP)
        finally:
            conn.rollback()
            conn.close()

    def test_it_creates_no_app_rows(self):
        body = "\n".join(
            ln for ln in UP.read_text().splitlines() if not ln.lstrip().startswith("--")
        )
        assert "INSERT INTO app " not in body and "INSERT INTO app(" not in body
        assert body.count("INSERT INTO") == body.count("ON CONFLICT")


def _per_app_licence(
    db: DatabaseManager,
    apps: dict[str, UUID],
    app_key: str,
    sub: str,
    bundle_id: UUID | None,
    realm: str = "service",
):
    subject = db.create_or_get_subject(keycloak_sub=sub, keycloak_realm=realm)
    plan = [p for p in db.list_plans(apps[app_key]) if p.code == "standard"][0]
    return db.create_license(
        subject_id=UUID(str(subject.id)),
        app_id=apps[app_key],
        plan_id=UUID(str(plan.id)),
        tenant_id=SEED_TENANT_ID,
        bundle_id=bundle_id,
    )


def _bundle_ids(db: DatabaseManager) -> dict[str, str | None]:
    """app key -> bundle_id of every active licence the seed tenant holds on a bundle app."""
    with db.engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT a.key, l.bundle_id FROM license l JOIN app a ON a.id = l.app_id "
                "WHERE l.tenant_id = :t AND l.status = 'active' AND a.key = ANY(:k)"
            ),
            {"t": SEED_TENANT_ID, "k": list(BUNDLE_APPS)},
        ).all()
    return {k: (str(b) if b else None) for k, b in rows}


class TestIntrospectNeverWidens:
    """Ruling 2026-09-28: a token authorises ONLY its own licence's app, bundle or not."""

    def _token(self, db: DatabaseManager, bundle_id: UUID, apps: dict[str, UUID]) -> str:
        lic = _per_app_licence(db, apps, "alpha-app", "svc-bundle-t", bundle_id)
        _tok, raw = db.create_token(license_id=UUID(str(lic.id)))
        return raw

    def test_a_bundled_licence_answers_only_its_own_app(self, db_manager):
        bundle_id, apps = _seed_bundle(db_manager)
        raw = self._token(db_manager, bundle_id, apps)
        result = db_manager.introspect_token(raw)
        assert result["active"] is True
        assert result["app"]["key"] == "alpha-app"
        assert "apps" not in result, "no bundle widening"

    def test_the_http_route_ignores_a_requested_other_app(self, db_manager, client):
        bundle_id, apps = _seed_bundle(db_manager)
        raw = self._token(db_manager, bundle_id, apps)
        resp = client.post("/license/introspect", json={"token": raw, "app": "zeta-app"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["active"] is True
        assert body["app"]["key"] == "alpha-app", "asking for a sibling app must not widen"
        assert "apps" not in body

    def test_a_legacy_licence_is_unchanged(self, client, seed_token):
        _tok, raw = seed_token
        body = client.post("/license/introspect", json={"token": raw}).json()
        assert body["active"] is True
        assert body["app"]["key"] == "test-app"
        assert "apps" not in body


class TestProvisionTenantBundle:
    @pytest.fixture(autouse=True)
    def _pinned_token(self, pinned_service_token):
        """--apply provisions metering through ``main``; never reach a real token endpoint."""

    def _run(self, db, *extra: str) -> int:
        return provision_tenant_bundle.main(
            ["--tenant", SEED_TENANT_ID, "--bundle", BUNDLE_KEY, *extra], db=db
        )

    def test_dry_run_writes_nothing_and_prints_the_plan(self, db_manager, capsys):
        _seed_bundle(db_manager)
        before = _counts(db_manager)
        assert self._run(db_manager) == 0
        assert _counts(db_manager) == before
        out = capsys.readouterr().out
        assert "DRY-RUN" in out and "nothing written" in out
        for key in BUNDLE_APPS:
            assert f"svc:{SEED_TENANT_ID}:{key}" in out
        assert "insert=3" in out

    def test_refuses_a_tenant_not_in_tenants(self, db_manager, capsys):
        _seed_bundle(db_manager)
        before = _counts(db_manager)
        rc = provision_tenant_bundle.main(
            [
                "--tenant",
                "01000000-0000-7000-8000-00000000dead",
                "--bundle",
                BUNDLE_KEY,
                "--apply",
            ],
            db=db_manager,
        )
        assert rc == 4
        assert _counts(db_manager) == before
        assert "is not in tenants" in capsys.readouterr().err

    def test_refuses_a_non_uuid_tenant(self, db_manager):
        assert (
            provision_tenant_bundle.main(
                ["--tenant", "seed-tenant-primary", "--bundle", BUNDLE_KEY], db=db_manager
            )
            == 2
        )

    def test_apply_creates_one_licence_per_app_and_is_idempotent(self, db_manager, capsys):
        bundle_id, apps = _seed_bundle(db_manager)
        before = _counts(db_manager)
        assert self._run(db_manager, "--apply") == 0
        after = _counts(db_manager)
        assert after["tenant_bundle"] == before["tenant_bundle"] + 1
        assert after["license"] == before["license"] + len(BUNDLE_APPS)
        assert after["subject"] == before["subject"] + len(BUNDLE_APPS)
        assert after["token"] == before["token"], "provisioning mints no tokens"
        assert _bundle_ids(db_manager) == {k: str(bundle_id) for k in BUNDLE_APPS}
        for key in BUNDLE_APPS:
            (lic,) = db_manager.list_tenant_app_licences(SEED_TENANT_ID, apps[key])
            assert lic.subject.keycloak_sub == f"svc:{SEED_TENANT_ID}:{key}"
            assert lic.subject.keycloak_realm == "service"

        capsys.readouterr()
        assert self._run(db_manager, "--apply") == 0
        assert _counts(db_manager) == after
        assert "exists=3 adopt=0 insert=0 REFUSE=0" in capsys.readouterr().out

    def test_adopts_a_sole_existing_licence_instead_of_duplicating(self, db_manager):
        bundle_id, apps = _seed_bundle(db_manager)
        existing = _per_app_licence(db_manager, apps, "kanban-t", "svc-kanban-legacy", None)
        before = _counts(db_manager)
        assert self._run(db_manager, "--apply") == 0
        after = _counts(db_manager)
        assert after["license"] == before["license"] + 2, "kanban-t adopted, not inserted"
        (lic,) = db_manager.list_tenant_app_licences(SEED_TENANT_ID, apps["kanban-t"])
        assert str(lic.id) == str(existing.id)
        assert str(lic.bundle_id) == str(bundle_id)
        assert lic.subject.keycloak_sub == "svc-kanban-legacy", "adoption keeps the subject"

    def test_refuses_an_ambiguous_app_and_lists_the_candidates(self, db_manager, capsys):
        bundle_id, apps = _seed_bundle(db_manager)
        a = _per_app_licence(db_manager, apps, "kanban-t", "svc-kanban-a", None)
        b = _per_app_licence(db_manager, apps, "kanban-t", "svc-kanban-b", None)

        before = _counts(db_manager)
        assert self._run(db_manager) == 3, "dry-run reports the refusal too"
        assert _counts(db_manager) == before

        capsys.readouterr()
        assert self._run(db_manager, "--apply") == 3
        cap = capsys.readouterr()
        assert str(a.id) in cap.out and str(b.id) in cap.out
        assert "kanban-t" in cap.err and "REFUSED" in cap.err
        ids = _bundle_ids(db_manager)
        assert ids["alpha-app"] == ids["zeta-app"] == str(bundle_id), "others still applied"
        rows = db_manager.list_tenant_app_licences(SEED_TENANT_ID, apps["kanban-t"])
        assert len(rows) == 2 and all(r.bundle_id is None for r in rows), "refused app untouched"

    def test_refuses_an_app_bound_to_another_bundle(self, db_manager, capsys):
        _bundle_id, apps = _seed_bundle(db_manager)
        with db_manager.engine.begin() as conn:
            other = conn.execute(
                text("INSERT INTO bundle (key, name) VALUES ('other-b', 'O') RETURNING id")
            ).scalar_one()
        _per_app_licence(db_manager, apps, "zeta-app", "svc-zeta", UUID(str(other)))
        assert self._run(db_manager, "--apply") == 3
        assert "another bundle" in capsys.readouterr().out
        (lic,) = db_manager.list_tenant_app_licences(SEED_TENANT_ID, apps["zeta-app"])
        assert str(lic.bundle_id) == str(other)

    def test_a_member_licence_is_never_adopted_a_tenant_level_one_is_created(
        self, db_manager, capsys
    ):
        """Ruling 2026-09-28: licences are TENANT-level. A portal member's sole licence
        stays OUTSIDE the bundle; the svc: licence is inserted beside it."""
        bundle_id, apps = _seed_bundle(db_manager)
        member = _per_app_licence(
            db_manager, apps, "kanban-t", "33333333-cccc-4ccc-8ccc-333333333333", None, "portal"
        )
        assert self._run(db_manager) == 0
        out = capsys.readouterr().out
        assert "insert=3" in out and "adopt=0" in out
        assert f"member licence {member.id}" in out
        assert "not adopted — migrate via migrate_member_licences_to_tenant.py" in out

        assert self._run(db_manager, "--apply") == 0
        rows = {
            str(r.id): r
            for r in db_manager.list_tenant_app_licences(SEED_TENANT_ID, apps["kanban-t"])
        }
        assert rows[str(member.id)].bundle_id is None, "member licence was put in the bundle"
        (svc,) = [r for r in rows.values() if r.bundle_id == bundle_id]
        assert svc.subject.keycloak_sub == f"svc:{SEED_TENANT_ID}:kanban-t"
        # exactly ONE licence carries the bundle for the app: no ambiguous_app_licence
        assert len(db_manager.find_bundle_app_licences(SEED_TENANT_ID, bundle_id, "kanban-t")) == 1

        capsys.readouterr()
        assert self._run(db_manager, "--apply") == 0, "idempotent beside the member licence"
        assert "exists=3 adopt=0 insert=0 REFUSE=0" in capsys.readouterr().out

    def test_a_member_licence_already_in_the_bundle_refuses(self, db_manager, capsys):
        bundle_id, apps = _seed_bundle(db_manager)
        _per_app_licence(
            db_manager,
            apps,
            "kanban-t",
            "33333333-cccc-4ccc-8ccc-333333333333",
            bundle_id,
            "secure-apps",
        )
        before = _counts(db_manager)
        assert self._run(db_manager, "--apply") == 3
        assert "MEMBER licence carries this bundle" in capsys.readouterr().out
        assert _counts(db_manager)["license"] == before["license"] + 2, "kanban-t not inserted"
        assert len(db_manager.find_bundle_app_licences(SEED_TENANT_ID, bundle_id, "kanban-t")) == 1

    def test_an_app_key_too_long_for_the_subject_refuses_cleanly(self, db_manager, capsys):
        from models import SERVICE_SUBJECT_APP_KEY_MAX_LEN

        bundle_id, _apps = _seed_bundle(db_manager)
        key = "x" * (SERVICE_SUBJECT_APP_KEY_MAX_LEN + 1)
        app = db_manager.create_app(key=key, name="long", issuers=("platform",))
        db_manager.create_plan(app_id=UUID(str(app.id)), code="standard", name="S")
        db_manager.ensure_bundle_app(bundle_id, UUID(str(app.id)))
        assert self._run(db_manager, "--apply") == 3
        assert f"allows at most {SERVICE_SUBJECT_APP_KEY_MAX_LEN}" in capsys.readouterr().out
        assert db_manager.list_tenant_app_licences(SEED_TENANT_ID, UUID(str(app.id))) == []

    def test_subject_sub_for_raises_a_clear_error_not_a_db_one(self):
        from models import MalformedSubjectIdentifierError

        with pytest.raises(MalformedSubjectIdentifierError, match="allows at most 214"):
            provision_tenant_bundle.subject_sub_for(SEED_TENANT_ID, "y" * 215)
        assert len(provision_tenant_bundle.subject_sub_for(SEED_TENANT_ID, "y" * 214)) == 255

    def test_default_is_not_apply(self):
        args = provision_tenant_bundle._parse_args(["--tenant", SEED_TENANT_ID, "--bundle", "x"])
        assert args.apply is False


class TestProvisionTenantBundleMetering:
    """An INSERTED licence is provisioned in billing like the HTTP create path's."""

    @pytest.fixture(autouse=True)
    def _setup(self, db_manager, pinned_service_token, monkeypatch):
        from unittest.mock import AsyncMock

        import main

        self.budget = AsyncMock(return_value=True)
        self.quota = AsyncMock(return_value=True)
        monkeypatch.setattr(main, "_push_token_budget", self.budget)
        monkeypatch.setattr(main, "_push_quota_config", self.quota)
        _bundle_id, self.apps = _seed_bundle(db_manager)
        with db_manager.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE plan SET token_budget_monthly = 7000, quota_monthly = 70 "
                    "WHERE app_id = :a"
                ),
                {"a": self.apps["alpha-app"]},
            )

    def _run(self, db, *extra: str) -> int:
        return provision_tenant_bundle.main(
            ["--tenant", SEED_TENANT_ID, "--bundle", BUNDLE_KEY, *extra], db=db
        )

    def test_apply_pushes_the_plan_budget_and_stamps(self, db_manager):
        assert self._run(db_manager, "--apply") == 0
        (lic,) = db_manager.list_tenant_app_licences(SEED_TENANT_ID, self.apps["alpha-app"])
        calls = {c.kwargs["license_id"]: c.kwargs for c in self.budget.await_args_list}
        assert calls[str(lic.id)]["budget_tokens"] == 7000
        assert calls[str(lic.id)]["tenant_id"] == SEED_TENANT_ID
        for key in BUNDLE_APPS:
            (row,) = db_manager.list_tenant_app_licences(SEED_TENANT_ID, self.apps[key])
            assert row.metering_provisioned_at is not None, key

    def test_dry_run_makes_no_metering_call(self, db_manager, capsys):
        assert self._run(db_manager) == 0
        self.budget.assert_not_awaited()
        self.quota.assert_not_awaited()
        assert "metering: token_budget=7000, quota=70" in capsys.readouterr().out

    def test_billing_down_creates_the_licence_unstamped_and_exits_6(self, db_manager, capsys):
        self.budget.return_value = False
        assert self._run(db_manager, "--apply") == provision_tenant_bundle.EXIT_METERING_PENDING
        (lic,) = db_manager.list_tenant_app_licences(SEED_TENANT_ID, self.apps["alpha-app"])
        assert lic.metering_provisioned_at is None
        err = capsys.readouterr().err
        assert "METERING NOT PROVISIONED" in err and str(lic.id) in err
        # a plan owing nothing is complete, and stamped
        (other,) = db_manager.list_tenant_app_licences(SEED_TENANT_ID, self.apps["zeta-app"])
        assert other.metering_provisioned_at is not None

    def test_refused_plus_unmetered_is_7_not_masked(self, db_manager, capsys):
        self.budget.return_value = False  # alpha-app insert -> unmetered
        _per_app_licence(db_manager, self.apps, "kanban-t", "svc-kanban-a", None)
        _per_app_licence(db_manager, self.apps, "kanban-t", "svc-kanban-b", None)  # REFUSE
        rc = self._run(db_manager, "--apply")
        assert rc == provision_tenant_bundle.EXIT_REFUSED_AND_METERING_PENDING == 7
        err = capsys.readouterr().err
        assert "METERING NOT PROVISIONED" in err and "REFUSED" in err

    def test_usage_text_documents_7(self, capsys):
        with pytest.raises(SystemExit):
            provision_tenant_bundle._parse_args(["--help"])
        assert "7  both 3 and 6" in capsys.readouterr().out

    def test_an_adopted_licence_is_never_pushed_to(self, db_manager):
        existing = _per_app_licence(db_manager, self.apps, "alpha-app", "svc-alpha-legacy", None)
        assert self._run(db_manager, "--apply") == 0
        pushed = {c.kwargs["license_id"] for c in self.budget.await_args_list}
        assert str(existing.id) not in pushed

    # --- per-licence overrides (George 2026-10-02) -------------------------------

    def _alpha_push(self, db_manager) -> tuple[int, int]:
        (lic,) = db_manager.list_tenant_app_licences(SEED_TENANT_ID, self.apps["alpha-app"])
        (b,) = [
            c.kwargs for c in self.budget.await_args_list if c.kwargs["license_id"] == str(lic.id)
        ]
        (q,) = [
            c.kwargs for c in self.quota.await_args_list if c.kwargs["license_id"] == str(lic.id)
        ]
        return b["budget_tokens"], q["quota_monthly"]

    def test_overrides_replace_the_plan_value_for_an_insert(self, db_manager):
        assert self._run(db_manager, "--apply", "--token-budget", "123", "--quota", "0") == 0
        assert self._alpha_push(db_manager) == (123, 0)
        # the plan row is never modified
        (plan,) = db_manager.list_plans(self.apps["alpha-app"])
        assert (plan.token_budget_monthly, plan.quota_monthly) == (7000, 70)
        # a plan owing nothing still gets the override pushed (it replaces NULL too)
        (zeta,) = db_manager.list_tenant_app_licences(SEED_TENANT_ID, self.apps["zeta-app"])
        pushed = {
            c.kwargs["license_id"]: c.kwargs["budget_tokens"] for c in self.budget.await_args_list
        }
        assert pushed[str(zeta.id)] == 123

    def test_no_override_keeps_the_plan_value(self, db_manager):
        assert self._run(db_manager, "--apply") == 0
        assert self._alpha_push(db_manager) == (7000, 70)

    def test_an_adopted_licence_is_never_pushed_even_with_overrides(self, db_manager):
        existing = _per_app_licence(db_manager, self.apps, "alpha-app", "svc-alpha-legacy", None)
        assert self._run(db_manager, "--apply", "--token-budget", "1", "--quota", "1") == 0
        pushed = {c.kwargs["license_id"] for c in self.budget.await_args_list}
        pushed |= {c.kwargs["license_id"] for c in self.quota.await_args_list}
        assert str(existing.id) not in pushed
        assert pushed, "positive control: the two INSERTED apps were pushed"

    def test_dry_run_shows_the_effective_values(self, db_manager, capsys):
        assert self._run(db_manager, "--token-budget", "5") == 0
        self.budget.assert_not_awaited()
        out = capsys.readouterr().out
        assert "metering: token_budget=5 (override; plan: 7000), quota=70" in out

    @pytest.mark.parametrize("flag", ["--token-budget", "--quota"])
    @pytest.mark.parametrize("value", ["-1", "abc"])
    def test_a_negative_or_non_int_override_is_exit_2(self, db_manager, flag, value):
        before = _counts(db_manager)
        with pytest.raises(SystemExit) as exc:
            self._run(db_manager, "--apply", flag, value)
        assert exc.value.code == 2
        assert _counts(db_manager) == before
        self.budget.assert_not_awaited()


class TestProvisionBudgetOverrides:
    """main._provision_budget: an override replaces the plan value for THAT push only."""

    @pytest.fixture(autouse=True)
    def _setup(self, db_manager, pinned_service_token, monkeypatch):
        from unittest.mock import AsyncMock

        import main

        self.budget = AsyncMock(return_value=True)
        self.quota = AsyncMock(return_value=True)
        monkeypatch.setattr(main, "_push_token_budget", self.budget)
        monkeypatch.setattr(main, "_push_quota_config", self.quota)
        monkeypatch.setattr(main, "db_manager", db_manager)
        app = db_manager.create_app(key="ov-app", name="ov-app", issuers=("platform",))
        self.plan = db_manager.create_plan(
            app_id=UUID(str(app.id)),
            code="standard",
            name="Standard",
            token_budget_monthly=7000,
            quota_monthly=70,
        )

    def _call(self, **kw) -> bool:
        import asyncio

        import main

        return asyncio.run(
            main.provision_license_metering(
                license_id="019a0000-0000-7000-8000-000000000001",
                tenant_id=SEED_TENANT_ID,
                plan_id=str(self.plan.id),
                **kw,
            )
        )

    @staticmethod
    def _last(spy) -> dict:
        call = spy.await_args
        assert call is not None, "the push was never awaited"
        return call.kwargs

    def test_none_keeps_the_plan_values(self):
        self._call()
        assert self._last(self.budget)["budget_tokens"] == 7000
        assert self._last(self.quota)["quota_monthly"] == 70

    def test_overrides_replace_the_plan_values(self):
        self._call(token_budget_override=9, quota_override=0)
        assert self._last(self.budget)["budget_tokens"] == 9
        assert self._last(self.quota)["quota_monthly"] == 0

    def test_a_negative_override_is_ignored(self):
        self._call(token_budget_override=-1, quota_override=-1)
        assert self._last(self.budget)["budget_tokens"] == 7000
        assert self._last(self.quota)["quota_monthly"] == 70


# alpha-app has both plans, kanban-t perpetual only, zeta-app standard only — the
# shape of the live non-agentic-ai bundle, where no single plan covers every app.
PREF_PLANS = {
    "alpha-app": ("standard", "perpetual"),
    "kanban-t": ("perpetual",),
    "zeta-app": ("standard",),
}


def _seed_mixed_plan_bundle(db: DatabaseManager) -> dict[str, UUID]:
    apps: dict[str, UUID] = {}
    for key, codes in PREF_PLANS.items():
        apps[key] = UUID(str(db.create_app(key=key, name=key, issuers=("platform",)).id))
        for code in codes:
            db.create_plan(
                app_id=apps[key],
                code=code,
                name=code,
                token_budget_monthly=1000 if code == "standard" else 2000,
            )
    with db.engine.begin() as conn:
        bundle_id = conn.execute(
            text("INSERT INTO bundle (key, name) VALUES (:k, 'Test Bundle') RETURNING id"),
            {"k": BUNDLE_KEY},
        ).scalar_one()
    session = db.get_session()
    try:
        for app_id in apps.values():
            session.add(BundleApp(bundle_id=bundle_id, app_id=app_id))
        session.commit()
    finally:
        session.close()
    return apps


def _licence_plan_codes(db: DatabaseManager) -> dict[str, str]:
    """app key -> plan code of the seed tenant's active licence on each bundle app."""
    with db.engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT a.key, p.code FROM license l JOIN app a ON a.id = l.app_id "
                "JOIN plan p ON p.id = l.plan_id "
                "WHERE l.tenant_id = :t AND l.status = 'active' AND a.key = ANY(:k)"
            ),
            {"t": SEED_TENANT_ID, "k": list(PREF_PLANS)},
        ).all()
    return {k: c for k, c in rows}


def _table_plan_column(out: str) -> dict[str, tuple[str, str]]:
    """app key -> (action, plan) as printed in the dry-run/apply table."""
    rows = {}
    for line in out.splitlines():
        parts = line.split()
        if parts and parts[0] in PREF_PLANS:
            rows[parts[0]] = (parts[1], parts[2])
    return rows


class TestProvisionTenantBundlePlanPreference:
    """--plan is an ordered preference list; each app gets the first code it has."""

    @pytest.fixture(autouse=True)
    def _setup(self, db_manager, pinned_service_token, monkeypatch):
        from unittest.mock import AsyncMock

        import main

        self.budget = AsyncMock(return_value=True)
        monkeypatch.setattr(main, "_push_token_budget", self.budget)
        monkeypatch.setattr(main, "_push_quota_config", AsyncMock(return_value=True))
        self.apps = _seed_mixed_plan_bundle(db_manager)

    def _run(self, db, *extra: str) -> int:
        return provision_tenant_bundle.main(
            ["--tenant", SEED_TENANT_ID, "--bundle", BUNDLE_KEY, *extra], db=db
        )

    def test_the_preference_picks_per_app(self, db_manager, capsys):
        assert self._run(db_manager, "--plan", "standard,perpetual", "--apply") == 0
        assert _licence_plan_codes(db_manager) == {
            "alpha-app": "standard",
            "kanban-t": "perpetual",
            "zeta-app": "standard",
        }

    def test_the_first_plan_in_the_list_wins_when_both_exist(self, db_manager):
        assert self._run(db_manager, "--plan", "perpetual,standard", "--apply") == 0
        assert _licence_plan_codes(db_manager) == {
            "alpha-app": "perpetual",
            "kanban-t": "perpetual",
            "zeta-app": "standard",
        }

    def test_a_revoked_plan_is_skipped_for_the_next_one(self, db_manager):
        with db_manager.engine.begin() as conn:
            conn.execute(
                text("UPDATE plan SET revoked = true WHERE app_id = :a AND code = 'standard'"),
                {"a": self.apps["alpha-app"]},
            )
        assert self._run(db_manager, "--plan", "standard,perpetual", "--apply") == 0
        assert _licence_plan_codes(db_manager)["alpha-app"] == "perpetual"

    def test_nothing_in_the_list_exists_refuses_and_writes_nothing(self, db_manager, capsys):
        before = _counts(db_manager)
        rc = self._run(db_manager, "--plan", "gold,platinum", "--apply")
        assert rc == provision_tenant_bundle.EXIT_REFUSED
        assert _counts(db_manager)["license"] == before["license"]
        out = capsys.readouterr()
        assert "REFUSE=3" in out.out
        assert "no plan 'gold,platinum' on this app" in out.err

    def test_a_single_value_is_unchanged(self, db_manager, capsys):
        rc = self._run(db_manager, "--plan", "standard", "--apply")
        assert rc == provision_tenant_bundle.EXIT_REFUSED
        assert _licence_plan_codes(db_manager) == {"alpha-app": "standard", "zeta-app": "standard"}
        err = capsys.readouterr().err
        assert "kanban-t (no plan 'standard' on this app (has: ['perpetual']))" in err

    def test_the_default_is_standard_only(self):
        args = provision_tenant_bundle._parse_args(["--tenant", SEED_TENANT_ID, "--bundle", "x"])
        assert args.plan == ["standard"]

    def test_the_dry_run_prints_the_chosen_plan_per_app(self, db_manager, capsys):
        before = _counts(db_manager)
        assert self._run(db_manager, "--plan", "standard,perpetual") == 0
        assert _counts(db_manager) == before
        out = capsys.readouterr().out
        assert "plan preference for inserts: standard,perpetual" in out
        assert _table_plan_column(out) == {
            "alpha-app": ("insert", "standard"),
            "kanban-t": ("insert", "perpetual"),
            "zeta-app": ("insert", "standard"),
        }

    def test_metering_pushes_the_chosen_plans_budget(self, db_manager):
        assert self._run(db_manager, "--plan", "perpetual,standard", "--apply") == 0
        pushed = {
            c.kwargs["license_id"]: c.kwargs["budget_tokens"] for c in self.budget.await_args_list
        }
        for key, budget in (("alpha-app", 2000), ("kanban-t", 2000), ("zeta-app", 1000)):
            (lic,) = db_manager.list_tenant_app_licences(SEED_TENANT_ID, self.apps[key])
            assert pushed[str(lic.id)] == budget, key
            assert lic.metering_provisioned_at is not None, key

    @pytest.mark.parametrize("bad", ["", "standard,", ",standard", "standard,standard"])
    def test_a_malformed_preference_is_a_usage_error(self, bad):
        with pytest.raises(SystemExit):
            provision_tenant_bundle._parse_args(
                ["--tenant", SEED_TENANT_ID, "--bundle", "x", "--plan", bad]
            )
