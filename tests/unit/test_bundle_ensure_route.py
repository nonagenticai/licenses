"""``POST /internal/tenants/{tenant_id}/bundles/{bundle_key}/ensure`` (PLAN §2.2, §3.4).

The HTTP form of ``scripts/provision_tenant_bundle.py``: the SAME per-app decision
(exists / adopt / insert / REFUSE, adopting tenant-level subjects only), now in
``bundle_ensure.plan_and_apply``, which the script also calls.

WHAT IS PINNED.
* Contract (§2.2): 200 ``{tenant_id, bundle, budget_currency, apps:{<app>:{decision,
  license_id, plan, metering}}, refused:[]}``; 409 with the same body and
  ``refused:[{app, reason}]``.
* Decisions: insert on a fresh tenant; a second call is all ``exists`` and writes
  nothing; a sole tenant-level unbundled licence is ``adopt``ed; several candidates,
  a member licence carrying the bundle and a missing plan are REFUSED.
* An EUR tenant's inserts push the QUOTA only (``metering: quota_only``), never a
  token budget; ``token_budget_override`` for an EUR tenant is 422.
* Archived tenant 409; unknown tenant 404; dry_run writes nothing.
* Role gating: ``bundle-provisioner`` or an owner-write role (``license-admin`` /
  ``license-manager``; the route is owner-scoped since migration 050, see
  tests/unit/test_tenant_ownership.py). ``service-internal``, ``bundle-token-minter``
  and ``license-reader`` are 403 — and the existing bundle-token route still refuses
  ``bundle-provisioner`` (its shape is unchanged).
"""

from __future__ import annotations

from collections.abc import Generator
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import main
from auth.models import KeycloakUser
from models import DatabaseManager
from tests.conftest import SEED_TENANT_ID, _make_authenticated_client
from tests.unit.test_budget_coverage import FakeBilling, _no_sleep, _patch_billing
from tests.unit.test_bundle_licence import (
    BUNDLE_APPS,
    BUNDLE_KEY,
    _counts,
    _per_app_licence,
    _seed_bundle,
)

TENANT = SEED_TENANT_ID
URL = f"/internal/tenants/{TENANT}/bundles/{BUNDLE_KEY}/ensure"
BUDGET = 61_355_000
QUOTA = 500


def _kc_user(sub: str, roles: list[str]) -> KeycloakUser:
    return KeycloakUser(
        sub=sub,
        preferred_username=f"svc-{sub}",
        email=f"{sub}@test.com",
        realm_access={"roles": roles},
        resource_access={},
        iss="https://keycloak.example.com/realms/service",
    )


def _client(db, roles: list[str]) -> Generator[TestClient]:
    yield from _make_authenticated_client(db, _kc_user("prov-sa", roles), TENANT)


@pytest.fixture
def provisioner(db_manager) -> Generator[TestClient]:
    yield from _client(db_manager, ["bundle-provisioner"])


@pytest.fixture
def fake(db_manager, pinned_service_token, monkeypatch) -> Generator[FakeBilling]:
    _no_sleep(monkeypatch)
    monkeypatch.setattr(main, "db_manager", db_manager)
    f = FakeBilling()
    patcher = _patch_billing(f)
    try:
        yield f
    finally:
        patcher.stop()


@pytest.fixture
def seeded(db_manager) -> tuple[UUID, dict[str, UUID]]:
    bundle_id, apps = _seed_bundle(db_manager)
    with db_manager.engine.begin() as conn:
        conn.execute(
            text("UPDATE plan SET token_budget_monthly = :b, quota_monthly = :q"),
            {"b": BUDGET, "q": QUOTA},
        )
    return bundle_id, apps


def _eur(db: DatabaseManager, tenant: str = TENANT) -> None:
    with db.engine.begin() as conn:
        conn.execute(
            text("UPDATE tenants SET budget_currency = 'EUR' WHERE id = :t"), {"t": tenant}
        )


BODY = {"plans": ["standard"], "quota_override": None, "dry_run": False}


