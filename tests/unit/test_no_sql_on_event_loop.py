"""Behavioural discriminator: NO SQL statement may execute on the event-loop thread.

This is the half of the event-loop work that a ``TestClient`` test structurally
cannot do. ``TestClient`` drives requests serially through an anyio portal, so it
returns 200 whether a handler blocks the loop or not — the existing endpoint
suite stays green through this change AND through a full revert of it.

So this test runs a REAL uvicorn, in-process, on an ephemeral port, and measures
the thing that actually matters:

  * the event-loop thread's ident is known EXACTLY — ``uvicorn.Server.run()``
    calls ``asyncio.run(...)`` in the calling thread, so the thread we start the
    server in IS the loop thread;
  * a ``before_cursor_execute`` listener on the engine records
    ``threading.get_ident()`` for every statement the server issues (same
    listener idiom as ``tests/unit/test_introspect_query_count.py``);
  * requests are driven over real HTTP with ``httpx``.

The assertion is deterministic — a thread identity, not a timing threshold — so
it cannot flake on a loaded machine, and it goes RED for BOTH failure shapes:
reverting a flipped handler to ``async def`` (its inline DB call lands on the
loop thread), and unwrapping an ``await asyncio.to_thread(db_manager...)`` in a
handler that must stay ``async``.

The listener is attached AFTER startup on purpose. ``lifespan`` legitimately runs
``create_tables()`` and the SQL migrations on the loop thread — that is startup,
before the socket serves anything, and blocking there is correct.

The KNOWN GAP this module used to carry is CLOSED (2026-08-16).
``POST /licenses/user/{subject_id}`` was not driven, because its fire-and-forget
metering task (``_provision_budget``) called ``_mark_metering_provisioned`` —
blocking SQL — directly on the loop from inside the background coroutine, so
driving it would have made this test RED for a reason it was not measuring. That
call site is now offloaded, and the route is driven. So is the metering-reconcile
route, whose scan and per-licence ``_mark_metering_verified`` had the same defect
and were never mentioned by that note at all.

⚠️ THE FIRE-AND-FORGET TASK MUST BE DRAINED BEFORE ASSERTING. ``_provision_budget``
is spawned via ``_spawn_background_task`` and the HTTP response returns without
waiting for it, so its SQL can land after the statement listener is detached —
which would silently drop the statements this module exists to measure.

Honest note on how load-bearing each guard is, measured against ``origin/main``
rather than asserted:

* Driving an UNLICENSED subject **is** load-bearing, and by a lot. With the
  licensed ``seed_subject`` the create route takes its idempotency return — 201,
  but before the metering task is spawned — and the offender list on unfixed code
  collapses from **3 statements to 1**: both licence-create defects disappear and
  only the reconcile scan is left. A test driving the seeded subject would have
  "covered" this route while executing none of it.
* ``_drain_background_tasks`` is **race protection, not a proven discriminator**.
  Removing it left the gate RED on unfixed code in every run measured here: the
  task finishes before the listener detaches on this machine. It is kept because
  that ordering is a timing accident — a slower box, a loaded CI runner, or one
  extra await in ``_provision_budget`` turns it into a silent miss — but no
  measurement in this file demonstrates it firing, and it should not be described
  as if one does.
"""

from __future__ import annotations

import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
import uvicorn
from sqlalchemy import event

import main
from tests.conftest import _make_mock_jwt

TENANT_ID = "01000000-0000-7000-8000-000000000001"


