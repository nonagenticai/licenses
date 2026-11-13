"""Concurrent duplicate creates must not 500.

``create_app``, ``create_region`` and ``create_offering`` all do a read-then-
insert against a UNIQUE constraint with no lock in between:

  * ``create_app``      — ``get_app_by_key`` then ``create_app``; unique ``app.key``
  * ``create_region``   — ``get_region_by_code`` then ``create_region``; unique ``region.code``
  * ``create_offering`` — the DAL SELECTs ``(app_id, code)`` then ``session.add``;
                          unique ``ux_offering_app_code``

This race is REACHABLE TODAY: the service runs ``replicas: 2``, so two pods can
sit in the gap at the same time. What the sync-``def`` flip changes is the odds,
not the possibility — inside one pod, an ``async def`` handler with no ``await``
in its body is atomic against every other request, because the single event loop
never yields inside it. Once FastAPI runs these in the threadpool, up to
``HANDLER_THREAD_LIMIT`` of them interleave freely, and a rare cross-pod timing
becomes a routine intra-pod one.

WHY THIS IS NOT A TIMING TEST. The interleaving is FORCED, not hoped for: a
``after_cursor_execute`` listener parks both threads on a ``threading.Barrier``
immediately after their pre-check SELECT completes, so both are guaranteed to
have observed "no such row" before either INSERT runs. Nothing about the code
under test is mocked — the handlers, the DAL, the constraint and the recovery
re-read are all the real ones.

Each test asserts the two things that matter to a caller: nobody gets a 5xx, and
both callers end up pointing at the SAME row.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import event

import main
from auth.models import KeycloakUser
from tests.support.jwt_keys import TEST_ISSUER


def _json(response: Any) -> dict[str, Any]:
    """Decode whatever a handler returned (JSONResponse or a pydantic model)."""
    body = getattr(response, "body", None)
    if body is not None:
        return json.loads(body)
    if hasattr(response, "model_dump"):
        return response.model_dump()
    return dict(response)


class _RaceHarness:
    """Park the first ``n`` statements matching ``needle`` on a shared barrier.

    Applied to ``after_cursor_execute``, so every racer has FINISHED its
    pre-check read before any of them is allowed to proceed to the insert.
    """

    def __init__(self, engine, needle: str, racers: int = 2, timeout: float = 20.0):
        self.engine = engine
        self.needle = needle
        self.barrier = threading.Barrier(racers, timeout=timeout)
        self.racers = racers
        self._lock = threading.Lock()
        self._seen = 0
        self.matched = 0

    def _after(self, conn, cursor, statement, parameters, context, executemany):
        if self.needle not in statement:
            return
        with self._lock:
            self._seen += 1
            if self._seen > self.racers:
                return
            self.matched += 1
        # Both racers have now read; neither has written. Release together.
        self.barrier.wait()

    def __enter__(self):
        event.listen(self.engine, "after_cursor_execute", self._after)
        return self

    def __exit__(self, *exc):
        event.remove(self.engine, "after_cursor_execute", self._after)
        self.barrier.abort()
        return False


def _run_racers(fn, count: int = 2) -> list[Any]:
    """Run ``fn`` in ``count`` threads, returning results or the raised exception."""

    def _wrapped(index: int) -> Any:
        try:
            return fn()
        except BaseException as exc:  # noqa: BLE001 — the failure IS the result here
            return exc

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(_wrapped, range(count)))


def _assert_no_server_error(results: list[Any], what: str) -> None:
    for result in results:
        if isinstance(result, HTTPException):
            assert result.status_code < 500, (
                f"{what}: a concurrent duplicate create returned "
                f"HTTP {result.status_code} ({result.detail!r}). A caller that "
                "merely lost a read-then-insert race must never see a 5xx."
            )
        elif isinstance(result, BaseException):
            raise AssertionError(
                f"{what}: a concurrent duplicate create raised {type(result).__name__}: "
                f"{result}. The unique-constraint violation is not being recovered."
            ) from result


@pytest.fixture(autouse=True)
def _point_main_at_test_db(db_manager, monkeypatch):
    monkeypatch.setattr(main, "db_manager", db_manager)


@pytest.fixture
def platform_admin(admin_user):
    """``admin_user`` as the platform break-glass client: ``create_app`` resolves
    the caller's owner (only that owner — or a cross_owner — may be named as an
    issuer), so the caller must be a registered api_client."""
    return admin_user.model_copy(update={"azp": "licenses-admin"})


class TestConcurrentCreateApp:
    def test_two_simultaneous_creates_return_the_same_app(self, db_manager, platform_admin):
        body = main.AppCreate(
            key="racy-app", name="Racy App", description=None, issuers=["platform"]
        )

        with _RaceHarness(db_manager.engine, needle="FROM app ") as harness:
            results = _run_racers(lambda: main.create_app(body, platform_admin))

        assert harness.matched == 2, (
            "the race was never forced — the pre-check SELECT did not match the "
            f"barrier needle (matched={harness.matched})"
        )
        _assert_no_server_error(results, "create_app")

        ids = {_json(r)["id"] for r in results}
        assert len(ids) == 1, f"the two callers got DIFFERENT apps: {ids}"

        statuses = sorted(r.status_code for r in results)
        assert statuses == [200, 201], (
            "expected exactly one create (201) and one idempotent hit (200), "
            f"got {statuses}. The docstring promises 'returns existing app if "
            "key matches'."
        )
        assert db_manager.get_app_by_key("racy-app") is not None


class TestConcurrentCreateRegion:
    def test_two_simultaneous_creates_return_the_same_region(self, db_manager, admin_user):
        body = main.RegionCreate(code="rc", name="Racy Region", active=True)

        with _RaceHarness(db_manager.engine, needle="FROM region ") as harness:
            results = _run_racers(lambda: main.create_region(body, admin_user))

        assert harness.matched == 2, f"the race was never forced (matched={harness.matched})"
        _assert_no_server_error(results, "create_region")

        ids = {_json(r)["id"] for r in results}
        assert len(ids) == 1, f"the two callers got DIFFERENT regions: {ids}"

        statuses = sorted(r.status_code for r in results)
        assert statuses == [200, 201], f"expected [200, 201], got {statuses}"
        assert db_manager.get_region_by_code("rc") is not None


class TestConcurrentCreateOffering:
    def test_two_simultaneous_upserts_do_not_500(self, db_manager, seed_app):
        # The offering routes are gated on app ownership (_authorize_app_admin):
        # seed_app is issued by `platform`, so race as a platform client.
        admin_user = _owned_admin("licenses-admin")
        body = main.OfferingCreate(
            app_key=str(seed_app.key),
            code="racy-offering",
            name="Racy Offering",
            byo_mode="local_allowed",
        )

        with _RaceHarness(db_manager.engine, needle="FROM offering") as harness:
            results = _run_racers(lambda: main.create_offering(body, admin_user))

        assert harness.matched == 2, f"the race was never forced (matched={harness.matched})"
        _assert_no_server_error(results, "create_offering")

        # This route is an UPSERT, so the loser retries and both callers see the
        # same converged row rather than one of them being handed the winner's.
        codes = {_json(r)["code"] for r in results}
        assert codes == {"racy-offering"}, codes

        stored = db_manager.get_offering(str(seed_app.key), "racy-offering")
        assert stored is not None
        assert stored["name"] == "Racy Offering"
        assert stored["byo_mode"] == "local_allowed"


def _owned_admin(azp: str = "fr-backend") -> KeycloakUser:
    """A license-admin signed by a REAL owning client (secure-apps/fr-backend -> fr)."""
    return KeycloakUser(
        sub=f"service-account-{azp}",
        preferred_username=f"service-account-{azp}",
        realm_access={"roles": ["license-admin"]},
        resource_access={},
        iss=TEST_ISSUER,
        azp=azp,
    )


class TestConcurrentCreateTenant:
    """Migration 050: ``POST /tenants`` is unique per (owner, name) and idempotent
    only on (owner, external_ref). Two racers for the same owner + name must give
    ONE tenant: one 201 and one 409 — never a 5xx, never a second row, and never a
    different owner's row."""

    def test_same_owner_same_name_race_is_one_201_and_one_409(self, db_manager):
        body = main.TenantCreate(name="racy-tenant")
        user = _owned_admin()
        with _RaceHarness(db_manager.engine, needle="FROM api_client") as harness:
            results = _run_racers(lambda: main.create_tenant(body, user))

        assert harness.matched == 2, f"the race was never forced (matched={harness.matched})"
        _assert_no_server_error(results, "create_tenant")
        created = [r for r in results if not isinstance(r, BaseException)]
        conflicts = [r for r in results if isinstance(r, HTTPException)]
        assert [r.status_code for r in created] == [201], results
        assert [c.status_code for c in conflicts] == [409], results
        assert conflicts[0].detail["error"] == "tenant_name_taken"
        rows = [t for t in db_manager.list_tenants(owner_key="fr") if t.name == "racy-tenant"]
        assert len(rows) == 1

    def test_same_external_ref_race_returns_the_same_tenant(self, db_manager):
        body = main.TenantCreate(name="racy-ref", external_ref="slug-1")
        user = _owned_admin()
        with _RaceHarness(db_manager.engine, needle="FROM tenants") as harness:
            results = _run_racers(lambda: main.create_tenant(body, user))

        assert harness.matched == 2, f"the race was never forced (matched={harness.matched})"
        _assert_no_server_error(results, "create_tenant")
        assert {_json(r)["id"] for r in results} == {
            str(db_manager.get_tenant_by_external_ref("fr", "slug-1").id)
        }
        assert sorted(r.status_code for r in results) == [200, 201]

    def test_two_owners_same_name_race_creates_two_tenants(self, db_manager):
        body = main.TenantCreate(name="racy-shared")
        users = iter([_owned_admin("fr-backend"), _owned_admin("commonpractice-licenses-admin")])
        lock = threading.Lock()

        def _create():
            with lock:
                user = next(users)
            return main.create_tenant(body, user)

        with _RaceHarness(db_manager.engine, needle="FROM api_client") as harness:
            results = _run_racers(_create)

        assert harness.matched == 2, f"the race was never forced (matched={harness.matched})"
        _assert_no_server_error(results, "create_tenant")
        assert sorted(r.status_code for r in results) == [201, 201]
        assert {_json(r)["owner_key"] for r in results} == {"fr", "commonpractice"}
        assert len({_json(r)["id"] for r in results}) == 2


class TestHarnessIsNotVacuous:
    """The barrier must be able to observe the pre-check read it claims to gate.

    If the needle stopped matching (a schema rename, a DAL rewrite), every test
    above would run its racers sequentially and pass without ever exercising the
    recovery path. ``harness.matched == 2`` in each test is that check; this one
    proves the negative case is detectable.
    """

    def test_a_needle_that_matches_nothing_records_zero(self, db_manager, platform_admin):
        body = main.AppCreate(
            key="control-app", name="Control", description=None, issuers=["platform"]
        )
        harness = _RaceHarness(db_manager.engine, needle="FROM table_that_does_not_exist", racers=1)
        with harness:
            main.create_app(body, platform_admin)
        assert harness.matched == 0