class TestDecisions:
    def test_fresh_eur_tenant_inserts_quota_only(self, db_manager, provisioner, fake, seeded):
        _eur(db_manager)
        resp = provisioner.post(URL, json=BODY)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["tenant_id"] == TENANT
        assert body["bundle"] == BUNDLE_KEY
        assert body["budget_currency"] == "EUR"
        assert body["refused"] == []
        assert sorted(body["apps"]) == sorted(BUNDLE_APPS)
        for app in BUNDLE_APPS:
            entry = body["apps"][app]
            assert entry["decision"] == "insert"
            assert entry["plan"] == "standard"
            assert entry["metering"] == "quota_only"
            UUID(entry["license_id"])
            assert fake.quota_of(entry["license_id"]) == QUOTA
        assert fake.budget_post_count == 0, "an EUR tenant must never get a token budget"

    def test_second_call_is_all_exists_and_writes_nothing(
        self, db_manager, provisioner, fake, seeded
    ):
        _eur(db_manager)
        first = provisioner.post(URL, json=BODY).json()
        before = _counts(db_manager)
        posts = len(fake.post_urls)
        resp = provisioner.post(URL, json=BODY)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert {a: e["decision"] for a, e in body["apps"].items()} == {
            a: "exists" for a in BUNDLE_APPS
        }
        assert {a: e["license_id"] for a, e in body["apps"].items()} == {
            a: e["license_id"] for a, e in first["apps"].items()
        }
        assert _counts(db_manager) == before
        assert len(fake.post_urls) == posts, "an idempotent call must not push to billing"

    def test_sole_tenant_level_licence_is_adopted(self, db_manager, provisioner, fake, seeded):
        _bundle_id, apps = seeded
        existing = _per_app_licence(db_manager, apps, "alpha-app", "svc-alpha-x", None)
        resp = provisioner.post(URL, json=BODY)
        assert resp.status_code == 200, resp.text
        alpha = resp.json()["apps"]["alpha-app"]
        assert alpha["decision"] == "adopt"
        assert alpha["license_id"] == str(existing.id)
        assert alpha["plan"] == "standard"
        assert resp.json()["budget_currency"] is None
        assert alpha["metering"] == "token_and_quota"

    def test_several_candidates_refused(self, db_manager, provisioner, fake, seeded):
        _bundle_id, apps = seeded
        _per_app_licence(db_manager, apps, "alpha-app", "svc-alpha-1", None)
        _per_app_licence(db_manager, apps, "alpha-app", "svc-alpha-2", None)
        resp = provisioner.post(URL, json=BODY)
        assert resp.status_code == 409, resp.text
        body = resp.json()
        assert body["refused"] == [{"app": "alpha-app", "reason": "several_candidates"}]
        assert "alpha-app" not in body["apps"]
        assert body["apps"]["kanban-t"]["decision"] == "insert"

    def test_member_licence_in_bundle_refused(self, db_manager, provisioner, fake, seeded):
        bundle_id, apps = seeded
        _per_app_licence(db_manager, apps, "zeta-app", "human-1", bundle_id, realm="portal")
        resp = provisioner.post(URL, json=BODY)
        assert resp.status_code == 409, resp.text
        assert resp.json()["refused"] == [{"app": "zeta-app", "reason": "member_licence"}]

    def test_no_plan_refused(self, db_manager, provisioner, fake, seeded):
        resp = provisioner.post(URL, json={**BODY, "plans": ["perpetual"]})
        assert resp.status_code == 409, resp.text
        refused = resp.json()["refused"]
        assert sorted(r["app"] for r in refused) == sorted(BUNDLE_APPS)
        assert {r["reason"] for r in refused} == {"no_plan"}

    def test_dry_run_writes_nothing(self, db_manager, provisioner, fake, seeded):
        _eur(db_manager)
        before = _counts(db_manager)
        resp = provisioner.post(URL, json={**BODY, "dry_run": True})
        assert resp.status_code == 200, resp.text
        assert {e["decision"] for e in resp.json()["apps"].values()} == {"insert"}
        assert all(e["license_id"] is None for e in resp.json()["apps"].values())
        assert _counts(db_manager) == before
        assert fake.post_urls == []

    def test_quota_override_is_pushed(self, db_manager, provisioner, fake, seeded):
        _eur(db_manager)
        resp = provisioner.post(URL, json={**BODY, "quota_override": 42})
        assert resp.status_code == 200, resp.text
        for entry in resp.json()["apps"].values():
            assert fake.quota_of(entry["license_id"]) == 42