@pytest.fixture
def live_server(db_manager, monkeypatch):
    """A real uvicorn serving ``main.app`` against the test database.

    Yields the port, and the ident of the thread the event loop runs in.
    """
    monkeypatch.setattr(main, "db_manager", db_manager)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]

    config = uvicorn.Config(
        main.app,
        log_level="warning",
        # Pin the loop implementation so the loop-thread identity argument does
        # not depend on whether uvloop happens to be installed.
        loop="asyncio",
        lifespan="on",
        access_log=False,
    )
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()

    deadline = time.monotonic() + 45
    while not server.started:
        if not thread.is_alive():
            pytest.fail("uvicorn thread died during startup")
        if time.monotonic() > deadline:
            server.should_exit = True
            pytest.fail("uvicorn did not start within 45s")
        time.sleep(0.02)

    try:
        yield SimpleNamespace(port=port, loop_thread_ident=thread.ident)
    finally:
        server.should_exit = True
        thread.join(timeout=30)


def _record_statements(engine):
    """Record (thread ident, thread name, statement) for every statement issued."""
    rows: list[tuple[int, str, str]] = []

    def _before(conn, cursor, statement, parameters, context, executemany):
        current = threading.current_thread()
        rows.append((current.ident or -1, current.name, statement))

    event.listen(engine, "before_cursor_execute", _before)
    return rows, lambda: event.remove(engine, "before_cursor_execute", _before)


def _drain_background_tasks(timeout: float = 30.0) -> None:
    """Block until ``main._BACKGROUND_TASKS`` empties.

    ``POST /licenses/user/{subject_id}`` spawns ``_provision_budget`` through
    ``_spawn_background_task`` and RETURNS WITHOUT AWAITING IT. Its SQL therefore
    lands AFTER the response — potentially after the statement listener has been
    detached, which would silently drop the very statements this module exists to
    measure.

    ⚠️ THIS FUNCTION CANNOT WITNESS THAT THE TASK RAN, and must not be asked to.
    A short task can start and finish between the response and the first sample
    here, so an empty set means "nothing pending" — it does NOT distinguish
    "finished already" from "never spawned". Measured: a first version of this
    returned ``max(len(...))`` across the poll and reported **0** on a run where
    the task demonstrably ran and stamped the row. The witness is the task's
    OBSERVABLE EFFECT; see ``test_the_background_metering_task_actually_ran``.
    """
    import main

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not main._BACKGROUND_TASKS:
            return
        time.sleep(0.02)
    pytest.fail(
        f"background tasks did not finish within {timeout}s: {main._BACKGROUND_TASKS!r} — "
        "the measurement below would be meaningless"
    )


def _drive_requests(
    base_url: str,
    *,
    app_id: str,
    plan_id: str,
    subject_sub: str,
    fresh_subject_sub: str,
    raw_token: str,
) -> list[str]:
    """Hit a representative slice of the API. Returns a failure list (empty = OK).

    Deliberately spans all four handler shapes:
      * public sync ``def`` with a direct session (``/health/ready``);
      * public ``async`` that offloads (``/license/introspect``);
      * authenticated sync ``def`` reads and writes;
      * authenticated ``async`` handlers whose DB calls are wrapped in
        ``asyncio.to_thread`` because they must await ``get_tenant_id``.
    """
    headers = {
        "Authorization": f"Bearer {_make_mock_jwt(_admin_user())}",
        "X-Tenant-Id": TENANT_ID,
    }
    failures: list[str] = []
    with httpx.Client(base_url=base_url, timeout=30.0) as http:
        calls = [
            ("GET", "/health/ready", None, False),
            ("POST", "/license/introspect", {"token": raw_token}, False),
            ("GET", f"/licenses/active?tenant_id={TENANT_ID}&app_key=test-app", None, True),
            ("GET", "/tenants", None, True),
            ("GET", f"/tenants/{TENANT_ID}", None, True),
            ("GET", "/apps", None, True),
            ("GET", f"/apps/{app_id}", None, True),
            ("GET", f"/apps/{app_id}/plans", None, True),
            ("GET", f"/apps/{app_id}/licenses", None, True),
            ("GET", "/licenses", None, True),
            ("GET", f"/licenses/user/{subject_sub}", None, True),
            ("GET", "/plans", None, True),
            ("GET", "/regions", None, True),
            ("GET", "/offerings", None, True),
            ("POST", "/apps", {"key": "loop-probe-app", "name": "Loop Probe"}, True),
            ("POST", "/regions", {"code": "lp", "name": "Loop Probe Region"}, True),
            # The two routes the KNOWN GAP excluded. Both were blocking the loop
            # one frame deeper than any gate looked:
            #   * this one spawns _provision_budget (SELECT + UPDATE/COMMIT) as a
            #     task ON THE SAME LOOP;
            #   * the reconcile route's scan materialises up to `limit` rows before
            #     its first await, then calls _mark_metering_verified per licence.
            # `dry_run=true` on the reconcile keeps the sweep read-only while still
            # executing the scan, which is the statement that must not land on the
            # loop thread.
            (
                "POST",
                f"/licenses/user/{fresh_subject_sub}",
                {"app_id": app_id, "plan_id": plan_id},
                True,
            ),
            (
                "POST",
                "/licenses/admin/metering/reconcile?dry_run=true&limit=50",
                None,
                True,
            ),
        ]
        for method, path, body, authed in calls:
            response = http.request(method, path, json=body, headers=headers if authed else None)
            if response.status_code >= 500:
                failures.append(f"{method} {path} -> {response.status_code} {response.text[:200]}")
    # The licence-create response returns BEFORE its metering task runs. Drain
    # here, inside the driver, so no caller can forget to.
    _drain_background_tasks()
    return failures