class TestRefusals:
    def test_token_budget_override_for_eur_tenant_is_422(
        self, db_manager, provisioner, fake, seeded
    ):
        _eur(db_manager)
        before = _counts(db_manager)
        resp = provisioner.post(URL, json={**BODY, "token_budget_override": 1000})
        assert resp.status_code == 422, resp.text
        assert _counts(db_manager) == before

    def test_archived_tenant_is_409(self, db_manager, provisioner, fake, seeded):
        db_manager.set_tenant_archived(TENANT, True)
        resp = provisioner.post(URL, json=BODY)
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["error"] == "tenant_archived"

    def test_unknown_tenant_is_404(self, db_manager, provisioner, fake, seeded):
        resp = provisioner.post(
            f"/internal/tenants/01000000-0000-7000-8000-0000000000ff/bundles/{BUNDLE_KEY}/ensure",
            json=BODY,
        )
        assert resp.status_code == 404, resp.text

    def test_unknown_bundle_is_404(self, db_manager, provisioner, fake, seeded):
        resp = provisioner.post(f"/internal/tenants/{TENANT}/bundles/nope/ensure", json=BODY)
        assert resp.status_code == 404, resp.text


class TestRoleGating:
    @pytest.mark.parametrize(
        "roles",
        [["service-internal"], ["bundle-token-minter"], ["license-reader"]],
    )
    def test_other_roles_are_403(self, db_manager, fake, seeded, roles):
        before = _counts(db_manager)
        for client in _client(db_manager, roles):
            resp = client.post(URL, json=BODY)
            assert resp.status_code == 403, resp.text
        assert _counts(db_manager) == before

    @pytest.mark.parametrize("role", ["license-admin", "license-manager"])
    def test_owner_write_roles_pass_the_role_gate(self, db_manager, fake, seeded, role):
        for client in _client(db_manager, [role]):
            resp = client.post(URL, json={**BODY, "dry_run": True})
            assert resp.status_code == 200, resp.text

    def test_ensure_roles_are_the_provisioner_plus_the_owner_write_roles(self):
        from auth.dependencies import BUNDLE_ENSURE_ROLES

        assert set(BUNDLE_ENSURE_ROLES) == {"bundle-provisioner", *main.OWNER_WRITE_ROLES}

    def test_bundle_token_route_still_refuses_the_provisioner(self, db_manager, provisioner):
        resp = provisioner.post(f"/internal/tenants/{TENANT}/bundle-token?app=kanban-t")
        assert resp.status_code == 403, resp.text


class TestInferenceRouterBundleForABrandNewEurTenant:
    """The provisioning path end to end (PLAN §1.1 step 1 + §3.4).

    Provisioning registers a tenant, marks it EUR, ensures the ``inference-router``
    bundle (migration 040: one app, ``inference-router``), then mints a short-lived
    token with ``?app=inference-router&bundle=inference-router``. The plan is
    parametrised over prod's measured ``standard`` (50,000,000 tokens / 1,000,000
    requests, read on the primary 2026-10-02) and a NULL-budget plan: neither may yield a
    token budget for an EUR tenant.
    """

    NEW_TENANT = "01000000-0000-7000-8000-0000000000e1"
    IR = "inference-router"

    def _seed_ir_bundle(self, db: DatabaseManager, budget, quota) -> None:
        from models import BundleApp

        app = db.create_app(key=self.IR, name="Inference Router", issuers=("platform",))
        app_id = UUID(str(app.id))
        plan = db.create_plan(app_id=app_id, code="standard", name="Standard")
        with db.engine.begin() as conn:
            conn.execute(
                text("UPDATE plan SET token_budget_monthly = :b, quota_monthly = :q WHERE id = :p"),
                {"b": budget, "q": quota, "p": str(plan.id)},
            )
            bundle_id = conn.execute(
                text("INSERT INTO bundle (key, name) VALUES (:k, 'Inference Router') RETURNING id"),
                {"k": self.IR},
            ).scalar_one()
        session = db.get_session()
        try:
            session.add(BundleApp(bundle_id=bundle_id, app_id=app_id))
            session.commit()
        finally:
            session.close()

    @pytest.mark.parametrize(
        ("budget", "quota"), [(50_000_000, 1_000_000), (None, None)], ids=["prod-standard", "null"]
    )
    def test_insert_quota_only_then_bundle_token_mints(
        self, db_manager, fake, budget, quota
    ) -> None:
        self._seed_ir_bundle(db_manager, budget, quota)
        admin = _kc_user(
            "prov-admin", ["license-admin", "bundle-provisioner", "bundle-token-minter"]
        )
        for client in _make_authenticated_client(db_manager, admin, self.NEW_TENANT):
            # 1. register a brand-new tenant and mark it EUR (before any licence exists)
            reg = client.put(f"/tenants/{self.NEW_TENANT}", json={"name": "brand-new-eur"})
            assert reg.status_code in (200, 201), reg.text
            cur = client.put(
                f"/tenants/{self.NEW_TENANT}/budget-currency", json={"budget_currency": "EUR"}
            )
            assert cur.status_code == 200, cur.text

            # 2. ensure the inference-router bundle
            url = f"/internal/tenants/{self.NEW_TENANT}/bundles/{self.IR}/ensure"
            resp = client.post(
                url, json={"plans": ["standard"], "quota_override": None, "dry_run": False}
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["tenant_id"] == self.NEW_TENANT
            assert body["bundle"] == self.IR
            assert body["budget_currency"] == "EUR"
            assert body["refused"] == []
            assert list(body["apps"]) == [self.IR]
            entry = body["apps"][self.IR]
            assert entry["decision"] == "insert"
            assert entry["plan"] == "standard"
            assert entry["metering"] == "quota_only"
            assert fake.budget_post_count == 0, "an EUR tenant must never get a token budget"
            assert fake.quota_of(entry["license_id"]) == quota

            # idempotent
            again = client.post(url, json={"plans": ["standard"]})
            assert again.status_code == 200, again.text
            assert again.json()["apps"][self.IR]["decision"] == "exists"
            assert again.json()["apps"][self.IR]["license_id"] == entry["license_id"]

            # 3. the existing bundle-token route mints on that licence, shape unchanged
            tok = client.post(
                f"/internal/tenants/{self.NEW_TENANT}/bundle-token"
                f"?app={self.IR}&bundle={self.IR}&ttl_minutes=5"
            )
            assert tok.status_code == 201, tok.text
            assert set(tok.json()) == {
                "token",
                "expires_at",
                "tenant_id",
                "bundle",
                "app",
                "license_id",
                "token_id",
            }
            assert tok.json()["license_id"] == entry["license_id"]
            assert tok.json()["bundle"] == self.IR


class TestEventLoop:
    """The route meters on the APP'S event loop — never a second loop via asyncio.run.

    Regression for the cross-loop ``asyncio.Lock`` defect: the route was a ``def``
    whose ``provision_metering`` called ``asyncio.run`` on a worker thread. That
    second loop shares the process-wide ``ServiceTokenProvider`` lock with uvicorn's
    loop; once it bound the lock, every later contended refresh on the main loop
    raised "bound to a different event loop" until the pod restarted.
    """

    def test_metering_runs_on_the_route_loop_without_asyncio_run(
        self, db_manager, provisioner, fake, seeded, monkeypatch
    ):
        import asyncio

        from services import bundle_ensure

        def _no_asyncio_run(*_a, **_k):
            raise AssertionError("asyncio.run called on a request path")

        monkeypatch.setattr(bundle_ensure.asyncio, "run", _no_asyncio_run)

        route_loops: list[object] = []
        real_validate = main.validate_tenant_uuid

        def _spy_validate(*a, **k):
            # Called in the route body: on the app's loop for an ``async def`` route,
            # on a loop-less worker thread for a ``def`` one.
            try:
                route_loops.append(asyncio.get_running_loop())
            except RuntimeError:
                route_loops.append(None)
            return real_validate(*a, **k)

        monkeypatch.setattr(main, "validate_tenant_uuid", _spy_validate)

        metering_loops: list[object] = []
        real_provision = main.provision_license_metering

        async def _spy_provision(*a, **k):
            metering_loops.append(asyncio.get_running_loop())
            return await real_provision(*a, **k)

        monkeypatch.setattr(main, "provision_license_metering", _spy_provision)

        resp = provisioner.post(URL, json=BODY)
        assert resp.status_code == 200, resp.text
        apps = resp.json()["apps"]
        assert {e["decision"] for e in apps.values()} == {"insert"}
        assert {e["metering"] for e in apps.values()} == {"token_and_quota"}, apps

        assert len(route_loops) == 1 and route_loops[0] is not None, (
            "the ensure route must be async (run on the app's event loop)"
        )
        assert len(metering_loops) == len(BUNDLE_APPS)
        assert all(loop is route_loops[0] for loop in metering_loops), (
            "metering must be awaited on the app's own loop, not a second one"
        )


class TestBudgetCurrencyArchived:
    def test_archived_tenant_is_409_and_unchanged(self, db_manager):
        db_manager.set_tenant_archived(TENANT, True)
        admin = _kc_user("cur-admin", ["license-admin"])
        for client in _make_authenticated_client(db_manager, admin, TENANT):
            resp = client.put(f"/tenants/{TENANT}/budget-currency", json={"budget_currency": "EUR"})
            assert resp.status_code == 409, resp.text
            assert resp.json()["detail"]["error"] == "tenant_archived"
        with db_manager.engine.connect() as conn:
            cur = conn.execute(
                text("SELECT budget_currency FROM tenants WHERE id = :t"), {"t": TENANT}
            ).scalar_one()
        assert cur is None, "an archived tenant's budget_currency must not change"

    def test_non_archived_twin_is_200(self, db_manager):
        admin = _kc_user("cur-admin", ["license-admin"])
        for client in _make_authenticated_client(db_manager, admin, TENANT):
            resp = client.put(f"/tenants/{TENANT}/budget-currency", json={"budget_currency": "EUR"})
            assert resp.status_code == 200, resp.text


class TestCpTenantLicencesAreNotBundleHolders:
    def test_cp_tenant_licence_is_never_adopted(self, db_manager, provisioner, fake, seeded):
        """⛔ A ``cp-tenant:`` licence belongs to CommonPractice. Read as tenant-level it is the
        sole unbundled candidate and gets ADOPTED into the bundle; it must be ignored, so the app
        gets its own ``svc:`` licence inserted and the CP licence keeps no bundle."""
        _bundle_id, apps = seeded
        cp = _per_app_licence(
            db_manager, apps, "alpha-app", f"cp-tenant:{TENANT}:a", None, realm="cp-tenant"
        )
        resp = provisioner.post(URL, json=BODY)
        assert resp.status_code == 200, resp.text
        alpha = resp.json()["apps"]["alpha-app"]
        assert alpha["decision"] == "insert"
        assert alpha["license_id"] != str(cp.id)


class TestConcurrentEnsure:
    """Two ensures for one tenant+bundle at once (prod 2026-10-14 09:57Z).

    CommonPractice retried on a client timeout while the first ensure was still
    applying, so two pods planned the same ``insert`` and the loser's INSERT hit
    ``uq_license_active_tenant_app_subject`` -> 500. ``plan`` + ``apply`` now run
    under a per-tenant ``pg_advisory_xact_lock``, and ``_apply_one`` turns that
    unique violation into ``exists`` as defence in depth.
    """

    @staticmethod
    def _rendezvous_after_plan(monkeypatch) -> list[str]:
        """Make each ``plan`` wait (bounded) for the other to finish planning.

        WITHOUT the tenant lock both plans meet here, both decide ``insert`` and
        both INSERT — the prod race, made deterministic. WITH it the second plan
        cannot start until the first ensure has released the lock, so the first
        waits out the timeout alone and the second then plans ``exists``.
        """
        import threading

        from services import bundle_ensure

        barrier = threading.Barrier(2, timeout=2.0)
        met: list[str] = []
        real_plan = bundle_ensure.plan

        def _plan(*a, **k):
            ep = real_plan(*a, **k)
            try:
                barrier.wait()
                met.append("met")
            except threading.BrokenBarrierError:
                met.append("alone")
            return ep

        monkeypatch.setattr(bundle_ensure, "plan", _plan)
        return met

    async def test_two_concurrent_ensures_both_succeed_one_licence_per_app(
        self, db_manager, fake, seeded, monkeypatch
    ):
        import asyncio

        from services import bundle_ensure

        _bundle_id, apps = seeded
        met = self._rendezvous_after_plan(monkeypatch)

        async def _ensure():
            return await bundle_ensure.plan_and_apply_async(
                db_manager, TENANT, BUNDLE_KEY, ["standard"], None, True
            )

        first, second = await asyncio.gather(_ensure(), _ensure())

        assert "met" not in met, "the two plans interleaved: the tenant lock is not held"
        decisions = sorted(
            [{p.app_key: p.action for p in first.apps}, {p.app_key: p.action for p in second.apps}],
            key=lambda d: sorted(d.values()),
        )
        assert decisions == [
            {a: "exists" for a in BUNDLE_APPS},
            {a: "insert" for a in BUNDLE_APPS},
        ]
        assert first.refused == [] and second.refused == []
        with db_manager.engine.connect() as conn:
            for key, app_id in apps.items():
                n = conn.execute(
                    text(
                        "SELECT count(*) FROM license WHERE tenant_id = :t AND app_id = :a "
                        "AND status = 'active'"
                    ),
                    {"t": TENANT, "a": app_id},
                ).scalar_one()
                assert n == 1, f"{key}: {n} active licences"
        ids = {p.app_key: p.license_id for p in first.apps}
        assert ids == {p.app_key: p.license_id for p in second.apps}

    async def test_lock_is_per_tenant_and_released(self, db_manager, fake, seeded):
        """The lock is gone once the ensure returns: a fresh session can take it."""
        from services import bundle_ensure

        await bundle_ensure.plan_and_apply_async(
            db_manager, TENANT, BUNDLE_KEY, ["standard"], None, True
        )
        with db_manager.engine.connect() as conn:
            got = conn.execute(
                text("SELECT pg_try_advisory_xact_lock(hashtextextended(:k, 0))"),
                {"k": bundle_ensure.tenant_lock_key(TENANT)},
            ).scalar_one()
            conn.rollback()
        assert got is True

    async def test_a_cancelled_waiting_ensure_never_keeps_the_lock(self, db_manager, fake, seeded):
        """Cancelled while blocked on the lock: once the lock frees, the abandoned
        acquire must end its own transaction, not hold the tenant lock forever."""
        import asyncio
        import time

        from services import bundle_ensure

        key = bundle_ensure.tenant_lock_key(TENANT)
        before = _counts(db_manager)

        def _try_lock() -> bool:
            with db_manager.engine.connect() as conn:
                got = conn.execute(
                    text("SELECT pg_try_advisory_xact_lock(hashtextextended(:k, 0))"),
                    {"k": key},
                ).scalar_one()
                conn.rollback()
                return bool(got)

        holder = db_manager.engine.connect()
        holder.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": key})
        task = asyncio.ensure_future(
            bundle_ensure.plan_and_apply_async(
                db_manager, TENANT, BUNDLE_KEY, ["standard"], None, True
            )
        )
        # Real waits: the ``fake`` fixture turns ``asyncio.sleep`` into a no-op.
        await asyncio.to_thread(time.sleep, 0.5)  # the ensure now blocks on the lock
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        holder.rollback()
        holder.close()
        # The abandoned acquire wins the lock in its thread, then must let it go.
        free = False
        for _ in range(50):
            await asyncio.to_thread(time.sleep, 0.1)
            if await asyncio.to_thread(_try_lock):
                free = True
                break
        assert free, "a cancelled ensure kept the tenant lock"
        assert _counts(db_manager) == before

    def test_dry_run_takes_no_lock_and_writes_nothing(self, db_manager, fake, seeded):
        from services import bundle_ensure

        before = _counts(db_manager)
        with db_manager.engine.connect() as holder:
            holder.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                {"k": bundle_ensure.tenant_lock_key(TENANT)},
            )
            # Would block (then time out) if dry-run took the tenant lock.
            ep = bundle_ensure.plan_and_apply(
                db_manager, TENANT, BUNDLE_KEY, ["standard"], None, False
            )
            holder.rollback()
        assert {p.action for p in ep.apps} == {"insert"}
        assert _counts(db_manager) == before

    def test_a_held_lock_times_out_as_ensure_in_progress(
        self, db_manager, fake, seeded, monkeypatch
    ):
        from services import bundle_ensure

        monkeypatch.setattr(bundle_ensure, "ENSURE_LOCK_TIMEOUT", "200ms")
        before = _counts(db_manager)
        with db_manager.engine.connect() as holder:
            holder.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                {"k": bundle_ensure.tenant_lock_key(TENANT)},
            )
            with pytest.raises(bundle_ensure.EnsureError) as exc:
                bundle_ensure.plan_and_apply(
                    db_manager, TENANT, BUNDLE_KEY, ["standard"], None, True
                )
            holder.rollback()
        assert exc.value.code == "ensure_in_progress"
        assert _counts(db_manager) == before

    def test_a_held_lock_is_409_ensure_in_progress_on_the_route(
        self, db_manager, provisioner, fake, seeded, monkeypatch
    ):
        from services import bundle_ensure

        monkeypatch.setattr(bundle_ensure, "ENSURE_LOCK_TIMEOUT", "200ms")
        with db_manager.engine.connect() as holder:
            holder.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
                {"k": bundle_ensure.tenant_lock_key(TENANT)},
            )
            resp = provisioner.post(URL, json=BODY)
            holder.rollback()
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["error"] == "ensure_in_progress"