def _admin_user():
    from auth.models import KeycloakUser

    return KeycloakUser(
        sub="loop-probe-admin",
        preferred_username="loop-probe-admin",
        email="loop-probe@test.com",
        realm_access={"roles": ["license-admin"]},
        resource_access={},
    )


@pytest.fixture
def seeded(db_manager, seed_app, seed_plan, seed_subject, seed_license, seed_token, monkeypatch):
    db_manager.ensure_tenant(TENANT_ID, name="loop-probe-tenant", owner_key="platform")
    _token, raw = seed_token

    # The licence-create route's metering task reaches for a Keycloak
    # service-account token before its billing pushes. Stub it: this module
    # measures WHICH THREAD issues SQL, and the token fetch is neither SQL nor on
    # the path under test.
    #
    # ⚠️ MEASURED AS FREE, not load-bearing. Removing this stub leaves the module
    # at 6 passed in the same ~2.5s — the token provider fails fast here rather
    # than spending a timeout, so the "it would cost a timeout on every run"
    # justification an earlier version of this comment gave is NOT observable.
    # Kept as insurance against an environment where the fetch does block (a CI
    # runner with a route to a slow Keycloak), and recorded as unmeasured-benefit
    # rather than presented as a demonstrated one.
    #
    # NOTE this does NOT stub away the work being measured — `seed_plan` sets
    # neither quota_monthly nor token_budget_monthly, so `_provision_budget`
    # legitimately skips both HTTP pushes and proceeds to the stamp, which IS the
    # blocking UPDATE this test exists to catch. Verified separately: injecting a
    # raise immediately after the plan read reds
    # `test_the_background_metering_task_actually_ran` with its intended message.
    async def _no_keycloak(tenant_id: str, license_id: str) -> dict[str, str]:
        return {"X-Tenant-Id": tenant_id}

    monkeypatch.setattr(main, "_metering_auth_headers", _no_keycloak)

    return SimpleNamespace(
        app_id=str(seed_app.id),
        plan_id=str(seed_plan.id),
        subject_sub=str(seed_subject.keycloak_sub),
        # ⚠️ A SUBJECT WITH NO LICENCE, and it must stay that way.
        # `POST /licenses/user/{subject_id}` returns the caller's existing active
        # licence for (tenant, app, subject) — 201, but through an idempotency
        # return that happens BEFORE the metering task is spawned. Driving it with
        # `seed_subject`, who already holds `seed_license` for this exact triple,
        # therefore exercises NOTHING: no `_provision_budget`, no background task,
        # no blocking SQL to measure.
        #
        # MEASURED, not assumed: swapping this back to `subject_sub` drops the
        # offender list on unfixed code from 3 statements to 1 — the plan SELECT
        # and the provisioning UPDATE both vanish and only the reconcile scan
        # remains. `test_the_background_metering_task_actually_ran` is what caught
        # it, by asserting the task's observable effect instead of its existence.
        fresh_subject_sub="loop-probe-unlicensed-subject",
        raw_token=raw,
    )