class TestUniqueRaceIsExists:
    """Defence in depth: an INSERT that loses ``uq_license_active_tenant_app_subject``
    (a writer outside the tenant lock) reports the winner as ``exists``."""

    def test_lost_insert_reports_the_winner_as_exists(self, db_manager, fake, seeded):
        from services import bundle_ensure

        bundle_id, apps = seeded
        ep = bundle_ensure.plan(db_manager, TENANT, BUNDLE_KEY, ["standard"])
        assert {p.action for p in ep.apps} == {"insert"}
        # The race: someone else inserts alpha-app's bundle licence after the plan.
        winner = _per_app_licence(
            db_manager,
            apps,
            "alpha-app",
            bundle_ensure.subject_sub_for(TENANT, "alpha-app"),
            bundle_id,
        )
        bundle_ensure.apply(db_manager, ep, provisioned_by="test", provision=lambda *a, **k: "")
        by_app = {p.app_key: p for p in ep.apps}
        assert by_app["alpha-app"].action == "exists"
        assert by_app["alpha-app"].license_id == str(winner.id)
        assert by_app["alpha-app"].existing_plan_id == UUID(str(winner.plan_id))
        assert {by_app[a].action for a in ("kanban-t", "zeta-app")} == {"insert"}
        assert ep.unmetered == [] and ep.lost_race == []
        with db_manager.engine.connect() as conn:
            n = conn.execute(
                text("SELECT count(*) FROM license WHERE app_id = :a AND status = 'active'"),
                {"a": apps["alpha-app"]},
            ).scalar_one()
        assert n == 1

    def test_lost_insert_to_an_unbundled_winner_is_refused_lost_race(
        self, db_manager, fake, seeded
    ):
        from services import bundle_ensure

        _bundle_id, apps = seeded
        ep = bundle_ensure.plan(db_manager, TENANT, BUNDLE_KEY, ["standard"])
        _per_app_licence(
            db_manager, apps, "alpha-app", bundle_ensure.subject_sub_for(TENANT, "alpha-app"), None
        )
        bundle_ensure.apply(db_manager, ep, provisioned_by="test", provision=lambda *a, **k: "")
        alpha = next(p for p in ep.apps if p.app_key == "alpha-app")
        assert alpha.action == "REFUSE"
        assert alpha.refuse_reason == bundle_ensure.REFUSE_LOST_RACE
        assert ep.lost_race == ["alpha-app"]

    def test_other_integrity_errors_are_not_swallowed(self, db_manager, fake, seeded, monkeypatch):
        from sqlalchemy.exc import IntegrityError

        from services import bundle_ensure

        ep = bundle_ensure.plan(db_manager, TENANT, BUNDLE_KEY, ["standard"])

        def _fk_violation(**_k):
            # A real non-unique IntegrityError from Postgres (FK), not a hand-built one.
            with db_manager.engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO license (id, subject_id, app_id, plan_id, tenant_id, "
                        "status, starts_at, created_at, updated_at) VALUES (gen_random_uuid(), "
                        "gen_random_uuid(), gen_random_uuid(), gen_random_uuid(), :t, "
                        "'active', now(), now(), now())"
                    ),
                    {"t": TENANT},
                )

        monkeypatch.setattr(db_manager, "create_license", _fk_violation)
        with pytest.raises(IntegrityError):
            bundle_ensure.apply(db_manager, ep, provisioned_by="test", provision=lambda *a, **k: "")