class TestNoSqlRunsOnTheEventLoopThread:
    def test_request_path_issues_no_statement_on_the_loop_thread(
        self, live_server, db_manager, seeded
    ):
        rows, detach = _record_statements(db_manager.engine)
        try:
            failures = _drive_requests(
                f"http://127.0.0.1:{live_server.port}",
                app_id=seeded.app_id,
                plan_id=seeded.plan_id,
                subject_sub=seeded.subject_sub,
                fresh_subject_sub=seeded.fresh_subject_sub,
                raw_token=seeded.raw_token,
            )
        finally:
            detach()

        assert not failures, f"requests 5xx'd, so the measurement is not meaningful: {failures}"

        # VACUITY FLOOR: without this, a run that issued no SQL at all would
        # satisfy the invariant trivially.
        assert len(rows) >= 18, (
            f"only {len(rows)} statements recorded across {18} requests — the "
            "listener did not capture the server's SQL, so nothing was proven"
        )

        offenders = [
            (name, statement.strip().splitlines()[0][:120])
            for ident, name, statement in rows
            if ident == live_server.loop_thread_ident
        ]
        assert offenders == [], (
            "SQL executed on the EVENT LOOP thread. Every other request on this "
            "pod was frozen for the duration of each statement below — this is "
            "exactly the head-of-line blocking that turns a cold-cache "
            "introspect into an `introspect_timeout` 403. Either the handler is "
            "`async def` with an inline `db_manager.*` call (flip it to `def`), "
            "or it must stay `async` and the call needs "
            "`await asyncio.to_thread(db_manager.<method>, ...)`. Offending "
            f"statements: {offenders}"
        )

    def test_the_reconcile_per_licence_stamp_runs_off_the_loop(
        self, live_server, db_manager, seeded, seed_app, monkeypatch
    ):
        """The third blocking site, which the driven sweep above cannot reach.

        ``_mark_metering_verified`` is the sharpest of the three — it runs ONCE PER
        LICENCE inside the reconcile loop, serially, up to ``limit`` (route ceiling
        5000), each a fresh connect/UPDATE/COMMIT/close under NullPool. On the loop
        thread that is 5000 serial stalls of every other request on the pod.

        ``_drive_requests`` cannot exercise it, and it would be easy to believe
        otherwise: it drives the reconcile route, and that route DOES reach this
        code. But two independent things stop the stamp being reached —
        ``dry_run=true`` returns before it, and ``seed_plan`` sets neither
        ``quota_monthly`` nor ``token_budget_monthly``, so the scan's
        "owes billing something" filter excludes every seeded licence and the loop
        body never executes at all. Verified: on ``origin/main`` the driven sweep
        reports the plan SELECT and the provisioning UPDATE as loop-thread
        offenders, and NOT this stamp.

        So this drives a real sweep: a plan that owes a quota, a licence that has
        not been verified, and the outbound billing calls stubbed — they are HTTP,
        not the SQL under measurement, and there is no billing service here.
        """
        from models import License

        async def _covered(client, *, tenant_id, headers):
            return set()

        async def _pushed(client, **kwargs):
            return True

        async def _quota_absent(client, **kwargs):
            # billing holds no request_quota_config row, so the push below is
            # owed. The sweep reads before writing since 2026-08-24; without this
            # stub the read is a real HTTP call to an empty METERING_SERVICE_URL,
            # the quota comes back UNKNOWN, the licence fails closed and the
            # vacuity floor below trips.
            # Three-tuple since 2026-09-04: the third element is the UNKNOWN
            # reason (None on absent/present). See `main.QUOTA_UNKNOWN_REASONS`.
            return ("absent", None, None)

        monkeypatch.setattr(main, "_fetch_license_sync_budget_ids", _covered)
        monkeypatch.setattr(main, "_fetch_quota_config_monthly", _quota_absent)
        monkeypatch.setattr(main, "_push_quota_config", _pushed)
        monkeypatch.setattr(main, "_push_token_budget", _pushed)

        # A plan that OWES a quota — without this the scan filters every licence
        # out and the loop body never runs.
        owing_plan = db_manager.create_plan(
            app_id=UUID(str(seed_app.id)),
            code="loop-probe-owing",
            name="Loop Probe Owing Plan",
            duration_days=30,
            quota_monthly=1000,
        )
        subject = db_manager.create_or_get_subject(
            keycloak_sub="loop-probe-reconcile-subject", keycloak_realm="secure-apps"
        )
        lic = db_manager.create_license(
            subject_id=UUID(str(subject.id)),
            app_id=UUID(str(seed_app.id)),
            plan_id=UUID(str(owing_plan.id)),
            tenant_id=TENANT_ID,
            status="active",
        )

        headers = {
            "Authorization": f"Bearer {_make_mock_jwt(_admin_user())}",
            "X-Tenant-Id": TENANT_ID,
        }
        rows, detach = _record_statements(db_manager.engine)
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{live_server.port}", timeout=60.0
            ) as http:
                response = http.post(
                    "/licenses/admin/metering/reconcile?dry_run=false&limit=50",
                    headers=headers,
                )
        finally:
            detach()

        assert response.status_code == 200, response.text

        # VACUITY FLOOR — this is the whole point of the setup above. If the sweep
        # examined nothing, "no loop-thread SQL" is trivially true and this test
        # measures the empty set.
        assert response.json()["repaired"], (
            f"the sweep converged no licence, so the per-licence stamp never ran "
            f"and nothing was measured: {response.json()}"
        )

        # ⚠️ THE `UPDATE` PREFIX IS LOAD-BEARING, not tidiness. Matching only
        # `"metering_verified_at" in statement` also matches the SCAN, because
        # with `revalidate=False` its WHERE clause carries
        # `metering_verified_at IS NULL`. Measured: replacing
        # `_mark_metering_verified`'s body with `return True` — claims success,
        # issues ZERO SQL — left both assertions below GREEN off the scan alone.
        # Only the final row read caught it. So without this prefix, two
        # assertions do not measure what their messages say.
        stamps = [
            (name, ident)
            for ident, name, statement in rows
            if "metering_verified_at" in statement
            and statement.lstrip().upper().startswith("UPDATE")
        ]
        assert stamps, (
            "no metering_verified_at UPDATE was issued — the per-licence stamp "
            f"never ran, so the thread assertion below measures nothing: {response.json()}"
        )

        offenders = [name for name, ident in stamps if ident == live_server.loop_thread_ident]
        assert offenders == [], (
            "the per-licence metering_verified_at stamp ran on the EVENT LOOP "
            f"thread — up to 5000 of these run serially in one sweep: {offenders}"
        )

        session = db_manager.get_session()
        try:
            row = session.query(License).filter(License.id == UUID(str(lic.id))).one()
            assert row.metering_verified_at is not None
        finally:
            session.close()

    def test_the_background_metering_task_actually_ran(self, live_server, db_manager, seeded):
        """POSITIVE CONTROL for the drain, and for the whole licence-create arm.

        ``_drain_background_tasks`` returns immediately when the set is already
        empty — which is indistinguishable from "the task ran and finished" if you
        only look at the return. So the invariant test above would still pass if
        ``_provision_budget`` were never spawned at all, and the offload it exists
        to measure would be untested.

        This asserts the OBSERVABLE EFFECT instead: the created licence comes back
        with ``metering_provisioned_at`` set, which only ``_mark_metering_provisioned``
        writes, and which only runs at the end of ``_provision_budget``. If the
        task did not run, this is NULL.
        """
        from models import License

        headers = {
            "Authorization": f"Bearer {_make_mock_jwt(_admin_user())}",
            "X-Tenant-Id": TENANT_ID,
        }
        with httpx.Client(base_url=f"http://127.0.0.1:{live_server.port}", timeout=30.0) as http:
            response = http.post(
                f"/licenses/user/{seeded.fresh_subject_sub}",
                json={"app_id": seeded.app_id, "plan_id": seeded.plan_id},
                headers=headers,
            )
        assert response.status_code == 201, response.text
        license_id = response.json()["id"]

        _drain_background_tasks()

        # Poll the EFFECT rather than the task set — see _drain_background_tasks
        # on why an empty set is not a witness. `metering_provisioned_at` is
        # written by `_mark_metering_provisioned` and by nothing else on this
        # path, and only at the very end of `_provision_budget`. A short poll
        # covers the window between the task's last statement committing and the
        # drain observing an empty set.
        stamped = None
        deadline = time.time() + 15.0
        while time.time() < deadline:
            session = db_manager.get_session()
            try:
                stamped = (
                    session.query(License)
                    .filter(License.id == UUID(license_id))
                    .one()
                    .metering_provisioned_at
                )
            finally:
                session.close()
            if stamped is not None:
                break
            time.sleep(0.05)

        assert stamped is not None, (
            "the fire-and-forget metering task never stamped metering_provisioned_at. "
            "Either it was not spawned at all — in which case the licence-create arm "
            "of `_drive_requests` measures nothing and the offload it exists to pin "
            "is untested — or it failed. Check the server log for "
            "METERING_PROVISIONING_FAILED."
        )

    def test_both_thread_pools_actually_carried_traffic(self, live_server, db_manager, seeded):
        """Prove the two pools are BOTH live, and that the custom executor is installed.

        ``licenses-to-thread`` is the ``thread_name_prefix`` of the executor
        ``lifespan`` installs via ``set_default_executor``. If that call were
        removed, ``asyncio.to_thread`` would fall back to the loop's default
        executor, whose threads are named ``asyncio_N`` — so this name is a
        direct, public-API witness that the governor took effect.
        """
        rows, detach = _record_statements(db_manager.engine)
        try:
            _drive_requests(
                f"http://127.0.0.1:{live_server.port}",
                app_id=seeded.app_id,
                plan_id=seeded.plan_id,
                subject_sub=seeded.subject_sub,
                fresh_subject_sub=seeded.fresh_subject_sub,
                raw_token=seeded.raw_token,
            )
        finally:
            detach()

        names = {name for _ident, name, _stmt in rows}
        assert any(n.startswith("licenses-to-thread") for n in names), (
            "no statement ran in the bounded to_thread executor. Either no "
            "handler used `asyncio.to_thread`, or `set_default_executor` was "
            f"dropped from `lifespan`. Threads seen: {sorted(names)}"
        )
        assert any("AnyIO worker" in n for n in names), (
            "no statement ran in anyio's threadpool, i.e. no sync `def` handler "
            f"issued SQL. Threads seen: {sorted(names)}"
        )


class TestSeedDataIsWhatTheProbeAssumes:
    """Guard the fixtures the discriminator depends on, so a fixture drift does
    not silently turn the probe above into a set of 404s that issue no SQL."""

    def test_seed_license_is_on_the_probe_tenant(self, db_manager, seed_license):
        assert str(seed_license.tenant_id) == TENANT_ID

    def test_seed_license_is_live(self, db_manager, seed_license):
        now = datetime.now(UTC)
        assert str(seed_license.status) == "active"
        assert seed_license.starts_at <= now
        assert seed_license.expires_at > now - timedelta(seconds=1)
        assert UUID(str(seed_license.app_id))
